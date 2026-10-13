"""Caixa QCM — interface Streamlit. Toda a regra de negócio vive em caixa/."""
from datetime import date

import pandas as pd
import streamlit as st

from caixa import connections, recon
from caixa.config import data_dir
from caixa.contasimples import (CARD_ACCOUNT_NAME, ContaSimplesClient, ContaSimplesError, ensure_accounts,
                                reconcile_card_limits, sync_contasimples)
from caixa.db import KINDS, connect, create_account, list_accounts, update_account_opening
from caixa.export import backup_database, classified_csv
from caixa.importer import CONFLITO, DUPLICADO, NOVO, POSSIVEL, commit_import, plan_import
from caixa.ledger import (LedgerError, account_summary, classify, clear_classification, company_breakdown,
                          delete_investment_position, distinct_values, investment_summary, link_transfer,
                          list_ignored, list_positions, list_unclassified, unignore_tx, set_investment_position, suggest_opening_from_statement,
                          suggest_transfer_matches, unlink_transfer)
from caixa.money import cents_to_plain, format_brl, parse_amount_to_cents
from caixa.ofx import OfxError
from caixa.rotina import daily_status, last_sync_ok
from caixa.timeutil import fmt_date, now_sp, today_sp

st.set_page_config(page_title="Caixa QCM", page_icon="💰", layout="wide")
conn = connect()

PAGES = ["Painel", "Rotina diária", "Importar extrato", "A identificar", "Investigar divergências", "Contas", "Exportar e backup"]


def flash(msg: str, kind: str = "success"):
    st.session_state["_flash"] = (kind, msg)


def show_flash():
    if "_flash" in st.session_state:
        kind, msg = st.session_state.pop("_flash")
        getattr(st, kind)(msg)


def kind_label(k: str | None) -> str:
    return KINDS.get(k, "— sem classificação —") if k else "— sem classificação —"


def combo(label: str, column: str, current: str = "", key: str | None = None) -> str:
    """Seleciona um valor já usado ou digita um novo."""
    options = distinct_values(conn, column)
    if current and current not in options:
        options = [current] + options
    value = st.selectbox(label, options, index=options.index(current) if current in options else None,
                         accept_new_options=True, placeholder="Escolha ou digite um novo", key=key)
    return (value or "").strip()


def accounts_or_stop():
    accs = list_accounts(conn)
    if not accs:
        st.info("Cadastre as contas primeiro, na página **Contas**.")
        st.stop()
    return accs


# ------------------------------------------------------------------------ Painel

