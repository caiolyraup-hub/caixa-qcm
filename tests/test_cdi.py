"""CDI/renda fixa: o caixa do PagBank é o saldo disponível + o que está aplicado."""
import sqlite3

import pytest

from caixa import recon
from caixa.db import connect, create_account
from caixa.importer import DUPLICADO, POSSIVEL, commit_import, plan_import
from caixa.ledger import (LedgerError, account_summary, company_breakdown, delete_investment_position,
                          get_classification, investment_summary, list_positions, set_investment_position,
                          suggest_opening_from_statement)
from caixa.ofx import parse_ofx


def pagbank_ofx(rows, ledger="R$ 0,00", asof="13/10/2026"):
    """Imita o OFX real do PagBank: XML identado com tags fechadas, saldo 'R$ 0,00' e data DD/MM/AAAA."""
    body = ""
    for d, amt, fitid, memo in rows:
        body += (f"<STMTTRN>\r\n<TRNTYPE>{'IN' if not amt.startswith('-') else 'OUT'}</TRNTYPE>\r\n"
                 f"<DTPOSTED>{d}120000[-3:BRT]</DTPOSTED>\r\n<TRNAMT>{amt}</TRNAMT>\r\n"
                 f"<FITID>{fitid}</FITID>\r\n<MEMO>{memo}</MEMO>\r\n</STMTTRN>\r\n")
    return ("OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:100\r\nSECURITY:NONE\r\nENCODING:UTF-8\r\nCHARSET:NONE\r\n\r\n"
            "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>BRL</CURDEF><BANKACCTFROM><BANKID>290</BANKID>"
            "<ACCTID>11111111-1</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM><BANKTRANLIST>"
            f"<DTSTART>20240103000000[-3:BRT]</DTSTART><DTEND>20261013000000[-3:BRT]</DTEND>\r\n{body}</BANKTRANLIST>"
            f"<LEDGERBAL><BALAMT>{ledger}</BALAMT><DTASOF>{asof}</DTASOF></LEDGERBAL></STMTRS></STMTTRNRS>"
            "</BANKMSGSRSV1></OFX>").encode("utf-8")


# Cenário: o dinheiro entra por Pix, vai todo para o CDB e o saldo disponível termina em zero.
ROWS = [
    ("20260825", "5000.00", "a1", "Pix recebido - Cliente X"),
    ("20260825", "-5000.00", "a2", "Renda Fixa - Aplicação em CDB"),
    ("20260901", "-200.00", "a3", "Pix enviado - Fornecedor"),
    ("20260901", "200.00", "a4", "Renda Fixa - Resgate parcial de CDB"),
    # evento com o MESMO FITID nas duas pontas (como o PagBank faz no vencimento/reinvestimento)
    ("20260924", "-2089.48", "dup1", "Renda fixa - Vencimento/Reinvestimento em CDB"),
    ("20260924", "2089.48", "dup1", "Renda fixa - Vencimento/Reinvestimento em CDB"),
    ("20261013", "-4800.00", "a5", "Pix enviado - Fornecedor 2"),
    ("20261013", "4800.00", "a6", "Renda Fixa - Resgate total de CDB"),
]
# disponível: +5000 -5000 -200 +200 -2089,48 +2089,48 -4800 +4800 = 0


@pytest.fixture
def pag(conn):
    return create_account(conn, "PagBank", 0, "2026-08-25")


def imp(conn, acc, rows=ROWS, **kw):
    return commit_import(conn, plan_import(conn, acc, "Extrato da Conta - PagBank.ofx", pagbank_ofx(rows, **kw)))


def test_parses_real_pagbank_format():
    st = parse_ofx(pagbank_ofx(ROWS))
    assert (st.ledger_balance_cents, st.ledger_balance_date) == (0, "2026-10-13")
    assert len(st.txs) == 8 and st.acct_id == "11111111-1"


def test_repeated_fitid_pair_imports_both_legs_without_false_duplicate(conn, pag):
    plan = plan_import(conn, pag, "x.ofx", pagbank_ofx(ROWS))
    assert POSSIVEL not in [r.status for r in plan.rows] and DUPLICADO not in [r.status for r in plan.rows]
    imp(conn, pag)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 8
    r = imp(conn, pag)                                   # reimportar não duplica nada
    assert r["novos"] == 0 and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 8


def test_cdi_movements_are_auto_classified_as_investment(conn, pag):
    r = imp(conn, pag)
    assert r["auto_classificados"] == 5
    kinds = {row["description"]: get_classification(conn, row["id"])
             for row in conn.execute("SELECT * FROM transactions")}
    assert kinds["Pix recebido - Cliente X"] is None                       # Pix continua para você classificar
    assert kinds["Renda Fixa - Aplicação em CDB"]["kind"] == "investimento"
    assert "Automático" in kinds["Renda Fixa - Resgate total de CDB"]["note"]
    assert conn.execute("SELECT COUNT(*) FROM classification_log WHERE action='classificar'").fetchone()[0] == 5


def test_investment_moves_are_not_company_revenue_or_expense(conn, pag):
    imp(conn, pag)
    from caixa.ledger import classify
    classify(conn, conn.execute("SELECT id FROM transactions WHERE description LIKE 'Pix recebido%'").fetchone()[0], "receita")
    classify(conn, conn.execute("SELECT id FROM transactions WHERE description='Pix enviado - Fornecedor'").fetchone()[0], "despesa")
    co = company_breakdown(conn)
    # saídas = 200 classificados + 4.800 do Pix ainda sem classificar; nada do CDI entra
    assert co["entradas_empresa_cents"] == 500_000 and co["saidas_empresa_cents"] == 20_000 + 480_000
    assert co["resultado_cents"] == 500_000 - 20_000
    assert co["investimentos_cents"] == 0                  # -5000 +200 -2089,48 +2089,48 +4800: internos, líquido zero
    assert co["by_kind"]["investimento"]["n"] == 5


