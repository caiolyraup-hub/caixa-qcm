"""Contas a receber e a pagar: obrigações, parcelas, recorrência e parcelamento.

Uma obrigação é o "contrato"; cada parcela é um valor com vencimento próprio, que carrega seus atributos.
O saldo em aberto de uma parcela = valor − baixas ativas (+ estornos). A parcela não cria movimento de caixa:
só o extrato bancário cria (ver baixas.py). Valores sempre em centavos inteiros.
"""
import calendar
import json
import re
import sqlite3
from datetime import date, timedelta

from .config import data_dir
from .ledger import LedgerError
from .timeutil import now_iso, today_sp

NATURES = {
    "operacional": "Operacional (receita ou despesa do negócio)",
    "prolabore": "Pró-labore (despesa administrativa)",
    "distribuicao_lucros": "Distribuição de lucros (fora da DRE)",
    "emprestimo_juros": "Empréstimo: juros (resultado financeiro)",
    "emprestimo_principal": "Empréstimo: principal (fora da DRE)",
    "compra_cartao": "Compra no cartão (DRE agora; caixa só na fatura)",
    "fatura_cartao": "Pagamento de fatura (só caixa; fora da DRE)",
    "imposto": "Imposto",
}
NATURES_RECEBER = ("operacional", "emprestimo_principal")
ORIGINS = {
    "contrato": "Contrato confirmado",
    "obrigacao": "Obrigação assumida",
    "estimativa": "Estimativa",
    "oportunidade": "Oportunidade",
}
CONFIRMED_ORIGINS = ("contrato", "obrigacao")      # só estas entram no caixa projetado principal e na DRE
EDITABLE = {"description", "counterparty", "category", "cost_center", "amount_cents", "due_date", "expected_date",
            "competence", "expected_account_id", "nature"}
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ------------------------------------------------------------------ utilidades

def log(conn, entity: str, entity_id: int | None, action: str, before=None, after=None) -> None:
    conn.execute(
        "INSERT INTO gestao_log (at, entity, entity_id, action, before_json, after_json) VALUES (?,?,?,?,?,?)",
        (now_iso(), entity, entity_id, action,
         json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
         json.dumps(after, ensure_ascii=False, default=str) if after is not None else None))


def add_months(d: date, n: int, day: int | None = None) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
    m += 1
    return date(y, m, min(day or d.day, calendar.monthrange(y, m)[1]))


def month_of(d: date | str) -> str:
    return (d if isinstance(d, str) else d.isoformat())[:7]


def shift_month(competence: str, n: int) -> str:
    y, m = int(competence[:4]), int(competence[5:7])
    return month_of(add_months(date(y, m, 1), n))


def split_cents(total: int, n: int) -> list[int]:
    """Divide em n parcelas sem perder centavos: as primeiras recebem o resto."""
    base, rest = divmod(total, n)
    return [base + (1 if i < rest else 0) for i in range(n)]


def _check_competence(c: str) -> str:
    if not _MONTH.match(c or ""):
        raise LedgerError("competência deve ser um mês no formato AAAA-MM")
    return c


def _save_attachment(obligation_id: int, name: str, content: bytes) -> str:
    folder = data_dir() / "anexos"
    folder.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", name) or "anexo"
    path = folder / f"obrigacao_{obligation_id}_{safe}"
    path.write_bytes(content)
    return str(path.relative_to(data_dir()))


# ------------------------------------------------------------------ leitura

_BASE = """
SELECT i.*, o.direction, o.origin, o.probability_pct, o.recurrence, o.note AS obligation_note,
       o.attachment_path, o.cancelled_at AS o_cancelled_at,
       COALESCE(SUM(CASE WHEN a.undone_at IS NULL AND a.kind IN ('pagamento','taxa','desconto','diferenca')
                         THEN a.amount_cents END), 0) AS settled_gross,
       COALESCE(SUM(CASE WHEN a.undone_at IS NULL AND a.kind='estorno' THEN a.amount_cents END), 0) AS reversed
FROM installments i JOIN obligations o ON o.id = i.obligation_id
LEFT JOIN allocations a ON a.installment_id = i.id
"""


