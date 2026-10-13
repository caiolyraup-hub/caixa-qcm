"""DRE gerencial mensal por COMPETÊNCIA (não é demonstração contábil oficial).

Duas fontes, sem sobreposição:
1. Parcelas de obrigações (contas a receber/pagar confirmadas) na sua competência. "Previsto" = valor da parcela;
   "Realizado" = o que já foi baixado (liquidado), mais os ajustes (taxa/desconto/diferença) como linhas próprias.
2. Lançamentos bancários classificados que NÃO foram ligados a obrigações (histórico), na competência definida por você
   (padrão: mês do banco, marcado como "presumida" até você revisar). Se um lançamento for ligado depois a uma parcela, a
   parte alocada sai daqui e passa a valer pela parcela: nunca conta duas vezes.

Fora da DRE operacional, sempre: transferências, aplicações/resgates, aportes, distribuição de lucros, principal de
empréstimos e pagamento de fatura de cartão (a compra no cartão já entrou como despesa na sua competência).
"""
from collections import defaultdict
from datetime import date

from .ledger import RESULT_KINDS  # noqa: F401  (documenta a origem das classificações usadas)
from .obrigacoes import CONFIRMED_ORIGINS, list_installments
from .timeutil import today_sp

LINES = [
    ("receita_servicos", "Receita de serviços"),
    ("deducoes", "(−) Deduções e impostos sobre a receita"),
    ("custos", "(−) Custos da entrega"),
    ("desp_comerciais", "(−) Despesas comerciais"),
    ("desp_administrativas", "(−) Despesas administrativas"),
    ("fin_receita", "(+) Receitas financeiras"),
    ("fin_despesa", "(−) Despesas financeiras"),
]
LINE_LABEL = dict(LINES) | {"fora_dre": "Fora da DRE operacional"}
DEFAULT_CATEGORY_LINES = {
    "rendimento financeiro": "fin_receita", "pró-labore": "desp_administrativas", "pro-labore": "desp_administrativas",
    "distribuição de lucros": "fora_dre", "juros de empréstimo": "fin_despesa", "principal de empréstimo": "fora_dre",
}
FIXED_NATURE_LINE = {
    "prolabore": "desp_administrativas", "distribuicao_lucros": "fora_dre", "emprestimo_juros": "fin_despesa",
    "emprestimo_principal": "fora_dre", "fatura_cartao": "fora_dre",
}
OUTSIDE_KINDS = ("aporte", "transferencia", "investimento")


# ------------------------------------------------------------------ mapeamento categoria → linha

def category_lines(conn) -> dict[str, str]:
    m = dict(DEFAULT_CATEGORY_LINES)
    m.update({r["category"].strip().lower(): r["dre_line"] for r in conn.execute("SELECT * FROM category_map")})
    return m


def set_category_line(conn, category: str, dre_line: str) -> None:
    if dre_line not in LINE_LABEL:
        raise ValueError(f"linha da DRE inválida: {dre_line}")
    with conn:
        conn.execute("INSERT INTO category_map (category, dre_line) VALUES (?,?) ON CONFLICT(category) DO UPDATE SET "
                     "dre_line=excluded.dre_line", (category.strip().lower(), dre_line))


def set_tx_competence(conn, tx_id: int, competence: str) -> None:
    from .obrigacoes import _check_competence
    from .timeutil import now_iso
    _check_competence(competence)
    with conn:
        conn.execute("INSERT INTO tx_competence (tx_id, competence, updated_at) VALUES (?,?,?) ON CONFLICT(tx_id) DO UPDATE SET "
                     "competence=excluded.competence, updated_at=excluded.updated_at", (tx_id, competence, now_iso()))


def installment_line(p: dict, cmap: dict) -> str | None:
    if p["nature"] in FIXED_NATURE_LINE:
        return FIXED_NATURE_LINE[p["nature"]]
    mapped = cmap.get((p["category"] or "").strip().lower())
    if mapped:
        return mapped
    if p["direction"] == "receber" and p["nature"] == "operacional":
        return "receita_servicos"
    if p["nature"] == "imposto":
        return "deducoes"
    return None


