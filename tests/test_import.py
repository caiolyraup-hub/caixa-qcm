import sqlite3

import pytest

from caixa.importer import DUPLICADO, NOVO, POSSIVEL, commit_import, plan_import
from tests.helpers import make_ofx

AUG = [("20260803", "4500.00", "PIX A", "F1"), ("20260810", "-3000.00", "TED B", "F2"),
       ("20260825", "1500.00", "PIX C", "F3")]
SEP = [("20260825", "1500.00", "PIX C", "F3"), ("20260902", "5200.00", "PIX D", "F4")]


def n_tx(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def do_import(conn, acc, name, data):
    plan = plan_import(conn, acc, name, data)
    return plan, commit_import(conn, plan)


def test_preview_does_not_write(conn, accounts):
    plan = plan_import(conn, accounts[0], "a.ofx", make_ofx(AUG))
    assert plan.count(NOVO) == 3 and n_tx(conn) == 0


def test_reimport_same_file_does_not_duplicate(conn, accounts):
    data = make_ofx(AUG, ledger=("100.00", "20260831"))
    do_import(conn, accounts[0], "a.ofx", data)
    plan, res = do_import(conn, accounts[0], "a.ofx", data)
    assert plan.file_seen_before and res["novos"] == 0 and res["duplicados"] == 3
    assert n_tx(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM statement_balances").fetchone()[0] == 1


def test_overlapping_periods_do_not_duplicate(conn, accounts):
    do_import(conn, accounts[0], "ago.ofx", make_ofx(AUG))
    plan, res = do_import(conn, accounts[0], "set.ofx", make_ofx(SEP, start="20260825", end="20260930"))
    assert res["novos"] == 1 and res["duplicados"] == 1
    assert n_tx(conn) == 4


def test_same_fitid_in_other_account_is_independent(conn, accounts):
    do_import(conn, accounts[0], "a.ofx", make_ofx(AUG))
    _, res = do_import(conn, accounts[1], "a.ofx", make_ofx(AUG, acct="999"))
    assert res["novos"] == 3


def test_no_fitid_dedup_and_flags(conn, accounts):
    # duas cobranças idênticas no mesmo dia, sem FITID: ambas entram, a 2ª fica sinalizada
    rows = [("20260812", "-15.00", "UBER TRIP", ""), ("20260812", "-15.00", "UBER TRIP", ""),
            ("20260814", "-1890.00", "GOOGLE ADS", "")]
    data = make_ofx(rows, fitid=False)
    plan, res = do_import(conn, accounts[1], "cs.ofx", data)
    assert [r.status for r in plan.rows] == [NOVO, POSSIVEL, NOVO]
    assert n_tx(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM dup_flags WHERE status='open'").fetchone()[0] == 1
    # reimportar o mesmo arquivo: nada novo, nenhuma sinalização nova
    plan2, res2 = do_import(conn, accounts[1], "cs.ofx", data)
    assert [r.status for r in plan2.rows] == [DUPLICADO] * 3
    assert n_tx(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM dup_flags").fetchone()[0] == 1


def test_no_fitid_overlap_with_partial_day_flags_instead_of_duplicating(conn, accounts):
    do_import(conn, accounts[1], "a.ofx", make_ofx([("20260812", "-15.00", "UBER", "")], fitid=False))
    # outro arquivo, mesmo dia, agora com duas ocorrências: a 1ª casa com a existente, a 2ª é nova mas sinalizada
    plan, _ = do_import(conn, accounts[1], "b.ofx",
                        make_ofx([("20260812", "-15.00", "UBER", ""), ("20260812", "-15.00", "UBER", "")], fitid=False))
    assert [r.status for r in plan.rows] == [DUPLICADO, POSSIVEL]
    assert n_tx(conn) == 2


def test_account_mismatch_detected(conn, accounts):
    do_import(conn, accounts[0], "a.ofx", make_ofx(AUG, acct="111"))
    plan = plan_import(conn, accounts[0], "b.ofx", make_ofx(SEP, acct="222"))
    assert plan.account_mismatch


def test_original_data_is_immutable(conn, accounts):
    do_import(conn, accounts[0], "a.ofx", make_ofx(AUG))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE transactions SET amount_cents=1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM transactions")
