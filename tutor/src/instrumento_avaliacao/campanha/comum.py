"""Constantes e utilitários compartilhados pelos módulos da campanha (evita ciclos de import)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from instrumento_avaliacao.caminhos import RAIZ_RESULTADOS

# Atenção ao trocar estas raízes em testes: cada módulo (execucao, custos, manifesto, analise) faz
# `from ...comum import DATA_ROOT` e guarda a própria referência. `patch` precisa mirar o módulo que lê
# o nome, por exemplo `patch.object(custos, "DATA_ROOT", ...)`. Trocar em `comum` ou na reexportação
# de `campanha/__init__.py` não muda o caminho usado pelas funções.
DATA_ROOT = RAIZ_RESULTADOS / "dados_brutos"
PROCESSED_ROOT = RAIZ_RESULTADOS / "dados_processados"
RESULTS_ROOT = RAIZ_RESULTADOS / "resultados"
OUT_OF_SCOPE_STATUS = "ENTRADA_FORA_DO_ESCOPO"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_run_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{4,70}", value):
        raise ValueError("run_id exige 5–71 caracteres: letras minúsculas, dígitos, _ ou -")
    return value
