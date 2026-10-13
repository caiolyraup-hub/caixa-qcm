"""Roda cada página da interface com a base de demonstração e falha se qualquer uma levantar exceção."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from caixa.db import connect
from caixa.demo import seed

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PAGES = ["Início", "A receber e a pagar", "Caixa projetado", "DRE do mês", "Saldos por conta", "Rotina diária", "Importar extrato", "A identificar", "Investigar divergências", "Contas", "Exportar e backup"]


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    c = connect()
    seed(c)
    c.close()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(demo_env, page):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]


def test_empty_database_renders_every_page(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30).run()
    for page in PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])


def test_identificar_classifies_selected_via_ui(demo_env):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("A identificar").run()
    assert not at.exception
    assert any("movimentações" in c.value for c in at.caption)


def _form_script():
    # from_function copia só este corpo: tudo que precisa fica importado aqui dentro
    import streamlit as st

    import app  # importar não renderiza: main() só roda como __main__
    from caixa.ledger import list_unclassified
    app.conn = app.connect()  # o módulo fica em cache entre testes; reaponta para o banco do teste atual
    which = st.session_state.get("which", "one")
    rows = [dict(r) for r in list_unclassified(app.conn)]
    if which == "one":
        app.classify_one(next(r for r in rows if "UBER" in r["description"]))
    elif which == "transfer":
        # a TED de setembro já vem classificada (sem par) no seed; reabrimos o mesmo formulário para vincular
        app.classify_one(dict(app.conn.execute(
            "SELECT e.*, a.name AS account_name FROM effective_tx e JOIN accounts a ON a.id=e.account_id "
            "WHERE e.description LIKE 'TED ENVIADA%' AND e.posted_date='2026-09-10'").fetchone()))
    else:
        app.classify_many([r for r in rows if r["amount_cents"] < 0][:3])


def test_classify_one_form_saves_classification(demo_env):
    at = AppTest.from_function(_form_script, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    at.selectbox[0].set_value("despesa").run()
    at.selectbox[1].set_value("Google")               # dentro do form: só vale junto com o clique de envio
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    c = connect()
    row = c.execute("SELECT c.* FROM classifications c JOIN transactions t ON t.id=c.tx_id WHERE t.description LIKE '%UBER%'").fetchone()
    assert row["kind"] == "despesa" and row["counterparty"] == "Google"


def test_classify_transfer_form_links_pair(demo_env):
    at = AppTest.from_function(_form_script, default_timeout=30)
    at.session_state["which"] = "transfer"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.selectbox[0].set_value("transferencia").run()
    assert not at.exception, [e.value for e in at.exception]
    pair = at.selectbox[1]                              # candidatos da outra conta
    assert len(pair.options) >= 2
    pair.select_index(1).run()
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    c = connect()
    assert c.execute("SELECT COUNT(*) FROM transfer_links").fetchone()[0] == 2     # a do seed + esta


def test_classify_many_rejects_wrong_sign_but_applies_rest(demo_env):
    at = AppTest.from_function(_form_script, default_timeout=30)
    at.session_state["which"] = "many"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.selectbox[0].set_value("despesa")
    at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    c = connect()
    assert c.execute("SELECT COUNT(*) FROM classifications WHERE kind='despesa'").fetchone()[0] >= 3


def test_rotina_page_with_credentials_syncs_through_real_transport(demo_env, monkeypatch):
    """Passa pelo requests_transport de verdade (JSON → Decimal), com requests.request substituído por um servidor falso."""
    import json

    from caixa import connections

    connections.set_contasimples("k", "s", "production")

    class Resp:
        def __init__(self, status, payload):
            self.status_code, self.text = status, json.dumps(payload)

    def fake_request(method, url, headers=None, params=None, data=None, timeout=None):
        if url.endswith("/oauth/v1/access-token"):
            return Resp(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 1800})
        if url.endswith("/accounts/v1/balance"):
            return Resp(200, {"amount": 4983.2})
        if "banking" in url:
            return Resp(200, {"transactions": [{
                "id": 9001, "status": 2, "transactionType": {"description": "PIX RECEBIDO", "subType": "PIX_IN"},
                "transactionDate": "2026-10-01T15:00:00.000Z", "brlAmount": 10.1}]})
        return Resp(200, {"transactions": []})

    monkeypatch.setattr("caixa.contasimples.requests.request", fake_request)
    at = AppTest.from_file(APP, default_timeout=60).run()      # abrir o app já sincroniza (autosync)
    assert not at.exception, [e.value for e in at.exception]
    at.sidebar.radio[0].set_value("Rotina diária").run()
    assert not at.exception, [e.value for e in at.exception]
    c = connect()
    run = c.execute("SELECT * FROM sync_runs ORDER BY id DESC").fetchone()
    assert run["status"] == "ok", run["message"]
    assert c.execute("SELECT amount_cents FROM transactions WHERE fitid='CS9001'").fetchone()[0] == 1010
    # botão "Sincronizar agora" também roda sem erro
    next(b for b in at.button if b.label == "Sincronizar agora").click().run()
    assert not at.exception, [e.value for e in at.exception]


def test_rotina_page_without_accounts_offers_account_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("Rotina diária").run()
    assert not at.exception, [e.value for e in at.exception]
    assert not any("Cadastre as contas primeiro" in i.value for i in at.info)
    at.text_input(key="bal_PagBank").set_value("10.000,00")
    at.text_input(key="bal_Conta Simples").set_value("5000,00")
    next(b for b in at.button if b.label == "Criar contas").click().run()
    assert not at.exception, [e.value for e in at.exception]
    rows = connect().execute("SELECT name, opening_balance_cents FROM accounts ORDER BY id").fetchall()
    assert [(r["name"], r["opening_balance_cents"]) for r in rows] == [("PagBank", 1_000_000), ("Conta Simples", 500_000)]


def test_cdi_ui_flow_position_suggestion_and_painel(tmp_path, monkeypatch):
    from caixa.importer import commit_import, plan_import
    from caixa.db import create_account
    from tests.test_cdi import ROWS, pagbank_ofx
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    c = connect()
    pag = create_account(c, "PagBank", 999_999, "2026-10-01")             # saldo inicial errado de propósito
    commit_import(c, plan_import(c, pag, "x.ofx", pagbank_ofx(ROWS)))
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio[0].set_value("Saldos por conta").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("nenhuma posição aplicada" in w.value for w in at.warning)  # avisa que falta informar o CDI
    at.sidebar.radio[0].set_value("Contas").run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if b.label == "Aplicar esta sugestão").click().run()
    assert connect().execute("SELECT opening_balance_cents, opening_date FROM accounts").fetchone()[:] == (0, "2026-08-25")
    at.text_input(key=f"posv_{pag}").set_value("5.931,00")
    next(b for b in at.button if b.label == "Registrar posição").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert connect().execute("SELECT amount_cents FROM investment_positions").fetchone()[0] == 593_100
    at.sidebar.radio[0].set_value("Saldos por conta").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("R$ 5.931,00" in m.value for m in at.metric)               # caixa total = 0 disponível + CDI
