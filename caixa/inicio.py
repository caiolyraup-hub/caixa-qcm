"""Dados da tela inicial: seis respostas, cada uma com os itens que formam o número (para detalhar ao clicar)."""
from datetime import date, timedelta

from . import dre as dre_mod
from . import recon
from .baixas import pending_suggestions
from .ledger import list_unclassified
from .money import format_brl
from .obrigacoes import CONFIRMED_ORIGINS, list_installments
from .posicao import count_cdi, positions, reserve_cents, reserve_defined
from .projecao import min_window, project
from .rotina import daily_status
from .timeutil import fmt_date, today_sp


def _sum(rows: list[dict]) -> int:
    return sum(r["open_cents"] for r in rows)


def inicio(conn, today: date | None = None) -> dict:
    today = today or today_sp()
    cdi_on = count_cdi(conn)

    # 1) caixa disponível agora, por conta
    pos = positions(conn, today)
    total = sum((p["total_cents"] if cdi_on else p["available_cents"]) for p in pos)
    caixa = {"accounts": pos, "total_cents": total, "cdi_counted": cdi_on,
             "limitations": [f"{p['name']}: {m}" for p in pos for m in p["limitations"]]}

    # 2) a receber e a pagar nos próximos 7 e 30 dias; vencidos em destaque
    ins = list_installments(conn, today, origins=CONFIRMED_ORIGINS)
    open_ = [p for p in ins if p["open_cents"] > 0]

    def window(direction: str, n: int) -> list[dict]:
        end = (today + timedelta(days=n)).isoformat()
        return [p for p in open_ if p["direction"] == direction and not p["overdue"]
                and today.isoformat() <= p["expected_date"] <= end]

    overdue_r = [p for p in open_ if p["direction"] == "receber" and p["overdue"]]
    overdue_p = [p for p in open_ if p["direction"] == "pagar" and p["overdue"]]
    obrig = {"receber": {n: window("receber", n) for n in (7, 30)}, "pagar": {n: window("pagar", n) for n in (7, 30)},
             "vencidos_receber": overdue_r, "vencidos_pagar": overdue_p}

    # 3 e 4) menor saldo projetado e folga/falta frente à reserva
    proj = project(conn, today, 90)
    m90 = min_window(proj, 90)
    projecao = {"projection": proj, "min90": m90, "min30": min_window(proj, 30), "min7": min_window(proj, 7),
                "reserve_cents": reserve_cents(conn), "reserve_defined": reserve_defined(conn),
                "negative_dates": proj.negative_dates}

    # 5) DRE do mês
    month = today.isoformat()[:7]
    d = dre_mod.dre_month(conn, month, today=today)

    # 6) atenção hoje
    atencao: list[dict] = []
    for s in daily_status(conn, today=today):
        if not s["ok"]:
            atencao.append({"key": "extratos", "text": f"{s['account']}: {s['detail']}", "count": 1})
    if overdue_p or overdue_r:
        atencao.append({"key": "vencidas", "count": len(overdue_p) + len(overdue_r),
                        "text": f"{len(overdue_p)} a pagar e {len(overdue_r)} a receber vencidos sem baixa "
                                f"({format_brl(_sum(overdue_p))} e {format_brl(_sum(overdue_r))})"})
    c = recon.investigation_counts(conn)
    div = [f"{c['saldos_divergentes']} saldo(s) divergente(s)" if c["saldos_divergentes"] else "",
           f"{c['transferencias_sem_par']} transferência(s) sem par" if c["transferencias_sem_par"] else "",
           f"{c['duplicatas']} possível(is) duplicata(s)" if c["duplicatas"] else "",
           f"{c['pares_com_diferenca']} transferência(s) com diferença" if c["pares_com_diferenca"] else "",
           f"{c['lacunas']} período(s) sem extrato" if c["lacunas"] else ""]
    div = [x for x in div if x]
    if div:
        atencao.append({"key": "investigar", "count": len(div), "text": "A conferir: " + ", ".join(div)})
    sug = pending_suggestions(conn, today)
    if sug:
        clear = sum(1 for s in sug if s["unambiguous"])
        atencao.append({"key": "conferir_baixas", "count": len(sug), "suggestions": sug,
                        "text": f"{len(sug)} lançamento(s) do extrato com baixa sugerida em contas a receber/pagar"
                                + (f" ({clear} sem ambiguidade)" if clear else "")})
    unc = list_unclassified(conn)
    if unc:
        atencao.append({"key": "identificar", "count": len(unc),
                        "text": f"{len(unc)} movimentação(ões) sem classificação ({format_brl(sum(abs(u['amount_cents']) for u in unc))} movimentados)"})
    npend = len(d["pending"])
    if npend:
        atencao.append({"key": "dre", "count": npend,
                        "text": f"{npend} item(ns) sem linha definida na DRE de {month} (ficam fora do resultado até você definir)"})
    if d["presumed_competence"]["count"]:
        atencao.append({"key": "dre", "count": d["presumed_competence"]["count"],
                        "text": f"{d['presumed_competence']['count']} lançamento(s) com competência presumida pelo mês do banco"})
    if not projecao["reserve_defined"]:
        atencao.append({"key": "reserva", "count": 1, "text": "Defina sua reserva mínima de caixa para ver a folga"})
    cdi_old = [m for p in pos for m in p["limitations"] if "CDI" in m]
    if cdi_old:
        atencao.append({"key": "cdi", "count": len(cdi_old), "text": "; ".join(cdi_old)})

    return {"today": today, "caixa": caixa, "obrigacoes": obrig, "projecao": projecao, "dre": d, "dre_month": month,
            "atencao": atencao}
