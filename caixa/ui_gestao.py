"""Telas de gestão: Início, A receber e a pagar, Caixa projetado e DRE do mês.

Princípios de interface: poucas respostas, linguagem simples (a receber, a pagar, recebido, pago, em aberto, a conferir),
uma ação principal por seção, composição de cada número sob demanda e limitações SEMPRE à vista (nunca só em tooltip).
"""
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from . import baixas, dre as dre_mod, obrigacoes as ob
from .db import list_accounts
from .inicio import inicio
from .ledger import LedgerError
from .money import cents_to_plain, format_brl, parse_amount_to_cents
from .posicao import get_setting, reserve_cents, reserve_defined, set_setting
from .projecao import HORIZONS, min_window, project
from .timeutil import fmt_date, today_sp

P_INICIO, P_OBRIG, P_PROJ, P_DRE = "Início", "A receber e a pagar", "Caixa projetado", "DRE do mês"
GESTAO_PAGES = [P_INICIO, P_OBRIG, P_PROJ, P_DRE]
DIRECTION_LABEL = {"receber": "A receber", "pagar": "A pagar"}
PAID_LABEL = {"receber": "Recebido", "pagar": "Pago"}


# ------------------------------------------------------------------ utilidades de tela

def flash(msg: str, kind: str = "success") -> None:
    st.session_state["_flash"] = (kind, msg)


def show_flash() -> None:
    if "_flash" in st.session_state:
        kind, msg = st.session_state.pop("_flash")
        getattr(st, kind)(msg)


def _goto(page: str) -> None:
    st.session_state["menu"] = page


def _toggle(key: str) -> None:
    st.session_state["detalhe"] = None if st.session_state.get("detalhe") == key else key


def _detail_button(key: str, label: str = "Ver composição") -> bool:
    st.button(label if st.session_state.get("detalhe") != key else "Ocultar composição", key=f"det_{key}",
              on_click=_toggle, args=(key,))
    return st.session_state.get("detalhe") == key


def _parcelas_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Vence": fmt_date(p["due_date"]), "Prevista": fmt_date(p["expected_date"]),
        "Quem": p["counterparty"] or "—", "Descrição": p["description"] + (f" ({p['number']}/{p['of_total']})" if p["of_total"] and p["of_total"] > 1 else ""),
        "Valor": format_brl(p["amount_cents"]), PAID_LABEL[p["direction"]]: format_brl(p["settled_cents"]),
        "Em aberto": format_brl(p["open_cents"]),
        "Situação": ("VENCIDA · " if p["overdue"] else "") + p["status"],
    } for p in rows])


def _tx_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"Data": fmt_date(r["posted_date"]), "Conta": r.get("account_name", ""), "Descrição": r["description"],
                          "Valor": format_brl(r["amount_cents"], sign=True)} for r in rows])


def _limits(lines: list[str]) -> None:
    for m in lines:
        st.warning(m)


# ------------------------------------------------------------------ INÍCIO

