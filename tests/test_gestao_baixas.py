"""Contas a receber/pagar e baixas: o previsto nunca cria movimento bancário, só dá baixa em saldo em aberto."""
from datetime import date

import pytest

from caixa import baixas, obrigacoes as ob
from caixa.importer import commit_import, plan_import
from caixa.ledger import LedgerError, account_summary, classify, link_transfer
from tests.helpers import make_ofx

TODAY = date(2026, 10, 20)


def bank(conn, account_id, rows, **kw):
    """rows: (YYYYMMDD, valor, descrição, fitid)."""
    commit_import(conn, plan_import(conn, account_id, "x.ofx", make_ofx(rows, **kw)))


def tx(conn, fitid):
    return conn.execute("SELECT id FROM transactions WHERE fitid=?", (fitid,)).fetchone()[0]


def inst_of(conn, oid):
    return ob.list_installments(conn, TODAY, obligation_id=oid)


def bessie(conn, pag, **kw):
    args = dict(direction="receber", description="Mensalidade Bessie", counterparty="Bessie Beauty Club", amount_cents=300_000,
                due_date="2026-10-15", expected_account_id=pag, category="Serviços")
    args.update(kw)
    return ob.create_obligation(conn, **args)


def test_mensalidade_bessie_parcial_depois_total_sem_duplicar_caixa(conn, accounts):
    pag, _ = accounts
    oid = bessie(conn, pag)
    (p,) = inst_of(conn, oid)
    assert (p["status"], p["amount_cents"], p["open_cents"], p["settled_cents"], p["overdue"]) == ("prevista", 300_000, 300_000, 0, True)

    bank(conn, pag, [("20261016", "1500.00", "PIX RECEBIDO BESSIE BEAUTY", "b1")])
    baixas.allocate(conn, p["id"], tx(conn, "b1"), 150_000)
    (p,) = inst_of(conn, oid)
    assert (p["status"], p["open_cents"], p["settled_cents"]) == ("parcialmente liquidada", 150_000, 150_000)

    bank(conn, pag, [("20261018", "1500.00", "PIX RECEBIDO BESSIE BEAUTY", "b2")])
    baixas.allocate(conn, p["id"], tx(conn, "b2"), 150_000)
    (p,) = inst_of(conn, oid)
    assert (p["status"], p["open_cents"], p["overdue"]) == ("liquidada", 0, False)

    # os dois créditos entram no caixa uma vez cada: 3.000 no total, não 6.000 (previsto não duplica)
    assert account_summary(conn, pag)["entradas_cents"] == 300_000
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_nao_aloca_alem_do_lancamento_nem_alem_do_saldo(conn, accounts):
    pag, _ = accounts
    p = inst_of(conn, bessie(conn, pag))[0]
    bank(conn, pag, [("20261016", "1000.00", "PIX BESSIE", "b1")])
    t = tx(conn, "b1")
    with pytest.raises(LedgerError, match="passar do valor do lançamento"):
        baixas.allocate(conn, p["id"], t, 100_001)
    baixas.allocate(conn, p["id"], t, 60_000)
    with pytest.raises(LedgerError, match="passar do valor do lançamento"):
        baixas.allocate(conn, p["id"], t, 40_001)                       # sobraram só 400,00 do lançamento
    assert baixas.tx_available_cents(conn, t) == 40_000
    # saldo da parcela: 3.000 − 600 = 2.400; um crédito de 3.000 não pode quitar além disso sem ajuste explícito
    bank(conn, pag, [("20261017", "3000.00", "PIX BESSIE", "b2")])
    with pytest.raises(LedgerError, match="saldo em aberto"):
        baixas.allocate(conn, p["id"], tx(conn, "b2"), 300_000)
    baixas.allocate(conn, p["id"], tx(conn, "b2"), 300_000, allow_excess=True)     # ação explícita
    q = ob.get_installment(conn, p["id"], TODAY)
    assert q["status"] == "liquidada" and q["open_cents"] == -60_000                 # excedente visível, não escondido


