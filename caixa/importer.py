"""Importação OFX em duas etapas: plan_import (prévia, não grava) → commit_import (grava)."""
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import data_dir
from .ofx import OfxStatement, OfxTx, parse_ofx
from .rules import auto_classification
from .timeutil import now_iso

NOVO = "novo"
DUPLICADO = "duplicado"
POSSIVEL = "possível duplicata"
CONFLITO = "FITID repetido com valores diferentes"


def _norm(desc: str) -> str:
    return re.sub(r"\s+", " ", desc).strip().upper()


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


@dataclass
class PlanRow:
    tx: OfxTx
    key: str
    status: str
    note: str = ""
    ref_existing_id: int | None = None
    ref_plan_idx: int | None = None


@dataclass
class ImportPlan:
    account_id: int
    filename: str
    sha256: str
    data: bytes
    stmt: OfxStatement
    rows: list[PlanRow] = field(default_factory=list)
    account_mismatch: bool = False   # ACCTID do arquivo difere do visto antes nesta conta
    file_seen_before: bool = False   # exatamente este arquivo já foi importado

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)

    @property
    def n_to_insert(self) -> int:
        return sum(1 for r in self.rows if r.status != DUPLICADO)


def plan_import(conn: sqlite3.Connection, account_id: int, filename: str, data: bytes) -> ImportPlan:
    return plan_statement(conn, account_id, filename, data, parse_ofx(data))


def plan_statement(conn: sqlite3.Connection, account_id: int, filename: str, data: bytes,
                   stmt: OfxStatement) -> ImportPlan:
    """Mesmo planejamento para OFX e para sincronização por API (data = bytes do arquivo/payload de origem)."""
    acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if acct is None:
        raise ValueError("conta inexistente")
    sha = hashlib.sha256(data).hexdigest()
    plan = ImportPlan(account_id, filename, sha, data, stmt)
    plan.account_mismatch = bool(acct["ofx_acct_id"] and stmt.acct_id and acct["ofx_acct_id"] != stmt.acct_id)
    plan.file_seen_before = conn.execute(
        "SELECT 1 FROM import_batches WHERE account_id=? AND file_sha256=?", (account_id, sha)
    ).fetchone() is not None

    # FITID só é confiável se existir e não se repetir dentro do próprio arquivo.
    counts: dict[str, int] = {}
    for t in stmt.txs:
        if t.fitid:
            counts[t.fitid] = counts.get(t.fitid, 0) + 1

    occ: dict[tuple, int] = {}          # ocorrência n-ésima de linhas idênticas no arquivo
    first_in_plan: dict[tuple, int] = {}  # impressão digital → 1º índice planejado

    for t in stmt.txs:
        fp = (t.posted_date, t.amount_cents, _norm(t.description))
        if t.fitid and counts[t.fitid] == 1:
            key = f"F:{t.fitid}"
            row = conn.execute(
                "SELECT id, posted_date, amount_cents FROM transactions WHERE account_id=? AND dedup_key=?",
                (account_id, key),
            ).fetchone()
            if row is None:
                plan.rows.append(PlanRow(t, key, NOVO))
            elif row["posted_date"] == t.posted_date and row["amount_cents"] == t.amount_cents:
                plan.rows.append(PlanRow(t, key, DUPLICADO, "FITID já importado", ref_existing_id=row["id"]))
            else:
                key2 = f"F:{t.fitid}|{t.posted_date}|{t.amount_cents}"
                seen = conn.execute(
                    "SELECT id FROM transactions WHERE account_id=? AND dedup_key=?", (account_id, key2)
                ).fetchone()
                if seen:
                    plan.rows.append(PlanRow(t, key2, DUPLICADO, "FITID conflitante já importado", ref_existing_id=seen["id"]))
                else:
                    plan.rows.append(PlanRow(
                        t, key2, CONFLITO,
                        "mesmo FITID de um lançamento existente, mas data/valor diferentes",
                        ref_existing_id=row["id"],
                    ))
            continue

        n = occ.get(fp, 0)
        occ[fp] = n + 1
        key = "H:" + _sha1("|".join(map(str, fp)) + f"|{n}")
        existing = conn.execute("SELECT id FROM transactions WHERE account_id=? AND dedup_key=?", (account_id, key)).fetchone()
        if existing:
            plan.rows.append(PlanRow(t, key, DUPLICADO, "mesma data, valor e descrição já importados", ref_existing_id=existing["id"]))
            continue

        # Linha nova sem FITID confiável: se já existe algo idêntico (no banco ou antes no arquivo), pode ser duplicata.
        ref_existing = None
        for r in conn.execute(
            "SELECT id, description FROM transactions WHERE account_id=? AND posted_date=? AND amount_cents=?",
            (account_id, t.posted_date, t.amount_cents),
        ):
            if _norm(r["description"]) == fp[2]:
                ref_existing = r["id"]
                break
        idx = len(plan.rows)
        if ref_existing is not None:
            plan.rows.append(PlanRow(t, key, POSSIVEL,
                                     "sem FITID; já há lançamento idêntico (data, valor, descrição) importado antes",
                                     ref_existing_id=ref_existing))
        elif fp in first_in_plan:
            plan.rows.append(PlanRow(t, key, POSSIVEL,
                                     "sem FITID; linha idêntica a outra do mesmo arquivo",
                                     ref_plan_idx=first_in_plan[fp]))
        else:
            plan.rows.append(PlanRow(t, key, NOVO))
        first_in_plan.setdefault(fp, idx)
    return plan


