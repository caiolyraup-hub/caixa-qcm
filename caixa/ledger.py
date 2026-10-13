"""Classificação, vínculo de transferências e cálculo de saldos."""
import json
import re
import sqlite3
from datetime import date, timedelta

from .config import data_dir
from .db import KINDS
from .timeutil import now_iso

# Sinal esperado do valor no extrato para cada tipo (os demais aceitam qualquer sinal).
_SIGN = {"receita": 1, "aporte": 1, "despesa": -1, "retirada": -1, "imposto": -1, "tarifa": -1}
# Tipos que compõem o resultado da empresa (transferência e movimentação de sócios ficam de fora).
RESULT_KINDS = ("receita", "despesa", "imposto", "tarifa", "estorno")


class LedgerError(ValueError):
    pass


def _tx(conn, tx_id):
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise LedgerError(f"lançamento {tx_id} não existe")
    return row


def _is_confirmed_dup(conn, tx_id) -> bool:
    """True se o lançamento está fora dos saldos: duplicata confirmada ou lançamento ignorado."""
    return (conn.execute("SELECT 1 FROM dup_flags WHERE tx_id=? AND status='confirmed'", (tx_id,)).fetchone() is not None
            or conn.execute("SELECT 1 FROM ignored_tx WHERE tx_id=?", (tx_id,)).fetchone() is not None)


def _link_of(conn, tx_id):
    return conn.execute("SELECT * FROM transfer_links WHERE out_tx_id=? OR in_tx_id=?", (tx_id, tx_id)).fetchone()


def is_linked(conn, tx_id) -> bool:
    return _link_of(conn, tx_id) is not None


def _log(conn, tx_id, action, before, after):
    conn.execute(
        "INSERT INTO classification_log (tx_id, changed_at, action, before_json, after_json) VALUES (?,?,?,?,?)",
        (tx_id, now_iso(), action, json.dumps(before, ensure_ascii=False) if before else None,
         json.dumps(after, ensure_ascii=False) if after else None),
    )


def get_classification(conn, tx_id) -> dict | None:
    row = conn.execute("SELECT * FROM classifications WHERE tx_id=?", (tx_id,)).fetchone()
    return dict(row) if row else None


def _save_attachment(tx_id: int, name: str, content: bytes) -> str:
    folder = data_dir() / "anexos"
    folder.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", name) or "anexo"
    path = folder / f"{tx_id}_{safe}"
    path.write_bytes(content)
    return str(path.relative_to(data_dir()))


def _upsert(conn, tx_id, kind, counterparty, category, cost_center, note, attachment, action):
    before = get_classification(conn, tx_id)
    att_path = before["attachment_path"] if before else None
    if attachment:
        att_path = _save_attachment(tx_id, attachment[0], attachment[1])
    conn.execute(
        "INSERT INTO classifications (tx_id, kind, counterparty, category, cost_center, note, attachment_path, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tx_id) DO UPDATE SET kind=excluded.kind,"
        " counterparty=excluded.counterparty, category=excluded.category, cost_center=excluded.cost_center,"
        " note=excluded.note, attachment_path=excluded.attachment_path, updated_at=excluded.updated_at",
        (tx_id, kind, counterparty.strip(), category.strip(), cost_center.strip(), note.strip(), att_path, now_iso()),
    )
    _log(conn, tx_id, action, before, get_classification(conn, tx_id))


def classify(conn, tx_id: int, kind: str, *, counterparty="", category="", cost_center="", note="",
             attachment: tuple[str, bytes] | None = None) -> None:
    """Classifica (ou reclassifica) um lançamento. Nunca altera o dado bancário original."""
    if kind not in KINDS:
        raise LedgerError(f"tipo inválido: {kind}")
    tx = _tx(conn, tx_id)
    if _is_confirmed_dup(conn, tx_id):
        raise LedgerError("lançamento marcado como duplicata confirmada; reabra a duplicata antes de classificar")
    expected = _SIGN.get(kind)
    if expected and (tx["amount_cents"] > 0) != (expected > 0):
        raise LedgerError(
            f"'{KINDS[kind]}' espera valor {'positivo (entrada)' if expected > 0 else 'negativo (saída)'}; "
            f"este lançamento é {'entrada' if tx['amount_cents'] > 0 else 'saída'}"
        )
    if _link_of(conn, tx_id) and kind != "transferencia":
        raise LedgerError("este lançamento está vinculado a uma transferência; desfaça o vínculo primeiro")
    with conn:
        _upsert(conn, tx_id, kind, counterparty, category, cost_center, note, attachment, "classificar")


