"""Cria uma base de demonstração (use com CAIXA_QCM_DATA=data_demo). Uso: python -m caixa.demo"""
from pathlib import Path

from .db import connect, create_account, list_accounts
from .importer import commit_import, plan_import
from .ledger import classify, link_transfer

DEMO = Path(__file__).resolve().parent.parent / "demo"


def _tx(conn, account, desc, date):
    return conn.execute(
        "SELECT t.id FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.name=? AND t.description LIKE ? AND t.posted_date=?", (account, f"%{desc}%", date)).fetchone()[0]


def seed(conn) -> bool:
    if list_accounts(conn):
        return False
    pag = create_account(conn, "PagBank", 1_000_000, "2026-08-01")
    cs = create_account(conn, "Conta Simples", 500_000, "2026-08-01")
    for acc, name in [(pag, "pagbank_2026-08.ofx"), (pag, "pagbank_2026-09.ofx"),
                      (cs, "contasimples_2026-08.ofx"), (cs, "contasimples_2026-09.ofx")]:
        commit_import(conn, plan_import(conn, acc, name, (DEMO / name).read_bytes()))

    # Classifica parte dos lançamentos; o resto fica em "A identificar" para o usuário praticar.
    classify(conn, _tx(conn, "PagBank", "Studio Aurora", "2026-08-03"), "receita", counterparty="Studio Aurora", category="Serviços")
    classify(conn, _tx(conn, "PagBank", "Clínica Vida", "2026-08-12"), "receita", counterparty="Clínica Vida", category="Serviços")
    classify(conn, _tx(conn, "PagBank", "Gráfica Rápida", "2026-08-05"), "despesa", counterparty="Gráfica Rápida", category="Materiais", cost_center="Operação")
    classify(conn, _tx(conn, "PagBank", "TARIFA", "2026-08-07"), "tarifa", counterparty="PagBank")
    classify(conn, _tx(conn, "PagBank", "PRO-LABORE", "2026-08-15"), "retirada", counterparty="Sócio", category="Pró-labore")
    classify(conn, _tx(conn, "PagBank", "DARF", "2026-08-20"), "imposto", counterparty="Receita Federal", category="Simples Nacional")
    classify(conn, _tx(conn, "Conta Simples", "GOOGLE ADS", "2026-08-14"), "despesa", counterparty="Google", category="Mídia paga", cost_center="Marketing")
    link_transfer(conn, _tx(conn, "PagBank", "TED ENVIADA", "2026-08-10"), _tx(conn, "Conta Simples", "TRANSFERENCIA", "2026-08-10"))
    # Transferência de setembro: só a saída classificada → aparece como "sem par" na investigação
    classify(conn, _tx(conn, "PagBank", "TED ENVIADA", "2026-09-10"), "transferencia")
    return True


def seed_gestao(conn) -> bool:
    """Contas a receber/pagar de exemplo (datas relativas a hoje), para ver a tela Início preenchida."""
    from datetime import timedelta

    from . import obrigacoes as ob
    from .timeutil import today_sp
    if conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]:
        return False
    pag = conn.execute("SELECT id FROM accounts WHERE name='PagBank'").fetchone()[0]
    cs = conn.execute("SELECT id FROM accounts WHERE name='Conta Simples'").fetchone()[0]
    t = today_sp()
    iso = lambda n: (t + timedelta(days=n)).isoformat()  # noqa: E731
    ob.create_obligation(conn, direction="receber", description="Mensalidade (exemplo)", counterparty="Cliente Exemplo A",
                         amount_cents=300_000, due_date=iso(5), recurrence={"months": 6}, expected_account_id=pag, category="Serviços")
    ob.create_obligation(conn, direction="receber", description="Projeto único (exemplo)", counterparty="Cliente Exemplo B",
                         amount_cents=450_000, due_date=iso(-3), expected_account_id=pag, category="Serviços")
    ob.create_obligation(conn, direction="receber", description="Possível contrato (exemplo)", counterparty="Lead Exemplo",
                         amount_cents=800_000, due_date=iso(20), origin="oportunidade", probability_pct=30, expected_account_id=pag)
    ob.create_obligation(conn, direction="pagar", description="Aluguel (exemplo)", counterparty="Locador Exemplo",
                         amount_cents=250_000, due_date=iso(9), recurrence={"months": 6}, expected_account_id=pag, category="Aluguel")
    ob.create_obligation(conn, direction="pagar", description="Ferramentas (exemplo)", counterparty="Software Exemplo",
                         amount_cents=29_900, due_date=iso(2), recurrence={"months": 6}, expected_account_id=cs, category="Ferramentas")
    return True


if __name__ == "__main__":
    c = connect()
    print("Base de demonstração criada." if seed(c) else "Base já existe; nada a fazer.")
    seed_gestao(c)
