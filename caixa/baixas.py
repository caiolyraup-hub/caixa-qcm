"""Baixas: ligam lançamentos do extrato a parcelas (previsto) sem criar nenhum movimento bancário novo.

Regras de integridade (todas verificadas aqui, no servidor, não só na tela):
- um lançamento nunca é alocado além do seu valor, somando todas as parcelas;
- uma parcela não recebe baixa de pagamento acima do seu saldo em aberto, a menos que a ação seja explícita (allow_excess);
- taxa, desconto e diferença são ajustes sem movimento bancário e precisam de ação própria (diferença exige justificativa);
- desfazer preenche `undone_at`: o lançamento original e o histórico nunca são apagados.
"""
import re
import unicodedata
from datetime import date, timedelta

from .ledger import LedgerError
from .obrigacoes import get_installment, list_installments, log
from .timeutil import now_iso, today_sp

SETTLING = ("pagamento", "taxa", "desconto", "diferenca")
ADJUSTMENTS = ("taxa", "desconto", "diferenca")
NOT_LINKABLE_KINDS = ("transferencia", "investimento")     # movimentos internos nunca quitam obrigações
_STOP = {"pix", "ted", "doc", "recebido", "recebida", "enviado", "enviada", "transferencia", "pagamento", "via", "para",
         "de", "da", "do", "das", "dos", "ltda", "eireli", "cliente", "conta", "boleto", "compra", "cartao", "the", "and"}


# ------------------------------------------------------------------ lançamentos

def _tx_row(conn, tx_id: int):
    row = conn.execute("SELECT * FROM effective_tx WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise LedgerError("lançamento inexistente ou fora dos saldos (ignorado/duplicata)")
    return row


def tx_available_cents(conn, tx_id: int) -> int:
    """Quanto do lançamento ainda não está alocado a nenhuma parcela."""
    row = _tx_row(conn, tx_id)
    used = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM allocations WHERE tx_id=? AND undone_at IS NULL", (tx_id,)).fetchone()[0]
    return abs(row["amount_cents"]) - used


def tx_allocations(conn, tx_id: int, include_undone: bool = False) -> list[dict]:
    sql = ("SELECT a.*, i.description, i.counterparty, i.due_date FROM allocations a "
           "JOIN installments i ON i.id=a.installment_id WHERE a.tx_id=?")
    if not include_undone:
        sql += " AND a.undone_at IS NULL"
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.id", (tx_id,))]


def _tx_kind(conn, tx_id: int) -> str | None:
    r = conn.execute("SELECT kind FROM classifications WHERE tx_id=?", (tx_id,)).fetchone()
    return r["kind"] if r else None


# ------------------------------------------------------------------ alocação

def _allocate(conn, installment_id: int, tx_id: int | None, amount_cents: int, kind: str, note: str,
              allow_excess: bool) -> int:
    """Valida e grava sem commitar (quem chama controla a transação)."""
    if amount_cents <= 0:
        raise LedgerError("o valor da baixa deve ser maior que zero")
    inst = get_installment(conn, installment_id)
    if inst["status"] == "cancelada":
        raise LedgerError("parcela cancelada")
    direction = inst["direction"]

    if kind in ("pagamento", "estorno"):
        tx = _tx_row(conn, tx_id)
        if _tx_kind(conn, tx_id) in NOT_LINKABLE_KINDS:
            raise LedgerError("transferências e aplicações são movimentos internos e não quitam obrigações")
        incoming = tx["amount_cents"] > 0
        if (kind == "pagamento") != (incoming == (direction == "receber")):
            raise LedgerError("o sentido do lançamento não combina com a parcela "
                              f"({'entrada' if incoming else 'saída'} × {direction}) ")
        if amount_cents > tx_available_cents(conn, tx_id):
            raise LedgerError("a soma das alocações não pode passar do valor do lançamento bancário")
        if kind == "pagamento" and amount_cents > inst["open_cents"] and not allow_excess:
            raise LedgerError("o valor passa do saldo em aberto da parcela; use ajuste explícito (allow_excess) ou divida em outras parcelas")
        if kind == "estorno":
            paid = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN kind='pagamento' THEN amount_cents ELSE -amount_cents END),0) FROM allocations "
                "WHERE installment_id=? AND undone_at IS NULL AND kind IN ('pagamento','estorno')", (installment_id,)).fetchone()[0]
            if amount_cents > paid:
                raise LedgerError("o estorno passa do valor efetivamente pago nesta parcela")
    else:
        if tx_id is not None:
            raise LedgerError("ajustes não têm lançamento bancário")
        if kind not in ADJUSTMENTS:
            raise LedgerError("tipo de baixa inválido")
        if kind == "taxa" and direction != "receber":
            raise LedgerError("taxa retida só se aplica a valores a receber")
        if kind == "diferenca" and not note.strip():
            raise LedgerError("a baixa por diferença exige uma justificativa")
        if amount_cents > inst["open_cents"]:
            raise LedgerError("o ajuste passa do saldo em aberto da parcela")

    cur = conn.execute(
        "INSERT INTO allocations (installment_id, tx_id, kind, amount_cents, note, created_at) VALUES (?,?,?,?,?,?)",
        (installment_id, tx_id, kind, amount_cents, note.strip(), now_iso()))
    log(conn, "allocation", cur.lastrowid, "baixar", None,
        {"parcela": installment_id, "lancamento": tx_id, "tipo": kind, "valor": amount_cents, "obs": note.strip()})
    return cur.lastrowid


