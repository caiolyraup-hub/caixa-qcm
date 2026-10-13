"""Caixa projetado, reserva, DRE por competência e tela inicial."""
from datetime import date

import pytest

from caixa import baixas, dre, obrigacoes as ob, posicao
from caixa.contasimples import CARD_ACCOUNT_NAME
from caixa.db import create_account
from caixa.importer import commit_import, plan_import
from caixa.inicio import inicio
from caixa.ledger import classify, link_transfer, set_investment_position
from caixa.projecao import project
from tests.helpers import make_ofx

TODAY = date(2026, 10, 20)


def bank(conn, acc, rows, **kw):
    commit_import(conn, plan_import(conn, acc, "x.ofx", make_ofx(rows, **kw)))


def tx(conn, fitid):
    return conn.execute("SELECT id FROM transactions WHERE fitid=?", (fitid,)).fetchone()[0]


@pytest.fixture
def base(conn, accounts):
    """PagBank com R$ 10.000 e Conta Simples com R$ 5.000 (saldos iniciais), extratos em dia até ontem (19/10)."""
    pag, cs = accounts
    bank(conn, pag, [("20261001", "0.01", "PIX TESTE A", "p0")], start="20261001", end="20261019")
    bank(conn, cs, [("20261001", "0.01", "PIX TESTE B", "c0")], start="20261001", end="20261019", acct="999")
    classify(conn, tx(conn, "p0"), "aporte")      # classificados, para não poluir "sem classificação" nos testes
    classify(conn, tx(conn, "c0"), "aporte")
    return pag, cs                       # saldos: PagBank 10.000,01 · Conta Simples 5.000,01


def new(conn, **kw):
    a = dict(direction="pagar", description="Obrigação", counterparty="Forn", amount_cents=100_000, due_date="2026-10-25",
             category="Software")
    a.update(kw)
    return ob.create_obligation(conn, **a)


# ------------------------------------------------------------------ caixa projetado

def test_linha_diaria_menor_saldo_e_reserva(conn, base):
    pag, cs = base
    new(conn, description="Aluguel", amount_cents=400_000, due_date="2026-10-22", expected_account_id=pag)
    new(conn, direction="receber", description="Mensalidade X", counterparty="Cliente X", amount_cents=250_000,
        due_date="2026-11-05", expected_account_id=pag)
    posicao.set_setting(conn, "reserva_minima_cents", "500000")
    pr = project(conn, TODAY, 90)
    first, d22, d5 = pr.rows[0], pr.rows[2], pr.rows[16]
    assert first["date"] == TODAY and first["closing"] == 1_000_001 + 500_001                    # 10.000,01 + 5.000,01
    assert (d22["receipts"], d22["payments"]) == (0, 400_000) and d22["closing"] == 1_500_002 - 400_000
    assert d22["opening"] == 1_500_002 and d22["date"] == date(2026, 10, 22)
    assert d5["receipts"] == 250_000 and d5["closing"] == 1_100_002 + 250_000
    assert pr.min[7] == {"cents": 1_100_002, "date": date(2026, 10, 22)} and pr.min[90]["date"] == date(2026, 10, 22)
    assert pr.slack[90] == 1_100_002 - 500_000                                                  # folga sobre a reserva
    assert pr.reserve_cents == 500_000
    assert [r["closing"] for r in pr.rows[:2]] == [1_500_002, 1_500_002]


def test_conta_individual_pode_ficar_negativa_mesmo_com_consolidado_positivo(conn, base):
    pag, cs = base
    new(conn, description="Fornecedor grande", amount_cents=1_200_000, due_date="2026-10-24", expected_account_id=pag)  # > 10.000,01
    pr = project(conn, TODAY, 30)
    assert pr.negative_dates == {pag: date(2026, 10, 24)}
    assert pr.min[30]["cents"] > 0                                                              # consolidado segue positivo
    assert cs not in pr.negative_dates