def _decorate(r: sqlite3.Row, today: date) -> dict:
    d = dict(r)
    d["settled_cents"] = d.pop("settled_gross") - d.pop("reversed")
    d["open_cents"] = d["amount_cents"] - d["settled_cents"]
    cancelled = bool(d["cancelled_at"] or d.pop("o_cancelled_at"))
    if cancelled:
        d["status"] = "cancelada"
    elif d["open_cents"] <= 0:
        d["status"] = "liquidada"
    elif d["settled_cents"] > 0:
        d["status"] = "parcialmente liquidada"
    else:
        d["status"] = "prevista"
    d["overdue"] = (not cancelled) and d["open_cents"] > 0 and date.fromisoformat(d["due_date"]) < today
    d["has_allocations"] = False  # preenchido por list_installments (uma consulta só, sem N+1)
    return d


def list_installments(conn, today: date | None = None, *, direction: str | None = None, include_cancelled: bool = False,
                      origins: tuple[str, ...] | None = None, installment_ids: list[int] | None = None,
                      obligation_id: int | None = None) -> list[dict]:
    today = today or today_sp()
    where, params = [], []
    if direction:
        where.append("o.direction=?")
        params.append(direction)
    if origins:
        where.append(f"o.origin IN ({','.join('?' * len(origins))})")
        params += list(origins)
    if installment_ids is not None:
        where.append(f"i.id IN ({','.join('?' * len(installment_ids)) or 'NULL'})")
        params += list(installment_ids)
    if obligation_id:
        where.append("i.obligation_id=?")
        params.append(obligation_id)
    sql = _BASE + (" WHERE " + " AND ".join(where) if where else "") + " GROUP BY i.id ORDER BY i.expected_date, i.id"
    rows = conn.execute(sql, params).fetchall()
    touched = {r[0] for r in conn.execute("SELECT DISTINCT installment_id FROM allocations WHERE undone_at IS NULL")}
    out = []
    for r in rows:
        d = _decorate(r, today)
        d["has_allocations"] = d["id"] in touched
        if include_cancelled or d["status"] != "cancelada":
            out.append(d)
    return out


def get_installment(conn, installment_id: int, today: date | None = None) -> dict:
    rows = list_installments(conn, today, installment_ids=[installment_id], include_cancelled=True)
    if not rows:
        raise LedgerError(f"parcela {installment_id} não existe")
    return rows[0]


def list_allocations(conn, installment_id: int, include_undone: bool = True) -> list[dict]:
    sql = ("SELECT a.*, t.posted_date, t.description AS tx_description, t.amount_cents AS tx_amount_cents "
           "FROM allocations a LEFT JOIN transactions t ON t.id=a.tx_id WHERE a.installment_id=?")
    if not include_undone:
        sql += " AND a.undone_at IS NULL"
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.id", (installment_id,))]


# ------------------------------------------------------------------ criação