def test_um_pagamento_para_varias_parcelas_e_varios_pagamentos_para_uma(conn, accounts):
    pag, _ = accounts
    oid = ob.create_obligation(conn, direction="receber", description="Curso", counterparty="Cliente Beta", amount_cents=300_000,
                               due_date="2026-10-10", installments=3, expected_account_id=pag)
    p1, p2, p3 = inst_of(conn, oid)
    assert (p1["amount_cents"], p2["amount_cents"], p3["amount_cents"]) == (100_000,) * 3
    assert [p["competence"] for p in (p1, p2, p3)] == ["2026-10", "2026-11", "2026-12"]
    bank(conn, pag, [("20261011", "2500.00", "PIX CLIENTE BETA", "t1")])
    # um crédito de 2.500 distribuído: parcela 1 inteira, parcela 2 inteira e metade da 3 (tudo ou nada)
    baixas.allocate_many(conn, [(p1["id"], tx(conn, "t1"), 100_000), (p2["id"], tx(conn, "t1"), 100_000),
                                (p3["id"], tx(conn, "t1"), 50_000)])
    st = [p["status"] for p in inst_of(conn, oid)]
    assert st == ["liquidada", "liquidada", "parcialmente liquidada"] and baixas.tx_available_cents(conn, tx(conn, "t1")) == 0
    # tudo-ou-nada: a 2ª alocação falha → a 1ª não pode ficar gravada
    bank(conn, pag, [("20261012", "100.00", "PIX CLIENTE BETA", "t2")])
    with pytest.raises(LedgerError):
        baixas.allocate_many(conn, [(p3["id"], tx(conn, "t2"), 5_000), (p3["id"], tx(conn, "t2"), 6_000)])
    assert baixas.tx_available_cents(conn, tx(conn, "t2")) == 10_000
    # vários lançamentos para uma parcela
    bank(conn, pag, [("20261013", "250.00", "PIX CLIENTE BETA", "t3"), ("20261014", "250.00", "PIX CLIENTE BETA", "t4")])
    baixas.allocate_many(conn, [(p3["id"], tx(conn, "t3"), 25_000), (p3["id"], tx(conn, "t4"), 25_000)])
    assert inst_of(conn, oid)[2]["status"] == "liquidada"


def test_desfazer_baixa_nao_apaga_nada_e_libera_o_lancamento(conn, accounts):
    pag, _ = accounts
    p = inst_of(conn, bessie(conn, pag))[0]
    bank(conn, pag, [("20261016", "3000.00", "PIX BESSIE", "b1")])
    a = baixas.allocate(conn, p["id"], tx(conn, "b1"), 300_000)
    assert ob.get_installment(conn, p["id"], TODAY)["status"] == "liquidada"
    baixas.undo(conn, a, "vínculo errado")
    q = ob.get_installment(conn, p["id"], TODAY)
    assert (q["status"], q["open_cents"]) == ("prevista", 300_000)
    assert baixas.tx_available_cents(conn, tx(conn, "b1")) == 300_000
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1           # lançamento original intacto
    assert conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 1            # o registro continua (com undone_at)
    with pytest.raises(LedgerError, match="já foi desfeita"):
        baixas.undo(conn, a)
    acts = [r["action"] for r in conn.execute("SELECT action FROM gestao_log ORDER BY id")]
    assert acts == ["criar", "baixar", "desfazer"]                                        # histórico auditável


def test_taxa_desconto_diferenca_sao_ajustes_separados(conn, accounts):
    pag, _ = accounts
    p = inst_of(conn, bessie(conn, pag))[0]
    bank(conn, pag, [("20261016", "2910.00", "PIX BESSIE", "b1")])          # chegou 2.910 de 3.000
    baixas.allocate(conn, p["id"], tx(conn, "b1"), 291_000)
    baixas.adjust(conn, p["id"], "taxa", 6_000, "taxa da maquininha")
    baixas.adjust(conn, p["id"], "desconto", 2_000, "desconto combinado")
    q = ob.get_installment(conn, p["id"], TODAY)
    assert (q["status"], q["open_cents"]) == ("parcialmente liquidada", 1_000)
    with pytest.raises(LedgerError, match="justificativa"):
        baixas.adjust(conn, p["id"], "diferenca", 1_000)                     # ajuste final precisa de motivo
    baixas.adjust(conn, p["id"], "diferenca", 1_000, "arredondamento aceito")
    assert ob.get_installment(conn, p["id"], TODAY)["status"] == "liquidada"
    # ajustes não criam caixa: só o crédito bancário de 2.910 existe
    assert account_summary(conn, pag)["entradas_cents"] == 291_000
    with pytest.raises(LedgerError, match="saldo em aberto"):
        baixas.adjust(conn, p["id"], "desconto", 1, "x")                     # nada em aberto
    pay = ob.create_obligation(conn, direction="pagar", description="Fornecedor", counterparty="Forn", amount_cents=100_000,
                               due_date="2026-10-20", category="Software")
    pid = inst_of(conn, pay)[0]["id"]
    with pytest.raises(LedgerError, match="a receber"):
        baixas.adjust(conn, pid, "taxa", 500, "x")                           # taxa retida só no recebimento