def test_parcela_liquidada_nao_entra_na_projecao_e_nao_duplica(conn, base):
    pag, _ = base
    oid = new(conn, direction="receber", description="Mensalidade Bessie", counterparty="Bessie", amount_cents=300_000,
              due_date="2026-10-15", expected_account_id=pag)
    pid = ob.list_installments(conn, TODAY, obligation_id=oid)[0]["id"]
    bank(conn, pag, [("20261018", "3000.00", "PIX BESSIE", "b1")], start="20261001", end="20261019")
    # vencida e em aberto: recebimento atrasado NÃO entra no caixa projetado (incerto) e aparece listado
    pr = project(conn, TODAY, 7)
    assert pr.rows[0]["closing"] == (1_000_001 + 300_000) + 500_001      # só o realizado (já inclui o crédito de 18/10)
    assert [i.installment_id for i in pr.overdue_receivables] == [pid]
    baixas.allocate(conn, pid, tx(conn, "b1"), 300_000)                  # dá baixa: nada fica pendente nem duplica
    pr2 = project(conn, TODAY, 7)
    assert pr2.rows[0]["closing"] == pr.rows[0]["closing"] and pr2.overdue_receivables == []
    assert max(r["receipts"] for r in pr2.rows) == 0


def test_pagamento_atrasado_sai_hoje_e_parcial_projeta_so_o_saldo_aberto(conn, base):
    pag, _ = base
    oid = new(conn, description="Ferramenta", amount_cents=100_000, due_date="2026-10-10", expected_account_id=pag)
    pid = ob.list_installments(conn, TODAY, obligation_id=oid)[0]["id"]
    pr = project(conn, TODAY, 7)
    assert pr.rows[0]["payments"] == 100_000 and [i.installment_id for i in pr.overdue_payables] == [pid]
    bank(conn, pag, [("20261012", "-400.00", "PAGTO FERRAMENTA", "f1")], start="20261001", end="20261019")
    baixas.allocate(conn, pid, tx(conn, "f1"), 40_000)                   # pagou 400 de 1.000 antes
    pr = project(conn, TODAY, 7)
    assert pr.rows[0]["payments"] == 60_000                              # só os 600 em aberto


def test_estimativa_e_oportunidade_ficam_fora_da_linha_principal(conn, base):
    pag, _ = base
    new(conn, direction="receber", description="Contrato novo", counterparty="Cliente Z", amount_cents=200_000,
        due_date="2026-10-28", origin="estimativa", expected_account_id=pag)
    new(conn, direction="receber", description="Lead quente", counterparty="Lead", amount_cents=400_000,
        due_date="2026-10-29", origin="oportunidade", probability_pct=50, expected_account_id=pag)
    new(conn, direction="receber", description="Lead frio", counterparty="Lead2", amount_cents=900_000,
        due_date="2026-10-30", origin="oportunidade", expected_account_id=pag)       # sem probabilidade
    pr = project(conn, TODAY, 30)
    assert max(r["receipts"] for r in pr.rows) == 0                                  # principal: nada disso entra
    sc = pr.scenario
    assert "NÃO é dinheiro garantido" in sc.scenario_label
    assert sum(r["receipts"] for r in sc.rows) == 200_000 + 200_000                  # estimativa + 50% da oportunidade
    assert any("sem probabilidade" in s["reason"] for s in sc.skipped)               # a sem probabilidade não entra


def test_extrato_desatualizado_gera_limitacao_e_usa_a_data_do_saldo(conn, accounts):
    pag, cs = accounts
    bank(conn, pag, [("20261005", "100.00", "PIX", "a")], start="20261001", end="20261015")    # extrato vai só até 15/10
    new(conn, description="Conta de luz", amount_cents=50_000, due_date="2026-10-18", expected_account_id=pag)  # entre 15/10 e hoje
    pr = project(conn, TODAY, 7)
    assert pr.accounts[pag]["asof"] == date(2026, 10, 15)
    assert any("15/10/2026" in m and "dias atrás" in m for m in pr.limitations)
    assert any("nenhum extrato" in m for m in pr.limitations)                                    # Conta Simples sem extrato
    # a conta de 18/10 pode já ter sido paga e ainda não aparecer no extrato: é aplicada na própria data
    assert pr.rows[0]["opening"] == 1_010_000 + 500_000 - 50_000


def test_cdi_conta_no_caixa_e_pode_ser_desligado(conn, base):
    pag, _ = base
    set_investment_position(conn, pag, "2026-10-19", 596_456)
    on = project(conn, TODAY, 7)
    posicao.set_setting(conn, "contar_cdi", "0")
    off = project(conn, TODAY, 7)
    assert on.rows[0]["closing"] - off.rows[0]["closing"] == 596_456
    assert posicao.account_position(conn, pag, TODAY)["total_cents"] == 1_000_001 + 596_456


