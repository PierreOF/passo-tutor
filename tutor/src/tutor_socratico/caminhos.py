"""Raízes de caminho do tutor: `RAIZ_TUTOR` (diretório `tutor/`) e `RAIZ_REPO` (raiz do Git)."""

from pathlib import Path

RAIZ_TUTOR = Path(__file__).resolve().parents[2]
RAIZ_REPO = RAIZ_TUTOR.parent
