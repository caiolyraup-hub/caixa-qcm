"""Migração de um banco anterior à gestão financeira: backup antes, nada original alterado, dados preservados."""
import hashlib
import sqlite3
from pathlib import Path

from caixa.db import SCHEMA, connect
from caixa.demo import seed
from caixa.ledger import account_summary

NEW_TABLES = ("settings", "obligations", "installments", "allocations", "gestao_log", "tx_competence", "category_map")


def digest(path: Path, tables) -> dict:
    c = sqlite3.connect(path)
    out = {}
    for t in tables:
        rows = c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
        out[t] = hashlib.sha256(repr(rows).encode()).hexdigest()
    c.close()
    return out


def make_old_db(tmp_path) -> Path:
    """Banco como era antes: tudo menos as tabelas de gestão (simulado removendo-as de um banco com dados reais de demo)."""
    c = connect()
    seed(c)
    c.close()
    path = tmp_path / "caixa_qcm.db"
    raw = sqlite3.connect(path)
    for t in ("allocations", "installments", "obligations", "gestao_log", "tx_competence", "category_map", "settings"):
        raw.execute(f"DROP TABLE {t}")
    raw.commit()
    raw.close()
    for d in (tmp_path / "backups",):
        if d.exists():
            for f in d.iterdir():
                f.unlink()
    return path


def test_migracao_faz_backup_cria_tabelas_e_nao_toca_nas_transacoes(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    path = make_old_db(tmp_path)
    protected = ("accounts", "import_batches", "transactions", "statement_balances", "classifications", "transfer_links",
                 "dup_flags", "classification_log", "ignored_tx", "pending_balance", "investment_positions")
    before = digest(path, protected)
    n_tx = sqlite3.connect(path).execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert n_tx > 0

    c = connect()                                                         # abre = migra
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(NEW_TABLES) <= names
    assert digest(path, protected) == before                              # nada original mudou, nem uma linha
    backups = list((tmp_path / "backups").glob("pre_gestao_*.db"))
    assert len(backups) == 1
    assert digest(backups[0], protected) == before                        # o backup é fiel ao banco de antes
    assert "obligations" not in {r[0] for r in sqlite3.connect(backups[0]).execute("SELECT name FROM sqlite_master")}
    # as funções antigas seguem funcionando sobre o banco migrado
    pag = c.execute("SELECT id FROM accounts WHERE name='PagBank'").fetchone()[0]
    assert account_summary(c, pag)["n_tx"] > 0
    c.close()

    connect().close()                                                     # reabrir não faz novo backup
    assert len(list((tmp_path / "backups").glob("pre_gestao_*.db"))) == 1


def test_banco_novo_nao_gera_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    connect().close()
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").glob("pre_gestao_*"))


def test_esquema_de_gestao_nao_permite_baixa_incoerente(tmp_path, monkeypatch):
    """O banco também se defende: ajuste sem lançamento só em taxa/desconto/diferença, pagamento sempre com lançamento."""
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    c = connect()
    c.execute("INSERT INTO obligations (direction, created_at) VALUES ('receber','t')")
    c.execute("INSERT INTO installments (obligation_id, number, description, amount_cents, due_date, expected_date, competence,"
              " created_at, updated_at) VALUES (1,1,'x',100,'2026-01-01','2026-01-01','2026-01','t','t')")
    for kind, tx in (("pagamento", None), ("taxa", 5)):
        try:
            c.execute("INSERT INTO allocations (installment_id, tx_id, kind, amount_cents, created_at) VALUES (1,?,?,10,'t')", (tx, kind))
            raise AssertionError("deveria ter sido recusado")
        except sqlite3.IntegrityError:
            pass
