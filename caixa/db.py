import sqlite3
from pathlib import Path

from .config import db_path
from .timeutil import now_iso

KINDS = {
    "receita": "Receita",
    "despesa": "Despesa",
    "transferencia": "Transferência entre contas",
    "aporte": "Aporte",
    "retirada": "Retirada / pró-labore",
    "imposto": "Imposto",
    "tarifa": "Tarifa",
    "estorno": "Estorno",
    "ajuste": "Ajuste",
    "investimento": "Aplicação / resgate (CDI)",
}

_KINDS_SQL = ",".join(f"'{k}'" for k in KINDS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    opening_balance_cents INTEGER NOT NULL,
    opening_date TEXT NOT NULL,          -- saldo vale no INÍCIO deste dia
    ofx_acct_id TEXT,                    -- ACCTID visto no primeiro OFX (proteção contra extrato da conta errada)
    kind TEXT NOT NULL DEFAULT 'conta',  -- reservado; hoje todas as contas são caixa
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    stored_path TEXT,
    imported_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    n_total INTEGER NOT NULL,
    n_new INTEGER NOT NULL,
    n_duplicate INTEGER NOT NULL,
    n_flagged INTEGER NOT NULL
);

-- Dados bancários originais: imutáveis (ver triggers abaixo).
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    batch_id INTEGER NOT NULL REFERENCES import_batches(id),
    fitid TEXT,
    dedup_key TEXT NOT NULL,
    posted_date TEXT NOT NULL,           -- YYYY-MM-DD em America/Sao_Paulo
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    trntype TEXT,
    UNIQUE (account_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS ix_tx_account_date ON transactions(account_id, posted_date);

CREATE TABLE IF NOT EXISTS statement_balances (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    batch_id INTEGER NOT NULL REFERENCES import_batches(id),
    as_of_date TEXT NOT NULL,            -- saldo ao FIM deste dia
    balance_cents INTEGER NOT NULL,
    UNIQUE (account_id, as_of_date, balance_cents)
);

CREATE TABLE IF NOT EXISTS classifications (
    tx_id INTEGER PRIMARY KEY REFERENCES transactions(id),
    kind TEXT NOT NULL CHECK (kind IN ({_KINDS_SQL})),
    counterparty TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    cost_center TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    attachment_path TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classification_log (
    id INTEGER PRIMARY KEY,
    tx_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);

CREATE TABLE IF NOT EXISTS transfer_links (
    id INTEGER PRIMARY KEY,
    out_tx_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    in_tx_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    created_at TEXT NOT NULL,
    CHECK (out_tx_id <> in_tx_id)
);

-- Posição informada por você (o extrato OFX não traz o saldo aplicado): valor total no CDI ao FIM do dia.
CREATE TABLE IF NOT EXISTS investment_positions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    as_of_date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                -- 'ok' | 'erro'
    n_new INTEGER NOT NULL DEFAULT 0,
    n_duplicate INTEGER NOT NULL DEFAULT 0,
    n_skipped INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS dup_flags (
    id INTEGER PRIMARY KEY,
    tx_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    other_tx_id INTEGER NOT NULL REFERENCES transactions(id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','dismissed','confirmed')),
    updated_at TEXT NOT NULL
);


-- ===================== Gestão financeira: previsto (contas a receber/pagar) =====================
-- Nada aqui altera ou duplica movimentos bancários: as baixas (allocations) só ligam um lançamento do extrato
-- a uma parcela e reduzem o saldo em aberto dela.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS obligations (
    id INTEGER PRIMARY KEY,
    direction TEXT NOT NULL CHECK (direction IN ('receber','pagar')),
    nature TEXT NOT NULL DEFAULT 'operacional',
    origin TEXT NOT NULL DEFAULT 'contrato' CHECK (origin IN ('contrato','obrigacao','estimativa','oportunidade')),
    probability_pct INTEGER,                       -- só para oportunidade; nunca vira "dinheiro garantido"
    expected_account_id INTEGER REFERENCES accounts(id),
    note TEXT NOT NULL DEFAULT '',
    attachment_path TEXT,
    recurrence TEXT,                               -- JSON com frequência e dia (ou NULL)
    created_at TEXT NOT NULL,
    cancelled_at TEXT
);

-- Cada parcela guarda seus próprios atributos: editar a série futura não reescreve parcelas já liquidadas.
CREATE TABLE IF NOT EXISTS installments (
    id INTEGER PRIMARY KEY,
    obligation_id INTEGER NOT NULL REFERENCES obligations(id),
    number INTEGER NOT NULL,
    of_total INTEGER,
    nature TEXT NOT NULL DEFAULT 'operacional',
    description TEXT NOT NULL,
    counterparty TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    cost_center TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    due_date TEXT NOT NULL,                        -- vencimento
    expected_date TEXT NOT NULL,                   -- data prevista de recebimento/pagamento (pode diferir)
    competence TEXT NOT NULL,                      -- AAAA-MM (mês em que a receita/despesa pertence)
    expected_account_id INTEGER REFERENCES accounts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_inst_due ON installments(due_date);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY,
    installment_id INTEGER NOT NULL REFERENCES installments(id),
    tx_id INTEGER REFERENCES transactions(id),     -- NULL em taxa/desconto/diferença (ajustes sem movimento bancário)
    kind TEXT NOT NULL CHECK (kind IN ('pagamento','taxa','desconto','diferenca','estorno')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    undone_at TEXT,                                -- desfazer = preencher; o registro nunca é apagado
    CHECK ((kind IN ('pagamento','estorno') AND tx_id IS NOT NULL) OR (kind NOT IN ('pagamento','estorno') AND tx_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_alloc_tx ON allocations(tx_id);
CREATE INDEX IF NOT EXISTS ix_alloc_inst ON allocations(installment_id);

CREATE TABLE IF NOT EXISTS gestao_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);

-- Competência (mês da DRE) de lançamentos bancários históricos que não estão ligados a obrigações.
CREATE TABLE IF NOT EXISTS tx_competence (
    tx_id INTEGER PRIMARY KEY REFERENCES transactions(id),
    competence TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Em que linha da DRE cada categoria cai.
CREATE TABLE IF NOT EXISTS category_map (
    category TEXT PRIMARY KEY,
    dre_line TEXT NOT NULL
);

-- Lançamentos que NÃO são dinheiro novo (ex.: pagamento de fatura de cartão pago com o limite já depositado):
-- continuam guardados, mas ficam fora de saldos e listas.
CREATE TABLE IF NOT EXISTS ignored_tx (
    tx_id INTEGER PRIMARY KEY REFERENCES transactions(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Compras/movimentos ainda pendentes que já reduzem o limite/saldo disponível (valor líquido, negativo = saída).
CREATE TABLE IF NOT EXISTS pending_balance (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    net_cents INTEGER NOT NULL,
    as_of_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Lançamentos que contam para saldos: tudo, exceto duplicatas confirmadas e lançamentos ignorados (o original segue guardado).
CREATE VIEW IF NOT EXISTS effective_tx AS
    SELECT t.* FROM transactions t
    WHERE NOT EXISTS (SELECT 1 FROM dup_flags d WHERE d.tx_id = t.id AND d.status = 'confirmed')
      AND NOT EXISTS (SELECT 1 FROM ignored_tx i WHERE i.tx_id = t.id);

CREATE TRIGGER IF NOT EXISTS transactions_no_update BEFORE UPDATE ON transactions
BEGIN SELECT RAISE(ABORT, 'lançamentos bancários originais são imutáveis'); END;
CREATE TRIGGER IF NOT EXISTS transactions_no_delete BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'lançamentos bancários originais são imutáveis'); END;
"""


def _backup_before_migration(path: Path) -> Path | None:
    """Se já existe um banco anterior à gestão financeira, guarda uma cópia consistente antes de migrar."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    probe = sqlite3.connect(str(path))
    try:
        names = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "transactions" not in names or "installments" in names:
            return None
        dest_dir = path.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"pre_gestao_{now_iso()[:19].replace(':', '').replace('-', '').replace('T', '_')}.db"
        out = sqlite3.connect(str(dest))
        try:
            probe.backup(out)
        finally:
            out.close()
        return dest
    finally:
        probe.close()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    _backup_before_migration(Path(path or db_path()))
    conn = sqlite3.connect(str(path or db_path()), check_same_thread=False)  # Streamlit roda cada rerun em outra thread
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bancos criados em versões anteriores: acrescenta colunas novas."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "kind" not in cols:
        with conn:
            conn.execute("ALTER TABLE accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'conta'")
    view = conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='effective_tx'").fetchone()
    if view and "ignored_tx" not in view["sql"]:
        conn.executescript("DROP VIEW effective_tx;\n" + SCHEMA)   # recria a visão com a regra nova
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='classifications'").fetchone()["sql"]
    if "'investimento'" not in sql:
        # o CHECK de tipos não aceita o tipo novo: recria a tabela preservando as linhas
        conn.executescript(
            "PRAGMA foreign_keys=OFF; BEGIN; ALTER TABLE classifications RENAME TO classifications_old;\n"
            + SCHEMA
            + "\nINSERT INTO classifications SELECT * FROM classifications_old;"
            " DROP TABLE classifications_old; COMMIT; PRAGMA foreign_keys=ON;")
    with conn:  # versão anterior tratava cartões da Conta Simples como dívida; são cartões de débito = caixa
        conn.execute("UPDATE accounts SET kind='conta' WHERE kind='cartao'")


def create_account(conn: sqlite3.Connection, name: str, opening_balance_cents: int, opening_date: str,
                   kind: str = "conta") -> int:
    name = name.strip()
    if not name:
        raise ValueError("nome da conta obrigatório")
    with conn:
        cur = conn.execute(
            "INSERT INTO accounts (name, opening_balance_cents, opening_date, created_at, kind) VALUES (?,?,?,?,?)",
            (name, int(opening_balance_cents), opening_date, now_iso(), kind),
        )
    return cur.lastrowid


def update_account_opening(conn: sqlite3.Connection, account_id: int, opening_balance_cents: int, opening_date: str) -> None:
    with conn:
        conn.execute(
            "UPDATE accounts SET opening_balance_cents=?, opening_date=? WHERE id=?",
            (int(opening_balance_cents), opening_date, account_id),
        )


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    """Dicts (não sqlite3.Row) para poderem ser usados como opções de widgets do Streamlit."""
    return [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]