def page_painel():
    st.header("Painel")
    show_flash()
    accs = accounts_or_stop()
    as_of = st.date_input("Saldo calculado até", value=today_sp(), format="DD/MM/YYYY")
    as_of_iso = as_of.isoformat()

    st.subheader("Saldo por conta")
    st.caption("Fórmula: **saldo inicial + entradas − saídas = saldo calculado** "
               "(considera lançamentos a partir da data do saldo inicial, inclusive).")
    total_calc = 0
    sums = []
    for a in accs:
        s = account_summary(conn, a["id"], as_of_iso)
        sums.append(s)
        total_calc += s["caixa_total_cents"]
        with st.container(border=True):
            st.markdown(f"**{s['name']}**  ·  saldo inicial em {fmt_date(s['opening_date'])}"
                        + ("  ·  **limite dos cartões = caixa**" if s["name"] == CARD_ACCOUNT_NAME else ""))
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Saldo inicial", format_brl(s["opening_cents"]))
            c2.metric("+ Entradas", format_brl(s["entradas_cents"]))
            c3.metric("− Saídas", format_brl(s["saidas_cents"]))
            c4.metric("= Saldo disponível calculado", format_brl(s["calculated_cents"]))
            if s["pending_cents"]:
                pc = st.columns(3)
                pc[0].metric("Saldo lançado", format_brl(s["calculated_cents"]))
                pc[1].metric("− Compras pendentes", format_brl(s["pending_cents"]),
                             help="Ainda não lançadas no extrato, mas já reduzem o limite disponível.")
                pc[2].metric("= Disponível (caixa)", format_brl(s["calculated_cents"] + s["pending_cents"]))
            if s["n_investimento"] or s["cdi_cents"]:
                cdi = st.columns(3)
                cdi[0].metric("+ Aplicado em CDI", format_brl(s["cdi_cents"]),
                              help=f"Posição informada em {fmt_date(s['cdi_date'])}" if s["cdi_date"] else "Nenhuma posição informada")
                cdi[1].metric("= Caixa total da conta", format_brl(s["caixa_total_cents"]))
                inv = investment_summary(conn, a["id"], as_of_iso)
                if inv["rendimento_cents"] is not None:
                    cdi[2].metric("Rendimento estimado do CDI", format_brl(inv["rendimento_cents"], sign=True),
                                  help=f"Entre {fmt_date(inv['from'])} e {fmt_date(inv['to'])}: posição final − inicial − aplicações + resgates")
                if not s["cdi_date"]:
                    st.warning("Há movimentos de CDI nesta conta, mas nenhuma posição aplicada foi informada: o caixa total "
                               "está sem o CDI. Informe em **Contas → posição em CDI**.")
            comp = [c for c in recon.statement_comparisons(conn, a["id"]) if c["date"] <= as_of_iso and c["diff_cents"] is not None]
            if comp:
                last = comp[-1]
                msg = (f"Extrato em {fmt_date(last['date'])}: {format_brl(last['statement_cents'])} · calculado nessa data: "
                       f"{format_brl(last['calculated_cents'])} · ")
                if last["diff_cents"] == 0:
                    st.success(msg + "**confere**")
                else:
                    st.error(msg + f"**diferença de {format_brl(last['diff_cents'], sign=True)}** (calculado − extrato)")
            else:
                st.caption("Nenhum saldo de extrato disponível para comparar.")
            if s["n_unclassified"]:
                st.caption(f"⚠️ {s['n_unclassified']} movimentações sem classificação nesta conta.")
            if s["n_before_opening"]:
                st.caption(f"ℹ️ {s['n_before_opening']} lançamentos anteriores à data do saldo inicial foram ignorados no cálculo.")

    t1, t2, t3 = st.columns(3)
    t1.metric("Saldo total (todas as contas)", format_brl(total_calc))
    t2.metric("Entradas (todas as contas)", format_brl(sum(s["entradas_cents"] for s in sums)))
    t3.metric("Saídas (todas as contas)", format_brl(sum(s["saidas_cents"] for s in sums)))
    st.caption("Saldo total = saldo disponível + CDI de cada conta. Entradas e saídas incluem transferências e "
               "aplicações/resgates do CDI (movimentos internos que se anulam). "
               "Os cartões da Conta Simples são de débito: o limite vem do saldo da conta, então a conta "
               "'Conta Simples (cartões)' conta como caixa.")

    n_unc = sum(s["n_unclassified"] for s in sums)
    counts = recon.investigation_counts(conn)
    c1, c2 = st.columns(2)
    c1.metric("Movimentações sem classificação", n_unc)
    c2.metric("Pontos a investigar", sum(counts.values()), help=str(counts))

    st.subheader("Empresa no período")
    st.caption("Visão consolidada: transferências entre contas **não** contam como receita nem despesa; aportes e retiradas ficam à parte.")
    first_open = min(date.fromisoformat(a["opening_date"]) for a in accs)
    p1, p2 = st.columns(2)
    start = p1.date_input("De", value=first_open, format="DD/MM/YYYY")
    end = p2.date_input("Até", value=as_of, format="DD/MM/YYYY")
    co = company_breakdown(conn, start.isoformat(), end.isoformat())
    m = st.columns(5)
    m[0].metric("Entradas da empresa", format_brl(co["entradas_empresa_cents"]))
    m[1].metric("Saídas da empresa", format_brl(co["saidas_empresa_cents"]))
    m[2].metric("Resultado", format_brl(co["resultado_cents"], sign=True), help="receitas + despesas + impostos + tarifas + estornos")
    m[3].metric("Sócios (aportes − retiradas)", format_brl(co["socios_cents"], sign=True))
    m[4].metric("Sem classificação", format_brl(co["nao_classificado_cents"], sign=True))
    if co["by_kind"]:
        st.dataframe(pd.DataFrame([
            {"Tipo": kind_label(k if k != "__nao_classificado" else None), "Lançamentos": v["n"], "Total": format_brl(v["total_cents"], sign=True)}
            for k, v in co["by_kind"].items()]), hide_index=True, use_container_width=True)

    st.subheader("Divergência de conciliação por data")
    comps = recon.statement_comparisons(conn)
    if comps:
        st.dataframe(pd.DataFrame([{
            "Conta": c["account_name"], "Data do extrato": fmt_date(c["date"]),
            "Saldo do extrato": format_brl(c["statement_cents"]), "Saldo calculado": format_brl(c["calculated_cents"]),
            "Diferença": format_brl(c["diff_cents"], sign=True), "Situação": c["status"]} for c in comps]),
            hide_index=True, use_container_width=True)
    else:
        st.caption("Importe extratos com saldo (LEDGERBAL) para comparar.")


# ------------------------------------------------------------------------ Importar

