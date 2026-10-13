"""Conciliação e investigação de divergências."""
from datetime import date, timedelta

from .ledger import LedgerError, account_summary, is_linked
from .money import format_brl
from .timeutil import now_iso, today_sp


def statement_comparisons(conn, account_id: int | None = None) -> list[dict]:
    """Saldo calculado × saldo informado no extrato, por data de extrato. diff = calculado − extrato."""
    sql = """SELECT s.*, a.name AS account_name, a.opening_date FROM statement_balances s
             JOIN accounts a ON a.id=s.account_id
             WHERE s.id = (SELECT MAX(s2.id) FROM statement_balances s2
                           WHERE s2.account_id=s.account_id AND s2.as_of_date=s.as_of_date)"""
    params: tuple = ()
    if account_id:
        sql += " AND s.account_id=?"
        params = (account_id,)
    out = []
    for s in conn.execute(sql + " ORDER BY s.as_of_date, s.account_id", params).fetchall():
        item = {"account_id": s["account_id"], "account_name": s["account_name"], "date": s["as_of_date"],
                "statement_cents": s["balance_cents"], "calculated_cents": None, "diff_cents": None,
                "status": "antes do saldo inicial", "hint": ""}
        if s["as_of_date"] >= s["opening_date"]:
            calc = account_summary(conn, s["account_id"], s["as_of_date"])["calculated_cents"]
            diff = calc - s["balance_cents"]
            item.update(calculated_cents=calc, diff_cents=diff, status="ok" if diff == 0 else "divergente")
            if diff:
                item["hint"] = _diff_hint(conn, s["account_id"], s["as_of_date"], diff)
        out.append(item)
    return out


def _diff_hint(conn, account_id: int, as_of: str, diff: int) -> str:
    """Pistas: a diferença bate com algum lançamento (faltando, duplicado ou com sinal trocado)?"""
    acc = conn.execute("SELECT opening_date FROM accounts WHERE id=?", (account_id,)).fetchone()
    hints = []
    rows = conn.execute(
        "SELECT id, posted_date, description, amount_cents FROM effective_tx WHERE account_id=? AND posted_date>=? AND posted_date<=?",
        (account_id, acc["opening_date"], as_of),
    ).fetchall()
    for r in rows:
        a = r["amount_cents"]
        if diff == a:
            hints.append(f"a diferença é igual ao lançamento #{r['id']} ({r['posted_date']}, {r['description']}, {format_brl(a)}): "
                         "se ele estiver duplicado ou não existir no banco, explica tudo")
        elif diff == -a:
            hints.append(f"a diferença é o inverso do lançamento #{r['id']} ({r['posted_date']}, {r['description']}): "
                         "pode faltar um igual a ele no extrato importado")
        elif diff == 2 * a:
            hints.append(f"a diferença é 2× o lançamento #{r['id']} ({r['posted_date']}): o sinal dele pode estar trocado")
    return "; ".join(hints[:3])


# ---------------------------------------------------------------- duplicatas

