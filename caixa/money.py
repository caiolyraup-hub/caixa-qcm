"""Dinheiro sempre em centavos inteiros. Nunca float."""
from decimal import Decimal, InvalidOperation


def parse_amount_to_cents(text: str) -> int:
    """'1234.56', '-1.234,56', 'R$ 10,5' → centavos. Falha se houver mais de 2 casas decimais significativas."""
    s = str(text).strip().replace("R$", "").replace(" ", "").replace(" ", "")
    if not s:
        raise ValueError("valor vazio")
    if "," in s and "." in s:
        # o último separador é o decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"valor inválido: {text!r}") from exc
    if not d.is_finite():
        raise ValueError(f"valor inválido: {text!r}")
    cents = d * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"valor com mais de 2 casas decimais: {text!r}")
    return int(cents)


def format_brl(cents: int | None, *, sign: bool = False) -> str:
    """12345 → 'R$ 123,45'. Com sign=True mostra '+' em positivos."""
    if cents is None:
        return "—"
    neg = cents < 0
    n = abs(int(cents))
    reais, cent = divmod(n, 100)
    body = f"{reais:,}".replace(",", ".") + f",{cent:02d}"
    prefix = "-" if neg else ("+" if sign and cents > 0 else "")
    return f"{prefix}R$ {body}"


def cents_to_plain(cents: int) -> str:
    """Para CSV/planilha: -12345 → '-123,45'."""
    neg = cents < 0
    reais, cent = divmod(abs(int(cents)), 100)
    return f"{'-' if neg else ''}{reais},{cent:02d}"
