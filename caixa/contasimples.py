"""Conector da API Conta Simples (somente leitura): extrato bancário, cartões e saldo.

Docs: https://developers.contasimples.com  (OAuth2 client credentials; 100 chamadas/min por IP).
Use uma credencial só com escopos de leitura de extrato/saldo: sem escopo sensível não há chave pública
e portanto nenhuma chamada precisa ser assinada (RFC 9421).
"""
import base64
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

import requests

from .db import create_account
from .ledger import LedgerError, clear_classification, get_classification, ignore_tx, is_linked, link_transfer, set_pending_balance
from .importer import commit_import, plan_statement
from .money import parse_amount_to_cents
from .ofx import OfxStatement, OfxTx
from .timeutil import SP, now_iso, today_sp

BASE_URL = {"production": "https://api.contasimples.com", "sandbox": "https://api-sandbox.contasimples.com"}
USER_AGENT = "CaixaQCM/1.0"
BANK_ACCOUNT_NAME = "Conta Simples"
CARD_ACCOUNT_NAME = "Conta Simples (cartões)"
SOURCE = "contasimples"
FILE_PREFIX = "API Conta Simples"


class ContaSimplesError(Exception):
    pass


Transport = Callable[[str, str, dict, dict | None, bytes | None], tuple[int, dict | list | None]]


def requests_transport(method: str, url: str, headers: dict, params: dict | None, data: bytes | None):
    r = requests.request(method, url, headers=headers, params=params, data=data, timeout=30)
    try:
        body = json.loads(r.text, parse_float=Decimal)  # valores em Decimal: nunca float
    except ValueError:
        body = None
    return r.status_code, body


class ContaSimplesClient:
    def __init__(self, api_key: str, api_secret: str, env: str = "production",
                 transport: Transport = requests_transport, sleep: Callable[[float], None] = time.sleep):
        self.base = BASE_URL[env]
        self._basic = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._transport, self._sleep = transport, sleep
        self._token: str | None = None
        self._token_exp = 0.0

    # ------------------------------------------------------------ http
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        status, body = self._transport(
            "POST", f"{self.base}/oauth/v1/access-token",
            {"Authorization": f"Basic {self._basic}", "Content-Type": "application/x-www-form-urlencoded",
             "User-Agent": USER_AGENT}, None, b"grant_type=client_credentials")
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            hint = ""
            if status in (401, 403) and self.base == BASE_URL["production"]:
                hint = (" Credenciais geradas no Internet Banking como Sandbox NÃO funcionam em produção: a produção "
                        "exige aprovação da Conta Simples (integração validada + checklist de segurança + termos) e "
                        "credenciais próprias.")
            raise ContaSimplesError(f"Falha ao autenticar (HTTP {status}). Confira API key, secret e ambiente. "
                                    f"{_msg(body)}{hint}")
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 1800))
        return self._token

    def _get(self, path: str, params: dict | None = None):
        reauth = False
        for attempt in range(5):
            headers = {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json",
                       "User-Agent": USER_AGENT}
            status, body = self._transport("GET", f"{self.base}{path}", headers, params, None)
            if status == 200:
                return body
            if status == 401 and not reauth:
                reauth, self._token = True, None
                continue
            if status in (429, 500, 502, 503, 504):
                self._sleep(2 ** attempt)
                continue
            hint = ""
            if status == 401:
                hint = (" Se a credencial tem chave pública registrada, toda chamada exige assinatura: crie uma "
                        "credencial só com escopos de leitura (sem escopos de escrita).")
            elif status == 403:
                hint = " A credencial não tem o escopo desta rota; crie outra com o escopo de leitura necessário."
            raise ContaSimplesError(f"HTTP {status} em {path}: {_msg(body)}{hint}")
        raise ContaSimplesError(f"Sem resposta estável de {path} após várias tentativas (HTTP {status}).")

    def _paginate(self, path: str, params: dict) -> list[dict]:
        out, cursor = [], None
        while True:
            q = dict(params)
            if cursor:
                q["nextPageStartKey"] = cursor
            body = self._get(path, q) or {}
            out += body.get("transactions", [])
            cursor = body.get("nextPageStartKey")
            if not cursor:
                return out

    # ------------------------------------------------------------ recursos
    def banking(self, start: date, end: date) -> list[dict]:
        return self._paginate("/statements/v1/banking",
                              {"startDate": start.isoformat(), "endDate": end.isoformat(), "limit": 50})

    def cards(self, start: date, end: date) -> list[dict]:
        """O extrato de cartão aceita no máximo 62 dias por consulta: fatiamos em janelas de 60."""
        out, cur = [], start
        while cur <= end:
            win_end = min(cur + timedelta(days=59), end)
            out += self._paginate("/statements/v1/credit-card",
                                  {"startDate": cur.isoformat(), "endDate": win_end.isoformat(), "limit": 100})
            cur = win_end + timedelta(days=1)
        return out

    def balance_cents(self) -> int:
        body = self._get("/accounts/v1/balance")
        return parse_amount_to_cents(str(body["amount"]))


