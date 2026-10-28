#!/usr/bin/env python3
"""Qualifica oráculos e 15 casos Replay; não chama o OpenRouter."""

from __future__ import annotations

import argparse
from pathlib import Path

from instrumento_avaliacao.qualificacao import run_qualification


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="ID único para os artefatos da qualificação")
    parser.add_argument("--output-root", type=Path, help="raiz alternativa para artefatos de teste")
    args = parser.parse_args()
    summary = run_qualification(args.output_root, run_id=args.run_id)
    print(f"qualificação={summary['decision']}; casos={summary['cases']}/{summary['expected_cases']}; verificações={summary['checks']}")
    if summary["decision"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