def allocate(conn, installment_id: int, tx_id: int, amount_cents: int, *, kind: str = "pagamento", note: str = "",
             allow_excess: bool = False) -> int:
    """Baixa (parcial ou total) de uma parcela com parte de um lançamento bancário."""
    with conn:
        return _allocate(conn, installment_id, tx_id, amount_cents, kind, note, allow_excess)


def allocate_many(conn, parts: list[tuple[int, int, int]], *, kind: str = "pagamento", note: str = "") -> list[int]:
    """Várias baixas atômicas: [(installment_id, tx_id, valor), ...]. Um lançamento para várias parcelas, várias
    parcelas para um lançamento ou vários lançamentos para uma parcela. Tudo ou nada."""
    with conn:
        return [_allocate(conn, i, t, v, kind, note, False) for i, t, v in parts]


def adjust(conn, installment_id: int, kind: str, amount_cents: int, note: str = "") -> int:
    """Taxa retida, desconto concedido ou baixa por diferença (explícita e justificada). Não mexe no caixa."""
    with conn:
        return _allocate(conn, installment_id, None, amount_cents, kind, note, False)


def undo(conn, allocation_id: int, note: str = "") -> None:
    """Desfaz a baixa: o lançamento volta a ficar disponível e a parcela volta a ter saldo em aberto."""
    row = conn.execute("SELECT * FROM allocations WHERE id=?", (allocation_id,)).fetchone()
    if row is None:
        raise LedgerError("baixa inexistente")
    if row["undone_at"]:
        raise LedgerError("esta baixa já foi desfeita")
    if row["kind"] == "pagamento":
        # desfazer um pagamento não pode deixar um estorno ativo sem pagamento que o sustente
        paid = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='pagamento' THEN amount_cents ELSE -amount_cents END),0) FROM allocations "
            "WHERE installment_id=? AND undone_at IS NULL AND kind IN ('pagamento','estorno') AND id<>?",
            (row["installment_id"], allocation_id)).fetchone()[0]
        if paid < 0:
            raise LedgerError("desfaça primeiro o estorno que depende deste pagamento")
    with conn:
        conn.execute("UPDATE allocations SET undone_at=? WHERE id=?", (now_iso(), allocation_id))
        log(conn, "allocation", allocation_id, "desfazer", {"parcela": row["installment_id"], "lancamento": row["tx_id"],
                                                            "tipo": row["kind"], "valor": row["amount_cents"]}, {"obs": note})


# ------------------------------------------------------------------ sugestões

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s)


def _tokens(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) >= 3 and w not in _STOP}