def tx_line(kind: str, category: str, cmap: dict) -> str | None:
    if kind in OUTSIDE_KINDS:
        return "fora_dre"
    mapped = cmap.get((category or "").strip().lower())
    default = {"receita": "receita_servicos", "imposto": "deducoes", "tarifa": "fin_despesa"}.get(kind)
    return mapped or default            # despesa / retirada / estorno / ajuste sem categoria mapeada → pendente (None)


# ------------------------------------------------------------------ cálculo

def _alloc_by_installment(conn) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in conn.execute("SELECT installment_id, kind, SUM(amount_cents) AS s FROM allocations WHERE undone_at IS NULL "
                          "GROUP BY installment_id, kind"):
        out[r["installment_id"]][r["kind"]] = r["s"]
    return out


def _allocated_by_tx(conn) -> dict[int, int]:
    return {r["tx_id"]: r["s"] for r in conn.execute(
        "SELECT tx_id, SUM(amount_cents) AS s FROM allocations WHERE undone_at IS NULL AND tx_id IS NOT NULL GROUP BY tx_id")}


def dre_month(conn, month: str, *, client: str | None = None, today: date | None = None) -> dict:
    """DRE de um mês de competência (AAAA-MM). Retorna linhas, subtotais e pendências explícitas."""
    today = today or today_sp()
    cmap = category_lines(conn)
    items: list[dict] = []          # cada item: line, previsto, realizado (centavos com sinal) + origem
    pending: list[dict] = []        # itens sem linha definida: ficam fora dos totais e aparecem na tela
    outside: list[dict] = []
    allocs = _alloc_by_installment(conn)
    want = (client or "").strip().lower()

    # 1) parcelas confirmadas
    for p in list_installments(conn, today, origins=CONFIRMED_ORIGINS):
        if p["competence"] != month or (want and p["counterparty"].strip().lower() != want):
            continue
        sign = 1 if p["direction"] == "receber" else -1
        line = installment_line(p, cmap)
        a = allocs.get(p["id"], {})
        P, T, D, F, E = (a.get(k, 0) for k in ("pagamento", "taxa", "desconto", "diferenca", "estorno"))
        base = {"source": "parcela", "id": p["id"], "date": p["expected_date"], "description": p["description"],
                "counterparty": p["counterparty"], "category": p["category"], "status": p["status"],
                "competence": p["competence"], "presumed": False}
        if line is None:
            pending.append({**base, "line": None, "previsto": sign * p["amount_cents"], "realizado": sign * (P + T + D + F - E),
                            "reason": "categoria sem linha definida na DRE"})
            continue
        if line == "fora_dre":
            outside.append({**base, "line": line, "previsto": sign * p["amount_cents"], "realizado": sign * (P + T + D + F - E)})
            continue
        realized_main = sign * (P + T + D + F - E)
        items.append({**base, "line": line, "previsto": sign * p["amount_cents"], "realizado": realized_main})
        if p["direction"] == "receber":
            if D + F:
                items.append({**base, "line": "deducoes", "description": f"Desconto/diferença — {p['description']}",
                              "previsto": 0, "realizado": -(D + F), "status": "ajuste"})
            if T:
                items.append({**base, "line": "fin_despesa", "description": f"Taxa retida — {p['description']}",
                              "previsto": 0, "realizado": -T, "status": "ajuste"})
        elif D + F:                      # desconto obtido reduz a despesa da própria linha
            items.append({**base, "line": line, "description": f"Desconto obtido — {p['description']}",
                          "previsto": 0, "realizado": D + F, "status": "ajuste"})

    # 2) lançamentos bancários classificados e não ligados a obrigações
    allocated = _allocated_by_tx(conn)
    unclassified = {"count": 0, "cents": 0}
    rows = conn.execute(
        """SELECT e.id, e.posted_date, e.description, e.amount_cents, e.account_id, a.name AS account_name,
                  c.kind, c.category, c.counterparty, tc.competence AS reviewed
           FROM effective_tx e JOIN accounts a ON a.id=e.account_id
           LEFT JOIN classifications c ON c.tx_id=e.id LEFT JOIN tx_competence tc ON tc.tx_id=e.id
           WHERE e.posted_date >= a.opening_date""").fetchall()
    presumed = {"count": 0, "cents": 0}
    for r in rows:
        comp = r["reviewed"] or r["posted_date"][:7]
        if comp != month:
            continue
        rest = abs(r["amount_cents"]) - allocated.get(r["id"], 0)
        if rest <= 0:
            continue                      # já representado pela(s) parcela(s) a que foi ligado
        effect = rest if r["amount_cents"] > 0 else -rest
        if r["kind"] is None:
            unclassified["count"] += 1
            unclassified["cents"] += effect
            continue
        if want and (r["counterparty"] or "").strip().lower() != want:
            continue
        line = tx_line(r["kind"], r["category"], cmap)
        base = {"source": "lancamento", "id": r["id"], "date": r["posted_date"], "description": r["description"],
                "counterparty": r["counterparty"] or "", "category": r["category"] or "", "status": r["kind"],
                "competence": comp, "presumed": r["reviewed"] is None, "account": r["account_name"],
                "previsto": effect, "realizado": effect}
        if line is None:
            pending.append({**base, "line": None, "reason": f"{r['kind']} sem categoria com linha definida na DRE"})
        elif line == "fora_dre":
            outside.append({**base, "line": line})
        else:
            items.append({**base, "line": line})
            if base["presumed"]:
                presumed["count"] += 1
                presumed["cents"] += effect

    lines: dict[str, dict] = {k: {"previsto": 0, "realizado": 0, "items": []} for k, _ in LINES}
    for it in items:
        ln = lines[it["line"]]
        ln["previsto"] += it["previsto"]
        ln["realizado"] += it["realizado"]
        ln["items"].append(it)

    def total(keys, field):
        return sum(lines[k][field] for k in keys)

    sub: dict[str, dict] = {}
    for f in ("previsto", "realizado"):
        rec_liq = total(("receita_servicos", "deducoes"), f)
        bruta = rec_liq + lines["custos"][f]
        oper = bruta + total(("desp_comerciais", "desp_administrativas"), f)
        fin = total(("fin_receita", "fin_despesa"), f)
        res = oper + fin
        sub[f] = {"receita_liquida": rec_liq, "margem_bruta": bruta, "resultado_operacional": oper,
                  "resultado_financeiro": fin, "resultado": res,
                  "margem": (res / rec_liq) if rec_liq > 0 else None}
    return {"month": month, "client": client, "lines": lines, "subtotals": sub, "pending": pending, "outside": outside,
            "unclassified": unclassified, "presumed_competence": presumed,
            "complete": not pending and not unclassified["count"] and not presumed["count"]}


