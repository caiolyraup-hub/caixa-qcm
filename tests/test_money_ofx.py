import pytest

from caixa.money import format_brl, parse_amount_to_cents
from caixa.ofx import OfxError, parse_ofx
from caixa.timeutil import parse_ofx_date
from tests.helpers import make_ofx


def test_money_is_exact_integer_cents():
    assert parse_amount_to_cents("0.10") + parse_amount_to_cents("0.20") == 30
    assert parse_amount_to_cents("-1.234,56") == -123456
    assert parse_amount_to_cents("1234.5") == 123450
    assert parse_amount_to_cents("R$ 10,05") == 1005
    with pytest.raises(ValueError):
        parse_amount_to_cents("1.005")
    assert format_brl(-123456) == "-R$ 1.234,56"


def test_ofx_dates_in_sao_paulo():
    assert parse_ofx_date("20260115") == "2026-01-15"
    assert parse_ofx_date("20260115120000[-3:BRT]") == "2026-01-15"
    # 01:30 GMT do dia 16 = 22:30 do dia 15 em São Paulo
    assert parse_ofx_date("20260116013000[0:GMT]") == "2026-01-15"
    # meia-noite com fuso é tratada como "só data"
    assert parse_ofx_date("20260115000000[0:GMT]") == "2026-01-15"


def test_parse_sgml_and_xml():
    txs = [("20260803", "4500.00", "PIX RECEBIDO Gráfica", "A1"), ("20260805", "-1200.50", "PIX ENVIADO", "A2")]
    for xml in (False, True):
        st = parse_ofx(make_ofx(txs, ledger=("100.00", "20260831"), xml=xml))
        assert [t.amount_cents for t in st.txs] == [450000, -120050]
        assert st.txs[0].description == "PIX RECEBIDO Gráfica"
        assert st.txs[0].fitid == "A1"
        assert (st.period_start, st.period_end) == ("2026-08-01", "2026-08-31")
        assert (st.ledger_balance_cents, st.ledger_balance_date) == (10000, "2026-08-31")
        assert st.acct_id == "123-4"


def test_not_ofx_rejected():
    with pytest.raises(OfxError):
        parse_ofx(b"data,valor\n1,2")
