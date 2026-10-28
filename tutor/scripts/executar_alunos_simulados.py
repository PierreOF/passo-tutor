"""Planeja, executa e analisa a campanha artificial; chamadas reais são explícitas."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from instrumento_avaliacao.aluno_simulado import planned_episodes
from instrumento_avaliacao.campanha import (
    analyse_run,
    check_manifest,
    freeze_manifest,
    reconcile_costs,
    run_phase,
)
from instrumento_avaliacao.campanha.comum import PROCESSED_ROOT
from instrumento_avaliacao.campanha.custos import _generation_cost
from instrumento_avaliacao.juiz.chamada import carregar_juizes, config_juiz
from instrumento_avaliacao.juiz.julgamento import (
    julgar_campanha,
    planejar_julgamento,
    rotulo_efetivo,
)
from instrumento_avaliacao.juiz.orcamento import Orcamento
from instrumento_avaliacao.juiz.qualificacao_juizes import (
    PREFIXO_SAIDA,
    gravar_hashes_reservados,
    linhas_hashes_reservados,
    qualificar,
)
from instrumento_avaliacao.juiz.resultado import resultado_campanha
from instrumento_avaliacao.modelos import carregar_modelos, exigir_rotas
from tutor_socratico.infraestrutura.configuracao import (
    OpenRouterConfig,
    load_local_environment,
)
from tutor_socratico.infraestrutura.llm import OpenRouterAdapter


def _juizes_pagos(args: argparse.Namespace, parser: argparse.ArgumentParser, executar) -> dict:
    """Comandos pagos dos juízes: exigem --execute e teto positivo; custo por generation_id."""
    if not args.execute:
        parser.error(f"{args.command} exige --execute para permitir chamadas pagas")
    if not args.cost_cap_usd > 0:
        parser.error("--cost-cap-usd deve ser positivo")
    try:
        exigir_rotas(carregar_modelos(), ["juiz_A", "juiz_B"])
    except ValueError as erro:
        parser.error(str(erro))
    load_local_environment()
    base = OpenRouterConfig.from_env()
    juizes = carregar_juizes()
    with httpx.Client() as client:
        # Juízes: a repetição de JSON truncado fica com POLITICA_REPETICAO_JUIZ (ADR 0018), que registra e cobra cada tentativa.
        adapters = {juiz["id"]: OpenRouterAdapter(config_juiz(base, juiz), client=client, truncation_retries=0) for juiz in juizes}
        orcamento = Orcamento(args.cost_cap_usd, lambda generation_id: _generation_cost(client, base, generation_id)[0])
        return executar(adapters, orcamento)


ROTULO_AJUDA = ("pasta rotulada dos pareceres (juizes_<rotulo>/, resultado_<rotulo>.json|md) para rejulgar "
               "com outra rubrica sem tocar a pasta antiga; só [a-z0-9_], até 40 caracteres")
REAVALIACAO_AJUDA = ("reavaliação de amostra (estabilidade dos juízes): julga de novo só as 9 primeiras conversas "
                    "conformes por (repetição, exercício, perfil) em juizes_reavaliacao/, sem tocar juizes/; "
                    "implica --rotulo reavaliacao")
TETO_AJUDA = ("teto de custo desta execução (USD); a execução para ao atingir o teto e pode excedê-lo "
              "em até 10 pareceres (os que estão em andamento no julgamento paralelo)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--phase", choices=("rehearsal", "pilot", "campaign"), default="campaign")
    run = commands.add_parser("run")
    run.add_argument("--phase", choices=("rehearsal", "pilot", "campaign"), required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--api-url", default="http://127.0.0.1:8000")
    run.add_argument("--manifest", type=Path)
    run.add_argument("--execute", action="store_true")
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--pilot-run-id", required=True)
    freeze.add_argument("--review", required=True, type=Path)
    freeze.add_argument("--output", required=True, type=Path)
    analyse = commands.add_parser("analyse")
    analyse.add_argument("--run-id", required=True)
    reconcile = commands.add_parser("reconcile-costs")
    reconcile.add_argument("--run-id", required=True)
    judge_controls = commands.add_parser("judge-controls", help="gera os controles e grava os hashes reservados (sem rede)")
    judge_controls.add_argument("--semente", type=int, default=0,
                                help="muda só o conjunto reservado; use outra depois de um gate reprovado (padrão 0)")
    judge_plan = commands.add_parser("judge-plan", help="conta os pacotes que os juízes receberiam (sem rede)")
    judge_plan.add_argument("--run-id", required=True)
    judge_plan.add_argument("--rotulo", help=ROTULO_AJUDA)
    judge_plan.add_argument("--reavaliacao", action="store_true", help=REAVALIACAO_AJUDA)
    judge_qualify = commands.add_parser("judge-qualify", help="pago: os dois juízes, duas vezes, nos controles")
    judge_qualify.add_argument("--cost-cap-usd", type=float, required=True, help=TETO_AJUDA)
    judge_qualify.add_argument("--conjunto", choices=("reservado", "desenvolvimento"), default="reservado",
                               help="só o reservado pode aprovar o gate da campanha")
    judge_qualify.add_argument("--execute", action="store_true")
    judge_run = commands.add_parser("judge-run", help="pago: julga os episódios conformes e não conformes")
    judge_run.add_argument("--run-id", required=True)
    judge_run.add_argument("--cost-cap-usd", type=float, required=True, help=TETO_AJUDA)
    judge_run.add_argument("--execute", action="store_true")
    judge_run.add_argument("--rotulo", help=ROTULO_AJUDA)
    judge_run.add_argument("--reavaliacao", action="store_true", help=REAVALIACAO_AJUDA)
    judge_compare = commands.add_parser("judge-compare", help="resultado e comparação dos juízes (sem rede)")
    judge_compare.add_argument("--run-id", required=True)
    judge_compare.add_argument("--rotulo", help=ROTULO_AJUDA)
    args = parser.parse_args()
    try:
        rotulo = rotulo_efetivo(getattr(args, "rotulo", None), getattr(args, "reavaliacao", False))
    except ValueError as erro:
        parser.error(str(erro))

    if args.command == "plan":
        print(json.dumps({"phase": args.phase, "episodes": [episode.episode_id for episode in planned_episodes(phase=args.phase)]}, ensure_ascii=False, indent=2))
        return
    if args.command == "analyse":
        print(analyse_run(args.run_id))
        return
    if args.command == "reconcile-costs":
        load_local_environment()
        print(reconcile_costs(args.run_id))
        return
    if args.command == "freeze":
        print(json.dumps(freeze_manifest(args.pilot_run_id, args.review, args.output), ensure_ascii=False, indent=2))
        return
    if args.command == "judge-controls":
        if args.semente < 0:
            parser.error("--semente deve ser um inteiro não negativo")
        caminho = gravar_hashes_reservados(semente=args.semente)
        print(f"{caminho} ({len(linhas_hashes_reservados(args.semente).splitlines()) - 1} controles reservados, "
              f"semente {args.semente})")
        return
    if args.command == "judge-plan":
        print(json.dumps(planejar_julgamento(args.run_id, rotulo=rotulo, reavaliacao=args.reavaliacao),
                         ensure_ascii=False, indent=2))
        return
    if args.command == "judge-compare":
        print(resultado_campanha(args.run_id, args.rotulo))
        return
    if args.command == "judge-qualify":
        saida = PROCESSED_ROOT / f"{PREFIXO_SAIDA}{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        documento = _juizes_pagos(args, parser, lambda adapters, orcamento: qualificar(
            adapters, conjunto=args.conjunto, saida=saida, orcamento=orcamento))
        print(json.dumps({"saida": str(saida), **documento}, ensure_ascii=False, indent=2))
        return
    if args.command == "judge-run":
        print(json.dumps(_juizes_pagos(args, parser, lambda adapters, orcamento: julgar_campanha(
            args.run_id, adapters, orcamento=orcamento, rotulo=rotulo, reavaliacao=args.reavaliacao)),
            ensure_ascii=False, indent=2))
        return
    if not args.execute:
        parser.error("run exige --execute para permitir chamadas pagas")
    try:
        exigir_rotas(carregar_modelos(), ["tutor", "aluno"])
    except ValueError as erro:
        parser.error(str(erro))
    load_local_environment()
    token = os.getenv("TUTOR_RESEARCH_TOKEN", "")
    if not token:
        parser.error("TUTOR_RESEARCH_TOKEN deve estar definido no backend e no executor")
    config = OpenRouterConfig.from_env()
    manifest = None
    if args.phase == "campaign":
        if not args.manifest:
            parser.error("campanha exige --manifest")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        check_manifest(manifest)
    elif args.manifest:
        parser.error("piloto e ensaio não usam manifesto de campanha")
    path = run_phase(phase=args.phase, run_id=args.run_id, base_url=args.api_url.rstrip("/"), token=token, config=config, manifest=manifest)
    print(path)


if __name__ == "__main__":
    main()