def clear_classification(conn, tx_id: int) -> None:
    if _link_of(conn, tx_id):
        raise LedgerError("desfaça o vínculo da transferência primeiro")
    with conn:
        before = get_classification(conn, tx_id)
        conn.execute("DELETE FROM classifications WHERE tx_id=?", (tx_id,))
        if before:
            _log(conn, tx_id, "remover", before, None)


# ---------------------------------------------------------------- transferências

def link_transfer(conn, out_tx_id: int, in_tx_id: int) -> int:
    """Vincula a saída de uma conta à entrada da outra. Os dois viram 'transferência' (fora do resultado)."""
    out, inn = _tx(conn, out_tx_id), _tx(conn, in_tx_id)
    if out["account_id"] == inn["account_id"]:
        raise LedgerError("as duas pontas precisam estar em contas diferentes")
    if out["amount_cents"] >= 0 or inn["amount_cents"] <= 0:
        raise LedgerError("a saída precisa ser negativa e a entrada positiva")
    for t in (out, inn):
        if _is_confirmed_dup(conn, t["id"]):
            raise LedgerError("um dos lançamentos é duplicata confirmada")
        if _link_of(conn, t["id"]):
            raise LedgerError(f"lançamento {t['id']} já está vinculado a outra transferência")
        c = get_classification(conn, t["id"])
        if c and c["kind"] != "transferencia":
            raise LedgerError(f"lançamento {t['id']} já está classificado como '{KINDS[c['kind']]}'; remova a classificação antes")
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM accounts")}
    with conn:
        cur = conn.execute("INSERT INTO transfer_links (out_tx_id, in_tx_id, created_at) VALUES (?,?,?)",
                           (out_tx_id, in_tx_id, now_iso()))
        for t, other in ((out, inn), (inn, out)):
            prev = get_classification(conn, t["id"]) or {}
            direction = "para" if t["id"] == out_tx_id else "de"
            _upsert(conn, t["id"], "transferencia", prev.get("counterparty", ""), prev.get("category", ""),
                    prev.get("cost_center", ""),
                    prev.get("note") or f"Transferência {direction} {names[other['account_id']]}", None, "vincular")
    return cur.lastrowid


def unlink_transfer(conn, link_id: int) -> None:
    """Desfaz o vínculo e devolve as duas pontas para 'A identificar'."""
    link = conn.execute("SELECT * FROM transfer_links WHERE id=?", (link_id,)).fetchone()
    if link is None:
        raise LedgerError("vínculo inexistente")
    with conn:
        conn.execute("DELETE FROM transfer_links WHERE id=?", (link_id,))
        for tid in (link["out_tx_id"], link["in_tx_id"]):
            before = get_classification(conn, tid)
            conn.execute("DELETE FROM classifications WHERE tx_id=?", (tid,))
            _log(conn, tid, "desvincular", before, None)


