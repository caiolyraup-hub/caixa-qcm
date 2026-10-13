"""Rotina diária por linha de comando: python -m caixa.sync [--notify] [--full]

1. Sincroniza a Conta Simples pela API (se houver credenciais).
2. Confere o que falta (ex.: OFX do PagBank de ontem) e, com --notify, avisa na bandeja do Windows.
Registra tudo em data/sync.log. Código de saída 1 se a sincronização falhar.
"""
import sys

from . import connections
from .config import data_dir
from .contasimples import ContaSimplesClient, ContaSimplesError, sync_contasimples
from .db import connect
from .money import format_brl
from .rotina import notify, pending_messages
from .timeutil import now_iso


def log(msg: str) -> None:
    line = f"{now_iso()}  {msg}"
    print(line)
    with open(data_dir() / "sync.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_contasimples(conn, full: bool = False) -> str | None:
    """Retorna None se ok (ou sem credenciais); texto do erro se falhou."""
    cfg = connections.get_contasimples()
    if not cfg:
        log("Conta Simples: sem credenciais configuradas; pulando.")
        return None
    if cfg.get("env") == "sandbox":
        log("Conta Simples: credencial de SANDBOX (dados fictícios); sincronização desativada.")
        return None
    try:
        r = sync_contasimples(conn, ContaSimplesClient(cfg["api_key"], cfg["api_secret"], cfg.get("env", "production")), full=full)
    except (ContaSimplesError, OSError) as exc:
        log(f"Conta Simples: ERRO - {exc}")
        return str(exc)
    extra = ""
    if r.saldo_api_cents is not None:
        extra = f" | saldo na API {format_brl(r.saldo_api_cents)}"
        if r.divergencia_cents:
            extra += f" | DIFERENÇA para o calculado {format_brl(r.divergencia_cents, sign=True)}"
    log(f"Conta Simples: {r.novos} novos, {r.duplicados} já existentes, {sum(r.descartados.values())} descartados{extra}")
    for s in r.sem_direcao:
        log(f"  sem direção (revisar): {s}")
    return None


def main(argv: list[str]) -> int:
    conn = connect()
    error = run_contasimples(conn, full="--full" in argv)
    pend = pending_messages(conn)
    for p in pend:
        log(f"PENDENTE - {p}")
    if "--notify" in argv and (pend or error):
        notify("Caixa QCM", " | ".join(pend + ([f"Falha na sincronização: {error}"] if error else []))[:250])
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