def page_importar():
    st.header("Importar extrato OFX")
    show_flash()
    accs = accounts_or_stop()
    acc = st.selectbox("Conta", accs, format_func=lambda a: a["name"])
    files = st.file_uploader("Arquivos .ofx", type=["ofx", "qfx"], accept_multiple_files=True)
    if not files:
        st.caption("Reimportar o mesmo arquivo ou períodos sobrepostos é seguro: o que já existe é reconhecido e ignorado.")
        return
    plans, allow_mismatch_needed = [], False
    for f in files:
        st.subheader(f.name)
        try:
            plan = plan_import(conn, acc["id"], f.name, f.getvalue())
        except OfxError as e:
            st.error(str(e))
            continue
        plans.append(plan)
        s = plan.stmt
        st.caption(f"Conta no arquivo: {s.acct_id or '—'} · período {fmt_date(s.period_start)} a {fmt_date(s.period_end)}"
                   + (f" · saldo do extrato {format_brl(s.ledger_balance_cents)} em {fmt_date(s.ledger_balance_date)}"
                      if s.ledger_balance_cents is not None else " · sem saldo no extrato"))
        c = st.columns(4)
        c[0].metric("Novos", plan.count(NOVO))
        c[1].metric("Já importados", plan.count(DUPLICADO))
        c[2].metric("Possível duplicata", plan.count(POSSIVEL))
        c[3].metric("FITID conflitante", plan.count(CONFLITO))
        if plan.file_seen_before:
            st.info("Este mesmo arquivo já foi importado antes; nada será duplicado.")
        if plan.account_mismatch:
            allow_mismatch_needed = True
            st.error("O número da conta neste arquivo é diferente do que já foi importado para esta conta. "
                     "Confira se você escolheu a conta certa.")
        if plan.count(POSSIVEL) or plan.count(CONFLITO):
            st.warning("Linhas sinalizadas serão importadas e ficam em **Investigar divergências → Duplicatas** para você decidir.")
        st.dataframe(pd.DataFrame([{
            "Situação": r.status, "Data": fmt_date(r.tx.posted_date), "Descrição": r.tx.description,
            "Valor": format_brl(r.tx.amount_cents, sign=True), "FITID": r.tx.fitid or "—", "Obs.": r.note} for r in plan.rows]),
            hide_index=True, use_container_width=True)

    if plans:
        ok = True
        if allow_mismatch_needed:
            ok = st.checkbox("Sei que a conta do arquivo é diferente e quero importar mesmo assim")
        if st.button("Confirmar importação", type="primary", disabled=not ok):
            res = [commit_import(conn, p) for p in plans]
            flash(f"Importação concluída: {sum(r['novos'] for r in res)} novos, "
                  f"{sum(r['duplicados'] for r in res)} já existentes ignorados, {sum(r['sinalizados'] for r in res)} sinalizados.")
            st.rerun()


# ------------------------------------------------------------------------ A identificar

def tx_card(tx):
    st.markdown(f"**{fmt_date(tx['posted_date'])}** · {tx['account_name']} · `{format_brl(tx['amount_cents'], sign=True)}`")
    st.write(tx["description"])


def page_identificar():
    st.header("A identificar")
    show_flash()
    accs = accounts_or_stop()
    f1, f2 = st.columns([1, 2])
    acc_filter = f1.selectbox("Conta", [None] + list(accs), format_func=lambda a: "Todas" if a is None else a["name"])
    text = f2.text_input("Buscar na descrição")
    rows = list_unclassified(conn, acc_filter["id"] if acc_filter else None)
    if text:
        rows = [r for r in rows if text.lower() in r["description"].lower()]
    if not rows:
        st.success("Nada a identificar. 🎉")
        return
    st.caption(f"{len(rows)} movimentações. Selecione uma linha para classificar (ou várias, para aplicar o mesmo tipo/categoria).")
    nonce = st.session_state.setdefault("ident_nonce", 0)
    df = pd.DataFrame([{
        "Data": fmt_date(r["posted_date"]), "Conta": r["account_name"], "Descrição": r["description"],
        "Valor": format_brl(r["amount_cents"], sign=True), "⚠": "possível duplicata" if r["dup_open"] else ""} for r in rows])
    ev = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun",
                      selection_mode="multi-row", key=f"ident_{nonce}")
    sel = [rows[i] for i in ev.selection.rows]
    if not sel:
        return
    st.divider()
    if len(sel) == 1:
        classify_one(sel[0])
    else:
        classify_many(sel)


def classify_one(tx):
    tx_card(tx)
    tid = tx["id"]
    kind = st.selectbox("Tipo", list(KINDS), format_func=KINDS.get, index=None, placeholder="Escolha o tipo", key=f"kind_{tid}")
    if not kind:
        return
    pair_id = None
    if kind == "transferencia":
        cands = suggest_transfer_matches(conn, tid)
        opts = [None] + cands
        pair = st.selectbox(
            "Vincular à outra ponta", opts, key=f"pair_{tid}",
            format_func=lambda c: "Deixar sem par por enquanto" if c is None else
            f"{c['account_name']} · {fmt_date(c['posted_date'])} · {format_brl(c['amount_cents'], sign=True)} · {c['description']}"
            + ("" if c["value_diff_cents"] == 0 else f"  (diferença {format_brl(c['value_diff_cents'])})"))
        pair_id = pair["id"] if pair else None
        if not cands:
            st.caption("Nenhum candidato na outra conta (±3 dias). Se o outro extrato ainda não foi importado, importe-o e vincule depois em Investigar.")
        st.caption("Transferência altera os saldos das contas, mas não conta como receita nem despesa.")
    with st.form(f"form_{tid}"):
        cp = combo("Cliente / fornecedor", "counterparty", key=f"cp_{tid}")
        c1, c2 = st.columns(2)
        with c1:
            cat = combo("Categoria", "category", key=f"cat_{tid}")
        with c2:
            cc = combo("Centro de custo", "cost_center", key=f"cc_{tid}")
        note = st.text_area("Descrição livre", key=f"note_{tid}")
        att = st.file_uploader("Anexo (opcional)", key=f"att_{tid}")
        if st.form_submit_button("Salvar classificação", type="primary"):
            try:
                classify(conn, tid, kind, counterparty=cp, category=cat, cost_center=cc, note=note,
                         attachment=(att.name, att.getvalue()) if att else None)
                if pair_id:
                    out_id, in_id = (tid, pair_id) if tx["amount_cents"] < 0 else (pair_id, tid)
                    link_transfer(conn, out_id, in_id)
            except LedgerError as e:
                st.error(str(e))
                return
            st.session_state["ident_nonce"] = st.session_state.get("ident_nonce", 0) + 1
            flash("Classificação salva.")
            st.rerun()


