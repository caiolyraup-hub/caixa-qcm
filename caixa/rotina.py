"""Rotina diária: o que precisa estar atualizado hoje e se está.

- Contas alimentadas por OFX manual (PagBank): o extrato importado precisa cobrir até ONTEM
  (o OFX do dia corrente é parcial, então a meta é D-1).
- Contas alimentadas por API (Conta Simples, quando há credenciais): precisa ter sincronizado hoje (ou nas últimas 26h).
"""
import subprocess
import sys
from datetime import date, datetime, timedelta

from . import connections
from .contasimples import BANK_ACCOUNT_NAME, CARD_ACCOUNT_NAME
from .recon import coverage
from .timeutil import SP, fmt_date, now_sp, today_sp

API_ACCOUNTS = {BANK_ACCOUNT_NAME, CARD_ACCOUNT_NAME}


def last_sync_ok(conn) -> datetime | None:
    row = conn.execute("SELECT MAX(finished_at) AS f FROM sync_runs WHERE source='contasimples' AND status='ok'").fetchone()
    return datetime.fromisoformat(row["f"]) if row["f"] else None


def daily_status(conn, today: date | None = None, now: datetime | None = None) -> list[dict]:
    today = today or today_sp()
    now = now or now_sp()
    cfg = connections.get_contasimples()
    use_api = cfg is not None and cfg.get("env") == "production"   # sandbox não sincroniza: segue manual
    synced = last_sync_ok(conn)
    out = []
    for c in coverage(conn, today.isoformat()):
        if c["account_name"] == CARD_ACCOUNT_NAME and use_api:
            continue  # cartões andam junto com a conta; um único item "Conta Simples (API)"
        item = {"account": c["account_name"], "mode": "ofx", "ok": True, "detail": ""}
        if c["account_name"] in API_ACCOUNTS and use_api:
            item["mode"] = "api"
            if synced and (now - synced.astimezone(SP)) <= timedelta(hours=26):
                item["detail"] = f"sincronizado em {synced.astimezone(SP):%d/%m %H:%M}"
            else:
                item["ok"] = False
                item["detail"] = ("nunca sincronizado" if not synced
                                  else f"última sincronização em {synced.astimezone(SP):%d/%m %H:%M}")
        else:
            need = today - timedelta(days=1)
            if c["no_statement"]:
                item.update(ok=False, detail="nenhum extrato importado ainda")
            else:
                last = date.fromisoformat(c["last_end"])
                if last >= need:
                    item["detail"] = f"extrato até {fmt_date(c['last_end'])}"
                else:
                    first_missing = last + timedelta(days=1)
                    item.update(ok=False, detail=f"extrato só vai até {fmt_date(c['last_end'])}; "
                                                 f"importe de {fmt_date(first_missing.isoformat())} até {fmt_date(need.isoformat())}")
        out.append(item)
    return out


def pending_messages(conn) -> list[str]:
    return [f"{i['account']}: {i['detail']}" for i in daily_status(conn) if not i["ok"]]


def notify(title: str, text: str) -> None:
    """Aviso na bandeja do Windows (não bloqueia o chamador; ignora falhas)."""
    if sys.platform != "win32":
        return
    esc = lambda s: s.replace("'", "''")  # noqa: E731
    script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
              "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
              f"$n.BalloonTipTitle='{esc(title)}';$n.BalloonTipText='{esc(text)}';$n.Visible=$true;"
              "$n.ShowBalloonTip(15000);Start-Sleep 16;$n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass
