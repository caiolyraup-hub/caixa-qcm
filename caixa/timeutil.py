"""Datas e horas em America/Sao_Paulo."""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SP = ZoneInfo("America/Sao_Paulo")

_BR_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_OFX_DT = re.compile(
    r"^(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2})?(?:\.\d+)?)?"
    r"(?:\[\s*([+-]?\d+(?:\.\d+)?)(?::\w+)?\s*\])?"
)


def now_sp() -> datetime:
    return datetime.now(SP)


def today_sp() -> date:
    return now_sp().date()


def now_iso() -> str:
    return now_sp().isoformat(timespec="seconds")


def parse_ofx_date(text: str) -> str:
    """Converte data OFX (YYYYMMDD[HHMMSS[.XXX]][[-3:BRT]]) na data civil de São Paulo, em ISO (YYYY-MM-DD).

    - Só data, ou hora sem fuso: é tratada como já estando no horário de São Paulo.
    - Com hora e fuso: converte para America/Sao_Paulo. Exceção: meia-noite exata com fuso diferente de
      São Paulo (ex.: 20260115000000[0:GMT]) é tratada como "só data", porque vários bancos usam esse
      formato para dizer "dia 15" e converter empurraria o lançamento para o dia anterior.
    """
    text = text.strip()
    br = _BR_DATE.match(text)  # alguns bancos (ex.: PagBank no LEDGERBAL) usam DD/MM/AAAA fora do padrão OFX
    if br:
        return date(int(br.group(3)), int(br.group(2)), int(br.group(1))).isoformat()
    m = _OFX_DT.match(text)
    if not m:
        raise ValueError(f"data OFX inválida: {text!r}")
    y, mo, d, hh, mm, ss, off = m.groups()
    day = date(int(y), int(mo), int(d))
    if hh is None or off is None:
        return day.isoformat()
    hour, minute, sec = int(hh), int(mm or 0), int(ss or 0)
    if hour == minute == sec == 0:
        return day.isoformat()
    tz = timezone(timedelta(hours=float(off)))
    return datetime(day.year, day.month, day.day, hour, minute, sec, tzinfo=tz).astimezone(SP).date().isoformat()


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}/{y}"