def classify_many(sel):
    st.markdown(f"**{len(sel)} movimentações selecionadas** · soma {format_brl(sum(t['amount_cents'] for t in sel), sign=True)}")
    with st.form("bulk"):
        kind = st.selectbox("Tipo", [k for k in KINDS if k != "transferencia"], format_func=KINDS.get, index=None)
        cp = combo("Cliente / fornecedor", "counterparty", key="bulk_cp")
        c1, c2 = st.columns(2)
        with c1:
            cat = combo("Categoria", "category", key="bulk_cat")
        with c2:
            cc = combo("Centro de custo", "cost_center", key="bulk_cc")
        note = st.text_area("Descrição livre")
        st.caption("Transferências precisam ser vinculadas uma a uma. Lançamentos cujo sinal não combina com o tipo são recusados.")
        if st.form_submit_button("Aplicar às selecionadas", type="primary"):
            if not kind:
                st.error("Escolha o tipo.")
                return
            errors, done = [], 0
            for t in sel:
                try:
                    classify(conn, t["id"], kind, counterparty=cp, category=cat, cost_center=cc, note=note)
                    done += 1
                except LedgerError as e:
                    errors.append(f"{fmt_date(t['posted_date'])} {t['description']}: {e}")
            st.session_state["ident_nonce"] = st.session_state.get("ident_nonce", 0) + 1
            flash(f"{done} classificadas." + (" Recusadas:\n- " + "\n- ".join(errors) if errors else ""),
                  "warning" if errors else "success")
            st.rerun()


# ------------------------------------------------------------------------ Investigar

def page_investigar():
    st.header("Investigar divergências")
    show_flash()
    accounts_or_stop()
    counts = recon.investigation_counts(conn)
    ignored = list_ignored(conn)
    tabs = st.tabs([f"Duplicatas ({counts['duplicatas']})", f"Transferências ({counts['transferencias_sem_par']} sem par)",
                    f"Saldos × extrato ({counts['saldos_divergentes']})", f"Períodos sem extrato ({counts['lacunas']})",
                    f"Ignorados ({len(ignored)})"])
    with tabs[0]:
        tab_duplicatas()
    with tabs[1]:
        tab_transferencias()
    with tabs[2]:
        tab_saldos()
    with tabs[3]:
        tab_periodos()
    with tabs[4]:
        tab_ignorados(ignored)


def tab_duplicatas():
    open_ = recon.possible_duplicates(conn, "open")
    if not open_:
        st.success("Nenhuma possível duplicata em aberto.")
    for d in open_:
        with st.container(border=True):
            st.markdown(f"**{d['account_name']}** · {d['reason']}")
            c1, c2 = st.columns(2)
            c1.markdown(f"Novo: #{d['tx_id']} · {fmt_date(d['posted_date'])} · {d['description']} · `{format_brl(d['amount_cents'], sign=True)}`")
            c2.markdown(f"Já existia: #{d['other_id']} · {fmt_date(d['other_date'])} · {d['other_description']} · `{format_brl(d['other_amount_cents'], sign=True)}`")
            b1, b2, _ = st.columns([1, 1, 2])
            if b1.button("É duplicata", key=f"dupy_{d['flag_id']}", help="Sai dos saldos; o original continua guardado"):
                act(lambda: recon.resolve_duplicate(conn, d["flag_id"], "confirmed"), "Marcada como duplicata (fora dos saldos).")
            if b2.button("Não é duplicata", key=f"dupn_{d['flag_id']}", help="São dois lançamentos reais (ex.: duas cobranças iguais)"):
                act(lambda: recon.resolve_duplicate(conn, d["flag_id"], "dismissed"), "Mantida como lançamento real.")
    decided = recon.possible_duplicates(conn, "confirmed") + recon.possible_duplicates(conn, "dismissed")
    if decided:
        with st.expander(f"Já decididas ({len(decided)})"):
            for d in decided:
                c1, c2 = st.columns([4, 1])
                c1.write(f"{'🗑 duplicata' if d['status'] == 'confirmed' else '✔ real'} · #{d['tx_id']} {fmt_date(d['posted_date'])} "
                         f"{d['description']} {format_brl(d['amount_cents'], sign=True)}")
                if c2.button("Reabrir", key=f"reopen_{d['flag_id']}"):
                    act(lambda: recon.resolve_duplicate(conn, d["flag_id"], "open"), "Reaberta.")


def act(fn, ok_msg: str):
    try:
        fn()
    except LedgerError as e:
        st.error(str(e))
        return
    flash(ok_msg)
    st.rerun()


