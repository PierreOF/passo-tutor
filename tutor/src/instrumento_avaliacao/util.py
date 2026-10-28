"""Utilitários genéricos do instrumento (hash de texto e leitura de JSONL), sem dependências internas."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_texto(texto: str) -> str:
    """SHA-256 hexadecimal do texto em UTF-8."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def ler_jsonl(caminho: Path, *, ausente_vazio: bool = False) -> list[dict[str, Any]]:
    """Objetos de um JSONL (linhas em branco ignoradas); com `ausente_vazio`, arquivo inexistente vira `[]`."""
    if ausente_vazio and not caminho.is_file():
        return []
    return [json.loads(linha) for linha in caminho.read_text(encoding="utf-8").splitlines() if linha.strip()]