def suggest_for_tx(conn, tx_id: int, today: date | None = None) -> list[dict]:
    """Parcelas compatíveis com um lançamento (direção, valor, data, contraparte, conta), com a justificativa."""
    today = today or today_sp()
    tx = _tx_row(conn, tx_id)
    avail = tx_available_cents(conn, tx_id)
    if avail <= 0 or _tx_kind(conn, tx_id) in NOT_LINKABLE_KINDS:
        return []
    direction = "receber" if tx["amount_cents"] > 0 else "pagar"
    cls = conn.execute("SELECT counterparty FROM classifications WHERE tx_id=?", (tx_id,)).fetchone()
    tx_words = _tokens(f"{tx['description']} {cls['counterparty'] if cls else ''}")
    tx_date = date.fromisoformat(tx["posted_date"])
    out = []
    for p in list_installments(conn, today, direction=direction):
        if p["open_cents"] <= 0:
            continue
        score, why, strong = 0, [], False
        if avail == p["open_cents"]:
            strong = True
            score += 50
            why.append("valor igual ao saldo em aberto")
        elif avail > p["open_cents"]:
            score += 8
            why.append("valor maior que o saldo em aberto (pode quitar e sobrar)")
        else:
            score += 15
            why.append("valor menor que o saldo em aberto (baixa parcial)")
        gap = abs((tx_date - date.fromisoformat(p["expected_date"])).days)
        if gap <= 3:
            score += 20
            why.append(f"data a {gap} dia(s) da prevista")
        elif gap <= 10:
            score += 10
            why.append(f"data a {gap} dias da prevista")
        elif gap <= 30:
            score += 3
            why.append(f"data a {gap} dias da prevista")
        words = _tokens(p["counterparty"])
        if words and words & tx_words:
            strong = True
            score += 30
            why.append(f"contraparte parecida ({', '.join(sorted(words & tx_words))})")
        if p["expected_account_id"]:
            if p["expected_account_id"] == tx["account_id"]:
                score += 10
                why.append("mesma conta esperada")
            else:
                score -= 20
                why.append("conta diferente da esperada")
        if strong and score >= 40:   # sem valor exato nem contraparte reconhecida, data próxima sozinha não basta
            out.append({"installment": p, "score": score, "reasons": why, "amount_cents": min(avail, p["open_cents"]),
                        "exact": avail == p["open_cents"]})
    return sorted(out, key=lambda c: (-c["score"], c["installment"]["expected_date"]))


def is_unambiguous(cands: list[dict]) -> bool:
    """Só vincula sozinho com um candidato claro: valor exato, pontuação alta e folga sobre o segundo colocado."""
    if not cands:
        return False
    top = cands[0]
    return top["exact"] and top["score"] >= 70 and (len(cands) == 1 or cands[1]["score"] <= top["score"] - 30)


def pending_suggestions(conn, today: date | None = None, days: int = 120) -> list[dict]:
    """Lançamentos recentes ainda sem baixa que têm ao menos uma parcela compatível."""
    today = today or today_sp()
    since = (today - timedelta(days=days)).isoformat()
    out = []
    for tx in conn.execute("SELECT id, posted_date, description, amount_cents, account_id FROM effective_tx "
                           "WHERE posted_date>=? ORDER BY posted_date DESC, id DESC", (since,)).fetchall():
        cands = suggest_for_tx(conn, tx["id"], today)
        if cands:
            out.append({"tx": dict(tx), "candidates": cands, "unambiguous": is_unambiguous(cands)})
    return out


def confirm_unambiguous(conn, today: date | None = None) -> list[int]:
    """Confirma só as sugestões sem ambiguidade (ação do usuário: botão 'confirmar sugestões claras')."""
    done = []
    for s in pending_suggestions(conn, today):
        if s["unambiguous"]:
            c = s["candidates"][0]
            done.append(allocate(conn, c["installment"]["id"], s["tx"]["id"], c["amount_cents"],
                                 note="Confirmado a partir de sugestão: " + "; ".join(c["reasons"])))
    return done


def suggest_transactions_for_installment(conn, installment_id: int, window_days: int = 45,
                                         today: date | None = None) -> list[dict]:
    """Lançamentos do extrato que poderiam baixar esta parcela (para o usuário escolher)."""
    inst = get_installment(conn, installment_id, today)
    exp = date.fromisoformat(inst["expected_date"])
    sign = ">" if inst["direction"] == "receber" else "<"
    rows = conn.execute(
        f"SELECT e.*, a.name AS account_name FROM effective_tx e JOIN accounts a ON a.id=e.account_id "
        f"WHERE e.amount_cents {sign} 0 AND e.posted_date BETWEEN ? AND ? ORDER BY e.posted_date",
        ((exp - timedelta(days=window_days)).isoformat(), (exp + timedelta(days=window_days)).isoformat())).fetchall()
    out = []
    for r in rows:
        if _tx_kind(conn, r["id"]) in NOT_LINKABLE_KINDS:
            continue
        av = tx_available_cents(conn, r["id"])
        if av > 0:
            d = dict(r)
            d["available_cents"] = av
            d["gap_days"] = abs((date.fromisoformat(r["posted_date"]) - exp).days)
            out.append(d)
    return sorted(out, key=lambda d: (d["available_cents"] != inst["open_cents"], d["gap_days"]))
