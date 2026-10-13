"""Caixa projetado: linha diária por conta e consolidada.

Parte do saldo realizado mais recente que o app consegue justificar (posicao.account_position) e, dia a dia, soma o que ainda
está em aberto: recebimentos contratados (+) e pagamentos assumidos (−). Parcela liquidada não entra: seu movimento já está no
extrato. Estimativas e oportunidades ficam fora da linha principal; existe um cenário separado, sempre rotulado.

Regras de data de cada parcela em aberto (d = data prevista):
- d ≥ hoje: acontece em d;
- d < hoje e depois do último dado da conta: ainda pode não ter aparecido no extrato; acontece em d;
- d < hoje e já coberto pelo extrato (venceu e não foi baixada): atrasada. Pagamento atrasado sai hoje (conservador);
  recebimento atrasado NÃO entra (incerto) salvo se pedido; ambos aparecem listados.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .obrigacoes import CONFIRMED_ORIGINS, list_installments
from .posicao import account_position, count_cdi, reserve_cents
from .contasimples import CARD_ACCOUNT_NAME
from .timeutil import today_sp

HORIZONS = (7, 30, 90)


@dataclass
class Item:
    installment_id: int
    date: date
    cents: int                    # com sinal: + receber, − pagar
    account_id: int | None
    description: str
    counterparty: str
    origin: str
    overdue: bool
    weight_note: str = ""


@dataclass
class Projection:
    start: date
    horizon: int
    rows: list[dict] = field(default_factory=list)
    accounts: dict = field(default_factory=dict)             # id → posição (saldo-base e limitações)
    min: dict = field(default_factory=dict)                  # horizonte → {"cents", "date"}
    reserve_cents: int = 0
    slack: dict = field(default_factory=dict)                # horizonte → mínimo − reserva
    negative_dates: dict = field(default_factory=dict)       # conta → primeira data negativa
    overdue_payables: list = field(default_factory=list)
    overdue_receivables: list = field(default_factory=list)
    skipped: list = field(default_factory=list)              # itens fora do caixa e o motivo
    limitations: list = field(default_factory=list)
    cdi_counted: bool = True
    scenario: "Projection | None" = None
    scenario_label: str = ""


def _card_account_id(conn) -> int | None:
    r = conn.execute("SELECT id FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()
    return r["id"] if r else None


def collect_items(conn, today: date, scenario: bool = False) -> tuple[list[Item], list[dict]]:
    """Parcelas em aberto com efeito de caixa. Retorna (itens, ignorados com motivo)."""
    origins = CONFIRMED_ORIGINS + (("estimativa", "oportunidade") if scenario else ())
    card_id = _card_account_id(conn)
    items, skipped = [], []
    for p in list_installments(conn, today, origins=origins):
        if p["open_cents"] <= 0:
            continue
        cents = p["open_cents"]
        note = ""
        if p["origin"] == "oportunidade":
            if p["probability_pct"] is None:
                skipped.append({"installment": p, "reason": "oportunidade sem probabilidade informada"})
                continue
            cents = cents * p["probability_pct"] // 100
            note = f"ponderado a {p['probability_pct']}% (não garantido)"
        elif p["origin"] == "estimativa":
            note = "estimativa (não garantido)"
        if p["nature"] == "compra_cartao":
            on_card_limit = card_id is not None and p["expected_account_id"] == card_id
            if not on_card_limit:
                skipped.append({"installment": p, "reason": "compra no cartão: o caixa só sai no pagamento da fatura"})
                continue
            if date.fromisoformat(p["expected_date"]) <= today:
                skipped.append({"installment": p, "reason": "compra no cartão com limite já realizada: "
                                                            "o saldo/limite atual já considera compras lançadas e pendentes"})
                continue
        sign = 1 if p["direction"] == "receber" else -1
        items.append(Item(p["id"], date.fromisoformat(p["expected_date"]), sign * cents, p["expected_account_id"],
                          p["description"], p["counterparty"], p["origin"], p["overdue"], note))
    return items, skipped


def project(conn, today: date | None = None, horizon: int = 90, *, scenario: bool = False,
            include_overdue_receivables: bool = False, _with_scenario: bool = True) -> Projection:
    today = today or today_sp()
    cdi_on = count_cdi(conn)
    proj = Projection(start=today, horizon=horizon, reserve_cents=reserve_cents(conn), cdi_counted=cdi_on)
    items, proj.skipped = collect_items(conn, today, scenario)
    days = [today + timedelta(days=k) for k in range(horizon + 1)]
    end = days[-1]

    pos = {a["id"]: account_position(conn, a["id"], today) for a in conn.execute("SELECT id FROM accounts")}
    proj.accounts = pos
    for p in pos.values():
        proj.limitations += [f"{p['name']}: {m}" for m in p["limitations"]]

    # série de cada conta (e uma "sem conta definida" para itens sem conta esperada)
    series_ids: list[int | None] = list(pos) + [None]
    flows: dict = {a: defaultdict(list) for a in series_ids}
    for it in items:
        key = it.account_id if it.account_id in pos else None
        asof = pos[key]["asof"] if key else today
        asof = asof or today
        d = it.date
        if d < today and d <= asof:                              # atrasada (já coberta pelo extrato)
            if it.cents > 0 and not include_overdue_receivables:
                proj.overdue_receivables.append(it)
                continue
            (proj.overdue_receivables if it.cents > 0 else proj.overdue_payables).append(it)
            d = today                                            # pagamento atrasado sai hoje
        if d <= end:
            flows[key][d].append(it)

    closing: dict = {}
    for key in series_ids:
        base_asof = pos[key]["asof"] if key else today
        base_asof = min(base_asof or today, today)
        if key:
            base = pos[key]["total_cents"] if cdi_on else pos[key]["available_cents"]
        else:
            base = 0
        bal, cur = base, min(base_asof + timedelta(days=1), today)
        daily: dict[date, tuple[int, int, int, int]] = {}
        while cur <= end:
            r = sum(i.cents for i in flows[key][cur] if i.cents > 0)
            p_ = sum(-i.cents for i in flows[key][cur] if i.cents < 0)
            opening = bal
            bal += r - p_
            daily[cur] = (opening, r, p_, bal)
            cur += timedelta(days=1)
        closing[key] = (base, base_asof, daily)

    for d in days:
        row = {"date": d, "opening": 0, "receipts": 0, "payments": 0, "closing": 0, "by_account": {}, "items": []}
        for key in series_ids:
            base, base_asof, daily = closing[key]
            if d in daily:
                o, r, p_, c = daily[d]
            else:                                                # dia coberto pelo extrato: saldo é o realizado
                o = r = p_ = 0
                c = base
            row["opening"] += o if d in daily else base
            row["receipts"] += r
            row["payments"] += p_
            row["closing"] += c
            if key:
                row["by_account"][key] = c
            row["items"] += flows[key][d]
        proj.rows.append(row)
        for key, c in row["by_account"].items():
            if c < 0 and key not in proj.negative_dates:
                proj.negative_dates[key] = d

    for n in HORIZONS:
        if n > horizon:
            continue
        window = proj.rows[: n + 1]
        low = min(window, key=lambda r: (r["closing"], r["date"]))
        proj.min[n] = {"cents": low["closing"], "date": low["date"]}
        proj.slack[n] = low["closing"] - proj.reserve_cents
    if _with_scenario and not scenario:
        sc = project(conn, today, horizon, scenario=True, include_overdue_receivables=include_overdue_receivables,
                     _with_scenario=False)
        sc.scenario_label = ("Cenário opcional: inclui estimativas e oportunidades (oportunidades só com probabilidade, "
                             "ponderadas). NÃO é dinheiro garantido.")
        proj.scenario = sc
    return proj


def min_window(proj: Projection, days: int) -> dict:
    window = proj.rows[: days + 1]
    low = min(window, key=lambda r: (r["closing"], r["date"]))
    return {"cents": low["closing"], "date": low["date"], "slack": low["closing"] - proj.reserve_cents}
