import sqlite3

import pytest

from caixa import recon
from caixa.export import backup_database, classified_csv
from caixa.importer import commit_import, plan_import
from caixa.ledger import (LedgerError, account_summary, classify, company_breakdown, link_transfer,
                          list_unclassified, suggest_transfer_matches, unlink_transfer)
from tests.helpers import make_ofx


def imp(conn, acc, txs, **kw):
    return commit_import(conn, plan_import(conn, acc, "x.ofx", make_ofx(txs, **kw)))


def tx_id(conn, desc):
    return conn.execute("SELECT id FROM transactions WHERE description=?", (desc,)).fetchone()[0]


@pytest.fixture
def loaded(conn, accounts):
    pag, cs = accounts
    imp(conn, pag, [("20260803", "4500.00", "RECEBIDO CLIENTE", "P1"), ("20260810", "-3000.00", "TED PARA CS", "P2"),
                    ("20260807", "-39.90", "TARIFA", "P3")], ledger=("11460.10", "20260831"))
    imp(conn, cs, [("20260810", "3000.00", "RECEBIDO DO PAG", "C1"), ("20260814", "-1890.00", "GOOGLE ADS", "C2")],
        acct="999", ledger=("6110.00", "20260831"))
    return pag, cs


def test_balance_formula(conn, loaded):
    pag, cs = loaded
    s = account_summary(conn, pag)
    assert (s["opening_cents"], s["entradas_cents"], s["saidas_cents"]) == (1_000_000, 450_000, 303_990)
    assert s["calculated_cents"] == 1_000_000 + 450_000 - 303_990 == 1_146_010
    assert account_summary(conn, pag, "2026-08-05")["calculated_cents"] == 1_450_000
    # os dois extratos informam o saldo certo → sem divergência
    assert all(c["status"] == "ok" for c in recon.statement_comparisons(conn))


def test_transactions_before_opening_date_are_excluded(conn, accounts):
    pag, _ = accounts
    imp(conn, pag, [("20260720", "999.00", "ANTES", "X1"), ("20260801", "100.00", "NO DIA", "X2")])
    s = account_summary(conn, pag)
    assert s["calculated_cents"] == 1_000_000 + 10_000 and s["n_before_opening"] == 1


def test_statement_divergence_and_hint(conn, accounts):
    pag, _ = accounts
    imp(conn, pag, [("20260803", "89.90", "FIGMA", "X1")], ledger=("10000.00", "20260831"))
    (c,) = recon.statement_comparisons(conn)
    assert c["status"] == "divergente" and c["diff_cents"] == 8_990
    assert "#" in c["hint"]  # diferença = valor do lançamento


def test_transfer_moves_balances_but_not_result(conn, loaded):
    pag, cs = loaded
    out_id, in_id = tx_id(conn, "TED PARA CS"), tx_id(conn, "RECEBIDO DO PAG")
    before = (account_summary(conn, pag)["calculated_cents"], account_summary(conn, cs)["calculated_cents"])
    classify(conn, tx_id(conn, "RECEBIDO CLIENTE"), "receita", counterparty="Cliente X")
    classify(conn, tx_id(conn, "GOOGLE ADS"), "despesa")
    classify(conn, tx_id(conn, "TARIFA"), "tarifa")
    assert len(list_unclassified(conn)) == 2
    link_transfer(conn, out_id, in_id)
    assert len(list_unclassified(conn)) == 0
    # classificar/vincular não muda saldo; a saída reduz o PagBank e a entrada aumenta a Conta Simples
    assert before == (account_summary(conn, pag)["calculated_cents"], account_summary(conn, cs)["calculated_cents"])
    assert account_summary(conn, pag)["calculated_cents"] == 1_146_010
    assert account_summary(conn, cs)["calculated_cents"] == 500_000 + 300_000 - 189_000
    co = company_breakdown(conn)
    assert co["transferencias_cents"] == 0           # -3000 + 3000 se anulam
    assert co["resultado_cents"] == 450_000 - 189_000 - 3_990
    assert co["entradas_empresa_cents"] == 450_000   # transferência não é receita
    assert co["saidas_empresa_cents"] == 189_000 + 3_990   # nem despesa
    assert recon.unpaired_transfers(conn) == []
    assert recon.transfer_pairs(conn)[0]["diff_cents"] == 0


