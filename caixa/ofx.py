"""Parser OFX tolerante (SGML 1.x e XML 2.x), sem dependências externas."""
import html
import re
from dataclasses import dataclass, field

from .money import parse_amount_to_cents
from .timeutil import parse_ofx_date


class OfxError(ValueError):
    pass


@dataclass
class OfxTx:
    fitid: str
    posted_date: str  # ISO
    description: str
    amount_cents: int
    trntype: str = ""


@dataclass
class OfxStatement:
    acct_id: str = ""
    bank_id: str = ""
    period_start: str | None = None
    period_end: str | None = None
    ledger_balance_cents: int | None = None
    ledger_balance_date: str | None = None
    txs: list[OfxTx] = field(default_factory=list)


_TAG = re.compile(r"<([A-Za-z0-9_.]+)>([^<\r\n]*)")


def _fields(chunk: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _TAG.finditer(chunk):
        out.setdefault(m.group(1).upper(), html.unescape(m.group(2).strip()))
    return out


def _decode(data: bytes) -> str:
    header = data[:800].decode("ascii", errors="ignore").upper()
    if "CHARSET:1252" in header or "ISO-8859-1" in header or "LATIN" in header:
        return data.decode("cp1252", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_ofx(data: bytes) -> OfxStatement:
    text = _decode(data)
    if not re.search(r"<OFX>", text, re.I) or not re.search(r"<STMTTRN>", text, re.I):
        raise OfxError("Arquivo não parece um extrato OFX (sem <OFX> ou <STMTTRN>).")

    st = OfxStatement()
    head = re.split(r"<STMTTRN>", text, maxsplit=1, flags=re.I)[0]
    hf = _fields(head)
    st.acct_id = hf.get("ACCTID", "")
    st.bank_id = hf.get("BANKID", "")
    try:
        if hf.get("DTSTART"):
            st.period_start = parse_ofx_date(hf["DTSTART"])
        if hf.get("DTEND"):
            st.period_end = parse_ofx_date(hf["DTEND"])
    except ValueError as exc:
        raise OfxError(str(exc)) from exc

    for i, part in enumerate(re.split(r"<STMTTRN>", text, flags=re.I)[1:], start=1):
        chunk = re.split(r"</STMTTRN>|</BANKTRANLIST>", part, maxsplit=1, flags=re.I)[0]
        f = _fields(chunk)
        try:
            if "DTPOSTED" not in f or "TRNAMT" not in f:
                raise ValueError("faltam DTPOSTED ou TRNAMT")
            name, memo = _clean(f.get("NAME", "")), _clean(f.get("MEMO", ""))
            if name and memo and memo.upper() not in name.upper() and name.upper() not in memo.upper():
                desc = f"{name} - {memo}"
            else:
                desc = memo if len(memo) >= len(name) else name
            st.txs.append(OfxTx(
                fitid=f.get("FITID", "").strip(),
                posted_date=parse_ofx_date(f["DTPOSTED"]),
                description=desc or f.get("TRNTYPE", "") or "(sem descrição)",
                amount_cents=parse_amount_to_cents(f["TRNAMT"]),
                trntype=f.get("TRNTYPE", ""),
            ))
        except ValueError as exc:
            raise OfxError(f"Lançamento #{i} inválido: {exc}") from exc

    m = re.search(r"<LEDGERBAL>", text, re.I)
    if m:
        bf = _fields(text[m.end(): m.end() + 400])
        try:
            if "BALAMT" in bf and "DTASOF" in bf:
                st.ledger_balance_cents = parse_amount_to_cents(bf["BALAMT"])
                st.ledger_balance_date = parse_ofx_date(bf["DTASOF"])
        except ValueError as exc:
            raise OfxError(f"Saldo do extrato inválido: {exc}") from exc

    if st.txs:
        dates = [t.posted_date for t in st.txs]
        st.period_start = st.period_start or min(dates)
        st.period_end = st.period_end or max(dates)
    return st