def _msg(body) -> str:
    return str(body.get("message", "")) if isinstance(body, dict) else ""


# ---------------------------------------------------------------- mapeamento

_OUT_WORDS = ("ENVIAD", "PAGAMENTO", "PAGTO", "COMPRA", "TARIFA", "SAQUE", "DEBITO", "DÉBITO", "FATURA", "IOF", "TAXA")
_IN_WORDS = ("RECEBID", "DEPOSITO", "DEPÓSITO", "CREDITO", "CRÉDITO", "ESTORNO", "REEMBOLSO", "CASHBACK", "RENTABILIDADE", "RENDIMENTO")


def banking_sign(t: dict) -> int | None:
    """+1 entrada, -1 saída, None se não der para saber. brlAmount vem SEMPRE positivo; a direção está no tipo."""
    tt = t.get("transactionType") or {}
    sub = str(tt.get("subType") or "").upper()
    if sub.endswith("_OUT") or sub.startswith("OUT_"):
        return -1
    if sub.endswith("_IN") or sub.startswith("IN_"):
        return 1
    text = f"{tt.get('description', '')} {t.get('description', '')}".upper()
    out = any(w in text for w in _OUT_WORDS)
    inn = any(w in text for w in _IN_WORDS)
    if out != inn:
        return -1 if out else 1
    return None