def test_cash_is_available_balance_plus_cdi_position(conn, pag):
    imp(conn, pag)
    s = account_summary(conn, pag)
    assert s["calculated_cents"] == 0 and s["cdi_cents"] == 0 and s["n_investimento"] == 5
    # o saldo do extrato (R$ 0,00 disponível) confere com o calculado: o CDI não distorce a conciliação
    assert all(c["status"] == "ok" for c in recon.statement_comparisons(conn))
    set_investment_position(conn, pag, "2026-10-13", 593_100)             # R$ 5.931,00 aplicados
    s = account_summary(conn, pag)
    assert s["cdi_cents"] == 593_100 and s["caixa_total_cents"] == 593_100 and s["calculated_cents"] == 0
    # posição de data futura não conta numa consulta no passado
    assert account_summary(conn, pag, "2026-09-01")["cdi_cents"] == 0
    with pytest.raises(LedgerError):
        set_investment_position(conn, pag, "2026-10-13", -1)


def test_yield_estimate_between_two_positions(conn, pag):
    imp(conn, pag)
    set_investment_position(conn, pag, "2026-08-25", 0)
    set_investment_position(conn, pag, "2026-10-13", 10_000)
    set_investment_position(conn, pag, "2026-10-13", 10_500)              # mesma data: substitui
    assert len(list_positions(conn, pag)) == 2
    inv = investment_summary(conn, pag)
    # aplicado depois de 25/08: 2089,48 (reinvest.) ; resgatado: 200 + 2089,48 + 4800 ; (os 5000 de 25/08 não contam: mesmo dia da 1ª posição)
    assert inv["aplicado_cents"] == 208_948 and inv["resgatado_cents"] == 20_000 + 208_948 + 480_000
    assert inv["rendimento_cents"] == 10_500 - 0 - 208_948 + (20_000 + 208_948 + 480_000)
    delete_investment_position(conn, list_positions(conn, pag)[0]["id"])
    assert investment_summary(conn, pag)["rendimento_cents"] is None       # com uma posição só não dá para estimar


def test_suggest_opening_balance_from_statement(conn):
    pag = create_account(conn, "PagBank", 999_999, "2026-10-01")          # saldo errado e data depois dos lançamentos
    imp(conn, pag)
    sug = suggest_opening_from_statement(conn, pag)
    assert sug["opening_date"] == "2026-08-25" and sug["opening_cents"] == 0
    assert sug["statement_cents"] == 0 and sug["statement_date"] == "2026-10-13"
    # com extrato dizendo R$ 100 no fim, o saldo inicial sugerido é 100 (a soma dos movimentos é zero)
    pag2 = create_account(conn, "Outra", 0, "2026-08-01")
    imp(conn, pag2, rows=[("20260825", "50.00", "z1", "Pix recebido - Y")], ledger="R$ 150,00")
    assert suggest_opening_from_statement(conn, pag2)["opening_cents"] == 100 * 100


def test_old_database_is_migrated_to_accept_investment_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    db = tmp_path / "caixa_qcm.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, opening_balance_cents INTEGER NOT NULL,
            opening_date TEXT NOT NULL, ofx_acct_id TEXT, created_at TEXT NOT NULL);
        CREATE TABLE import_batches (id INTEGER PRIMARY KEY);
        CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER, batch_id INTEGER, fitid TEXT,
            dedup_key TEXT NOT NULL, posted_date TEXT NOT NULL, description TEXT NOT NULL, amount_cents INTEGER NOT NULL,
            trntype TEXT, UNIQUE (account_id, dedup_key));
        CREATE TABLE classifications (tx_id INTEGER PRIMARY KEY REFERENCES transactions(id),
            kind TEXT NOT NULL CHECK (kind IN ('receita','despesa','transferencia','aporte','retirada','imposto','tarifa','estorno','ajuste')),
            counterparty TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', cost_center TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '', attachment_path TEXT, updated_at TEXT NOT NULL);
        INSERT INTO accounts VALUES (1,'PagBank',0,'2026-08-01',NULL,'t');
        INSERT INTO import_batches VALUES (1);
        INSERT INTO transactions VALUES (1,1,1,'f','k','2026-08-02','x',100,NULL);
        INSERT INTO classifications VALUES (1,'receita','Cli','Cat','','n',NULL,'t');
    """)
    old.commit()
    old.close()
    c = connect()                                                      # migra
    row = c.execute("SELECT * FROM classifications").fetchone()
    assert (row["kind"], row["counterparty"]) == ("receita", "Cli")     # dados preservados
    from caixa.ledger import classify
    c.execute("INSERT INTO transactions (id, account_id, batch_id, fitid, dedup_key, posted_date, description, amount_cents) "
              "VALUES (2,1,1,'g','k2','2026-08-03','Renda Fixa',-50)")
    classify(c, 2, "investimento")                                     # o CHECK novo aceita o tipo
    assert c.execute("SELECT kind FROM classifications WHERE tx_id=2").fetchone()[0] == "investimento"
    connect().close()                                                  # migrar de novo é inofensivo