def create_obligation(conn, *, direction: str, description: str, amount_cents: int, due_date: str,
                      counterparty: str = "", category: str = "", cost_center: str = "", nature: str = "operacional",
                      origin: str = "contrato", probability_pct: int | None = None, expected_account_id: int | None = None,
                      expected_date: str | None = None, competence: str | None = None, competence_offset_months: int = 0,
                      note: str = "", attachment: tuple[str, bytes] | None = None, installments: int = 1,
                      amounts: list[int] | None = None, recurrence: dict | None = None) -> int:
    """Cria a obrigação e suas parcelas.

    - installments=N (>1): parcelamento. amount_cents é o TOTAL (dividido sem perder centavos) ou use `amounts`.
    - recurrence={"freq": "mensal", "months": 12, "day": 15}: N mensalidades de amount_cents cada.
    - competência da 1ª parcela: `competence` (AAAA-MM) ou mês do vencimento + competence_offset_months.
    """
    if direction not in ("receber", "pagar"):
        raise LedgerError("tipo deve ser 'receber' ou 'pagar'")
    if nature not in NATURES:
        raise LedgerError(f"natureza inválida: {nature}")
    if direction == "receber" and nature not in NATURES_RECEBER:
        raise LedgerError("a receber só pode ser operacional ou principal de empréstimo")
    if origin not in ORIGINS:
        raise LedgerError(f"origem inválida: {origin}")
    if probability_pct is not None and not 0 <= probability_pct <= 100:
        raise LedgerError("probabilidade deve estar entre 0 e 100")
    if not description.strip():
        raise LedgerError("descrição obrigatória")
    if amount_cents <= 0:
        raise LedgerError("o valor deve ser maior que zero")
    first_due = date.fromisoformat(due_date)
    offset_days = (date.fromisoformat(expected_date) - first_due).days if expected_date else 0

    if recurrence:
        n = int(recurrence.get("months", 12))
        if n < 1:
            raise LedgerError("a recorrência precisa de ao menos 1 mês")
        day = int(recurrence.get("day") or first_due.day)
        values = [amount_cents] * n
        dues = [add_months(first_due.replace(day=1), i, day) for i in range(n)]
        rec_json = json.dumps({"freq": "mensal", "months": n, "day": day, "expected_offset_days": offset_days,
                               "competence_offset_months": competence_offset_months})
    elif installments > 1:
        n = installments
        values = list(amounts) if amounts else split_cents(amount_cents, n)
        if len(values) != n or any(v <= 0 for v in values):
            raise LedgerError("valores das parcelas inválidos")
        if amounts and sum(values) != amount_cents:
            raise LedgerError("a soma das parcelas difere do total informado")
        dues = [add_months(first_due, i) for i in range(n)]
        rec_json = None
    else:
        n, values, dues, rec_json = 1, [amount_cents], [first_due], None

    comp0 = _check_competence(competence) if competence else shift_month(month_of(first_due), competence_offset_months)
    now = now_iso()
    with conn:
        oid = conn.execute(
            "INSERT INTO obligations (direction, nature, origin, probability_pct, expected_account_id, note, recurrence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (direction, nature, origin, probability_pct, expected_account_id, note.strip(), rec_json, now)).lastrowid
        if attachment:
            conn.execute("UPDATE obligations SET attachment_path=? WHERE id=?",
                         (_save_attachment(oid, attachment[0], attachment[1]), oid))
        for i, (v, due) in enumerate(zip(values, dues), start=1):
            if recurrence and not competence:
                comp = shift_month(month_of(due), competence_offset_months)   # cada mensalidade no mês do seu vencimento
            else:
                comp = shift_month(comp0, i - 1)                              # parcelas: mês a mês a partir da 1ª
            conn.execute(
                "INSERT INTO installments (obligation_id, number, of_total, nature, description, counterparty, category,"
                " cost_center, amount_cents, due_date, expected_date, competence, expected_account_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, i, n, nature, description.strip(), counterparty.strip(), category.strip(), cost_center.strip(), v,
                 due.isoformat(), (due + timedelta(days=offset_days)).isoformat(), comp, expected_account_id, now, now))
        log(conn, "obligation", oid, "criar", None, {"direction": direction, "description": description, "parcelas": n,
                                                     "valor_total": sum(values), "origem": origin, "natureza": nature})
    return oid


def extend_recurrence(conn, obligation_id: int, extra_months: int) -> list[int]:
    """Gera mais mensalidades depois da última, com os atributos da última parcela."""
    insts = list_installments(conn, obligation_id=obligation_id, include_cancelled=True)
    if not insts:
        raise LedgerError("obrigação sem parcelas")
    rec = conn.execute("SELECT recurrence FROM obligations WHERE id=?", (obligation_id,)).fetchone()["recurrence"]
    if not rec:
        raise LedgerError("esta obrigação não é recorrente")
    cfg = json.loads(rec)
    last = max(insts, key=lambda r: r["number"])
    now, new_ids = now_iso(), []
    with conn:
        for k in range(1, extra_months + 1):
            due = add_months(date.fromisoformat(last["due_date"]).replace(day=1), k, cfg["day"])
            cur = conn.execute(
                "INSERT INTO installments (obligation_id, number, of_total, nature, description, counterparty, category,"
                " cost_center, amount_cents, due_date, expected_date, competence, expected_account_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obligation_id, last["number"] + k, last["of_total"] + k if last["of_total"] else None, last["nature"],
                 last["description"], last["counterparty"], last["category"], last["cost_center"], last["amount_cents"],
                 due.isoformat(), (due + timedelta(days=cfg.get("expected_offset_days", 0))).isoformat(),
                 shift_month(month_of(due), cfg.get("competence_offset_months", 0)), last["expected_account_id"], now, now))
            new_ids.append(cur.lastrowid)
        log(conn, "obligation", obligation_id, "estender recorrência", None, {"meses": extra_months})
    return new_ids