def test_cartao_pending_nao_reduz_duas_vezes_e_compra_no_credito_nao_sai_do_caixa(conn, base):
    pag, cs = base
    card = create_account(conn, CARD_ACCOUNT_NAME, 0, "2026-10-01")
    from caixa.ledger import set_pending_balance
    set_pending_balance(conn, card, -41_363, "2026-10-19")               # compras pendentes já reduzem o limite
    bank(conn, card, [("20261001", "1000.00", "LIMITE", "l1")], start="20261001", end="20261019", acct="777")
    assert posicao.account_position(conn, card, TODAY)["available_cents"] == 100_000 - 41_363
    # compra no cartão de crédito (sem limite): DRE agora, caixa só na fatura
    new(conn, description="Anúncios no cartão", amount_cents=80_000, due_date="2026-10-22", nature="compra_cartao",
        expected_account_id=pag)
    # compra no cartão da Conta Simples com data passada: já está no saldo/limite
    new(conn, description="Compra já feita", amount_cents=30_000, due_date="2026-10-19", nature="compra_cartao",
        expected_account_id=card)
    new(conn, description="Fatura cartão", amount_cents=80_000, due_date="2026-10-25", nature="fatura_cartao", expected_account_id=pag)
    pr = project(conn, TODAY, 10)
    reasons = " | ".join(s["reason"] for s in pr.skipped)
    assert "só sai no pagamento da fatura" in reasons and "já considera compras lançadas e pendentes" in reasons
    assert sum(r["payments"] for r in pr.rows) == 80_000                          # só a fatura, uma vez


# ------------------------------------------------------------------ DRE

def test_competencia_difere_do_mes_de_recebimento(conn, base):
    pag, _ = base
    oid = new(conn, direction="receber", description="Serviço de outubro", counterparty="Cliente Y", amount_cents=500_000,
              due_date="2026-11-05", competence="2026-10", expected_account_id=pag, category="Serviços")
    pid = ob.list_installments(conn, TODAY, obligation_id=oid)[0]["id"]
    bank(conn, pag, [("20261106", "5000.00", "PIX CLIENTE Y", "y1")], start="20261001", end="20261130")
    baixas.allocate(conn, pid, tx(conn, "y1"), 500_000)
    out = dre.dre_month(conn, "2026-10", today=date(2026, 11, 30))
    nov = dre.dre_month(conn, "2026-11", today=date(2026, 11, 30))
    assert out["lines"]["receita_servicos"]["previsto"] == 500_000 and out["lines"]["receita_servicos"]["realizado"] == 500_000
    assert nov["lines"]["receita_servicos"]["previsto"] == 0                         # novembro: recebeu, mas a receita é de outubro
    from caixa.ledger import account_summary
    assert account_summary(conn, pag)["entradas_cents"] == 500_000 + 1                # o caixa recebeu uma vez só (+1 do saldo de teste)


def test_previsto_x_realizado_e_subtotais(conn, base):
    pag, _ = base
    oid = new(conn, direction="receber", description="Mensalidade", counterparty="Cli", amount_cents=300_000,
              due_date="2026-10-15", expected_account_id=pag, category="Serviços")
    pid = ob.list_installments(conn, TODAY, obligation_id=oid)[0]["id"]
    new(conn, description="Produção de vídeo", amount_cents=60_000, due_date="2026-10-20", category="Freelancers")
    new(conn, description="Software", amount_cents=20_000, due_date="2026-10-21", category="Ferramentas")
    new(conn, description="DAS", amount_cents=30_000, due_date="2026-10-20", nature="imposto", category="Simples")
    dre.set_category_line(conn, "Freelancers", "custos")
    dre.set_category_line(conn, "Ferramentas", "desp_administrativas")
    bank(conn, pag, [("20261016", "1800.00", "PIX CLI", "r1")], start="20261001", end="20261019")
    baixas.allocate(conn, pid, tx(conn, "r1"), 180_000)
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    p, r = d["subtotals"]["previsto"], d["subtotals"]["realizado"]
    assert d["lines"]["receita_servicos"]["previsto"] == 300_000 and d["lines"]["receita_servicos"]["realizado"] == 180_000
    assert p["receita_liquida"] == 300_000 - 30_000 and p["margem_bruta"] == 270_000 - 60_000
    assert p["resultado_operacional"] == 210_000 - 20_000 and p["resultado"] == 190_000
    assert p["margem"] == pytest.approx(190_000 / 270_000)
    assert r["resultado"] == 180_000                                                  # só o que já foi baixado
    items = d["lines"]["receita_servicos"]["items"]
    assert items[0]["source"] == "parcela" and items[0]["id"] == pid                 # dá para ver o que compõe a linha
    assert d["complete"] is True, (d["pending"], d["unclassified"], d["presumed_competence"])


