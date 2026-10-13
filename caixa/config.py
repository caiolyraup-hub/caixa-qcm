import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Pasta de dados (banco, extratos originais, anexos, backups). Lida a cada chamada para facilitar testes."""
    path = Path(os.environ.get("CAIXA_QCM_DATA") or ROOT / "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "caixa_qcm.db"
