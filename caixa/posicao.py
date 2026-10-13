"""Configurações e posição de saldo por conta: o saldo mais recente que o app consegue justificar, com suas limitações."""
from datetime import date, timedelta

from . import connections
from .contasimples import BANK_ACCOUNT_NAME, CARD_ACCOUNT_NAME
from .ledger import account_summary, latest_position
from .money import format_brl
from .rotina import API_ACCOUNTS, last_sync_ok
from .timeutil import SP, fmt_date, today_sp

CDI_MAX_AGE_DAYS = 7


# ------------------------------------------------------------------ configurações

def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))


def reserve_cents(conn) -> int:
    """Reserva mínima de caixa (0 enquanto você não definir)."""
    return int(get_setting(conn, "reserva_minima_cents", "0"))


def reserve_defined(conn) -> bool:
    return get_setting(conn, "reserva_minima_cents") is not None


def count_cdi(conn) -> bool:
    """Se o CDI aplicado conta como caixa na projeção e nos indicadores (padrão: sim)."""
    return get_setting(conn, "contar_cdi", "1") == "1"


# ------------------------------------------------------------------ posição por conta

def account_position(conn, account_id: int, today: date | None = None) -> dict:
    """Saldo da conta na data mais recente justificável.

    - Conta alimentada por OFX: vale até o fim do último extrato importado.
    - Conta sincronizada pela API: vale até a última sincronização bem-sucedida.
    - Disponível = saldo calculado + efeito de compras pendentes (cartão). CDI/renda fixa vem separado, da posição que você informou.
    """
    today = today or today_sp()
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    cfg = connections.get_contasimples()
    api = bool(cfg) and cfg.get("env") == "production" and acc["name"] in API_ACCOUNTS
    asof, notes, limitations = None, [], []
    if api:
        synced = last_sync_ok(conn)
        asof = synced.astimezone(SP).date() if synced else None
        if asof is None:
            limitations.append("ainda não sincronizada pela API")
    else:
        row = conn.execute("SELECT MAX(period_end) AS e FROM import_batches WHERE account_id=?", (account_id,)).fetchone()
        asof = date.fromisoformat(row["e"]) if row["e"] else None
        if asof is None:
            limitations.append("nenhum extrato importado")
    if asof and asof > today:
        asof = today
    s = account_summary(conn, account_id, asof.isoformat() if asof else None)
    pos = latest_position(conn, account_id)
    cdi, cdi_date = (pos["amount_cents"], pos["as_of_date"]) if pos else (0, None)
    stale_days = (today - asof).days if asof else None
    # OFX só chega até ontem; a API é considerada em dia se sincronizou hoje
    limit_days = 0 if api else 1
    if asof and stale_days > limit_days:
        limitations.append(f"dados só até {fmt_date(asof.isoformat())} ({stale_days} dias atrás)")
    if s["n_investimento"] and not pos:
        limitations.append("há aplicações em CDI mas nenhuma posição aplicada foi informada")
    elif pos and (today - date.fromisoformat(cdi_date)).days > CDI_MAX_AGE_DAYS:
        limitations.append(f"posição em CDI antiga (informada em {fmt_date(cdi_date)})")
    if s["n_unclassified"]:
        notes.append(f"{s['n_unclassified']} movimentação(ões) sem classificação")
    available = s["calculated_cents"] + s["pending_cents"]
    return {
        "account_id": account_id, "name": acc["name"], "asof": asof, "stale_days": stale_days, "api": api,
        "available_cents": available, "pending_cents": s["pending_cents"], "cdi_cents": cdi, "cdi_date": cdi_date,
        "total_cents": available + cdi, "limitations": limitations, "notes": notes,
        "reliable": not limitations, "is_card_limit": acc["name"] == CARD_ACCOUNT_NAME,
    }


def positions(conn, today: date | None = None) -> list[dict]:
    return [account_position(conn, a["id"], today) for a in conn.execute("SELECT id FROM accounts ORDER BY id")]


def describe(p: dict, cdi_counts: bool = True) -> str:
    """Frase curta sobre o saldo (valor e a data a que ele se refere)."""
    base = p["total_cents"] if cdi_counts else p["available_cents"]
    when = f"em {fmt_date(p['asof'].isoformat())}" if p["asof"] else "sem data de extrato"
    return f"{format_brl(base)} ({when})"