def page_inicio(conn) -> None:
    show_flash()
    st.header("Início")
    if not list_accounts(conn):
        st.info("Cadastre as contas em **Rotina diária** (ou em **Contas**) para começar.")
        return
    h = inicio(conn)
    st.caption(f"Hoje, {fmt_date(h['today'].isoformat())}. Cada número pode ser aberto para ver o que o compõe.")

    # 1 ─ Caixa disponível agora
    with st.container(border=True):
        st.subheader("1. Caixa disponível agora")
        c = h["caixa"]
        st.metric("Total" + (" (com CDI)" if c["cdi_counted"] else " (sem CDI)") + (" — com ressalvas" if c["limitations"] else ""),
                  format_brl(c["total_cents"]))
        if c["limitations"]:
            st.caption("Este total não é o saldo de hoje em todas as contas: veja as ressalvas abaixo e a data de cada conta.")
        _limits(c["limitations"])
        if _detail_button("caixa"):
            st.dataframe(pd.DataFrame([{
                "Conta": p["name"] + (" — limite disponível" if p["is_card_limit"] else ""),
                "Disponível": format_brl(p["available_cents"]), "Aplicado (CDI)": format_brl(p["cdi_cents"]),
                "Total": format_brl(p["total_cents"]),
                "Informação de": fmt_date(p["asof"].isoformat()) if p["asof"] else "sem extrato"} for p in c["accounts"]]),
                hide_index=True, use_container_width=True)

    # 2 ─ A receber e a pagar
    with st.container(border=True):
        st.subheader("2. A receber e a pagar")
        o = h["obrigacoes"]
        if o["vencidos_pagar"] or o["vencidos_receber"]:
            st.error(f"**Vencidos em aberto:** a pagar {format_brl(sum(p['open_cents'] for p in o['vencidos_pagar']))} "
                     f"({len(o['vencidos_pagar'])}) · a receber {format_brl(sum(p['open_cents'] for p in o['vencidos_receber']))} "
                     f"({len(o['vencidos_receber'])})")
        m = st.columns(4)
        for col, (d, n, lab) in zip(m, (("receber", 7, "A receber em 7 dias"), ("receber", 30, "A receber em 30 dias"),
                                        ("pagar", 7, "A pagar em 7 dias"), ("pagar", 30, "A pagar em 30 dias"))):
            col.metric(lab, format_brl(sum(p["open_cents"] for p in o[d][n])))
        if _detail_button("obrig"):
            rows = (o["vencidos_pagar"] + o["vencidos_receber"] + o["receber"][30] + o["pagar"][30])
            if rows:
                st.dataframe(_parcelas_df(sorted(rows, key=lambda p: p["expected_date"])), hide_index=True, use_container_width=True)
            else:
                st.caption("Nada em aberto nos próximos 30 dias.")
        st.button("Cadastrar ou dar baixa", key="go_obrig", on_click=_goto, args=(P_OBRIG,))

    # 3 e 4 ─ Menor saldo e folga
    with st.container(border=True):
        st.subheader("3. Menor saldo nos próximos 90 dias")
        pr, m90 = h["projecao"]["projection"], h["projecao"]["min90"]
        a, b = st.columns(2)
        a.metric("Menor saldo projetado", format_brl(m90["cents"]), help=None)
        b.metric("Quando", fmt_date(m90["date"].isoformat()))
        for acc_id, d in h["projecao"]["negative_dates"].items():
            st.error(f"**{pr.accounts[acc_id]['name']}** ficaria negativa em {fmt_date(d.isoformat())}, mesmo que o total siga positivo.")
        st.subheader("4. Folga em relação à reserva mínima")
        if not h["projecao"]["reserve_defined"]:
            with st.form("reserva_form"):
                v = st.text_input("Defina sua reserva mínima de caixa (R$)", value="0,00")
                if st.form_submit_button("Salvar reserva"):
                    try:
                        set_setting(conn, "reserva_minima_cents", str(parse_amount_to_cents(v)))
                        flash("Reserva mínima salva.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        else:
            slack = m90["slack"]
            r1, r2 = st.columns(2)
            r1.metric("Reserva mínima", format_brl(h["projecao"]["reserve_cents"]))
            r2.metric("Folga" if slack >= 0 else "Falta", format_brl(abs(slack)))
            st.write(f"Você pode gastar até **{format_brl(max(slack, 0))}** sem tocar na reserva." if slack > 0
                     else ("Não há folga: o caixa projetado chega **na** reserva." if slack == 0
                           else f"**Atenção:** o caixa projetado fica {format_brl(-slack)} abaixo da reserva."))
        _limits(pr.limitations)
        if pr.overdue_receivables:
            st.caption(f"{len(pr.overdue_receivables)} recebimento(s) vencido(s) não estão contados no caixa projetado (incerteza de data).")
        if _detail_button("proj"):
            low = m90["date"]
            comp = [i for r in pr.rows if r["date"] <= low for i in r["items"]]
            st.dataframe(pd.DataFrame([{"Dia": fmt_date(i.date.isoformat()), "Quem": i.counterparty or "—", "Descrição": i.description,
                                        "Valor": format_brl(i.cents, sign=True)} for i in comp]) if comp
                         else pd.DataFrame({"Info": ["Sem previsões até essa data: o menor saldo é o saldo atual."]}),
                         hide_index=True, use_container_width=True)
        st.button("Ver caixa projetado dia a dia", key="go_proj", on_click=_goto, args=(P_PROJ,))

    # 5 ─ DRE do mês
    with st.container(border=True):
        d = h["dre"]
        st.subheader(f"5. DRE de {d['month'][5:]}/{d['month'][:4]} (por competência)")
        sp, sr = d["subtotals"]["previsto"], d["subtotals"]["realizado"]
        m = st.columns(4)
        m[0].metric("Resultado previsto", format_brl(sp["resultado"], sign=True))
        m[1].metric("Resultado realizado", format_brl(sr["resultado"], sign=True))
        m[2].metric("Margem prevista", f"{sp['margem'] * 100:.1f}%" if sp["margem"] is not None else "—")
        m[3].metric("Margem realizada", f"{sr['margem'] * 100:.1f}%" if sr["margem"] is not None else "—")
        if not d["complete"]:
            st.warning("DRE incompleta: " + "; ".join(x for x in (
                f"{len(d['pending'])} item(ns) sem linha definida" if d["pending"] else "",
                f"{d['unclassified']['count']} movimentação(ões) sem classificação no mês" if d["unclassified"]["count"] else "",
                f"{d['presumed_competence']['count']} com competência presumida" if d["presumed_competence"]["count"] else "") if x))
        st.button("Abrir DRE", key="go_dre", on_click=_goto, args=(P_DRE,))

    # 6 ─ Atenção hoje
    with st.container(border=True):
        st.subheader("6. Precisa da sua atenção hoje")
        pages = {"extratos": "Rotina diária", "vencidas": P_OBRIG, "investigar": "Investigar divergências",
                 "conferir_baixas": P_OBRIG, "identificar": "A identificar", "dre": P_DRE, "cdi": "Contas",
                 "reserva": P_INICIO}
        if not h["atencao"]:
            st.success("Nada pendente. Os números acima são confiáveis com os dados atuais.")
        for n, a in enumerate(h["atencao"]):
            c1, c2 = st.columns([6, 1])
            c1.write(f"• {a['text']}")
            if a["key"] != "reserva":
                c2.button("Abrir", key=f"att_{n}", on_click=_goto, args=(pages[a["key"]],))
        if any(a["key"] == "conferir_baixas" for a in h["atencao"]):
            st.caption("Dica: em *A receber e a pagar → A conferir* você confirma as baixas sugeridas com um clique.")


# ------------------------------------------------------------------ A RECEBER E A PAGAR

def _new_obligation_form(conn, accs: list[dict]) -> None:
    kind = st.radio("O que é?", ["a receber", "a pagar"], horizontal=True, key="nob_kind")
    direction = "receber" if kind == "a receber" else "pagar"
    with st.form("nova_obrigacao", clear_on_submit=True):
        c1, c2 = st.columns(2)
        who = c1.text_input("Cliente" if direction == "receber" else "Fornecedor")
        desc = c2.text_input("Descrição (ex.: mensalidade)")
        c3, c4, c5 = st.columns(3)
        val = c3.text_input("Valor (R$)", placeholder="3.000,00")
        due = c4.date_input("Vencimento", value=today_sp(), format="DD/MM/YYYY")
        rep = c5.selectbox("Repetição", ["Uma vez", "Todo mês", "Parcelado"])
        n = st.number_input("Quantos meses / parcelas (se repetir ou parcelar)", min_value=2, max_value=60, value=12)
        with st.expander("Mais opções (data prevista, competência, categoria, conta, origem...)"):
            e1, e2 = st.columns(2)
            exp = e1.date_input("Data prevista de " + ("recebimento" if direction == "receber" else "pagamento")
                                + " (se diferente do vencimento)", value=None, format="DD/MM/YYYY")
            comp = e2.text_input("Competência (AAAA-MM; vazio = mês do vencimento)")
            f1, f2 = st.columns(2)
            cat = f1.text_input("Categoria gerencial")
            cc = f2.text_input("Centro de custo")
            g1, g2 = st.columns(2)
            acc = g1.selectbox("Conta esperada", [None] + accs, format_func=lambda a: "Não definida" if a is None else a["name"])
            origin = g2.selectbox("Origem", list(ob.ORIGINS), format_func=ob.ORIGINS.get,
                                  help="Só contrato confirmado e obrigação assumida entram no caixa projetado principal e na DRE.")
            nat_opts = ob.NATURES_RECEBER if direction == "receber" else tuple(ob.NATURES)
            nature = st.selectbox("Natureza", nat_opts, format_func=ob.NATURES.get)
            prob = st.number_input("Probabilidade % (só para oportunidade)", min_value=0, max_value=100, value=0)
            note = st.text_area("Observação")
            att = st.file_uploader("Comprovante (opcional)")
        if st.form_submit_button("Cadastrar", type="primary"):
            try:
                total = parse_amount_to_cents(val)
                rec = {"months": int(n)} if rep == "Todo mês" else None
                ob.create_obligation(
                    conn, direction=direction, description=desc, counterparty=who, amount_cents=total, due_date=due.isoformat(),
                    category=cat, cost_center=cc, nature=nature, origin=origin,
                    probability_pct=int(prob) if origin == "oportunidade" and prob else None,
                    expected_account_id=acc["id"] if acc else None, expected_date=exp.isoformat() if exp else None,
                    competence=comp.strip() or None, note=note, attachment=(att.name, att.getvalue()) if att else None,
                    installments=int(n) if rep == "Parcelado" else 1, recurrence=rec)
            except (ValueError, LedgerError) as e:
                st.error(str(e))
            else:
                flash("Cadastrado.")
                st.rerun()


def _installment_panel(conn, p: dict) -> None:
    """Ações sobre uma parcela: dar baixa, ajustar, editar, cancelar, ver e desfazer baixas."""
    st.markdown(f"**{p['counterparty'] or '—'} · {p['description']}** — vence {fmt_date(p['due_date'])} · "
                f"valor {format_brl(p['amount_cents'])} · {PAID_LABEL[p['direction']].lower()} {format_brl(p['settled_cents'])} · "
                f"**em aberto {format_brl(p['open_cents'])}** · {p['status']}")
    if p["open_cents"] > 0:
        cands = baixas.suggest_transactions_for_installment(conn, p["id"])
        if cands:
            pick = st.selectbox(
                "Lançamento do extrato", cands, key=f"pk_{p['id']}",
                format_func=lambda r: f"{fmt_date(r['posted_date'])} · {r['account_name']} · {r['description'][:42]} · "
                                      f"disponível {format_brl(r['available_cents'])}")
            default = min(pick["available_cents"], p["open_cents"])
            amt = st.text_input("Valor desta baixa (R$)", value=cents_to_plain(default), key=f"amt_{p['id']}_{pick['id']}")
            if st.button("Dar baixa", type="primary", key=f"bx_{p['id']}"):
                try:
                    baixas.allocate(conn, p["id"], pick["id"], parse_amount_to_cents(amt))
                except (ValueError, LedgerError) as e:
                    st.error(str(e))
                else:
                    flash("Baixa registrada. O lançamento do banco não foi duplicado.")
                    st.rerun()
        else:
            st.caption("Nenhum lançamento compatível no extrato ainda. Importe o extrato e volte.")
        with st.expander("Taxa, desconto ou diferença (ajuste sem movimento bancário)"):
            kinds = ["taxa", "desconto", "diferenca"] if p["direction"] == "receber" else ["desconto", "diferenca"]
            k = st.selectbox("Tipo de ajuste", kinds, format_func={"taxa": "Taxa retida", "desconto": "Desconto", "diferenca": "Diferença (exige motivo)"}.get,
                             key=f"ak_{p['id']}")
            v = st.text_input("Valor (R$)", key=f"av_{p['id']}")
            why = st.text_input("Motivo", key=f"aw_{p['id']}")
            if st.button("Registrar ajuste", key=f"ab_{p['id']}"):
                try:
                    baixas.adjust(conn, p["id"], k, parse_amount_to_cents(v), why)
                except (ValueError, LedgerError) as e:
                    st.error(str(e))
                else:
                    flash("Ajuste registrado.")
                    st.rerun()
    allocs = ob.list_allocations(conn, p["id"])
    if allocs:
        with st.expander(f"Baixas e histórico ({len(allocs)})"):
            for a in allocs:
                c1, c2 = st.columns([6, 1])
                what = {"pagamento": PAID_LABEL[p["direction"]], "estorno": "Estorno", "taxa": "Taxa", "desconto": "Desconto",
                        "diferenca": "Diferença"}[a["kind"]]
                src = f" · lançamento {fmt_date(a['posted_date'])} {a['tx_description'][:40]}" if a["tx_id"] else ""
                c1.write(f"{'~~' if a['undone_at'] else ''}{what} {format_brl(a['amount_cents'])}{src}"
                         f"{'~~ (desfeita)' if a['undone_at'] else ''} {('— ' + a['note']) if a['note'] else ''}")
                if not a["undone_at"] and c2.button("Desfazer", key=f"un_{a['id']}"):
                    try:
                        baixas.undo(conn, a["id"])
                    except LedgerError as e:
                        st.error(str(e))
                    else:
                        flash("Baixa desfeita. O lançamento do banco continua intacto.")
                        st.rerun()
    if p["status"] not in ("liquidada", "cancelada"):
        with st.expander("Editar esta parcela"):
            with st.form(f"ed_{p['id']}"):
                c1, c2 = st.columns(2)
                nv = c1.text_input("Valor (R$)", value=cents_to_plain(p["amount_cents"]))
                nd = c2.date_input("Vencimento", value=date.fromisoformat(p["due_date"]), format="DD/MM/YYYY")
                c3, c4 = st.columns(2)
                ne = c3.date_input("Data prevista", value=date.fromisoformat(p["expected_date"]), format="DD/MM/YYYY")
                nc = c4.text_input("Competência (AAAA-MM)", value=p["competence"])
                ncat = st.text_input("Categoria", value=p["category"])
                scope = st.radio("Aplicar a", ["Só esta parcela", "Esta e as próximas sem baixa"], horizontal=True)
                if st.form_submit_button("Salvar"):
                    try:
                        ch = {"amount_cents": parse_amount_to_cents(nv), "competence": nc, "category": ncat}
                        if scope == "Só esta parcela":
                            ob.update_installment(conn, p["id"], due_date=nd.isoformat(), expected_date=ne.isoformat(), **ch)
                            flash("Parcela atualizada.")
                        else:
                            ob.update_installment(conn, p["id"], due_date=nd.isoformat(), expected_date=ne.isoformat(), **ch)
                            res = ob.update_series(conn, p["obligation_id"], p["due_date"], **{k: v for k, v in ch.items() if k != "competence"})
                            flash(f"Atualizadas: {len(res['updated'])} · preservadas (já têm baixa): {len(res['skipped'])}.")
                    except (ValueError, LedgerError) as e:
                        st.error(str(e))
                    else:
                        st.rerun()
        if not p["has_allocations"] and st.button("Cancelar esta parcela", key=f"cx_{p['id']}"):
            ob.cancel_installment(conn, p["id"])
            flash("Parcela cancelada.")
            st.rerun()


def _list_tab(conn, direction: str) -> None:
    show = st.radio("Mostrar", ["Em aberto", "Tudo"], horizontal=True, key=f"show_{direction}")
    rows = [p for p in ob.list_installments(conn, direction=direction)
            if show == "Tudo" or p["open_cents"] > 0]
    if not rows:
        st.caption("Nada cadastrado ainda." if show == "Em aberto" else "Nada por aqui.")
        return
    open_total = sum(p["open_cents"] for p in rows if p["open_cents"] > 0)
    st.caption(f"{len(rows)} parcela(s) · em aberto: **{format_brl(open_total)}**")
    ev = st.dataframe(_parcelas_df(rows), hide_index=True, use_container_width=True, on_select="rerun",
                      selection_mode="single-row", key=f"df_{direction}")
    if ev.selection.rows:
        st.divider()
        _installment_panel(conn, rows[ev.selection.rows[0]])


def _conferir_tab(conn) -> None:
    sug = baixas.pending_suggestions(conn)
    if not sug:
        st.success("Nada a conferir: nenhum lançamento do extrato tem baixa sugerida.")
        return
    clear = [s for s in sug if s["unambiguous"]]
    if clear and st.button(f"Confirmar as {len(clear)} sugestões sem ambiguidade", type="primary", key="conf_all"):
        done = baixas.confirm_unambiguous(conn)
        flash(f"{len(done)} baixa(s) confirmada(s).")
        st.rerun()
    st.caption("Só vinculo automático quando valor, data e quem pagou apontam para uma única parcela. O resto fica para você decidir.")
    for s in sug:
        t = s["tx"]
        with st.container(border=True):
            st.markdown(f"**{fmt_date(t['posted_date'])} · {format_brl(t['amount_cents'], sign=True)} · {t['description']}**"
                        + ("  ·  ✔ sem ambiguidade" if s["unambiguous"] else "  ·  ⚠ mais de uma opção, confirme"))
            for c in s["candidates"][:3]:
                p = c["installment"]
                c1, c2 = st.columns([6, 1])
                c1.write(f"{p['counterparty'] or '—'} · {p['description']} · vence {fmt_date(p['due_date'])} · em aberto "
                         f"{format_brl(p['open_cents'])} — baixar {format_brl(c['amount_cents'])}  \n_{'; '.join(c['reasons'])}_")
                if c2.button("Vincular", key=f"lk_{t['id']}_{p['id']}"):
                    try:
                        baixas.allocate(conn, p["id"], t["id"], c["amount_cents"], note="Sugestão confirmada: " + "; ".join(c["reasons"]))
                    except LedgerError as e:
                        st.error(str(e))
                    else:
                        flash("Baixa registrada.")
                        st.rerun()


def page_obrigacoes(conn) -> None:
    show_flash()
    st.header("A receber e a pagar")
    accs = list_accounts(conn)
    if not accs:
        st.info("Cadastre as contas primeiro (Rotina diária ou Contas).")
        return
    with st.expander("➕ Cadastrar a receber / a pagar", expanded=not ob.list_installments(conn)):
        _new_obligation_form(conn, accs)
    n_conf = len(baixas.pending_suggestions(conn))
    t1, t2, t3 = st.tabs(["A receber", "A pagar", f"A conferir ({n_conf})"])
    with t1:
        _list_tab(conn, "receber")
    with t2:
        _list_tab(conn, "pagar")
    with t3:
        _conferir_tab(conn)


# ------------------------------------------------------------------ CAIXA PROJETADO

def page_projecao(conn) -> None:
    show_flash()
    st.header("Caixa projetado")
    accs = list_accounts(conn)
    if not accs:
        st.info("Cadastre as contas primeiro.")
        return
    c1, c2, c3 = st.columns([2, 2, 3])
    n = c1.radio("Horizonte", list(HORIZONS), index=2, format_func=lambda x: f"{x} dias", horizontal=True)
    with_sc = c2.checkbox("Mostrar cenário com estimativas", help="Estimativas e oportunidades, claramente separadas do caixa contratado.")
    cdi = c3.checkbox("Contar o CDI como caixa", value=get_setting(conn, "contar_cdi", "1") == "1")
    if cdi != (get_setting(conn, "contar_cdi", "1") == "1"):
        set_setting(conn, "contar_cdi", "1" if cdi else "0")
        st.rerun()
    proj = project(conn, horizon=90)
    w = min_window(proj, n)

    r1, r2 = st.columns(2)
    reserve_txt = r1.text_input("Reserva mínima (R$)", value=cents_to_plain(reserve_cents(conn)))
    if r1.button("Salvar reserva", key="save_reserve"):
        try:
            set_setting(conn, "reserva_minima_cents", str(parse_amount_to_cents(reserve_txt)))
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    m = st.columns(3)
    m[0].metric(f"Menor saldo em {n} dias", format_brl(w["cents"]))
    m[1].metric("No dia", fmt_date(w["date"].isoformat()))
    m[2].metric("Folga" if w["slack"] >= 0 else "Falta", format_brl(abs(w["slack"])) + " vs. reserva")
    _limits(proj.limitations)
    for acc_id, d in proj.negative_dates.items():
        st.error(f"**{proj.accounts[acc_id]['name']}** ficaria negativa em {fmt_date(d.isoformat())}.")

    rows = proj.rows[: n + 1]
    chart = pd.DataFrame({"Saldo projetado": [r["closing"] / 100 for r in rows],
                          "Reserva mínima": [proj.reserve_cents / 100] * len(rows)},
                         index=[r["date"] for r in rows])
    if with_sc and proj.scenario:
        chart["Cenário (inclui estimativas, NÃO garantido)"] = [r["closing"] / 100 for r in proj.scenario.rows[: n + 1]]
    st.line_chart(chart)
    if with_sc and proj.scenario:
        st.caption(proj.scenario.scenario_label)

    k1, k2 = st.columns(2)
    by_acc = k1.checkbox("Detalhar por conta")
    all_days = k2.checkbox("Mostrar todos os dias", help="Desligado: só os dias com recebimentos ou pagamentos.")
    df = pd.DataFrame([{
        "Dia": fmt_date(r["date"].isoformat()), "Abre": format_brl(r["opening"]), "A receber": format_brl(r["receipts"]),
        "A pagar": format_brl(r["payments"]), "Fecha": format_brl(r["closing"]),
        **({proj.accounts[k]["name"]: format_brl(v) for k, v in r["by_account"].items()} if by_acc else {})} for r in rows
        if r["receipts"] or r["payments"] or r is rows[0] or r is rows[-1] or all_days])
    st.dataframe(df, hide_index=True, use_container_width=True)

    if proj.overdue_payables or proj.overdue_receivables:
        with st.expander(f"Vencidos em aberto ({len(proj.overdue_payables) + len(proj.overdue_receivables)})", expanded=True):
            st.caption("Pagamentos vencidos entram no caixa projetado **hoje**. Recebimentos vencidos **não** entram (a data é incerta).")
            st.dataframe(pd.DataFrame([{"Tipo": "A pagar" if i.cents < 0 else "A receber", "Quem": i.counterparty or "—",
                                        "Descrição": i.description, "Previsto": fmt_date(i.date.isoformat()),
                                        "Em aberto": format_brl(abs(i.cents))}
                                       for i in proj.overdue_payables + proj.overdue_receivables]), hide_index=True, use_container_width=True)
    if proj.skipped:
        with st.expander(f"Fora do caixa projetado ({len(proj.skipped)}) e por quê"):
            st.dataframe(pd.DataFrame([{"Descrição": s["installment"]["description"], "Em aberto": format_brl(s["installment"]["open_cents"]),
                                        "Motivo": s["reason"]} for s in proj.skipped]), hide_index=True, use_container_width=True)


# ------------------------------------------------------------------ DRE

def _months_around(today: date) -> list[str]:
    return [ob.shift_month(today.isoformat()[:7], k) for k in range(2, -9, -1)]


def page_dre(conn) -> None:
    show_flash()
    st.header("DRE gerencial do mês")
    st.caption("Por competência (mês a que a receita ou a despesa pertence), não pelo mês do dinheiro. "
               "É um relatório gerencial, **não** uma demonstração contábil oficial.")
    today = today_sp()
    c1, c2 = st.columns([1, 2])
    month = c1.selectbox("Mês", _months_around(today), index=2, format_func=lambda m: f"{m[5:]}/{m[:4]}")
    clients = sorted({p["counterparty"] for p in ob.list_installments(conn) if p["counterparty"]}
                     | {r[0] for r in conn.execute("SELECT DISTINCT counterparty FROM classifications WHERE counterparty<>''")})
    client = c2.selectbox("Cliente (opcional)", [""] + clients, format_func=lambda c: c or "Todos")
    d = dre_mod.dre_month(conn, month, client=client or None, today=today)

    if d["pending"]:
        st.warning(f"**{len(d['pending'])} item(ns) sem linha definida na DRE** ficam fora dos totais até você escolher a linha "
                   f"({format_brl(sum(abs(p['previsto']) for p in d['pending']))} em valor). Defina abaixo em *Categorias sem linha*.")
    if d["unclassified"]["count"]:
        st.warning(f"{d['unclassified']['count']} movimentação(ões) sem classificação neste mês não entram na DRE.")
    if d["presumed_competence"]["count"]:
        st.info(f"{d['presumed_competence']['count']} lançamento(s) usam competência presumida (mês do banco). Revise abaixo.")

    sub = d["subtotals"]
    rows = []

    def add(label, key=None, sp=None, sr=None, bold=False):
        rows.append({"Linha": ("**" + label + "**") if bold else label,
                     "Previsto": format_brl(d["lines"][key]["previsto"] if key else sp, sign=True),
                     "Realizado": format_brl(d["lines"][key]["realizado"] if key else sr, sign=True)})
    add("Receita de serviços", "receita_servicos")
    add("(−) Deduções e impostos sobre a receita", "deducoes")
    add("= Receita líquida", sp=sub["previsto"]["receita_liquida"], sr=sub["realizado"]["receita_liquida"])
    add("(−) Custos da entrega", "custos")
    add("= Margem bruta", sp=sub["previsto"]["margem_bruta"], sr=sub["realizado"]["margem_bruta"])
    add("(−) Despesas comerciais", "desp_comerciais")
    add("(−) Despesas administrativas", "desp_administrativas")
    add("= Resultado operacional", sp=sub["previsto"]["resultado_operacional"], sr=sub["realizado"]["resultado_operacional"])
    add("(+) Receitas financeiras", "fin_receita")
    add("(−) Despesas financeiras", "fin_despesa")
    add("= Resultado financeiro", sp=sub["previsto"]["resultado_financeiro"], sr=sub["realizado"]["resultado_financeiro"])
    add("= RESULTADO GERENCIAL", sp=sub["previsto"]["resultado"], sr=sub["realizado"]["resultado"])
    rows.append({"Linha": "Margem sobre a receita líquida",
                 "Previsto": f"{sub['previsto']['margem'] * 100:.1f}%" if sub["previsto"]["margem"] is not None else "—",
                 "Realizado": f"{sub['realizado']['margem'] * 100:.1f}%" if sub["realizado"]["margem"] is not None else "—"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("**Previsto** = o que foi contratado/assumido e já aconteceu no banco. **Realizado** = o que já foi baixado (recebido/pago) "
               "e os ajustes. Estimativas e oportunidades não entram.")

    line = st.selectbox("Ver o que compõe a linha", [k for k, _ in dre_mod.LINES], format_func=dre_mod.LINE_LABEL.get)
    items = d["lines"][line]["items"]
    st.dataframe(pd.DataFrame([{"Origem": "Parcela" if i["source"] == "parcela" else "Lançamento do banco", "Data": fmt_date(i["date"]),
                                "Quem": i["counterparty"] or "—", "Descrição": i["description"], "Previsto": format_brl(i["previsto"], sign=True),
                                "Realizado": format_brl(i["realizado"], sign=True),
                                "Competência": i["competence"] + (" (presumida)" if i["presumed"] else "")} for i in items])
                 if items else pd.DataFrame({"Info": ["Nada nesta linha neste mês."]}), hide_index=True, use_container_width=True)

    if d["outside"]:
        with st.expander(f"Fora da DRE operacional ({len(d['outside'])}): transferências, aplicações, aportes, distribuição, principal, fatura"):
            st.dataframe(pd.DataFrame([{"Data": fmt_date(o["date"]), "Descrição": o["description"],
                                        "Valor": format_brl(o["previsto"], sign=True)} for o in d["outside"]]), hide_index=True, use_container_width=True)

    unmapped = dre_mod.unmapped_categories(conn)
    if unmapped or d["pending"]:
        with st.expander("Categorias sem linha na DRE", expanded=bool(d["pending"])):
            for cat in unmapped:
                a, b = st.columns([3, 3])
                a.write(cat)
                pick = b.selectbox("Linha", [k for k, _ in dre_mod.LINES] + ["fora_dre"], index=None, key=f"cm_{cat}",
                                   format_func=dre_mod.LINE_LABEL.get, placeholder="Escolha a linha")
                if pick:
                    dre_mod.set_category_line(conn, cat, pick)
                    st.rerun()
            for p in d["pending"]:
                if not p["category"]:
                    st.caption(f"Sem categoria: {fmt_date(p['date'])} · {p['description']} ({p['reason']}). Classifique em *A identificar* ou edite a parcela.")
    review = dre_mod.tx_for_competence_review(conn, month)
    if review:
        with st.expander(f"Revisar competência de lançamentos históricos ({len(review)})"):
            st.caption("Estes lançamentos entram na DRE pelo mês do banco. Se pertencem a outro mês, mude a competência.")
            for r in review[:30]:
                a, b, c = st.columns([5, 2, 1])
                a.write(f"{fmt_date(r['posted_date'])} · {r['description'][:50]} · {format_brl(r['amount_cents'], sign=True)}")
                new = b.text_input("Competência", value=month, key=f"cp_{r['id']}", label_visibility="collapsed")
                if c.button("Salvar", key=f"cps_{r['id']}"):
                    try:
                        dre_mod.set_tx_competence(conn, r["id"], new)
                    except LedgerError as e:
                        st.error(str(e))
                    else:
                        st.rerun()
