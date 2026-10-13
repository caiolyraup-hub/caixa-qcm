from datetime import date, datetime
from decimal import Decimal

import pytest

from caixa import connections, recon
from caixa.contasimples import (BANK_ACCOUNT_NAME, CARD_ACCOUNT_NAME, ContaSimplesClient, ContaSimplesError,
                                banking_sign, map_banking, map_card, sync_contasimples)
from caixa.db import create_account
from caixa.ledger import account_summary, link_transfer
from caixa.rotina import daily_status
from caixa.timeutil import SP


def btx(id_, sub, desc, amount, day, status=2, name="Fulano"):
    return {"id": id_, "transactionType": {"id": 1, "description": desc, "subType": sub}, "status": status,
            "accountId": 1, "transactionDate": f"{day}T15:00:00.000Z", "brlAmount": Decimal(amount),
            "sourceDestinationName": name}


def ctx(id_, op, type_, merchant, amount, day, status="PROCESSED"):
    return {"id": id_, "operation": op, "type": type_, "merchant": merchant, "amountBrl": Decimal(amount),
            "transactionDate": f"{day}T15:00:00.000Z", "status": status, "isCanceled": False, "card": {}}


class FakeApi:
    """Transporte falso: serve OAuth, extrato (2 páginas), cartões e saldo. Guarda as chamadas."""

    def __init__(self, banking, cards, balance="0"):
        self.banking, self.cards, self.balance = banking, cards, balance
        self.calls, self.fail = [], []

    def __call__(self, method, url, headers, params, data):
        self.calls.append((method, url, dict(params or {})))
        if self.fail:
            return self.fail.pop(0)
        if url.endswith("/oauth/v1/access-token"):
            assert headers["Authorization"].startswith("Basic ")
            return 200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 1800}
        assert headers["Authorization"] == "Bearer tok" and headers["User-Agent"]
        if url.endswith("/accounts/v1/balance"):
            return 200, {"amount": Decimal(self.balance)}
        pool = self.banking if "banking" in url else self.cards
        start, end = params["startDate"], params["endDate"]
        rows = [r for r in pool if start <= r["transactionDate"][:10] <= end]
        if "nextPageStartKey" in params:
            return 200, {"transactions": rows[1:]}
        return 200, {"transactions": rows[:1], **({"nextPageStartKey": "k2"} if len(rows) > 1 else {})}


@pytest.fixture
def cs(conn):
    return create_account(conn, BANK_ACCOUNT_NAME, 500_000, "2026-08-01")


def client(api):
    return ContaSimplesClient("key", "secret", "production", transport=api, sleep=lambda s: None)


BANK = [
    btx(1, "PIX_IN", "PIX RECEBIDO", "1000.00", "2026-08-05"),
    btx(2, "PIX_OUT", "PIX ENVIADO", "250.50", "2026-08-06"),
    btx(3, "TED_OUT", "TED ENVIADA", "3000.00", "2026-08-10"),
    btx(4, "PIX_OUT", "PIX ENVIADO", "99.00", "2026-08-11", status=3),     # pendente: não entra
    btx(5, "PIX_OUT", "PIX ENVIADO", "10.00", "2026-08-11", status=1),     # cancelado: não entra
    btx(6, "WEIRD", "MOVIMENTO ESTRANHO", "5.00", "2026-08-12"),           # direção desconhecida: não entra
]
CARDS = [
    ctx("C1", "CASH_OUT", "PURCHASE", "UBER *TRIP", "45.90", "2026-08-12"),
    ctx("C2", "CASH_OUT", "PURCHASE", "GOOGLE ADS", "1890.00", "2026-08-14"),
    ctx("C3", "CASH_IN", "INVOICE_PAYMENT", "PAGAMENTO FATURA", "1935.90", "2026-08-20"),
    ctx("C4", "CASH_OUT", "BALANCE_INQUIRY", "CONSULTA", "0.00", "2026-08-21"),
    ctx("C5", "CASH_IN", "LIMIT_CREDIT", "", "500.00", "2026-08-22"),     # carga de limite = caixa levado ao cartão
]
TODAY = date(2026, 8, 25)
# saldo esperado: 5000 + 1000 - 250,50 - 3000 = 2749,50
SALDO = "2749.50"


