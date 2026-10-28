"""Execução auditável da avaliação artificial pela API pública do tutor.

Este pacote só reexporta a API pública; a lógica vive em `execucao`, `custos`, `manifesto`,
`verificadores` e `analise` (constantes e utilitários comuns em `comum`).

As constantes reexportadas aqui (`DATA_ROOT`, `PROCESSED_ROOT`, `RESULTS_ROOT`...) são cópias da
referência: para trocá-las num teste, use `patch` no módulo que as lê, nunca neste pacote.
"""

from instrumento_avaliacao.campanha.analise import analyse_run
from instrumento_avaliacao.campanha.comum import (
    DATA_ROOT,
    OUT_OF_SCOPE_STATUS,
    PROCESSED_ROOT,
    RESULTS_ROOT,
)
from instrumento_avaliacao.campanha.custos import (
    COST_DEFERRED_WAITS_SECONDS,
    COST_ONLY_ERRORS,
    effective_status,
    effective_statuses,
    interruption_breakdown,
    provedores_esperados,
    reconcile_costs,
)
from instrumento_avaliacao.campanha.execucao import (
    digest,
    error_record,
    modelo_esperado,
    record_rejected_student_turn,
    recover_timed_out_turn,
    retrato_modelos,
    run_episode,
    run_phase,
    student_config,
)
from instrumento_avaliacao.campanha.manifesto import (
    check_manifest,
    freeze_manifest,
    manifest_watched_paths,
)
from instrumento_avaliacao.campanha.verificadores import (
    PRECONDITIONS,
    REQUIREMENTS,
    VT_TO_NEW,
    verify_episode,
)

__all__ = [
    "COST_DEFERRED_WAITS_SECONDS", "COST_ONLY_ERRORS", "DATA_ROOT",
    "OUT_OF_SCOPE_STATUS", "PRECONDITIONS", "PROCESSED_ROOT", "REQUIREMENTS", "RESULTS_ROOT",
    "analyse_run", "check_manifest", "digest", "effective_status",
    "effective_statuses", "error_record", "freeze_manifest",
    "interruption_breakdown", "manifest_watched_paths", "modelo_esperado", "provedores_esperados", "reconcile_costs",
    "record_rejected_student_turn", "recover_timed_out_turn", "retrato_modelos", "run_episode", "run_phase",
    "VT_TO_NEW", "student_config", "verify_episode",
]