def test_unpaired_transfer_and_unlink(conn, loaded):
    out_id, in_id = tx_id(conn, "TED PARA CS"), tx_id(conn, "RECEBIDO DO PAG")
    classify(conn, out_id, "transferencia")
    assert [u["id"] for u in recon.unpaired_transfers(conn)] == [out_id]
    cands = suggest_transfer_matches(conn, out_id)
    assert cands[0]["id"] == in_id and cands[0]["value_diff_cents"] == 0
    link_transfer(conn, out_id, in_id)
    assert recon.unpaired_transfers(conn) == []
    unlink_transfer(conn, recon.transfer_pairs(conn)[0]["link_id"])
    assert len(list_unclassified(conn)) == 5


def test_transfer_with_fee_difference_is_reported(conn, accounts):
    pag, cs = accounts
    imp(conn, pag, [("20260810", "-3000.00", "TED OUT", "P1")])
    imp(conn, cs, [("20260811", "2990.00", "TED IN", "C1")], acct="999")
    link_transfer(conn, tx_id(conn, "TED OUT"), tx_id(conn, "TED IN"))
    (p,) = recon.transfer_pairs(conn)
    assert p["diff_cents"] == 1_000 and p["days_apart"] == 1


def test_link_validations(conn, loaded):
    out_id, in_id = tx_id(conn, "TED PARA CS"), tx_id(conn, "RECEBIDO DO PAG")
    with pytest.raises(LedgerError):
        link_transfer(conn, in_id, out_id)                      # invertido
    with pytest.raises(LedgerError):
        link_transfer(conn, out_id, tx_id(conn, "TARIFA"))      # mesma conta
    classify(conn, in_id, "aporte")
    with pytest.raises(LedgerError):
        link_transfer(conn, out_id, in_id)                      # já classificada como outra coisa


def test_sign_rules(conn, loaded):
    with pytest.raises(LedgerError):
        classify(conn, tx_id(conn, "GOOGLE ADS"), "receita")


def test_reclassify_keeps_original_and_logs(conn, loaded):
    t = tx_id(conn, "GOOGLE ADS")
    classify(conn, t, "despesa", category="Mídia")
    classify(conn, t, "despesa", category="Marketing", note="campanha", attachment=("nota.pdf", b"%PDF"))
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (t,)).fetchone()
    assert (row["amount_cents"], row["description"], row["posted_date"]) == (-189_000, "GOOGLE ADS", "2026-08-14")
    assert conn.execute("SELECT COUNT(*) FROM classification_log WHERE tx_id=?", (t,)).fetchone()[0] == 2
    assert conn.execute("SELECT attachment_path FROM classifications WHERE tx_id=?", (t,)).fetchone()[0].startswith("anexos")


def test_confirmed_duplicate_leaves_balance_but_stays_stored(conn, accounts):
    _, cs = accounts
    imp(conn, cs, [("20260812", "-15.00", "UBER", ""), ("20260812", "-15.00", "UBER", "")], fitid=False)
    assert account_summary(conn, cs)["calculated_cents"] == 500_000 - 3_000
    (d,) = recon.possible_duplicates(conn)
    recon.resolve_duplicate(conn, d["flag_id"], "confirmed")
    assert account_summary(conn, cs)["calculated_cents"] == 500_000 - 1_500
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    recon.resolve_duplicate(conn, d["flag_id"], "open")
    assert account_summary(conn, cs)["calculated_cents"] == 500_000 - 3_000


def test_coverage_gaps(conn, accounts):
    pag, cs = accounts
    imp(conn, pag, [("20260805", "1.00", "A", "A1")], start="20260805", end="20260831")
    imp(conn, pag, [("20260915", "1.00", "B", "B1")], start="20260910", end="20260930")
    cov = {c["account_name"]: c for c in recon.coverage(conn, today="2026-10-13")}
    assert cov["PagBank"]["gaps"] == [("2026-08-01", "2026-08-04"), ("2026-09-01", "2026-09-09")]
    assert cov["PagBank"]["stale_days"] == 13
    assert cov["Conta Simples"]["no_statement"]


def test_csv_export_and_backup(conn, loaded, tmp_path):
    classify(conn, tx_id(conn, "GOOGLE ADS"), "despesa", counterparty="Google", category="Mídia")
    text = classified_csv(conn).decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert len(lines) == 2 and "-1890,00" in lines[1] and "Despesa" in lines[1]
    dest = backup_database(conn, tmp_path / "bk")
    assert sqlite3.connect(dest).execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 5
