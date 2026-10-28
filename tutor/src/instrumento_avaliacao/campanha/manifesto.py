"""Congelamento e conferência do manifesto da campanha."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from instrumento_avaliacao.aluno_simulado import (
    PROFILE_PATH,
    STUDENT_GENERATION_POLICY,
    load_profiles,
    planned_episodes,
)
from instrumento_avaliacao.caminhos import RAIZ_TUTOR
from instrumento_avaliacao.campanha.comum import DATA_ROOT, _now, _require_run_id
from instrumento_avaliacao.campanha.execucao import (
    PARALLEL_EPISODES,
    digest,
    retrato_modelos,
)
from instrumento_avaliacao.campanha.verificadores import _student_attempts_valid
from instrumento_avaliacao.juiz.rubrica import CAMINHO_RUBRICA
from instrumento_avaliacao.util import ler_jsonl
from tutor_socratico.contratos.esquemas import RESOLUTION_CONTRACT_VERSION
from tutor_socratico.contratos.politica_correcao import TUTOR_CORRECTION_POLICY
from tutor_socratico.sela.nivel0.escopo import public_record as domain_scope_record


def freeze_manifest(pilot_run_id: str, review_path: Path, output_path: Path) -> dict[str, Any]:
    _require_run_id(pilot_run_id)
    rows = ler_jsonl(DATA_ROOT / pilot_run_id / "episodes.jsonl")
    expected = {episode.episode_id for episode in planned_episodes(phase="pilot")}
    if {row.get("episode_id") for row in rows} != expected or len(rows) != 5:
        raise ValueError("piloto não contém exatamente cinco episódios planejados")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if set(review.get("approved_episode_ids", [])) != expected:
        raise ValueError("aprovação de aderência do piloto incompleta")
    pricing = review.get("pricing_snapshot")
    if not isinstance(pricing, dict) or not all(pricing.get(key) for key in ("checked_at", "source_url", "tutor", "student")):
        raise ValueError("revisão do piloto exige preços e data da consulta dos dois modelos")
    if any(row.get("status") != "COMPLETO" or not isinstance(row.get("cost_usd"), (int, float)) for row in rows):
        raise ValueError("piloto incompleto ou sem custo verificável")
    maximum = max(row["cost_usd"] for row in rows)
    if maximum <= 0:
        raise ValueError("custo do piloto não observável")
    pilot_runtime = json.loads((DATA_ROOT / pilot_run_id / "runtime.json").read_text(encoding="utf-8"))
    if pilot_runtime.get("phase") != "pilot" or pilot_runtime.get("student_generation_policy") != STUDENT_GENERATION_POLICY or (pilot_runtime.get("tutor") or {}).get("tutor_correction_policy") != TUTOR_CORRECTION_POLICY or (pilot_runtime.get("tutor") or {}).get("resolution_contract_version") != RESOLUTION_CONTRACT_VERSION:
        # Pilotos de política 1.0.0 ou sem contrato 2.0.0 (V9b, 13/10) não autorizam esta versão.
        raise ValueError("piloto sem política de geração vigente")
    if pilot_runtime.get("domain_scope") != domain_scope_record() or pilot_runtime.get("profiles_version") != load_profiles()["version"]:
        # O manifesto só congela os instrumentos que o piloto de fato usou.
        raise ValueError("piloto com escopo curricular ou perfis diferentes dos vigentes")
    registro = retrato_modelos()
    if pilot_runtime.get("registro_modelos") != registro:
        # Piloto rodado com outros modelos, rotas ou versão do registro não autoriza esta campanha.
        raise ValueError("piloto com registro de modelos diferente do vigente")
    if pilot_runtime.get("parallel_episodes") != PARALLEL_EPISODES:
        # Piloto sequencial (sem o campo) ou com outro paralelismo não autoriza esta campanha.
        raise ValueError("piloto com paralelismo diferente do vigente")
    attempt_journal = DATA_ROOT / pilot_run_id / "student_attempts.jsonl"
    if not attempt_journal.is_file() or any("student_attempts" not in row or not _student_attempts_valid(row) for row in rows):
        raise ValueError("piloto sem auditoria completa das tentativas do aluno")
    journal_events = ler_jsonl(attempt_journal)
    # Com episódios em paralelo, o journal intercala episódios; a ordem vale dentro de cada episódio.
    by_episode: dict[str, list[Any]] = {}
    for event in journal_events:
        by_episode.setdefault(event.get("episode_id"), []).append(event)
    if by_episode != {row["episode_id"]: row["student_attempts"] for row in rows if row["student_attempts"]}:
        raise ValueError("journal de tentativas diverge dos episódios do piloto")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=RAIZ_TUTOR, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=RAIZ_TUTOR, capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise ValueError("commit limpo obrigatório antes de congelar o manifesto")
    watched = manifest_watched_paths()
    manifest = {
        "version": "1.4.0", "kind": "simulated_student_campaign", "frozen_at": _now(),
        "parallel_episodes": PARALLEL_EPISODES,
        "pilot_run_id": pilot_run_id, "episode_order": [episode.episode_id for episode in planned_episodes(phase="campaign")],
        "git_commit": commit,
        "episode_count": 45, "max_student_turns": 10,
        "domain_scope": domain_scope_record(), "profiles_version": load_profiles()["version"],
        "models": registro["models"], "modelos_sha256": registro["modelos_sha256"],
        "cost_cap_usd": round(1.5 * 45 * maximum, 8), "pilot_max_episode_cost_usd": maximum,
        "pricing_snapshot": pricing,
        "tutor_runtime": pilot_runtime["tutor"], "student_runtime": pilot_runtime["student"],
        "student_generation_policy": dict(STUDENT_GENERATION_POLICY),
        "tutor_correction_policy": dict(TUTOR_CORRECTION_POLICY),
        "resolution_contract_version": RESOLUTION_CONTRACT_VERSION,
        "pilot_attempts_sha256": digest(attempt_journal),
        "sha256": {str(path.relative_to(RAIZ_TUTOR)).replace("\\", "/"): digest(path) for path in watched},
        "pilot_review_sha256": digest(review_path),
    }
    if output_path.exists():
        raise FileExistsError(output_path)
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def manifest_watched_paths() -> list[Path]:
    """Artefatos cujo SHA-256 o manifesto congela, inclusive o catálogo revisado de lacunas."""
    watched = [PROFILE_PATH, RAIZ_TUTOR / "scripts/executar_alunos_simulados.py"]
    for pacote in ("tutor_socratico", "instrumento_avaliacao"):
        watched.extend(
            sorted(
                path for path in (RAIZ_TUTOR / "src" / pacote).rglob("*.py")
                if "__pycache__" not in path.parts
            )
        )
    watched.append(RAIZ_TUTOR / "src/tutor_socratico/sela/nivel0/dominio/escopo_cs1.v1.json")
    watched.extend(sorted((RAIZ_TUTOR / "avaliacao/exercicios").glob("*/oraculo.v1.json")))
    watched.extend(sorted((RAIZ_TUTOR / "avaliacao/exercicios").glob("*/enunciado.md")))
    # Juízes (spec §6): rubrica vigente (CAMINHO_RUBRICA), configuração, frases-modelo do gerador e hashes dos controles reservados.
    watched.append(CAMINHO_RUBRICA)
    watched.extend(
        RAIZ_TUTOR / "avaliacao/juiz" / nome
        for nome in ("frases_modelo.v1.json", "controles_reservados.v1.sha256")
    )
    watched.append(RAIZ_TUTOR / "avaliacao/modelos.v1.json")
    return watched


def check_manifest(manifest: Mapping[str, Any]) -> None:
    """Confere o manifesto contra o instrumento vigente.

    Exige a forma nova (modelo e rota dos quatro papéis mais `modelos_sha256`, iguais ao registro vigente).
    """
    if manifest.get("version") != "1.4.0" or manifest.get("episode_count") != 45 or manifest.get("max_student_turns") != 10:
        raise ValueError("manifesto inválido")
    if manifest.get("parallel_episodes") != PARALLEL_EPISODES:
        raise ValueError("manifesto com paralelismo diferente do vigente")
    registro = retrato_modelos()
    if manifest.get("models") != registro["models"] or manifest.get("modelos_sha256") != registro["modelos_sha256"]:
        raise ValueError("modelos do manifesto divergentes do registro de modelos")
    if not isinstance(manifest.get("tutor_runtime"), dict) or not isinstance(manifest.get("student_runtime"), dict):
        raise ValueError("parâmetros dos dois papéis ausentes do manifesto")
    if not isinstance(manifest.get("pricing_snapshot"), dict):
        raise ValueError("preços não registrados no manifesto")
    if manifest.get("student_generation_policy") != STUDENT_GENERATION_POLICY:
        raise ValueError("política de geração do aluno diverge do instrumento vigente")
    if manifest.get("tutor_correction_policy") != TUTOR_CORRECTION_POLICY:
        raise ValueError("politica de correcao do tutor ausente ou divergente")
    if manifest.get("resolution_contract_version") != RESOLUTION_CONTRACT_VERSION:
        raise ValueError("contrato de resolução ausente ou divergente")
    if manifest.get("domain_scope") != domain_scope_record():
        raise ValueError("escopo curricular do N0-A ausente ou divergente")
    if manifest.get("profiles_version") != load_profiles()["version"]:
        raise ValueError("versão dos perfis ausente ou divergente")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=RAIZ_TUTOR, capture_output=True, text=True, check=True).stdout.strip()
    if manifest.get("git_commit") != commit:
        raise ValueError("commit diverge do manifesto")
    # O conjunto vigiado hoje deve ser o congelado: arquivo novo ou removido também invalida o manifesto.
    frozen = set(manifest.get("sha256", {}))
    current = {str(path.relative_to(RAIZ_TUTOR)).replace("\\", "/") for path in manifest_watched_paths()}
    if frozen != current:
        added, missing = sorted(current - frozen), sorted(frozen - current)
        raise ValueError(f"conjunto de artefatos vigiados mudou após congelamento; adicionados: {added}; ausentes: {missing}")
    for relative, expected_hash in manifest.get("sha256", {}).items():
        path = RAIZ_TUTOR / relative
        if not path.is_file() or digest(path) != expected_hash:
            raise ValueError(f"artefato mudou após congelamento: {relative}")
