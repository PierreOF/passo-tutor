"""Falha se credenciais OpenRouter forem encontradas em arquivos textuais do projeto."""

from __future__ import annotations

import re
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[2]  # tutor/scripts -> raiz do Git
SECRET = re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}")
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".md", ".csv", ".toml", ".yaml", ".yml", ".sh", ".ps1", ".example"}
IGNORED_PARTS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", "tmp"}


def candidates() -> list[Path]:
    return [
        path
        for path in RAIZ_REPO.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in TEXT_SUFFIXES
        and not IGNORED_PARTS.intersection(path.relative_to(RAIZ_REPO).parts)
    ]


def main() -> None:
    violations: list[str] = []
    for path in candidates():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SECRET.search(content):
            violations.append(str(path.relative_to(RAIZ_REPO)))
    if violations:
        raise SystemExit("Credencial OpenRouter encontrada em: " + ", ".join(sorted(violations)))
    print("auditoria de segredos: OK")


if __name__ == "__main__":
    main()
