import pytest

from caixa.db import connect, create_account


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIXA_QCM_DATA", str(tmp_path))
    c = connect()
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    pag = create_account(conn, "PagBank", 1_000_000, "2026-08-01")      # R$ 10.000,00
    cs = create_account(conn, "Conta Simples", 500_000, "2026-08-01")   # R$ 5.000,00
    return pag, cs