def tab_transferencias():
    unpaired = recon.unpaired_transfers(conn)
    st.markdown("**Transferências sem par**")
    if not unpaired:
        st.success("Todas as transferências classificadas estão vinculadas.")
    for u in unpaired:
        with st.container(border=True):
            st.markdown(f"#{u['id']} · {u['account_name']} · {fmt_date(u['posted_date'])} · {u['description']} · `{format_brl(u['amount_cents'], sign=True)}`")
            cands = suggest_transfer_matches(conn, u["id"])
            if not cands:
                st.caption("Sem candidato na outra conta (±3 dias). Importe o extrato da outra conta ou confira o período.")
                b = st.button("Remover classificação", key=f"unc_{u['id']}")
                if b:
                    act(lambda: clear_classification(conn, u["id"]), "Classificação removida; voltou para A identificar.")
                continue
            pick = st.selectbox("Possível par", cands, key=f"cand_{u['id']}", format_func=lambda c: (
                f"{c['account_name']} · {fmt_date(c['posted_date'])} · {format_brl(c['amount_cents'], sign=True)} · {c['description']}"
                + ("" if c["value_diff_cents"] == 0 else f"  (diferença {format_brl(c['value_diff_cents'])})")))
            if st.button("Vincular", key=f"lnk_{u['id']}"):
                o, i = (u["id"], pick["id"]) if u["amount_cents"] < 0 else (pick["id"], u["id"])
                act(lambda: link_transfer(conn, o, i), "Transferência vinculada.")
    st.markdown("**Transferências vinculadas**")
    pairs = recon.transfer_pairs(conn)
    if not pairs:
        st.caption("Nenhuma ainda.")
    for p in pairs:
        c1, c2 = st.columns([5, 1])
        diff = "" if p["diff_cents"] == 0 else f" · ⚠️ diferença de {format_brl(p['diff_cents'])} (tarifa ou valor divergente)"
        c1.write(f"{p['out_account']} {fmt_date(p['out_date'])} {format_brl(p['out_cents'])} → "
                 f"{p['in_account']} {fmt_date(p['in_date'])} {format_brl(p['in_cents'], sign=True)}{diff}")
        if c2.button("Desvincular", key=f"unl_{p['link_id']}"):
            act(lambda: unlink_transfer(conn, p["link_id"]), "Vínculo desfeito; as duas pontas voltaram para A identificar.")


def tab_saldos():
    comps = recon.statement_comparisons(conn)
    bad = [c for c in comps if c["status"] == "divergente"]
    if not comps:
        st.info("Nenhum saldo de extrato importado.")
        return
    if not bad:
        st.success("Todos os saldos de extrato conferem com o calculado.")
    for c in bad:
        with st.container(border=True):
            st.markdown(f"**{c['account_name']}** em {fmt_date(c['date'])}: extrato {format_brl(c['statement_cents'])} × "
                        f"calculado {format_brl(c['calculated_cents'])} → **diferença {format_brl(c['diff_cents'], sign=True)}**")
            if c["hint"]:
                st.caption("Pistas: " + c["hint"])
            else:
                st.caption("Nenhum lançamento isolado explica a diferença. Verifique períodos sem extrato e o saldo inicial.")
    ok = [c for c in comps if c["status"] != "divergente"]
    if ok:
        with st.expander("Conferem / não comparáveis"):
            st.dataframe(pd.DataFrame([{"Conta": c["account_name"], "Data": fmt_date(c["date"]),
                                        "Extrato": format_brl(c["statement_cents"]), "Situação": c["status"]} for c in ok]),
                         hide_index=True, use_container_width=True)


def tab_ignorados(ignored):
    st.caption("Lançamentos que **não são dinheiro novo** (ex.: pagamento de fatura de cartão pago com o limite já depositado). "
               "Continuam guardados, mas ficam fora dos saldos. Restaurar devolve o lançamento aos saldos.")
    if not ignored:
        st.success("Nenhum lançamento ignorado.")
    for r in ignored:
        c1, c2 = st.columns([6, 1])
        c1.write(f"{r['account_name']} · {fmt_date(r['posted_date'])} · {format_brl(r['amount_cents'], sign=True)} · "
                 f"{r['description']}\n\n_{r['reason']}_")
        if c2.button("Restaurar", key=f"unign_{r['id']}"):
            act(lambda: unignore_tx(conn, r["id"]), "Lançamento restaurado nos saldos.")


def tab_periodos():
    for c in recon.coverage(conn):
        with st.container(border=True):
            st.markdown(f"**{c['account_name']}**")
            if c["no_statement"]:
                st.warning("Nenhum extrato importado para esta conta.")
                continue
            st.write("Coberto: " + "; ".join(f"{fmt_date(a)} a {fmt_date(b)}" for a, b in c["covered"]))
            if c["gaps"]:
                st.error("Períodos sem extrato: " + "; ".join(f"{fmt_date(a)} a {fmt_date(b)}" for a, b in c["gaps"]))
            else:
                st.success("Sem lacunas entre o saldo inicial e o último extrato.")
            if c["stale_days"]:
                st.caption(f"Último extrato vai até {fmt_date(c['last_end'])} ({c['stale_days']} dias atrás).")


# ------------------------------------------------------------------------ Contas