def suggest_transfer_matches(conn, tx_id: int, window_days: int = 3, only_same_amount: bool = False) -> list[dict]:
    """Pontas candidatas na outra conta: sinal oposto, ±window_days, ainda sem vínculo. Melhores primeiro."""
    tx = _tx(conn, tx_id)
    d = date.fromisoformat(tx["posted_date"])
    sign_cond = "amount_cents > 0" if tx["amount_cents"] < 0 else "amount_cents < 0"
    rows = conn.execute(
        f"""SELECT e.*, a.name AS account_name FROM effective_tx e JOIN accounts a ON a.id=e.account_id
            WHERE e.account_id <> ? AND e.{sign_cond} AND e.posted_date BETWEEN ? AND ?
              AND e.id NOT IN (SELECT out_tx_id FROM transfer_links UNION SELECT in_tx_id FROM transfer_links)
              AND NOT EXISTS (SELECT 1 FROM classifications c WHERE c.tx_id=e.id AND c.kind<>'transferencia')""",
        (tx["account_id"], (d - timedelta(days=window_days)).isoformat(), (d + timedelta(days=window_days)).isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        diff = abs(tx["amount_cents"]) - abs(r["amount_cents"])
        if only_same_amount and diff != 0:
            continue
        out.append({**dict(r), "value_diff_cents": diff,
                    "days_apart": abs((date.fromisoformat(r["posted_date"]) - d).days)})
    out.sort(key=lambda r: (abs(r["value_diff_cents"]), r["days_apart"]))
    return out


# ---------------------------------------------------------------- listagens

def list_unclassified(conn, account_id: int | None = None) -> list[sqlite3.Row]:
    sql = """SELECT e.*, a.name AS account_name,
                    EXISTS (SELECT 1 FROM dup_flags d WHERE d.tx_id=e.id AND d.status='open') AS dup_open
             FROM effective_tx e JOIN accounts a ON a.id=e.account_id
             WHERE NOT EXISTS (SELECT 1 FROM classifications c WHERE c.tx_id=e.id)"""
    params: tuple = ()
    if account_id:
        sql += " AND e.account_id=?"
        params = (account_id,)
    return conn.execute(sql + " ORDER BY e.posted_date, e.id", params).fetchall()


def list_classified(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT e.*, a.name AS account_name, c.kind, c.counterparty, c.category, c.cost_center, c.note,
                  c.attachment_path, c.updated_at AS classified_at,
                  (SELECT CASE WHEN l.out_tx_id=e.id THEN l.in_tx_id ELSE l.out_tx_id END
                     FROM transfer_links l WHERE l.out_tx_id=e.id OR l.in_tx_id=e.id) AS pair_tx_id,
                  b.filename AS source_file
           FROM effective_tx e JOIN classifications c ON c.tx_id=e.id
           JOIN accounts a ON a.id=e.account_id JOIN import_batches b ON b.id=e.batch_id
           ORDER BY e.posted_date, e.id"""
    ).fetchall()


def distinct_values(conn, column: str) -> list[str]:
    assert column in ("counterparty", "category", "cost_center")
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {column} FROM classifications WHERE {column}<>'' ORDER BY {column} COLLATE NOCASE")]


# ---------------------------------------------------------------- saldos

def account_summary(conn, account_id: int, as_of: str | None = None) -> dict:
    """saldo inicial + entradas − saídas = saldo calculado (lançamentos desde a data do saldo inicial até as_of)."""
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if acc is None:
        raise LedgerError("conta inexistente")
    end = as_of or "9999-12-31"
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents END),0) AS entradas,
                  COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents END),0) AS saidas,
                  COUNT(*) AS n
           FROM effective_tx WHERE account_id=? AND posted_date>=? AND posted_date<=?""",
        (account_id, acc["opening_date"], end),
    ).fetchone()
    before = conn.execute(
        "SELECT COUNT(*) FROM effective_tx WHERE account_id=? AND posted_date<?", (account_id, acc["opening_date"])
    ).fetchone()[0]
    unclassified = conn.execute(
        "SELECT COUNT(*) FROM effective_tx e WHERE e.account_id=? AND NOT EXISTS"
        " (SELECT 1 FROM classifications c WHERE c.tx_id=e.id)", (account_id,)
    ).fetchone()[0]
    opening = acc["opening_balance_cents"]
    pos = latest_position(conn, account_id, as_of)
    pend = conn.execute("SELECT * FROM pending_balance WHERE account_id=?", (account_id,)).fetchone()
    pending = pend["net_cents"] if pend and (as_of is None or as_of >= pend["as_of_date"]) else 0
    n_inv = conn.execute(
        "SELECT COUNT(*) FROM effective_tx e JOIN classifications c ON c.tx_id=e.id "
        "WHERE e.account_id=? AND c.kind='investimento'", (account_id,)).fetchone()[0]
    calculated = opening + r["entradas"] - r["saidas"]
    return {
        "cdi_cents": pos["amount_cents"] if pos else 0, "cdi_date": pos["as_of_date"] if pos else None,
        "n_investimento": n_inv, "pending_cents": pending,
        "caixa_total_cents": calculated + (pos["amount_cents"] if pos else 0) + pending,
        "account_id": account_id, "name": acc["name"], "kind": acc["kind"], "opening_date": acc["opening_date"],
        "opening_cents": opening, "entradas_cents": r["entradas"], "saidas_cents": r["saidas"],
        "calculated_cents": opening + r["entradas"] - r["saidas"], "n_tx": r["n"],
        "n_before_opening": before, "n_unclassified": unclassified,
    }


def company_breakdown(conn, start: str | None = None, end: str | None = None) -> dict:
    """Visão da empresa (consolidada): transferências entre contas não entram como receita nem despesa."""
    rows = conn.execute(
        """SELECT COALESCE(c.kind,'__nao_classificado') AS kind, SUM(e.amount_cents) AS total, COUNT(*) AS n
           FROM effective_tx e JOIN accounts a ON a.id=e.account_id
           LEFT JOIN classifications c ON c.tx_id=e.id
           WHERE e.posted_date>=a.opening_date AND e.posted_date>=? AND e.posted_date<=?
           GROUP BY 1""",
        (start or "0000-01-01", end or "9999-12-31"),
    ).fetchall()
    by_kind = {r["kind"]: {"total_cents": r["total"], "n": r["n"]} for r in rows}
    ext = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN e.amount_cents>0 THEN e.amount_cents END),0) AS ent,
                  COALESCE(SUM(CASE WHEN e.amount_cents<0 THEN -e.amount_cents END),0) AS sai
           FROM effective_tx e JOIN accounts a ON a.id=e.account_id
           LEFT JOIN classifications c ON c.tx_id=e.id
           WHERE e.posted_date>=a.opening_date AND e.posted_date>=? AND e.posted_date<=?
             AND COALESCE(c.kind,'') NOT IN ('transferencia','investimento')""",
        (start or "0000-01-01", end or "9999-12-31"),
    ).fetchone()
    total = lambda k: by_kind.get(k, {"total_cents": 0})["total_cents"]  # noqa: E731
    return {
        "by_kind": by_kind,
        "entradas_empresa_cents": ext["ent"],
        "saidas_empresa_cents": ext["sai"],
        "resultado_cents": sum(total(k) for k in RESULT_KINDS),
        "socios_cents": total("aporte") + total("retirada"),
        "transferencias_cents": total("transferencia"),
        "investimentos_cents": total("investimento"),
        "nao_classificado_cents": total("__nao_classificado"),
        "ajustes_cents": total("ajuste"),
    }


# ---------------------------------------------------------------- CDI / renda fixa

def set_investment_position(conn, account_id: int, as_of: str, amount_cents: int) -> None:
    """Registra quanto havia aplicado (CDI/renda fixa) ao fim do dia as_of. Uma posição por dia; repetir substitui."""
    if amount_cents < 0:
        raise LedgerError("a posição aplicada não pode ser negativa")
    with conn:
        conn.execute(
            "INSERT INTO investment_positions (account_id, as_of_date, amount_cents, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(account_id, as_of_date) DO UPDATE SET amount_cents=excluded.amount_cents, created_at=excluded.created_at",
            (account_id, as_of, int(amount_cents), now_iso()))


def delete_investment_position(conn, position_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM investment_positions WHERE id=?", (position_id,))


def list_positions(conn, account_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM investment_positions WHERE account_id=? ORDER BY as_of_date", (account_id,)).fetchall()


def latest_position(conn, account_id: int, as_of: str | None = None):
    return conn.execute(
        "SELECT * FROM investment_positions WHERE account_id=? AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1",
        (account_id, as_of or "9999-12-31")).fetchone()


def investment_summary(conn, account_id: int, as_of: str | None = None) -> dict:
    """Aplicações, resgates e rendimento estimado entre a primeira e a última posição informadas.

    rendimento = posição final − posição inicial − aplicações + resgates (movimentos classificados como 'investimento'
    depois da 1ª posição e até a última). Só existe com pelo menos duas posições informadas.
    """
    pos = [p for p in list_positions(conn, account_id) if p["as_of_date"] <= (as_of or "9999-12-31")]
    out = {"positions": len(pos), "aplicado_cents": 0, "resgatado_cents": 0, "rendimento_cents": None,
           "from": None, "to": None}
    if len(pos) < 2:
        return out
    first, last = pos[0], pos[-1]
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN e.amount_cents<0 THEN -e.amount_cents END),0) AS apl,
                  COALESCE(SUM(CASE WHEN e.amount_cents>0 THEN e.amount_cents END),0) AS res
           FROM effective_tx e JOIN classifications c ON c.tx_id=e.id
           WHERE e.account_id=? AND c.kind='investimento' AND e.posted_date>? AND e.posted_date<=?""",
        (account_id, first["as_of_date"], last["as_of_date"])).fetchone()
    out.update(aplicado_cents=r["apl"], resgatado_cents=r["res"], **{"from": first["as_of_date"], "to": last["as_of_date"]})
    out["rendimento_cents"] = last["amount_cents"] - first["amount_cents"] - r["apl"] + r["res"]
    return out


