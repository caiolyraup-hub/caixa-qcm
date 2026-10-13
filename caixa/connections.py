"""Credenciais das integrações. Ficam em data/conexoes.json (fora do banco e do git).

O arquivo é texto simples: quem tiver acesso à pasta data/ tem acesso às credenciais. Use credenciais só de leitura.
"""
import json

from .config import data_dir


def _path():
    return data_dir() / "conexoes.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save(cfg: dict) -> None:
    _path().write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_contasimples() -> dict | None:
    cfg = load().get("contasimples")
    return cfg if cfg and cfg.get("api_key") and cfg.get("api_secret") else None


def set_contasimples(api_key: str, api_secret: str, env: str = "production") -> None:
    if env not in ("production", "sandbox"):
        raise ValueError("ambiente inválido")
    cfg = load()
    cfg["contasimples"] = {"api_key": api_key.strip(), "api_secret": api_secret.strip(), "env": env}
    _save(cfg)


def clear_contasimples() -> None:
    cfg = load()
    cfg.pop("contasimples", None)
    _save(cfg)