def test_taxa_e_desconto_viram_linhas_proprias_e_o_resultado_bate_com_o_caixa(conn, base):
    pag, _ = base
    oid = new(conn, direction="receber", description="Mensalidade", counterparty="Cli", amount_cents=300_000,
              due_date="2026-10-15", expected_account_id=pag, category="Serviços")
    pid = ob.list_installments(conn, TODAY, obligation_id=oid)[0]["id"]
    bank(conn, pag, [("20261016", "2910.00", "PIX CLI", "r1")], start="20261001", end="20261019")
    baixas.allocate(conn, pid, tx(conn, "r1"), 291_000)
    baixas.adjust(conn, pid, "taxa", 6_000, "maquininha")
    baixas.adjust(conn, pid, "desconto", 3_000, "combinado")
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    assert d["lines"]["receita_servicos"]["realizado"] == 300_000
    assert d["lines"]["deducoes"]["realizado"] == -3_000 and d["lines"]["fin_despesa"]["realizado"] == -6_000
    assert d["subtotals"]["realizado"]["resultado"] == 291_000                        # = dinheiro que entrou no banco


def test_historico_classificado_entra_com_competencia_e_nao_soma_duas_vezes(conn, base):
    pag, _ = base
    bank(conn, pag, [("20261016", "3000.00", "PIX BESSIE", "h1"), ("20261017", "-200.00", "PAGTO ANUNCIO", "h2"),
                     ("20261018", "-9000.00", "DISTRIBUICAO", "h3"), ("20261018", "5000.00", "APORTE SOCIO", "h4"),
                     ("20261019", "-50.00", "ALGO SEM CLASSIFICAR", "h5")], start="20261001", end="20261019")
    classify(conn, tx(conn, "h1"), "receita", counterparty="Bessie")
    classify(conn, tx(conn, "h2"), "despesa", category="Tráfego pago")
    classify(conn, tx(conn, "h3"), "retirada", category="Distribuição de lucros")
    classify(conn, tx(conn, "h4"), "aporte")
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    assert d["lines"]["receita_servicos"]["realizado"] == 300_000
    assert [p["description"] for p in d["pending"]] == ["PAGTO ANUNCIO"]               # categoria sem linha: pendente explícito, não inventado
    assert {o["description"] for o in d["outside"]} >= {"DISTRIBUICAO", "APORTE SOCIO"}   # fora da DRE operacional
    assert d["unclassified"] == {"count": 1, "cents": -5_000}
    assert d["presumed_competence"]["count"] == 1 and d["complete"] is False
    dre.set_category_line(conn, "Tráfego pago", "desp_comerciais")
    dre.set_tx_competence(conn, tx(conn, "h1"), "2026-09")                              # revisão manual: era receita de setembro
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    assert d["lines"]["receita_servicos"]["realizado"] == 0
    assert d["lines"]["desp_comerciais"]["realizado"] == -20_000
    assert dre.dre_month(conn, "2026-09", today=TODAY)["lines"]["receita_servicos"]["realizado"] == 300_000
    # depois de ligado a uma obrigação, o lançamento não é somado de novo
    oid = new(conn, direction="receber", description="Mensalidade Bessie", counterparty="Bessie", amount_cents=300_000,
              due_date="2026-10-15", competence="2026-10", category="Serviços")
    dre.set_tx_competence(conn, tx(conn, "h1"), "2026-10")
    pid = ob.list_installments(conn, TODAY, obligation_id=oid)[0]["id"]
    baixas.allocate(conn, pid, tx(conn, "h1"), 300_000)
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    assert d["lines"]["receita_servicos"]["previsto"] == 300_000 and d["lines"]["receita_servicos"]["realizado"] == 300_000


