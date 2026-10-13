"""Telas de gestão com dados: cadastro pelo formulário, reserva, sugestão de baixa e composição dos números."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from caixa import obrigacoes as ob
from caixa.db import connect
from caixa.demo import seed

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    c = connect()
    seed(c)
    c.close()


def label(items, text):
    return next(i for i in items if i.label == text)


def open_page(page):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["_autosync_done"] = True
    at.run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_inicio_mostra_seis_secoes_na_ordem_certa(demo_env):
    at = open_page("Início")
    titles = [s.value for s in at.subheader]
    assert [t[:2] for t in titles if t[0].isdigit()] == ["1.", "2.", "3.", "4.", "5.", "6."]
    assert "Caixa disponível agora" in titles[0] and "A receber e a pagar" in titles[1] and "Menor saldo" in titles[2]
    assert "reserva" in titles[3].lower() and "DRE" in titles[4] and "atenção" in titles[5]


def test_cadastrar_pelo_formulario_e_ver_no_inicio(demo_env):
    at = open_page("A receber e a pagar")
    label(at.text_input, "Cliente").set_value("Bessie Beauty Club")
    label(at.text_input, "Descrição (ex.: mensalidade)").set_value("Mensalidade Bessie")
    label(at.text_input, "Valor (R$)").set_value("3.000,00")
    label(at.selectbox, "Repetição").set_value("Todo mês")
    label(at.button, "Cadastrar").click().run()
    assert not at.exception, [e.value for e in at.exception]
    rows = ob.list_installments(connect())
    assert len(rows) == 12 and {p["amount_cents"] for p in rows} == {300_000} and rows[0]["direction"] == "receber"
    at.sidebar.radio[0].set_value("Início").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("A receber em 30 dias" == m.label for m in at.metric)


def test_reserva_minima_pelo_inicio_aparece_como_folga(demo_env):
    at = open_page("Início")
    label(at.text_input, "Defina sua reserva mínima de caixa (R$)").set_value("1.000,00")
    label(at.button, "Salvar reserva").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert connect().execute("SELECT value FROM settings WHERE key='reserva_minima_cents'").fetchone()[0] == "100000"
    at.sidebar.radio[0].set_value("Início").run()
    assert any(m.label == "Reserva mínima" and m.value == "R$ 1.000,00" for m in at.metric)
    assert any(m.label in ("Folga", "Falta") for m in at.metric)


def test_conferir_vincula_sugestao_sem_duplicar_caixa(demo_env):
    c = connect()
    pag = c.execute("SELECT id FROM accounts WHERE name='PagBank'").fetchone()[0]
    ob.create_obligation(c, direction="receber", description="Consultoria Aurora", counterparty="Studio Aurora Ltda",
                         amount_cents=520_000, due_date="2026-09-02", expected_account_id=pag, category="Serviços")
    from caixa.ledger import account_summary
    before = account_summary(c, pag)["calculated_cents"]
    at = open_page("A receber e a pagar")
    assert any("A conferir (" in t.label for t in at.tabs)
    btn = next(b for b in at.button if b.label == "Vincular")
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    c = connect()
    p = ob.list_installments(c)[0]
    assert p["status"] == "liquidada" and p["settled_cents"] == 520_000
    assert account_summary(c, pag)["calculated_cents"] == before                       # baixa não mexe no caixa
    assert c.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 1


def test_paginas_de_gestao_com_dados_nao_quebram(demo_env):
    c = connect()
    pag = c.execute("SELECT id FROM accounts WHERE name='PagBank'").fetchone()[0]
    ob.create_obligation(c, direction="pagar", description="Software", counterparty="Fornecedor", amount_cents=19_900,
                         due_date="2026-10-20", recurrence={"months": 6}, category="Ferramentas", expected_account_id=pag)
    ob.create_obligation(c, direction="receber", description="Lead", counterparty="Lead", amount_cents=500_000, due_date="2026-11-10",
                         origin="oportunidade", probability_pct=40)
    for page in ("Início", "A receber e a pagar", "Caixa projetado", "DRE do mês"):
        at = open_page(page)
        assert not at.exception, (page, [e.value for e in at.exception])
    at = open_page("Caixa projetado")
    label(at.checkbox, "Mostrar cenário com estimativas").check().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("NÃO é dinheiro garantido" in x.value for x in at.caption)