def commit_import(conn: sqlite3.Connection, plan: ImportPlan) -> dict:
    """Grava o plano numa única transação. Reimportar é idempotente (linhas DUPLICADO são ignoradas)."""
    origem = data_dir() / "origem"
    origem.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", plan.filename)
    stored_path = origem / f"{plan.sha256[:10]}_{safe}"
    if not stored_path.exists():
        stored_path.write_bytes(plan.data)
    stored = str(stored_path.relative_to(data_dir()))

    flagged = sum(1 for r in plan.rows if r.status in (POSSIVEL, CONFLITO))
    with conn:
        cur = conn.execute(
            "INSERT INTO import_batches (account_id, filename, file_sha256, stored_path, imported_at, period_start,"
            " period_end, n_total, n_new, n_duplicate, n_flagged) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (plan.account_id, plan.filename, plan.sha256, stored, now_iso(), plan.stmt.period_start,
             plan.stmt.period_end, len(plan.rows), plan.n_to_insert, plan.count(DUPLICADO), flagged),
        )
        batch_id = cur.lastrowid
        new_ids: dict[int, int] = {}
        for idx, r in enumerate(plan.rows):
            if r.status == DUPLICADO:
                continue
            c = conn.execute(
                "INSERT INTO transactions (account_id, batch_id, fitid, dedup_key, posted_date, description,"
                " amount_cents, trntype) VALUES (?,?,?,?,?,?,?,?)",
                (plan.account_id, batch_id, r.tx.fitid or None, r.key, r.tx.posted_date, r.tx.description,
                 r.tx.amount_cents, r.tx.trntype or None),
            )
            new_ids[idx] = c.lastrowid
            if r.status in (POSSIVEL, CONFLITO):
                other = r.ref_existing_id if r.ref_existing_id is not None else new_ids[r.ref_plan_idx]
                conn.execute(
                    "INSERT INTO dup_flags (tx_id, other_tx_id, reason, status, updated_at) VALUES (?,?,?,'open',?)",
                    (c.lastrowid, other, f"{r.status}: {r.note}", now_iso()),
                )
        if plan.stmt.ledger_balance_cents is not None:
            conn.execute(
                "INSERT OR IGNORE INTO statement_balances (account_id, batch_id, as_of_date, balance_cents) VALUES (?,?,?,?)",
                (plan.account_id, batch_id, plan.stmt.ledger_balance_date, plan.stmt.ledger_balance_cents),
            )
        if plan.stmt.acct_id:
            conn.execute("UPDATE accounts SET ofx_acct_id=? WHERE id=? AND ofx_acct_id IS NULL",
                         (plan.stmt.acct_id, plan.account_id))
    from .ledger import classify  # import tardio: ledger não depende do importador
    auto = 0
    for idx, r in enumerate(plan.rows):
        rule = auto_classification(r.tx.description, r.tx.amount_cents) if idx in new_ids else None
        if rule:
            classify(conn, new_ids[idx], rule["kind"], category=rule.get("category", ""), note=rule["note"])
            auto += 1
    return {"batch_id": batch_id, "novos": plan.n_to_insert, "duplicados": plan.count(DUPLICADO),
            "sinalizados": flagged, "auto_classificados": auto}
