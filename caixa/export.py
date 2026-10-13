import csv
import io
import sqlite3
from pathlib import Path

from .config import data_dir
from .db import KINDS
from .ledger import list_classified
from .money import cents_to_plain
from .timeutil import now_sp

CSV_COLUMNS = [
    "id", "conta", "data", "descricao_extrato", "valor_centavos", "valor", "tipo", "cliente_fornecedor",
    "categoria", "centro_custo", "descricao_livre", "anexo", "fitid", "arquivo_origem", "par_transferencia_id",
    "classificado_em",
]


def classified_csv(conn) -> bytes:
    """CSV em UTF-8 com BOM, separador ';' e vírgula decimal (abre direto no Excel em português)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in list_classified(conn):
        w.writerow([
            r["id"], r["account_name"], r["posted_date"], r["description"], r["amount_cents"],
            cents_to_plain(r["amount_cents"]), KINDS[r["kind"]], r["counterparty"], r["category"], r["cost_center"],
            r["note"], r["attachment_path"] or "", r["fitid"] or "", r["source_file"], r["pair_tx_id"] or "",
            r["classified_at"],
        ])
    return buf.getvalue().encode("utf-8-sig")


def backup_database(conn: sqlite3.Connection, dest_dir: Path | None = None) -> Path:
    """Cópia consistente do SQLite (API de backup) em data/backups/."""
    dest_dir = dest_dir or data_dir() / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"caixa_qcm_{now_sp():%Y%m%d_%H%M%S}.db"
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()
    return dest