def test_transferencias_aplicacoes_emprestimo_e_prolabore_fora_ou_dentro_da_dre(conn, base):
    pag, cs = base
    bank(conn, pag, [("20261010", "-2000.00", "TED PARA CS", "t1"), ("20261010", "-800.00", "RENDA FIXA APLICACAO CDB", "t2")],
         start="20261001", end="20261019")
    bank(conn, cs, [("20261010", "2000.00", "TED RECEBIDA", "t3")], start="20261001", end="20261019", acct="999")
    link_transfer(conn, tx(conn, "t1"), tx(conn, "t3"))
    for nat, cat in (("prolabore", "Pró-labore"), ("distribuicao_lucros", "Dist"), ("emprestimo_juros", "Banco"),
                     ("emprestimo_principal", "Banco")):
        new(conn, description=f"Teste {nat}", amount_cents=100_000, due_date="2026-10-20", nature=nat, category=cat)
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    assert d["lines"]["desp_administrativas"]["previsto"] == -100_000                  # pró-labore é despesa
    assert d["lines"]["fin_despesa"]["previsto"] == -100_000                           # só os juros
    assert {o["description"] for o in d["outside"]} >= {"Teste distribuicao_lucros", "Teste emprestimo_principal",
                                                        "TED PARA CS", "TED RECEBIDA", "RENDA FIXA APLICACAO CDB"}
    assert d["subtotals"]["previsto"]["resultado"] == -200_000


def test_compra_no_cartao_conta_na_dre_e_a_fatura_nao(conn, base):
    pag, _ = base
    new(conn, description="Anúncios", amount_cents=80_000, due_date="2026-10-10", nature="compra_cartao", category="Tráfego pago")
    new(conn, description="Pagamento da fatura", amount_cents=80_000, due_date="2026-10-25", nature="fatura_cartao",
        expected_account_id=pag)
    dre.set_category_line(conn, "Tráfego pago", "desp_comerciais")
    d = dre.dre_month(conn, "2026-10", today=TODAY)
    assert d["lines"]["desp_comerciais"]["previsto"] == -80_000
    assert d["subtotals"]["previsto"]["resultado"] == -80_000                         # a despesa aparece uma vez só
    assert "Pagamento da fatura" in [o["description"] for o in d["outside"]]


def test_filtro_por_cliente(conn, base):
    new(conn, direction="receber", description="A", counterparty="Cliente A", amount_cents=100_000, due_date="2026-10-10")
    new(conn, direction="receber", description="B", counterparty="Cliente B", amount_cents=200_000, due_date="2026-10-10")
    assert dre.dre_month(conn, "2026-10", today=TODAY)["lines"]["receita_servicos"]["previsto"] == 300_000
    assert dre.dre_month(conn, "2026-10", client="cliente b", today=TODAY)["lines"]["receita_servicos"]["previsto"] == 200_000


# ------------------------------------------------------------------ tela inicial

def test_inicio_responde_as_seis_perguntas_com_composicao(conn, base):
    pag, _ = base
    posicao.set_setting(conn, "reserva_minima_cents", "100000")
    new(conn, direction="receber", description="Mensalidade", counterparty="Cli", amount_cents=300_000, due_date="2026-10-22",
        expected_account_id=pag)
    new(conn, description="Aluguel", amount_cents=200_000, due_date="2026-10-28", expected_account_id=pag)
    new(conn, description="Atrasada", amount_cents=10_000, due_date="2026-10-10", expected_account_id=pag)
    h = inicio(conn, TODAY)
    assert h["caixa"]["total_cents"] == 1_500_002 and len(h["caixa"]["accounts"]) == 2
    assert sum(p["open_cents"] for p in h["obrigacoes"]["receber"][7]) == 300_000
    assert sum(p["open_cents"] for p in h["obrigacoes"]["pagar"][7]) == 0
    assert sum(p["open_cents"] for p in h["obrigacoes"]["pagar"][30]) == 200_000
    assert [p["description"] for p in h["obrigacoes"]["vencidos_pagar"]] == ["Atrasada"]
    assert h["projecao"]["min90"]["cents"] == 1_500_002 - 10_000                       # a vencida sai hoje
    assert h["projecao"]["reserve_defined"] and h["dre_month"] == "2026-10"
    keys = [a["key"] for a in h["atencao"]]
    assert "vencidas" in keys
    assert keys.index("extratos") < keys.index("vencidas") if "extratos" in keys else True