def to_sp_date(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (dt.astimezone(SP) if dt.tzinfo else dt).date().isoformat()


def _cents(v) -> int:
    return parse_amount_to_cents(str(v))


def map_banking(t: dict) -> tuple[OfxTx | None, str]:
    """(lançamento, "") ou (None, motivo do descarte)."""
    status = t.get("status")
    if status == 3:
        return None, "pendente"
    if status == 1:
        return None, "cancelado"
    if status != 2:                      # só 2 = PROCESSADO está liquidado (a API também devolve 0 em alguns rendimentos)
        return None, "nao_liquidado"
    sign = banking_sign(t)
    if sign is None:
        return None, "sem_direcao"
    cents = abs(_cents(t["brlAmount"]))
    if cents == 0:
        return None, "valor_zero"
    tt = t.get("transactionType") or {}
    parts = [tt.get("description") or "TRANSAÇÃO", t.get("sourceDestinationName") or t.get("description")]
    desc = " - ".join(p for p in dict.fromkeys(parts) if p)
    return OfxTx(fitid=f"CS{t['id']}", posted_date=to_sp_date(t["transactionDate"]), description=desc,
                 amount_cents=sign * cents, trntype=str(tt.get("subType") or "")), ""


# Consultas e resumos não movimentam dinheiro. LIMIT/LIMIT_CREDIT (carga de limite no cartão) MOVIMENTAM:
# nos cartões de débito da Conta Simples o limite é dinheiro do caixa levado para o cartão.
_CARD_IGNORED_TYPES = {"BALANCE_INQUIRY", "PAYMENT_PLAN_INQUIRY", "SUMMARY"}
_CARD_LIMIT_TYPES = {"LIMIT", "LIMIT_CREDIT"}


def map_card(t: dict) -> tuple[OfxTx | None, str]:
    if t.get("isCanceled") or t.get("status") == "CANCELED":
        return None, "cancelado"
    if t.get("status") == "PENDING":
        return None, "pendente"
    if t.get("type") in _CARD_IGNORED_TYPES:
        return None, "nao_monetario"
    op = t.get("operation")
    if op not in ("CASH_IN", "CASH_OUT"):
        return None, "sem_direcao"
    cents = abs(_cents(t["amountBrl"]))
    if cents == 0:
        return None, "valor_zero"
    card = t.get("card") or {}
    last4 = card.get("finalNumber") or card.get("lastDigits") or ""
    if t.get("type") in _CARD_LIMIT_TYPES:
        what = "Limite carregado no cartão" if op == "CASH_IN" else "Limite devolvido ao caixa"
    else:
        what = t.get("merchant") or t.get("type")
    desc = f"{what}" + (f" (cartão {last4})" if last4 else "")
    return OfxTx(fitid=f"CC{t['id']}", posted_date=to_sp_date(t["transactionDate"]), description=desc,
                 amount_cents=cents if op == "CASH_IN" else -cents, trntype=str(t.get("type") or "")), ""


# ---------------------------------------------------------------- sincronização

@dataclass
class SyncResult:
    novos: int = 0
    duplicados: int = 0
    sinalizados: int = 0
    descartados: dict = field(default_factory=dict)       # motivo → quantidade
    sem_direcao: list = field(default_factory=list)       # amostras que precisam de revisão manual
    saldo_api_cents: int | None = None
    divergencia_cents: int | None = None
    limites_pareados: int = 0              # cargas de limite ligadas à saída da conta (transferência)
    limites_bloqueados: int = 0            # cargas com par, mas já classificadas por você como outro tipo (não sobrescreve)
    liquidacoes_ignoradas: int = 0         # pagamentos de fatura (não são dinheiro novo)
    pendentes_cartao_cents: int = 0        # compras de cartão ainda pendentes (reduzem o limite disponível)


def _account(conn, name: str):
    return conn.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()


def ensure_accounts(conn) -> tuple[dict, dict]:
    bank = _account(conn, BANK_ACCOUNT_NAME)
    if bank is None:
        raise ContaSimplesError(f"Cadastre primeiro a conta '{BANK_ACCOUNT_NAME}' (com saldo inicial) em Contas.")
    card = _account(conn, CARD_ACCOUNT_NAME)
    if card is None:
        create_account(conn, CARD_ACCOUNT_NAME, 0, bank["opening_date"])
        card = _account(conn, CARD_ACCOUNT_NAME)
    elif card["opening_balance_cents"] == 0 and card["opening_date"] != bank["opening_date"]:
        # a conta de cartões acompanha a data inicial da conta bancária (se você corrigir uma, a outra segue)
        with conn:
            conn.execute("UPDATE accounts SET opening_date=? WHERE id=?", (bank["opening_date"], card["id"]))
        card = _account(conn, CARD_ACCOUNT_NAME)
    return dict(bank), dict(card)


def _window_start(conn, account: dict, today: date, overlap_days: int, full: bool = False) -> date:
    row = conn.execute(
        "SELECT MAX(period_end) AS e, MIN(period_start) AS s FROM import_batches WHERE account_id=? AND filename LIKE ?",
        (account["id"], f"{FILE_PREFIX}%")).fetchone()
    opening = date.fromisoformat(account["opening_date"])
    if full:
        return opening
    if row["s"] and date.fromisoformat(row["s"]) > opening:
        return opening  # você recuou a data do saldo inicial: busca o histórico que ficou para trás (reimportar é seguro)
    if not row["e"]:
        # Primeira sincronização: se a conta já tem extratos OFX, começa no dia seguinte ao último deles
        # (OFX e API usam IDs diferentes, então misturar o mesmo período duplicaria lançamentos).
        ofx = conn.execute(
            "SELECT MAX(period_end) AS e FROM import_batches WHERE account_id=? AND filename NOT LIKE ?",
            (account["id"], f"{FILE_PREFIX}%")).fetchone()
        if ofx["e"]:
            return max(opening, date.fromisoformat(ofx["e"]) + timedelta(days=1))
        return opening
    return max(opening, date.fromisoformat(row["e"]) - timedelta(days=overlap_days))


def _import(conn, account, label, raw, txs, start, end, ledger=None) -> SyncResult:
    stmt = OfxStatement(period_start=start.isoformat(), period_end=end.isoformat(), txs=txs)
    if ledger is not None:
        stmt.ledger_balance_cents, stmt.ledger_balance_date = ledger, end.isoformat()
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    plan = plan_statement(conn, account["id"], f"{FILE_PREFIX} {label} {start}..{end}.json", payload, stmt)
    res = commit_import(conn, plan)
    return SyncResult(novos=res["novos"], duplicados=res["duplicados"], sinalizados=res["sinalizados"])


def sync_contasimples(conn, client: ContaSimplesClient, *, today: date | None = None, overlap_days: int = 7,
                      full: bool = False) -> SyncResult:
    """Traz extrato bancário e cartões até hoje (com sobreposição de alguns dias para pegar lançamentos tardios).
    Reexecutar é seguro: o importador reconhece o que já existe pelo ID da transação.
    full=True refaz tudo desde a data do saldo inicial (use depois de mudar regras de importação)."""
    if client.base == BASE_URL["sandbox"]:
        raise ContaSimplesError("O Sandbox devolve dados fictícios fixos e não pode ser sincronizado com o seu caixa. "
                                "Ele serve só para 'Testar conexão'. Para sincronizar, use credenciais de produção.")
    today = today or today_sp()
    started = now_iso()
    run_id = conn.execute("INSERT INTO sync_runs (source, started_at, status) VALUES (?,?,'em andamento')",
                          (SOURCE, started)).lastrowid
    conn.commit()
    total = SyncResult()
    try:
        bank, card = ensure_accounts(conn)

        start = _window_start(conn, bank, today, overlap_days, full)
        raw = client.banking(start, today)
        mapped = []
        for t in raw:
            tx, why = map_banking(t)
            if tx:
                mapped.append(tx)
            else:
                total.descartados[why] = total.descartados.get(why, 0) + 1
                if why == "sem_direcao":
                    total.sem_direcao.append(f"#{t.get('id')} {(t.get('transactionType') or {}).get('description')} "
                                             f"{t.get('brlAmount')} {t.get('transactionDate', '')[:10]}")
        total.saldo_api_cents = client.balance_cents()
        r = _import(conn, bank, "banking", raw, mapped, start, today, ledger=total.saldo_api_cents)
        total.novos, total.duplicados, total.sinalizados = r.novos, r.duplicados, r.sinalizados

        cstart = _window_start(conn, card, today, overlap_days, full)
        craw = client.cards(cstart, today)
        cmapped = []
        for t in craw:
            tx, why = map_card(t)
            if tx:
                cmapped.append(tx)
            else:
                total.descartados[why] = total.descartados.get(why, 0) + 1
                if why == "sem_direcao":
                    total.sem_direcao.append(f"cartão {t.get('id')} {t.get('merchant')} {t.get('amountBrl')}")
        r = _import(conn, card, "cartoes", craw, cmapped, cstart, today)
        total.novos += r.novos
        total.duplicados += r.duplicados
        total.sinalizados += r.sinalizados

        pend = sum((-1 if t.get("operation") == "CASH_OUT" else 1) * _cents(t["amountBrl"])
                   for t in craw if t.get("status") == "PENDING" and not t.get("isCanceled")
                   and t.get("type") not in _CARD_IGNORED_TYPES)
        total.pendentes_cartao_cents = pend
        set_pending_balance(conn, card["id"], pend, today.isoformat())
        total.limites_pareados, total.liquidacoes_ignoradas, total.limites_bloqueados = reconcile_card_limits(
            conn, bank, card, today)

        from .recon import statement_comparisons
        cmp_ = [c for c in statement_comparisons(conn, bank["id"]) if c["date"] == today.isoformat()]
        if cmp_:
            total.divergencia_cents = cmp_[-1]["diff_cents"]
    except Exception as exc:
        conn.execute("UPDATE sync_runs SET finished_at=?, status='erro', message=? WHERE id=?",
                     (now_iso(), str(exc)[:500], run_id))
        conn.commit()
        raise
    msg = _summary(total)
    conn.execute("UPDATE sync_runs SET finished_at=?, status='ok', n_new=?, n_duplicate=?, n_skipped=?, message=? WHERE id=?",
                 (now_iso(), total.novos, total.duplicados, sum(total.descartados.values()), msg, run_id))
    conn.commit()
    return total


def reconcile_card_limits(conn, bank: dict, card: dict, today: date, force: bool = False) -> tuple[int, int, int]:
    """Cartões da Conta Simples (limite garantido por depósito): o único dinheiro que sai da conta é a
    "Transferência de limite" (SECURED_CARD_DEPOSIT_*). No extrato do cartão ela aparece como LIMIT e TAMBÉM aparece
    como LIMIT cada pagamento de fatura (que só liquida compras já abatidas do limite, não é dinheiro novo).

    1. Pareia cada carga de limite com a saída da conta de mesmo valor (±2 dias) e vincula como transferência.
    2. O que sobra sem par há mais de 3 dias é pagamento de fatura: fica ignorado nos saldos (original preservado).
    Roda sobre o banco todo, então também corrige o que foi importado por versões anteriores.

    Se um dos lados do par já tem outra classificação sua (ex.: Ajuste), NÃO sobrescreve: conta em `bloqueados`.
    Com force=True, remove essa classificação (fica no histórico) e vincula como transferência.
    Retorna (pareados, ignorados, bloqueados)."""
    deposits = [dict(r) for r in conn.execute(
        "SELECT * FROM effective_tx WHERE account_id=? AND trntype LIKE 'SECURED_CARD_DEPOSIT%'", (bank["id"],))
        if not is_linked(conn, r["id"])]
    limits = [dict(r) for r in conn.execute(
        "SELECT * FROM effective_tx WHERE account_id=? AND trntype IN ('LIMIT','LIMIT_CREDIT')", (card["id"],))
        if not is_linked(conn, r["id"])]
    paired = ignored = blocked = 0
    for lim in sorted(limits, key=lambda r: (r["posted_date"], r["id"])):
        d0 = date.fromisoformat(lim["posted_date"])
        match = next((d for d in deposits if d["amount_cents"] == -lim["amount_cents"]
                      and abs((date.fromisoformat(d["posted_date"]) - d0).days) <= 2), None)
        if match:
            deposits.remove(match)
            out, inn = (match, lim) if match["amount_cents"] < 0 else (lim, match)
            others = [t for t in (out, inn) if (get_classification(conn, t["id"]) or {"kind": "transferencia"})["kind"] != "transferencia"]
            if others and not force:
                deposits.append(match)       # não consome o depósito; o par fica pendente de decisão sua
                blocked += 1
                continue
            for t in others:
                clear_classification(conn, t["id"])
            link_transfer(conn, out["id"], inn["id"])
            paired += 1
        elif lim["amount_cents"] > 0 and d0 <= today - timedelta(days=3):
            ignore_tx(conn, lim["id"], "Pagamento de fatura do cartão: liquida compras já abatidas do limite, não é dinheiro novo")
            ignored += 1
    return paired, ignored, blocked


def _summary(r: SyncResult) -> str:
    parts = [f"{r.novos} novos, {r.duplicados} já existentes"]
    if r.sinalizados:
        parts.append(f"{r.sinalizados} sinalizados como possível duplicata")
    if r.descartados:
        parts.append("descartados: " + ", ".join(f"{k}={v}" for k, v in sorted(r.descartados.items())))
    if r.limites_pareados or r.liquidacoes_ignoradas:
        parts.append(f"cartões: {r.limites_pareados} cargas de limite pareadas, {r.liquidacoes_ignoradas} pagamentos de fatura ignorados")
    if r.limites_bloqueados:
        parts.append(f"{r.limites_bloqueados} cargas de limite NÃO pareadas: já estão classificadas por você como outro tipo")
    if r.pendentes_cartao_cents:
        parts.append(f"compras pendentes no cartão {r.pendentes_cartao_cents} centavos")
    if r.sem_direcao:
        parts.append(f"{len(r.sem_direcao)} SEM DIREÇÃO (entrada/saída desconhecida), não importados")
    if r.divergencia_cents:
        parts.append(f"saldo calculado difere do saldo da API em {r.divergencia_cents} centavos")
    return "; ".join(parts)
