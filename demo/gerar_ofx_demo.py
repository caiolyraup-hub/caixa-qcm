"""Gera os OFX fictícios de demonstração (dados 100% inventados). Uso: python demo/gerar_ofx_demo.py

Cenário (saldo inicial PagBank R$ 10.000,00 e Conta Simples R$ 5.000,00, ambos em 01/08/2026):
- PagBank (SGML, com FITID): agosto e setembro, com período sobreposto (25–31/08) para testar reimportação.
- Conta Simples (XML, SEM FITID): agosto correto; setembro com saldo do extrato R$ 89,90 acima do calculado
  (os lançamentos têm uma saída a mais do que o banco considerou) e começando em 03/09 (lacuna em 01–02/09).
- Duas transferências PagBank → Conta Simples; a de setembro chega R$ 10,00 menor (tarifa).
- Duas cobranças idênticas de Uber no mesmo dia (legítimas) para exercitar o alerta de possível duplicata.
"""
from decimal import Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parent

PAG_OPEN = 1_000_000
CS_OPEN = 500_000

# (data, centavos, descrição)
PAG = [
    ("20260803", 450000, "PIX RECEBIDO - Studio Aurora Ltda"),
    ("20260805", -120000, "PIX ENVIADO - Gráfica Rápida"),
    ("20260807", -3990, "TARIFA PACOTE DE SERVIÇOS"),
    ("20260810", -300000, "TED ENVIADA - CONTA SIMPLES"),
    ("20260812", 280000, "PIX RECEBIDO - Clínica Vida"),
    ("20260815", -250000, "PIX ENVIADO - PRO-LABORE"),
    ("20260820", -85000, "PAGAMENTO DARF - SIMPLES NACIONAL"),
    ("20260825", 150000, "PIX RECEBIDO - Mercado Boa Compra"),
    ("20260828", -30000, "PIX ENVIADO - Contador Silva"),
    ("20260902", 520000, "PIX RECEBIDO - Studio Aurora Ltda"),
    ("20260905", -115000, "PIX ENVIADO - Gráfica Rápida"),
    ("20260907", -3990, "TARIFA PACOTE DE SERVIÇOS"),
    ("20260910", -200000, "TED ENVIADA - CONTA SIMPLES"),
    ("20260914", 310000, "PIX RECEBIDO - Clínica Vida"),
    ("20260915", -250000, "PIX ENVIADO - PRO-LABORE"),
    ("20260920", -85000, "PAGAMENTO DARF - SIMPLES NACIONAL"),
    ("20260926", 12000, "ESTORNO PIX - Gráfica Rápida"),
]

CS = [
    ("20260810", 300000, "TRANSFERENCIA RECEBIDA - PAGBANK"),
    ("20260812", -1500, "COMPRA CARTAO - UBER *TRIP"),
    ("20260812", -1500, "COMPRA CARTAO - UBER *TRIP"),
    ("20260814", -189000, "GOOGLE ADS BRASIL"),
    ("20260818", -42000, "META PLATFORMS ADS"),
    ("20260822", -8990, "FIGMA INC"),
    ("20260829", -19900, "CANVA PTY LTD"),
    ("20260903", -189000, "GOOGLE ADS BRASIL"),
    ("20260910", 199000, "TRANSFERENCIA RECEBIDA - PAGBANK"),
    ("20260912", -35000, "META PLATFORMS ADS"),
    ("20260918", -8990, "FIGMA INC"),
    ("20260924", -4800, "REGISTRO.BR DOMINIO"),
]


def reais(c: int) -> str:
    return str(Decimal(c) / 100)  # exato: Decimal, nunca float


