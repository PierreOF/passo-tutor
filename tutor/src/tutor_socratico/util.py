"""Utilitários neutros do tutor (relógio e hashes), sem dependências internas."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def now_iso() -> str:
    """Instante atual em UTC, ISO 8601."""
    return datetime.now(UTC).isoformat()


def sha256_text(text: str) -> str:
    """SHA-256 hexadecimal do texto em UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json_sha256(value: object) -> str:
    """SHA-256 do JSON com chaves ordenadas e `ensure_ascii=False` (separadores padrão)."""
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