def suggest_opening_from_statement(conn, account_id: int) -> dict | None:
    """Saldo inicial que faz o saldo calculado bater com o último saldo informado no extrato, tomando como ponto de
    partida o início do dia do 1º lançamento importado. É o saldo DISPONÍVEL (sem o que está no CDI)."""
    st = conn.execute("SELECT * FROM statement_balances WHERE account_id=? ORDER BY as_of_date DESC, id DESC LIMIT 1",
                      (account_id,)).fetchone()
    first = conn.execute("SELECT MIN(posted_date) FROM effective_tx WHERE account_id=?", (account_id,)).fetchone()[0]
    if st is None or first is None or first > st["as_of_date"]:
        return None
    total = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM effective_tx WHERE account_id=? AND posted_date>=? AND posted_date<=?",
        (account_id, first, st["as_of_date"])).fetchone()[0]
    return {"opening_cents": st["balance_cents"] - total, "opening_date": first, "statement_date": st["as_of_date"],
            "statement_cents": st["balance_cents"]}


# ---------------------------------------------------------------- lançamentos ignorados / pendentes

def ignore_tx(conn, tx_id: int, reason: str) -> None:
    """Tira o lançamento dos saldos sem apagá-lo (ex.: pagamento de fatura que só liquida compras já contadas)."""
    _tx(conn, tx_id)
    if _link_of(conn, tx_id):
        raise LedgerError("o lançamento está vinculado a uma transferência; desfaça o vínculo antes")
    with conn:
        before = get_classification(conn, tx_id)
        conn.execute("DELETE FROM classifications WHERE tx_id=?", (tx_id,))
        if before:
            _log(conn, tx_id, "ignorar", before, None)
        conn.execute("INSERT OR REPLACE INTO ignored_tx (tx_id, reason, created_at) VALUES (?,?,?)", (tx_id, reason, now_iso()))


def unignore_tx(conn, tx_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM ignored_tx WHERE tx_id=?", (tx_id,))


def list_ignored(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.*, a.name AS account_name, i.reason FROM ignored_tx i JOIN transactions t ON t.id=i.tx_id "
        "JOIN accounts a ON a.id=t.account_id ORDER BY t.posted_date, t.id").fetchall()


def set_pending_balance(conn, account_id: int, net_cents: int, as_of: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO pending_balance (account_id, net_cents, as_of_date, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET net_cents=excluded.net_cents, as_of_date=excluded.as_of_date, "
            "updated_at=excluded.updated_at", (account_id, int(net_cents), as_of, now_iso()))