def test_sign_rules():
    assert banking_sign(btx(1, "PIX_OUT", "x", "1", "2026-01-01")) == -1
    assert banking_sign(btx(1, "PIX_IN", "x", "1", "2026-01-01")) == 1
    assert banking_sign(btx(1, "", "PIX ENVIADO", "1", "2026-01-01")) == -1
    assert banking_sign(btx(1, "", "DEPÓSITO RECEBIDO", "1", "2026-01-01")) == 1
    assert banking_sign(btx(1, "", "ESTORNO DE PAGAMENTO", "1", "2026-01-01")) is None  # ambíguo: não chuta
    assert banking_sign(btx(1, "X", "???", "1", "2026-01-01")) is None


def test_mapping_dates_in_sao_paulo_and_exact_cents():
    tx, _ = map_banking({**BANK[1], "transactionDate": "2026-08-07T01:30:00.000Z", "brlAmount": Decimal("0.10")})
    assert tx.posted_date == "2026-08-06" and tx.amount_cents == -10 and tx.fitid == "CS2"
    tx, why = map_card(CARDS[2])
    assert tx.amount_cents == 193590 and tx.fitid == "CCC3"


def test_sync_imports_banking_and_cards_and_matches_api_balance(conn, cs):
    api = FakeApi(BANK, CARDS, SALDO)
    r = sync_contasimples(conn, client(api), today=TODAY)
    assert r.novos == 3 + 4                                    # 3 do banco + 3 de cartão (consulta de saldo ignorada)
    assert r.descartados == {"pendente": 1, "cancelado": 1, "sem_direcao": 1, "nao_monetario": 1}
    assert len(r.sem_direcao) == 1 and "MOVIMENTO ESTRANHO" in r.sem_direcao[0]
    assert account_summary(conn, cs)["calculated_cents"] == 274_950
    assert r.saldo_api_cents == 274_950 and r.divergencia_cents == 0
    card_acc = conn.execute("SELECT * FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()
    assert card_acc["kind"] == "conta"
    assert account_summary(conn, card_acc["id"])["calculated_cents"] == -4_590 - 189_000 + 193_590   # C5 (sem par) ignorado
    assert r.liquidacoes_ignoradas == 1 and r.limites_pareados == 0
    assert any(c[2].get("nextPageStartKey") == "k2" for c in api.calls)          # paginou
    run = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert run["status"] == "ok" and run["n_new"] == 7


def test_sync_is_idempotent_and_uses_overlap_window(conn, cs):
    api = FakeApi(BANK, CARDS, SALDO)
    sync_contasimples(conn, client(api), today=TODAY)
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    api.calls.clear()
    r = sync_contasimples(conn, client(api), today=date(2026, 8, 26))
    assert r.novos == 0 and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n
    starts = {c[2]["startDate"] for c in api.calls if "banking" in c[1]}
    assert starts == {"2026-08-18"}                                              # fim anterior (25/08) - 7 dias


def test_late_transaction_in_overlap_is_picked_up(conn, cs):
    api = FakeApi(BANK, CARDS, SALDO)
    sync_contasimples(conn, client(api), today=TODAY)
    api.banking = BANK + [btx(7, "PIX_IN", "PIX RECEBIDO", "40.00", "2026-08-24")]
    r = sync_contasimples(conn, client(api), today=date(2026, 8, 26))
    assert r.novos == 1


def test_balance_divergence_is_reported(conn, cs):
    r = sync_contasimples(conn, client(FakeApi(BANK, CARDS, "2839.40")), today=TODAY)   # API diz 89,90 a mais
    assert r.divergencia_cents == -8_990


def test_invoice_payment_links_to_card_account_as_transfer(conn, cs):
    bank = BANK + [btx(8, "BILL_OUT", "PAGAMENTO DE FATURA", "1935.90", "2026-08-20")]
    sync_contasimples(conn, client(FakeApi(bank, CARDS, "813.60")), today=TODAY)
    out_id = conn.execute("SELECT id FROM transactions WHERE fitid='CS8'").fetchone()[0]
    in_id = conn.execute("SELECT id FROM transactions WHERE fitid='CCC3'").fetchone()[0]
    link_transfer(conn, out_id, in_id)
    assert recon.transfer_pairs(conn)[0]["diff_cents"] == 0


def test_requires_bank_account(conn):
    with pytest.raises(ContaSimplesError, match="Cadastre"):
        sync_contasimples(conn, client(FakeApi([], [])), today=TODAY)
    assert conn.execute("SELECT status FROM sync_runs").fetchone()[0] == "erro"


def test_http_errors_and_retries(conn, cs):
    api = FakeApi(BANK, CARDS, SALDO)
    api.fail = [(200, {"access_token": "tok", "expires_in": 1800}), (429, {}), (503, {})]   # token ok; depois 429, 503
    assert sync_contasimples(conn, client(api), today=TODAY).novos == 7                       # recuperou sozinho
    bad = FakeApi([], [])
    bad.fail = [(401, {"message": "invalid_client"})]
    with pytest.raises(ContaSimplesError, match="autenticar"):
        client(bad).balance_cents()
    sig = FakeApi([], [])
    sig.fail = [(200, {"access_token": "tok", "expires_in": 1800}), (401, {}), (200, {"access_token": "tok", "expires_in": 1800}), (401, {})]
    with pytest.raises(ContaSimplesError, match="assinatura"):
        client(sig).balance_cents()


def test_daily_status_ofx_and_api(conn, cs, accounts_pag=None):
    pag = create_account(conn, "PagBank", 0, "2026-08-01")
    today, now = date(2026, 8, 25), datetime(2026, 8, 25, 9, 0, tzinfo=SP)
    st = {i["account"]: i for i in daily_status(conn, today, now)}
    assert not st["PagBank"]["ok"] and "nenhum extrato" in st["PagBank"]["detail"]
    from tests.helpers import make_ofx
    from caixa.importer import commit_import, plan_import
    commit_import(conn, plan_import(conn, pag, "a.ofx", make_ofx([("20260820", "1.00", "X", "F1")], start="20260801", end="20260822")))
    st = {i["account"]: i for i in daily_status(conn, today, now)}
    assert not st["PagBank"]["ok"] and "22/08/2026" in st["PagBank"]["detail"] and "24/08/2026" in st["PagBank"]["detail"]
    commit_import(conn, plan_import(conn, pag, "b.ofx", make_ofx([("20260824", "1.00", "Y", "F2")], start="20260823", end="20260824")))
    assert {i["account"]: i for i in daily_status(conn, today, now)}["PagBank"]["ok"]

    connections.set_contasimples("k", "s", "production")
    st = {i["account"]: i for i in daily_status(conn, today, now)}
    assert st[BANK_ACCOUNT_NAME]["mode"] == "api" and not st[BANK_ACCOUNT_NAME]["ok"]
    sync_contasimples(conn, client(FakeApi([], [], "5000.00")), today=today)
    st = {i["account"]: i for i in daily_status(conn, today, datetime.now(SP))}
    assert st[BANK_ACCOUNT_NAME]["ok"] and CARD_ACCOUNT_NAME not in st


def test_first_api_sync_starts_after_existing_ofx_to_avoid_duplicates(conn, cs):
    from caixa.importer import commit_import, plan_import
    from tests.helpers import make_ofx
    commit_import(conn, plan_import(conn, cs, "cs.ofx", make_ofx(
        [("20260805", "1000.00", "PIX RECEBIDO", "")], fitid=False, start="20260801", end="20260810")))
    api = FakeApi(BANK, CARDS, SALDO)
    sync_contasimples(conn, client(api), today=TODAY)
    assert {c[2]["startDate"] for c in api.calls if "banking" in c[1]} == {"2026-08-11"}
    # a movimentação de 05/08 existe uma vez só (OFX); a API trouxe só o que veio depois
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=? AND posted_date<='2026-08-10'", (cs,)).fetchone()[0] == 1


def test_sandbox_never_syncs_into_the_real_database(conn, cs):
    sandbox = ContaSimplesClient("k", "s", "sandbox", transport=FakeApi(BANK, CARDS, SALDO), sleep=lambda s: None)
    with pytest.raises(ContaSimplesError, match="Sandbox"):
        sync_contasimples(conn, sandbox, today=TODAY)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    # testar conexão (somente leitura de saldo) segue funcionando no sandbox
    assert ContaSimplesClient("k", "s", "sandbox", transport=FakeApi([], [], "2.50")).balance_cents() == 250


def test_card_limit_load_links_to_bank_outflow_as_transfer(conn, cs):
    bank = BANK + [btx(9, "SECURED_CARD_DEPOSIT_OUT", "Transferência de limite", "500.00", "2026-08-22")]
    r = sync_contasimples(conn, client(FakeApi(bank, CARDS, "2249.50")), today=TODAY)
    assert r.limites_pareados == 1 and r.liquidacoes_ignoradas == 0        # pareou sozinho, sem ação manual
    card_id = conn.execute("SELECT id FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()[0]
    bank_cents = account_summary(conn, cs)["calculated_cents"]
    card_cents = account_summary(conn, card_id)["calculated_cents"]
    assert bank_cents == 224_950                                   # saiu 500 do saldo da conta
    assert card_cents == -4_590 - 189_000 + 193_590 + 50_000       # e entrou 500 no cartão (caixa)
    from caixa.ledger import company_breakdown
    assert company_breakdown(conn)["transferencias_cents"] == 0    # a carga não é receita nem despesa


def test_migration_converts_old_card_accounts_to_cash(tmp_path, monkeypatch):
    from caixa.db import connect
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    c = connect()
    c.execute("INSERT INTO accounts (name, opening_balance_cents, opening_date, created_at, kind) "
              "VALUES ('X',0,'2026-01-01','t','cartao')")
    c.commit()
    c.close()
    assert connect().execute("SELECT kind FROM accounts WHERE name='X'").fetchone()[0] == "conta"


def test_card_account_follows_bank_opening_date(conn, cs):
    sync_contasimples(conn, client(FakeApi(BANK, CARDS, SALDO)), today=TODAY)
    from caixa.db import update_account_opening
    update_account_opening(conn, cs, 500_000, "2026-07-01")
    sync_contasimples(conn, client(FakeApi(BANK, CARDS, SALDO)), today=TODAY)
    assert conn.execute("SELECT opening_date FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()[0] == "2026-07-01"


def test_moving_opening_date_back_backfills_history(conn, cs):
    from caixa.db import update_account_opening
    api = FakeApi(BANK, CARDS, SALDO)
    sync_contasimples(conn, client(api), today=date(2026, 8, 12))      # conta nova: só pega até 12/08
    n0 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    update_account_opening(conn, cs, 0, "2026-07-20")
    api.calls.clear()
    sync_contasimples(conn, client(api), today=date(2026, 8, 12))
    assert {c[2]["startDate"] for c in api.calls if "banking" in c[1]} == {"2026-07-20"}
    api.calls.clear()
    sync_contasimples(conn, client(api), today=date(2026, 8, 13))      # depois do backfill volta ao incremental (−7 dias)
    assert {c[2]["startDate"] for c in api.calls if "banking" in c[1]} == {"2026-08-05"}
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n0   # nada duplicado


# ------------------------------------------------------------------ cartões com limite garantido por depósito

def test_bill_payments_are_not_new_money_and_deposits_pair_with_bank(conn, cs):
    """Espelha o caso real: 2 depósitos de limite saem da conta; 2 'limites' no cartão são pagamentos de fatura."""
    bank = [btx(1, "SECURED_CARD_DEPOSIT_OUT", "Transferência de limite", "1000.00", "2026-08-01"),
            btx(2, "SECURED_CARD_DEPOSIT_OUT", "Transferência de limite", "300.00", "2026-08-10")]
    cards = [ctx("L1", "CASH_IN", "LIMIT", "", "1000.00", "2026-08-01"),      # carga (tem saída na conta)
             ctx("L2", "CASH_IN", "LIMIT", "", "300.00", "2026-08-10"),
             ctx("P1", "CASH_OUT", "PURCHASE", "FACEBK *A", "400.00", "2026-08-02"),
             ctx("P2", "CASH_OUT", "PURCHASE", "FACEBK *B", "250.00", "2026-08-11"),
             ctx("B1", "CASH_IN", "LIMIT", "", "400.00", "2026-08-07")]       # = fatura de 400 paga (sem saída na conta)
    r = sync_contasimples(conn, client(FakeApi(bank, cards, "3700.00")), today=TODAY)
    assert (r.limites_pareados, r.liquidacoes_ignoradas) == (2, 1)
    card_id = conn.execute("SELECT id FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()[0]
    # limite disponível = depósitos (1300) − compras (650); o pagamento da fatura (400) não soma
    assert account_summary(conn, card_id)["calculated_cents"] == 130_000 - 65_000
    assert [p["diff_cents"] for p in recon.transfer_pairs(conn)] == [0, 0]
    from caixa.ledger import company_breakdown, list_ignored
    assert [r["reason"][:20] for r in list_ignored(conn)] == ["Pagamento de fatura "]
    assert company_breakdown(conn)["transferencias_cents"] == 0
    # reexecutar não muda nada
    r2 = sync_contasimples(conn, client(FakeApi(bank, cards, "3700.00")), today=TODAY)
    assert (r2.novos, r2.limites_pareados, r2.liquidacoes_ignoradas) == (0, 0, 0)
    assert account_summary(conn, card_id)["calculated_cents"] == 65_000


def test_recent_unmatched_limit_is_not_ignored_yet(conn, cs):
    """A saída na conta pode demorar a aparecer: só ignora 'limite' sem par depois de 3 dias."""
    cards = [ctx("L9", "CASH_IN", "LIMIT", "", "200.00", "2026-08-24")]
    r = sync_contasimples(conn, client(FakeApi([], cards, "5000.00")), today=TODAY)
    assert r.liquidacoes_ignoradas == 0
    card_id = conn.execute("SELECT id FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()[0]
    assert account_summary(conn, card_id)["calculated_cents"] == 20_000
    # a saída chega depois: agora pareia
    bank = [btx(5, "SECURED_CARD_DEPOSIT_OUT", "Transferência de limite", "200.00", "2026-08-24")]
    assert sync_contasimples(conn, client(FakeApi(bank, cards, "4800.00")), today=TODAY).limites_pareados == 1


def test_old_wrong_card_rows_are_cleaned_by_reconcile(conn, cs):
    """Linhas importadas por versão anterior (todas as LIMIT somadas) são corrigidas sem apagar nada."""
    from caixa.contasimples import ensure_accounts, reconcile_card_limits
    from caixa.importer import commit_import, plan_statement
    from caixa.ofx import OfxStatement, OfxTx
    bank, card = ensure_accounts(conn)
    for acc, tx in ((bank, OfxTx("CS1", "2026-08-01", "Transferência de limite", -100_000, "SECURED_CARD_DEPOSIT_OUT")),
                    (card, OfxTx("CCL1", "2026-08-01", "Limite carregado", 100_000, "LIMIT")),
                    (card, OfxTx("CCB1", "2026-08-07", "Limite carregado", 40_000, "LIMIT"))):
        st = OfxStatement(period_start="2026-08-01", period_end="2026-08-07", txs=[tx])
        commit_import(conn, plan_statement(conn, acc["id"], "API Conta Simples antigo.json", b"{}", st))
    assert account_summary(conn, card["id"])["calculated_cents"] == 140_000                 # errado (soma a fatura)
    assert reconcile_card_limits(conn, bank, card, TODAY) == (1, 1, 0)
    assert account_summary(conn, card["id"])["calculated_cents"] == 100_000                 # certo
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3             # nada foi apagado


def test_pending_card_purchases_reduce_available_limit(conn, cs):
    bank = [btx(1, "SECURED_CARD_DEPOSIT_OUT", "Transferência de limite", "1000.00", "2026-08-01")]
    cards = [ctx("L1", "CASH_IN", "LIMIT", "", "1000.00", "2026-08-01"),
             ctx("P1", "CASH_OUT", "PURCHASE", "FACEBK *A", "300.00", "2026-08-02"),
             ctx("P2", "CASH_OUT", "PURCHASE", "FACEBK *B", "120.50", "2026-08-24", status="PENDING")]
    r = sync_contasimples(conn, client(FakeApi(bank, cards, "4000.00")), today=TODAY)
    card_id = conn.execute("SELECT id FROM accounts WHERE name=?", (CARD_ACCOUNT_NAME,)).fetchone()[0]
    s = account_summary(conn, card_id)
    assert r.pendentes_cartao_cents == -12_050
    assert s["calculated_cents"] == 70_000 and s["pending_cents"] == -12_050 and s["caixa_total_cents"] == 57_950
    # a compra liquida: entra no extrato e some dos pendentes (nada é contado duas vezes)
    cards[2] = ctx("P2", "CASH_OUT", "PURCHASE", "FACEBK *B", "120.50", "2026-08-24", status="PROCESSED")
    sync_contasimples(conn, client(FakeApi(bank, cards, "4000.00")), today=TODAY)
    s = account_summary(conn, card_id)
    assert s["pending_cents"] == 0 and s["caixa_total_cents"] == 57_950


def test_bank_yield_is_income_and_unsettled_rows_are_skipped(conn, cs):
    bank = [{"id": 70, "transactionType": {"id": 9, "description": "Rentabilidade CDI", "subType": None}, "status": 2,
             "transactionDate": "2026-08-10T03:00:00.000Z", "brlAmount": Decimal("0.05")},
            {"id": 71, "transactionType": {"id": 9, "description": "Rentabilidade CDI", "subType": None}, "status": 0,
             "transactionDate": "2026-08-11T03:00:00.000Z", "brlAmount": Decimal("0.07")}]
    r = sync_contasimples(conn, client(FakeApi(bank, [], "5000.05")), today=TODAY)
    assert r.novos == 1 and r.descartados == {"nao_liquidado": 1}
    row = conn.execute("SELECT t.amount_cents, c.kind, c.category FROM transactions t JOIN classifications c ON c.tx_id=t.id").fetchone()
    assert tuple(row) == (5, "receita", "Rendimento financeiro")


def test_ignored_tx_stays_stored_blocks_classification_and_can_be_restored(conn, cs):
    from caixa.ledger import classify, ignore_tx, unignore_tx
    sync_contasimples(conn, client(FakeApi(BANK, CARDS, SALDO)), today=TODAY)
    tid = conn.execute("SELECT id FROM transactions WHERE fitid='CS1'").fetchone()[0]
    before = account_summary(conn, cs)["calculated_cents"]
    ignore_tx(conn, tid, "teste")
    assert account_summary(conn, cs)["calculated_cents"] == before - 100_000
    with pytest.raises(Exception):
        classify(conn, tid, "receita")
    unignore_tx(conn, tid)
    assert account_summary(conn, cs)["calculated_cents"] == before


def test_full_resync_refetches_from_opening_date(conn, cs):
    api = FakeApi(BANK, CARDS, SALDO)
    sync_contasimples(conn, client(api), today=TODAY)
    api.calls.clear()
    sync_contasimples(conn, client(api), today=date(2026, 8, 26), full=True)
    assert {c[2]["startDate"] for c in api.calls if "banking" in c[1]} == {"2026-08-01"}


def test_user_classification_blocks_pairing_until_forced(conn, cs):
    """Se você já classificou as pontas (ex.: Ajuste), o app avisa e NÃO sobrescreve; com force converte (histórico fica)."""
    from caixa.contasimples import ensure_accounts, reconcile_card_limits
    from caixa.ledger import classify, get_classification
    bank = [btx(1, "SECURED_CARD_DEPOSIT_OUT", "Transferência de limite", "300.00", "2026-08-10")]
    cards = [ctx("L1", "CASH_IN", "LIMIT", "", "300.00", "2026-08-10")]
    sync_contasimples(conn, client(FakeApi(bank, cards, "4700.00")), today=TODAY)
    dep = conn.execute("SELECT id FROM transactions WHERE fitid='CS1'").fetchone()[0]
    lim = conn.execute("SELECT id FROM transactions WHERE fitid='CCL1'").fetchone()[0]
    from caixa.ledger import clear_classification, unlink_transfer
    unlink_transfer(conn, conn.execute("SELECT id FROM transfer_links").fetchone()[0])      # volta ao estado "sem par"
    classify(conn, dep, "ajuste")
    classify(conn, lim, "investimento")
    b, c = ensure_accounts(conn)
    assert reconcile_card_limits(conn, b, c, TODAY) == (0, 0, 1)                            # bloqueado, nada alterado
    assert get_classification(conn, dep)["kind"] == "ajuste"
    r2 = sync_contasimples(conn, client(FakeApi(bank, cards, "4700.00")), today=date(2026, 8, 26))
    assert r2.limites_bloqueados == 1
    assert reconcile_card_limits(conn, b, c, TODAY, force=True) == (1, 0, 0)
    assert get_classification(conn, dep)["kind"] == "transferencia"
    assert conn.execute("SELECT COUNT(*) FROM classification_log WHERE tx_id=? AND action='remover'", (dep,)).fetchone()[0] == 1