def test_estorno_reabre_o_saldo_e_nao_passa_do_que_foi_pago(conn, accounts):
    pag, _ = accounts
    p = inst_of(conn, bessie(conn, pag))[0]
    bank(conn, pag, [("20261016", "3000.00", "PIX BESSIE", "b1"), ("20261020", "-500.00", "ESTORNO PIX BESSIE", "e1"),
                     ("20261021", "-4000.00", "OUTRA SAIDA", "e2")])
    baixas.allocate(conn, p["id"], tx(conn, "b1"), 300_000)
    with pytest.raises(LedgerError, match="estorno passa"):
        baixas.allocate(conn, p["id"], tx(conn, "e2"), 400_000, kind="estorno")    # só 3.000 foram pagos
    baixas.allocate(conn, p["id"], tx(conn, "e1"), 50_000, kind="estorno")
    q = ob.get_installment(conn, p["id"], TODAY)
    assert (q["status"], q["open_cents"]) == ("parcialmente liquidada", 50_000)
    with pytest.raises(LedgerError, match="estorno que depende"):
        baixas.undo(conn, conn.execute("SELECT id FROM allocations WHERE kind='pagamento'").fetchone()[0])


def test_sentido_e_movimentos_internos_sao_barrados(conn, accounts):
    pag, cs = accounts
    p = inst_of(conn, bessie(conn, pag))[0]
    bank(conn, pag, [("20261016", "-3000.00", "PIX ENVIADO", "s1")])
    with pytest.raises(LedgerError, match="sentido"):
        baixas.allocate(conn, p["id"], tx(conn, "s1"), 300_000)               # saída não quita a receber
    bank(conn, pag, [("20261017", "3000.00", "TED RECEBIDA", "i1")])
    bank(conn, cs, [("20261017", "-3000.00", "TED ENVIADA", "o1")], acct="999")
    link_transfer(conn, tx(conn, "o1"), tx(conn, "i1"))
    with pytest.raises(LedgerError, match="movimentos internos"):
        baixas.allocate(conn, p["id"], tx(conn, "i1"), 300_000)               # transferência não é cliente pagando


def test_parcelamento_nao_perde_centavos_e_recorrencia_gera_meses(conn, accounts):
    assert sum(ob.split_cents(100_000, 3)) == 100_000 and ob.split_cents(100_000, 3) == [33_334, 33_333, 33_333]
    oid = ob.create_obligation(conn, direction="pagar", description="Notebook", counterparty="Loja", amount_cents=100_000,
                               due_date="2026-10-31", installments=3, category="Equipamentos")
    ps = inst_of(conn, oid)
    assert [p["amount_cents"] for p in ps] == [33_334, 33_333, 33_333]
    assert [p["due_date"] for p in ps] == ["2026-10-31", "2026-11-30", "2026-12-31"]       # dia 31 ajustado ao fim do mês
    rid = ob.create_obligation(conn, direction="pagar", description="Software", counterparty="Fornecedor", amount_cents=19_900,
                               due_date="2026-10-05", recurrence={"months": 4, "day": 5}, competence_offset_months=0)
    rs = inst_of(conn, rid)
    assert [p["due_date"] for p in rs] == ["2026-10-05", "2026-11-05", "2026-12-05", "2027-01-05"]
    assert [p["competence"] for p in rs] == ["2026-10", "2026-11", "2026-12", "2027-01"]
    assert len(ob.extend_recurrence(conn, rid, 2)) == 2 and len(inst_of(conn, rid)) == 6


