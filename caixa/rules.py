"""Regras de classificação automática na importação (o usuário pode trocar depois)."""
import re

_CDI = re.compile(r"renda\s*fixa|\bcdb\b", re.I)
_YIELD = re.compile(r"rentabilidade\s*cdi|rendimento", re.I)


def auto_classification(description: str, amount_cents: int) -> dict | None:
    """Campos de classificação automática (kind, counterparty, category, note) ou None.

    - Renda fixa / CDB (aplicação, resgate, vencimento): movimento interno do caixa, não é receita nem despesa.
    - "Rentabilidade CDI" creditada no saldo: receita financeira.
    """
    if _CDI.search(description):
        return {"kind": "investimento", "note": "Automático: movimento de renda fixa (CDI)"}
    if amount_cents > 0 and _YIELD.search(description):
        return {"kind": "receita", "category": "Rendimento financeiro", "note": "Automático: rentabilidade do saldo"}
    return None