def unmapped_categories(conn) -> list[str]:
    """Categorias em uso (despesas/retiradas/estornos/ajustes classificados e parcelas) sem linha na DRE."""
    cmap = category_lines(conn)
    seen = set()
    for r in conn.execute("SELECT DISTINCT c.kind, c.category FROM classifications c JOIN effective_tx e ON e.id=c.tx_id "
                          "WHERE c.kind IN ('despesa','retirada','estorno','ajuste') AND c.category<>''"):
        seen.add(r["category"].strip())
    for r in conn.execute("SELECT DISTINCT category FROM installments WHERE category<>'' AND cancelled_at IS NULL "
                          "AND nature IN ('operacional','compra_cartao')"):
        seen.add(r["category"].strip())
    return sorted(c for c in seen if c.lower() not in cmap)


def tx_for_competence_review(conn, month: str) -> list[dict]:
    """Lançamentos históricos classificados cuja competência ainda é a presumida (mês do banco)."""
    out = []
    for r in conn.execute(
            """SELECT e.id, e.posted_date, e.description, e.amount_cents, c.kind, c.category FROM effective_tx e
               JOIN classifications c ON c.tx_id=e.id LEFT JOIN tx_competence tc ON tc.tx_id=e.id
               WHERE tc.tx_id IS NULL AND substr(e.posted_date,1,7)=? AND c.kind NOT IN ('aporte','transferencia','investimento')
               ORDER BY e.posted_date""", (month,)):
        out.append(dict(r))
    return out