def possible_duplicates(conn, status: str = "open") -> list[dict]:
    rows = conn.execute(
        """SELECT d.id AS flag_id, d.reason, d.status,
                  t.id AS tx_id, t.posted_date, t.description, t.amount_cents, a.name AS account_name, t.batch_id,
                  o.id AS other_id, o.posted_date AS other_date, o.description AS other_description,
                  o.amount_cents AS other_amount_cents, o.batch_id AS other_batch_id
           FROM dup_flags d JOIN transactions t ON t.id=d.tx_id JOIN transactions o ON o.id=d.other_tx_id
           JOIN accounts a ON a.id=t.account_id WHERE d.status=? ORDER BY t.posted_date, t.id""",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_duplicate(conn, flag_id: int, decision: str) -> None:
    """decision: 'dismissed' (não é duplicata), 'confirmed' (é duplicata: sai dos saldos, original preservado) ou 'open'."""
    if decision not in ("dismissed", "confirmed", "open"):
        raise LedgerError("decisão inválida")
    flag = conn.execute("SELECT * FROM dup_flags WHERE id=?", (flag_id,)).fetchone()
    if flag is None:
        raise LedgerError("sinalização inexistente")
    if decision == "confirmed":
        if is_linked(conn, flag["tx_id"]):
            raise LedgerError("o lançamento está vinculado a uma transferência; desfaça o vínculo antes")
        with conn:
            conn.execute("DELETE FROM classifications WHERE tx_id=?", (flag["tx_id"],))
    with conn:
        conn.execute("UPDATE dup_flags SET status=?, updated_at=? WHERE id=?", (decision, now_iso(), flag_id))


# ---------------------------------------------------------------- transferências

def unpaired_transfers(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id, e.posted_date, e.description, e.amount_cents, a.name AS account_name
           FROM effective_tx e JOIN classifications c ON c.tx_id=e.id JOIN accounts a ON a.id=e.account_id
           WHERE c.kind='transferencia'
             AND e.id NOT IN (SELECT out_tx_id FROM transfer_links UNION SELECT in_tx_id FROM transfer_links)
           ORDER BY e.posted_date, e.id"""
    ).fetchall()
    return [dict(r) for r in rows]


def transfer_pairs(conn) -> list[dict]:
    """Pares vinculados; diff_cents = |saída| − entrada (≠ 0 indica tarifa/valor divergente)."""
    rows = conn.execute(
        """SELECT l.id AS link_id, o.id AS out_id, o.posted_date AS out_date, o.amount_cents AS out_cents,
                  ao.name AS out_account, i.id AS in_id, i.posted_date AS in_date, i.amount_cents AS in_cents,
                  ai.name AS in_account
           FROM transfer_links l JOIN transactions o ON o.id=l.out_tx_id JOIN transactions i ON i.id=l.in_tx_id
           JOIN accounts ao ON ao.id=o.account_id JOIN accounts ai ON ai.id=i.account_id
           ORDER BY o.posted_date, l.id"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["diff_cents"] = -r["out_cents"] - r["in_cents"]
        d["days_apart"] = abs((date.fromisoformat(r["in_date"]) - date.fromisoformat(r["out_date"])).days)
        out.append(d)
    return out


# ---------------------------------------------------------------- cobertura de extratos

def coverage(conn, today: str | None = None) -> list[dict]:
    """Por conta: intervalos cobertos pelos extratos importados, lacunas entre o saldo inicial e o último extrato."""
    today_d = date.fromisoformat(today) if today else today_sp()
    result = []
    for acc in conn.execute("SELECT * FROM accounts ORDER BY id").fetchall():
        spans = sorted(
            (date.fromisoformat(r["period_start"]), date.fromisoformat(r["period_end"]))
            for r in conn.execute(
                "SELECT period_start, period_end FROM import_batches WHERE account_id=? AND period_start IS NOT NULL",
                (acc["id"],))
        )
        merged: list[list[date]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + timedelta(days=1):
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        opening = date.fromisoformat(acc["opening_date"])
        gaps: list[tuple[str, str]] = []
        if not merged:
            result.append({"account_id": acc["id"], "account_name": acc["name"], "covered": [], "gaps": [],
                           "no_statement": True, "last_end": None, "stale_days": None})
            continue
        if merged[0][0] > opening:
            gaps.append((opening.isoformat(), (merged[0][0] - timedelta(days=1)).isoformat()))
        for a, b in zip(merged, merged[1:]):
            gaps.append(((a[1] + timedelta(days=1)).isoformat(), (b[0] - timedelta(days=1)).isoformat()))
        last_end = merged[-1][1]
        result.append({
            "account_id": acc["id"], "account_name": acc["name"],
            "covered": [(s.isoformat(), e.isoformat()) for s, e in merged], "gaps": gaps,
            "no_statement": False, "last_end": last_end.isoformat(),
            "stale_days": max((today_d - last_end).days, 0),
        })
    return result


def investigation_counts(conn) -> dict:
    cmp = statement_comparisons(conn)
    cov = coverage(conn)
    return {
        "duplicatas": len(possible_duplicates(conn)),
        "transferencias_sem_par": len(unpaired_transfers(conn)),
        "pares_com_diferenca": sum(1 for p in transfer_pairs(conn) if p["diff_cents"] != 0),
        "saldos_divergentes": sum(1 for c in cmp if c["status"] == "divergente"),
        "lacunas": sum(len(c["gaps"]) + (1 if c["no_statement"] else 0) for c in cov),
    }