def new_accounts_form():
    st.info("Informe o saldo inicial de cada conta. O saldo vale no **início** do dia informado.")
    with st.form("new_accounts"):
        vals = {}
        for name in ("PagBank", "Conta Simples"):
            c1, c2 = st.columns(2)
            bal = c1.text_input(f"{name} — saldo inicial (R$)", value="0,00", key=f"bal_{name}")
            dt = c2.date_input(f"{name} — data do saldo", value=today_sp().replace(day=1), format="DD/MM/YYYY", key=f"dt_{name}")
            vals[name] = (bal, dt)
        if st.form_submit_button("Criar contas", type="primary"):
            try:
                cents = {name: parse_amount_to_cents(bal) for name, (bal, _) in vals.items()}  # valida tudo antes de gravar
                for name, (_, dt) in vals.items():
                    create_account(conn, name, cents[name], dt.isoformat())
            except ValueError as e:
                st.error(str(e))
                return
            flash("Contas criadas.")
            st.rerun()


def account_extras(a: dict):
    """Sugestão de saldo inicial pelo extrato e posição aplicada em CDI de uma conta."""
    if a["name"] == CARD_ACCOUNT_NAME:
        st.info("**Limite disponível dos cartões = caixa.** Nos cartões da Conta Simples o limite é um depósito: o dinheiro sai "
                "da conta (\"Transferência de limite\"), entra no cartão (pareado automaticamente como transferência) e as compras "
                "consomem esse limite. O pagamento da fatura só liquida compras que já abateram o limite, então **não** é "
                "dinheiro novo (aparece em *Investigar → Ignorados*). Limite disponível = depósitos − compras − compras "
                "pendentes. Se já havia limite antes da data inicial, coloque-o como saldo inicial desta conta.")
    sug = suggest_opening_from_statement(conn, a["id"])
    if sug and (sug["opening_cents"] != a["opening_balance_cents"] or sug["opening_date"] != a["opening_date"]):
        with st.container(border=True):
            st.markdown(
                f"💡 **Sugestão pelo extrato:** o saldo do extrato em {fmt_date(sug['statement_date'])} é "
                f"{format_brl(sug['statement_cents'])}. Para o saldo calculado bater, o saldo **disponível** no início de "
                f"{fmt_date(sug['opening_date'])} (dia do 1º lançamento importado) seria **{format_brl(sug['opening_cents'])}**.")
            st.caption("Vale só para o saldo disponível: o que está aplicado em CDI entra à parte, em *posição em CDI*.")
            if st.button("Aplicar esta sugestão", key=f"sug_{a['id']}"):
                update_account_opening(conn, a["id"], sug["opening_cents"], sug["opening_date"])
                flash(f"Saldo inicial de {a['name']} ajustado para {format_brl(sug['opening_cents'])} em {fmt_date(sug['opening_date'])}.")
                st.rerun()
    with st.expander(f"Posição em CDI / renda fixa — {a['name']}"):
        st.caption("O extrato OFX mostra só o saldo disponível. Informe aqui quanto está **aplicado** (total no CDI, com "
                   "rendimento, como aparece no app do banco) ao fim de um dia. Uma posição por dia; atualize quando quiser. "
                   "Com duas ou mais posições o app estima o rendimento.")
        with st.form(f"pos_{a['id']}"):
            c1, c2 = st.columns(2)
            val = c1.text_input("Total aplicado (R$)", key=f"posv_{a['id']}")
            d = c2.date_input("Posição ao fim do dia", value=today_sp(), format="DD/MM/YYYY", key=f"posd_{a['id']}")
            if st.form_submit_button("Registrar posição"):
                try:
                    set_investment_position(conn, a["id"], d.isoformat(), parse_amount_to_cents(val))
                except (ValueError, LedgerError) as e:
                    st.error(str(e))
                else:
                    flash("Posição em CDI registrada.")
                    st.rerun()
        for p in list_positions(conn, a["id"]):
            c1, c2 = st.columns([5, 1])
            c1.write(f"{fmt_date(p['as_of_date'])}: {format_brl(p['amount_cents'])}")
            if c2.button("Remover", key=f"rmpos_{p['id']}"):
                delete_investment_position(conn, p["id"])
                st.rerun()


def page_contas():
    st.header("Contas")
    show_flash()
    accs = list_accounts(conn)
    if not accs:
        new_accounts_form()
        return
    for a in accs:
        with st.form(f"acc_{a['id']}"):
            st.markdown(f"**{a['name']}**" + (f" · conta no OFX: {a['ofx_acct_id']}" if a["ofx_acct_id"] else ""))
            c1, c2 = st.columns(2)
            bal = c1.text_input("Saldo inicial (R$)", value=cents_to_plain(a["opening_balance_cents"]))
            dt = c2.date_input("Data do saldo (início do dia)", value=date.fromisoformat(a["opening_date"]), format="DD/MM/YYYY")
            if st.form_submit_button("Atualizar"):
                try:
                    update_account_opening(conn, a["id"], parse_amount_to_cents(bal), dt.isoformat())
                except ValueError as e:
                    st.error(str(e))
                    return
                flash("Conta atualizada.")
                st.rerun()
        account_extras(a)


# ------------------------------------------------------------------------ Exportar