def test_editar_serie_so_atinge_parcelas_futuras_sem_baixa(conn, accounts):
    pag, _ = accounts
    oid = ob.create_obligation(conn, direction="receber", description="Mensalidade Bessie", counterparty="Bessie",
                               amount_cents=300_000, due_date="2026-09-15", recurrence={"months": 5, "day": 15},
                               expected_account_id=pag)
    ps = inst_of(conn, oid)                              # set, out, nov, dez, jan
    bank(conn, pag, [("20260916", "3000.00", "PIX BESSIE", "m1"), ("20261016", "1000.00", "PIX BESSIE", "m2")])
    baixas.allocate(conn, ps[0]["id"], tx(conn, "m1"), 300_000)          # setembro liquidada
    baixas.allocate(conn, ps[1]["id"], tx(conn, "m2"), 100_000)          # outubro parcial
    res = ob.update_series(conn, oid, "2026-10-01", amount_cents=350_000, description="Mensalidade Bessie (reajuste)")
    assert res["skipped"] == [ps[1]["id"]]                                # parcial é preservada
    assert res["updated"] == [ps[2]["id"], ps[3]["id"], ps[4]["id"]]
    now = {p["number"]: p for p in inst_of(conn, oid)}
    assert now[1]["amount_cents"] == 300_000 and now[1]["description"] == "Mensalidade Bessie"       # liquidada intacta
    assert now[2]["amount_cents"] == 300_000                                                           # parcial intacta
    assert [now[n]["amount_cents"] for n in (3, 4, 5)] == [350_000] * 3
    with pytest.raises(LedgerError, match="liquidada"):
        ob.update_installment(conn, ps[0]["id"], amount_cents=1)
    with pytest.raises(LedgerError, match="menor que o já baixado"):
        ob.update_installment(conn, ps[1]["id"], amount_cents=50_000)
    with pytest.raises(LedgerError, match="baixas"):
        ob.cancel_installment(conn, ps[1]["id"])
    out = ob.cancel_obligation(conn, oid, from_due_date="2026-10-01")
    assert out["kept"] == [ps[1]["id"]] and len(out["cancelled"]) == 3


def test_sugestoes_explicam_e_so_confirmam_o_inequivoco(conn, accounts):
    pag, cs = accounts
    a = bessie(conn, pag)                                                        # Bessie 3.000 dia 15
    ob.create_obligation(conn, direction="receber", description="Consultoria", counterparty="Studio Aurora Ltda",
                         amount_cents=120_000, due_date="2026-10-18", expected_account_id=pag)
    bank(conn, pag, [("20261016", "3000.00", "PIX RECEBIDO BESSIE BEAUTY CLUB", "b1"),
                     ("20261019", "1200.00", "PIX RECEBIDO STUDIO AURORA", "a1"),
                     ("20261019", "777.00", "PIX RECEBIDO DESCONHECIDO", "x1")])
    s = baixas.suggest_for_tx(conn, tx(conn, "b1"), TODAY)
    assert len(s) == 1 and s[0]["installment"]["obligation_id"] == a and s[0]["exact"]
    assert any("contraparte" in r for r in s[0]["reasons"]) and any("valor igual" in r for r in s[0]["reasons"])
    assert baixas.suggest_for_tx(conn, tx(conn, "x1"), TODAY) == []                # nada compatível: não inventa
    done = baixas.confirm_unambiguous(conn, TODAY)
    assert len(done) == 2 and baixas.tx_available_cents(conn, tx(conn, "x1")) == 77_700

    # ambiguidade: duas parcelas iguais, mesma contraparte → NÃO vincula sozinho
    ob.create_obligation(conn, direction="receber", description="Mensalidade A", counterparty="Cliente Dobro", amount_cents=50_000,
                         due_date="2026-10-20")
    ob.create_obligation(conn, direction="receber", description="Mensalidade B", counterparty="Cliente Dobro", amount_cents=50_000,
                         due_date="2026-10-20")
    bank(conn, pag, [("20261020", "500.00", "PIX CLIENTE DOBRO", "d1")])
    s = baixas.suggest_for_tx(conn, tx(conn, "d1"), TODAY)
    assert len(s) == 2 and not baixas.is_unambiguous(s)
    assert baixas.confirm_unambiguous(conn, TODAY) == []
    assert baixas.tx_available_cents(conn, tx(conn, "d1")) == 50_000
