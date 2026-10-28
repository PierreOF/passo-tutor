"""Raízes de caminho do instrumento de avaliação.

- `RAIZ_TUTOR`: diretório `tutor/` (código, oráculos, perfis e roteiros em `avaliacao/`);
- `RAIZ_REPO`: raiz do repositório Git;
- `RAIZ_RESULTADOS`: onde a avaliação grava `dados_brutos/`, `dados_processados/` e `resultados/`.
"""

from pathlib import Path

RAIZ_TUTOR = Path(__file__).resolve().parents[2]
RAIZ_REPO = RAIZ_TUTOR.parent
RAIZ_RESULTADOS = RAIZ_REPO / "pesquisa" / "11_avaliacao"