def page_exportar():
    st.header("Exportar e backup")
    show_flash()
    n = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    st.subheader("Lançamentos classificados (CSV)")
    st.caption(f"{n} lançamentos classificados. Separador ';', vírgula decimal, UTF-8 — abre direto no Excel.")
    st.download_button("Baixar CSV", classified_csv(conn), file_name=f"lancamentos_classificados_{today_sp():%Y%m%d}.csv",
                       mime="text/csv", disabled=n == 0)
    st.subheader("Backup do banco")
    st.caption(f"Pasta de dados: {data_dir()}")
    if st.button("Criar backup agora"):
        path = backup_database(conn)
        st.session_state["last_backup"] = str(path)
    if "last_backup" in st.session_state:
        from pathlib import Path
        p = Path(st.session_state["last_backup"])
        if p.exists():
            st.success(f"Backup salvo em {p}")
            st.download_button("Baixar este backup", p.read_bytes(), file_name=p.name, mime="application/octet-stream")


# ------------------------------------------------------------------------ Rotina diária

def banner():
    """Aviso no topo de todas as páginas: o que ainda não está atualizado hoje."""
    if not list_accounts(conn):
        return
    late = [i for i in daily_status(conn) if not i["ok"]]
    if late:
        st.warning("**Atualização pendente hoje:** " + " · ".join(f"{i['account']}: {i['detail']}" for i in late))


def run_contasimples_sync(full: bool = False) -> tuple[bool, str]:
    cfg = connections.get_contasimples()
    if not cfg:
        return False, "Credenciais da Conta Simples não configuradas."
    try:
        r = sync_contasimples(conn, ContaSimplesClient(cfg["api_key"], cfg["api_secret"], cfg.get("env", "production")), full=full)
    except (ContaSimplesError, OSError) as e:
        return False, str(e)
    msg = f"{r.novos} novos, {r.duplicados} já existentes."
    if r.saldo_api_cents is not None:
        msg += f" Saldo na API: {format_brl(r.saldo_api_cents)}."
    if r.divergencia_cents:
        msg += f" ⚠️ O saldo calculado difere do saldo da API em {format_brl(r.divergencia_cents, sign=True)}."
    if r.limites_bloqueados:
        msg += (f" ⚠️ {r.limites_bloqueados} cargas de limite dos cartões não foram pareadas com a saída da conta porque já têm "
                "outra classificação sua; use *Converter cargas de limite em transferências* em Rotina diária.")
    if r.sem_direcao:
        msg += (f" ⚠️ {len(r.sem_direcao)} movimentações sem direção (entrada/saída) conhecida não foram importadas: "
                + "; ".join(r.sem_direcao[:5]))
    return True, msg


