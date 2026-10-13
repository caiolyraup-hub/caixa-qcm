"""Gera OFX fictício para os testes."""


def make_ofx(txs, *, acct="123-4", start="20260801", end="20260831", ledger=None, fitid=True, xml=False):
    """txs: lista de (data 'YYYYMMDD', valor 'str', descrição, fitid). ledger: (valor 'str', 'YYYYMMDD')."""
    lines = []
    for d, amt, desc, fid in txs:
        lines.append("<STMTTRN>")
        lines.append("<TRNTYPE>OTHER")
        lines.append(f"<DTPOSTED>{d}120000[-3:BRT]")
        lines.append(f"<TRNAMT>{amt}")
        if fitid and fid:
            lines.append(f"<FITID>{fid}")
        lines.append(f"<MEMO>{desc}")
        lines.append("</STMTTRN>")
    body = "\n".join(lines)
    bal = f"<LEDGERBAL><BALAMT>{ledger[0]}<DTASOF>{ledger[1]}</LEDGERBAL>" if ledger else ""
    head = "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nCHARSET:1252\n\n" if not xml else '<?xml version="1.0"?>\n'
    return (f"{head}<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><BANKID>290<ACCTID>{acct}</BANKACCTFROM>"
            f"<BANKTRANLIST><DTSTART>{start}<DTEND>{end}\n{body}\n</BANKTRANLIST>{bal}</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
            ).encode("cp1252")