def sgml(acct, txs, start, end, ledger, with_fitid=True, prefix="P"):
    lines = ["OFXHEADER:100", "DATA:OFXSGML", "VERSION:102", "SECURITY:NONE", "ENCODING:USASCII", "CHARSET:1252",
             "COMPRESSION:NONE", "OLDFILEUID:NONE", "NEWFILEUID:NONE", "", "<OFX>", "<SIGNONMSGSRSV1><SONRS>",
             "<STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20261013120000[-3:BRT]<LANGUAGE>POR</SONRS></SIGNONMSGSRSV1>",
             "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS><STMTRS><CURDEF>BRL",
             f"<BANKACCTFROM><BANKID>290<ACCTID>{acct}<ACCTTYPE>CHECKING</BANKACCTFROM>",
             f"<BANKTRANLIST><DTSTART>{start}000000[-3:BRT]<DTEND>{end}000000[-3:BRT]"]
    for i, (d, c, desc) in enumerate(txs, 1):
        lines += ["<STMTTRN>", f"<TRNTYPE>{'CREDIT' if c > 0 else 'DEBIT'}", f"<DTPOSTED>{d}120000[-3:BRT]",
                  f"<TRNAMT>{reais(c)}"]
        if with_fitid:
            lines.append(f"<FITID>{prefix}{d}{i:03d}")
        lines += [f"<MEMO>{desc}", "</STMTTRN>"]
    lines += ["</BANKTRANLIST>", f"<LEDGERBAL><BALAMT>{reais(ledger[0])}<DTASOF>{ledger[1]}120000[-3:BRT]</LEDGERBAL>",
              "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"]
    return "\r\n".join(lines).encode("cp1252")


def xml(acct, txs, start, end, ledger):
    t = ['<?xml version="1.0" encoding="UTF-8"?>', '<?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE"?>', "<OFX>",
         "<BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>BRL</CURDEF>",
         f"<BANKACCTFROM><BANKID>CS</BANKID><ACCTID>{acct}</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>",
         f"<BANKTRANLIST><DTSTART>{start}</DTSTART><DTEND>{end}</DTEND>"]
    for d, c, desc in txs:
        t.append(f"<STMTTRN><TRNTYPE>{'CREDIT' if c > 0 else 'DEBIT'}</TRNTYPE><DTPOSTED>{d}</DTPOSTED>"
                 f"<TRNAMT>{reais(c)}</TRNAMT><MEMO>{desc}</MEMO></STMTTRN>")
    t += ["</BANKTRANLIST>", f"<LEDGERBAL><BALAMT>{reais(ledger[0])}</BALAMT><DTASOF>{ledger[1]}</DTASOF></LEDGERBAL>",
          "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"]
    return "\n".join(t).encode("utf-8")


def main():
    pag_aug = [t for t in PAG if t[0] <= "20260831"]
    pag_sep = [t for t in PAG if t[0] >= "20260825"]  # sobrepõe 25–31/08
    # FITIDs precisam ser os mesmos nas duas exportações: derivados da lista completa
    fitid_by_tx = {t: f"P{t[0]}{i:03d}" for i, t in enumerate(PAG, 1)}

    # build() injeta o FITID estável (posição na lista completa) logo após cada <TRNAMT>

    def build(txs, start, end, ledger_cents, ledger_date):
        lines = sgml("0001234-5", txs, start, end, (ledger_cents, ledger_date), with_fitid=False).decode("cp1252").split("\r\n")
        out, k = [], 0
        for ln in lines:
            out.append(ln)
            if ln.startswith("<TRNAMT>"):
                out.append(f"<FITID>{fitid_by_tx[txs[k]]}")
                k += 1
        return "\r\n".join(out).encode("cp1252")

    aug_bal = PAG_OPEN + sum(c for _, c, _ in pag_aug)
    sep_bal = PAG_OPEN + sum(c for _, c, _ in PAG)
    (OUT / "pagbank_2026-08.ofx").write_bytes(build(pag_aug, "20260801", "20260831", aug_bal, "20260831"))
    (OUT / "pagbank_2026-09.ofx").write_bytes(build(pag_sep, "20260825", "20260930", sep_bal, "20260930"))

    cs_aug = [t for t in CS if t[0] <= "20260831"]
    cs_sep = [t for t in CS if t[0] >= "20260903"]
    cs_aug_bal = CS_OPEN + sum(c for _, c, _ in cs_aug)
    cs_sep_bal = CS_OPEN + sum(c for _, c, _ in CS) + 8990   # extrato informa R$ 89,90 a mais que o calculado: divergência de propósito
    (OUT / "contasimples_2026-08.ofx").write_bytes(xml("98765-0", cs_aug, "20260801", "20260831", (cs_aug_bal, "20260831")))
    (OUT / "contasimples_2026-09.ofx").write_bytes(xml("98765-0", cs_sep, "20260903", "20260930", (cs_sep_bal, "20260930")))
    print("OFX de demonstração gerados em", OUT)


if __name__ == "__main__":
    main()