def page_rotina():
    st.header("Rotina diária")
    show_flash()
    if not list_accounts(conn):
        new_accounts_form()   # a rotina precisa das contas (e do saldo inicial); cadastra aqui mesmo
    st.caption("Meta de todo dia: **PagBank** com o OFX importado até ontem (feito por você) e **Conta Simples** "
               "sincronizada pela API (automático).")
    for i in daily_status(conn):
        (st.success if i["ok"] else st.error)(
            f"**{i['account']}** ({'API' if i['mode'] == 'api' else 'OFX manual'}): {i['detail']}")

    st.subheader("PagBank — extrato OFX (manual, todo dia)")
    st.markdown("1. No app/site do PagBank: **Extrato → Exportar → OFX** (período desde o último dia importado).\n"
                "2. Aqui, **Importar extrato** → escolha a conta PagBank → envie o arquivo → confirme.\n\n"
                "Pode reimportar períodos sobrepostos sem medo: o que já existe é ignorado. "
                "O lembrete do Windows (abaixo) avisa quando faltar.")

    st.subheader("Conta Simples — API (automático)")
    cfg = connections.get_contasimples()
    with st.form("cs_cred"):
        st.caption("Gere a credencial em ib.contasimples.com → Integrações → API → Credenciais, marcando **somente escopos "
                   "de leitura** (extrato bancário, extrato de cartão, saldo). Credencial com escopo de escrita exige "
                   "assinatura de requisições, que este app não faz.")
        key = st.text_input("API key", value=cfg["api_key"] if cfg else "")
        secret = st.text_input("API secret", value=cfg["api_secret"] if cfg else "", type="password")
        env = st.selectbox("Ambiente", ["production", "sandbox"],
                           index=1 if cfg and cfg.get("env") == "sandbox" else 0,
                           format_func=lambda e: "Produção" if e == "production" else "Sandbox (respostas fictícias fixas)")
        if st.form_submit_button("Salvar credenciais"):
            if not key.strip() or not secret.strip():
                st.error("Informe API key e secret.")
            else:
                connections.set_contasimples(key, secret, env)
                flash("Credenciais salvas em data/conexoes.json (texto simples, só neste computador).")
                st.rerun()
    sandbox = bool(cfg) and cfg.get("env") == "sandbox"
    if sandbox:
        st.warning("Credencial de **Sandbox**: serve só para testar a conexão (devolve dados fictícios fixos, que não são "
                   "importados). Para sincronizar de verdade é preciso credencial de **produção**, que a Conta Simples "
                   "libera após validar a integração (veja abaixo). Enquanto isso, importe o OFX da Conta Simples "
                   "normalmente em *Importar extrato*.")
    c1, c2, c3 = st.columns(3)
    if c1.button("Testar conexão", disabled=not cfg):
        try:
            cl = ContaSimplesClient(cfg["api_key"], cfg["api_secret"], cfg.get("env", "production"))
            st.success(f"Conexão ok. Saldo disponível na Conta Simples: {format_brl(cl.balance_cents())}")
        except (ContaSimplesError, OSError) as e:
            st.error(str(e))
    if c2.button("Sincronizar agora", type="primary", disabled=not cfg or sandbox):
        with st.spinner("Sincronizando..."):
            ok, msg = run_contasimples_sync()
        flash(msg, "success" if ok else "error")
        st.rerun()
    if st.button("Ressincronizar tudo desde a data inicial", disabled=not cfg or sandbox,
                 help="Refaz o histórico completo (seguro: não duplica). Use depois de uma atualização do app."):
        with st.spinner("Ressincronizando o histórico..."):
            ok, msg = run_contasimples_sync(full=True)
        flash(msg, "success" if ok else "error")
        st.rerun()
    if c3.button("Remover credenciais", disabled=not cfg):
        connections.clear_contasimples()
        flash("Credenciais removidas.")
        st.rerun()
    runs = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 10").fetchall()
    if runs:
        st.dataframe(pd.DataFrame([{"Início": r["started_at"][:16].replace("T", " "), "Situação": r["status"],
                                    "Novos": r["n_new"], "Resultado": r["message"] or ""} for r in runs]),
                     hide_index=True, use_container_width=True)

    with st.expander("Como conseguir a credencial de produção"):
        st.markdown("A Conta Simples libera produção depois de: (1) integração validada no Sandbox, (2) checklist de "
                    "segurança e boas práticas aprovado, (3) termos de uso e privacidade aceitos; então as credenciais de "
                    "produção aparecem em ib.contasimples.com → Integrações → API → Credenciais. Se não aparecerem, peça "
                    "a liberação ao suporte da Conta Simples citando a API de extrato (somente leitura). Detalhes: "
                    "developers.contasimples.com → Ambientes.")
    st.subheader("Cartões da Conta Simples (débito = caixa)")
    st.markdown("O limite disponível dos cartões é dinheiro do caixa que você moveu da conta para o cartão: a conta "
                "**Conta Simples (cartões)** entra no saldo total pelo **limite disponível** (depósitos − compras − compras "
                "pendentes). A sincronização pareia sozinha cada carga de limite com a saída da conta (transferência: não é "
                "receita nem despesa) e ignora o pagamento da fatura, que não é dinheiro novo.")

    if st.button("Converter cargas de limite em transferências",
                 help="Para as cargas de limite que você já classificou como outro tipo (ex.: Ajuste/Investimento): troca a "
                      "classificação por Transferência e vincula conta ↔ cartão. A classificação antiga fica no histórico."):
        try:
            bank, card = ensure_accounts(conn)
            paired, ignored, blocked = reconcile_card_limits(conn, bank, card, today_sp(), force=True)
            flash(f"{paired} cargas de limite vinculadas como transferência; {ignored} pagamentos de fatura ignorados.")
        except (ContaSimplesError, LedgerError) as e:
            flash(str(e), "error")
        st.rerun()

    st.subheader("Lembrete e sincronização automática no Windows")
    st.markdown("Dê dois cliques em **`agendar_sincronizacao.bat`** (uma vez). Todo dia às 08:00 o Windows sincroniza a "
                "Conta Simples e mostra um aviso se faltar o OFX do PagBank. Se o PC estiver desligado nesse horário, "
                "roda assim que ligar. Além disso, ao abrir o app ele sincroniza sozinho quando a última sincronização "
                "tem mais de 6 horas.")


def autosync_on_open():
    """Ao abrir o app (1x por sessão): sincroniza a Conta Simples se a última sincronização passou de 6h."""
    if st.session_state.get("_autosync_done") or not connections.get_contasimples() or not list_accounts(conn):
        return
    st.session_state["_autosync_done"] = True
    last = last_sync_ok(conn)
    if last and (now_sp() - last).total_seconds() < 6 * 3600:
        return
    with st.spinner("Sincronizando Conta Simples..."):
        ok, msg = run_contasimples_sync()
    flash(("Conta Simples sincronizada. " if ok else "Falha ao sincronizar a Conta Simples: ") + msg,
          "success" if ok else "warning")


# ------------------------------------------------------------------------ Roteamento

def main():
    st.sidebar.title("💰 Caixa QCM")
    page = st.sidebar.radio("Menu", PAGES, label_visibility="collapsed")
    n_unc = conn.execute("SELECT COUNT(*) FROM effective_tx e WHERE NOT EXISTS (SELECT 1 FROM classifications c WHERE c.tx_id=e.id)").fetchone()[0]
    st.sidebar.caption(f"{n_unc} a identificar")
    autosync_on_open()
    if page != "Rotina diária":
        banner()
    {
        "Painel": page_painel, "Rotina diária": page_rotina, "Importar extrato": page_importar, "A identificar": page_identificar,
        "Investigar divergências": page_investigar, "Contas": page_contas, "Exportar e backup": page_exportar,
    }[page]()


if __name__ == "__main__":
    main()