# ------------------------------------------------------------------ edição

def _validate_changes(changes: dict) -> dict:
    bad = set(changes) - EDITABLE
    if bad:
        raise LedgerError(f"campos não editáveis: {', '.join(sorted(bad))}")
    if "amount_cents" in changes and changes["amount_cents"] <= 0:
        raise LedgerError("o valor deve ser maior que zero")
    if "competence" in changes:
        _check_competence(changes["competence"])
    if "nature" in changes and changes["nature"] not in NATURES:
        raise LedgerError("natureza inválida")
    for k in ("due_date", "expected_date"):
        if k in changes:
            date.fromisoformat(changes[k])
    return changes


def update_installment(conn, installment_id: int, **changes) -> None:
    """Edita uma parcela. Parcela liquidada não muda; parcela com baixa parcial não pode ficar abaixo do já baixado."""
    changes = _validate_changes(changes)
    cur = get_installment(conn, installment_id)
    if cur["status"] == "cancelada":
        raise LedgerError("parcela cancelada não pode ser editada")
    if cur["status"] == "liquidada":
        raise LedgerError("parcela já liquidada não pode ser reescrita; desfaça a baixa antes, se necessário")
    if "amount_cents" in changes and changes["amount_cents"] < cur["settled_cents"]:
        raise LedgerError("o novo valor não pode ser menor que o já baixado nesta parcela")
    if not changes:
        return
    sets = ", ".join(f"{k}=?" for k in changes)
    with conn:
        conn.execute(f"UPDATE installments SET {sets}, updated_at=? WHERE id=?",
                     (*changes.values(), now_iso(), installment_id))
        log(conn, "installment", installment_id, "editar", {k: cur[k] for k in changes}, changes)


def update_series(conn, obligation_id: int, from_due_date: str, **changes) -> dict:
    """Aplica a mudança às parcelas FUTURAS (vencimento ≥ from_due_date) que ainda não têm nenhuma baixa.
    Parcelas liquidadas, parcialmente baixadas ou anteriores não são reescritas. Retorna {updated, skipped}."""
    changes = _validate_changes(changes)
    updated, skipped = [], []
    for p in list_installments(conn, obligation_id=obligation_id, include_cancelled=False):
        if p["due_date"] < from_due_date:
            continue
        if p["has_allocations"]:
            skipped.append(p["id"])
            continue
        c = dict(changes)
        if "due_date" in c or "expected_date" in c:
            c.pop("due_date", None)  # datas de uma série não se aplicam em bloco; edite parcela a parcela
            c.pop("expected_date", None)
        if c:
            update_installment(conn, p["id"], **c)
        updated.append(p["id"])
    with conn:
        log(conn, "obligation", obligation_id, "editar série futura", None,
            {"a_partir_de": from_due_date, **changes, "atualizadas": updated, "preservadas": skipped})
    return {"updated": updated, "skipped": skipped}


def cancel_installment(conn, installment_id: int) -> None:
    cur = get_installment(conn, installment_id)
    if cur["has_allocations"]:
        raise LedgerError("a parcela tem baixas; desfaça as baixas antes de cancelar")
    with conn:
        conn.execute("UPDATE installments SET cancelled_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), installment_id))
        log(conn, "installment", installment_id, "cancelar", {"status": cur["status"]}, None)


def cancel_obligation(conn, obligation_id: int, from_due_date: str | None = None) -> dict:
    """Cancela as parcelas sem baixa (a partir de uma data, se informada). Parcelas com baixa permanecem."""
    cancelled, kept = [], []
    for p in list_installments(conn, obligation_id=obligation_id):
        if from_due_date and p["due_date"] < from_due_date:
            continue
        if p["has_allocations"]:
            kept.append(p["id"])
        else:
            cancel_installment(conn, p["id"])
            cancelled.append(p["id"])
    if not kept and not from_due_date:
        with conn:
            conn.execute("UPDATE obligations SET cancelled_at=? WHERE id=?", (now_iso(), obligation_id))
    return {"cancelled": cancelled, "kept": kept}
