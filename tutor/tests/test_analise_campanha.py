from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from instrumento_avaliacao import campanha
from instrumento_avaliacao.aluno_simulado import planned_episodes
from instrumento_avaliacao.campanha import analise, custos
from instrumento_avaliacao.campanha.analise import barreiras_internas, conformidade


class AnaliseDeterministicaTests(unittest.TestCase):
    def test_barreiras_internas_counts_n1v_and_barrier_and_fallback(self) -> None:
        row = {"audit": {"events": [
            {"agent": "publication_barrier", "status": "rejected", "n1v_criteria": {"nao_revela": "nao_atende"}, "barrier_approved": True},
            {"agent": "publication_barrier", "status": "rejected", "barrier_approved": False, "reason": "nivel_5_bloqueado"},
            {"agent": "N1-G", "source": "deterministic_policy"},
        ]}}
        self.assertEqual(barreiras_internas(row), {
            "n1v_reprovou_nao_revela": 1, "barreira_bloqueou": 1,
            "barreira_bloqueou_por_motivo": {"nivel_5_bloqueado": 1}, "fallback_publicado": 1,
        })

    def test_barreiras_internas_without_audit_is_zeroed(self) -> None:
        self.assertEqual(barreiras_internas({"audit": None}), {
            "n1v_reprovou_nao_revela": 0, "barreira_bloqueou": 0,
            "barreira_bloqueou_por_motivo": {}, "fallback_publicado": 0,
        })

    def test_barreiras_internas_no_longer_apply_e1_to_rejected_candidates(self) -> None:
        # Decisão 11, revisão de 26/10/2026 (opção B): o E1 saiu da campanha, inclusive das candidatas reprovadas.
        from instrumento_avaliacao.oraculos import load_oracle
        referencia = load_oracle("E01").reference_path.read_text(encoding="utf-8").strip()
        row = {"exercise_id": "E01", "audit": {"events": [
            {"agent": "publication_barrier", "status": "rejected", "mode": "PROBING", "candidate_text": f"Use:\n{referencia}"},
        ]}}
        self.assertFalse(any(chave.startswith("candidatas_") for chave in barreiras_internas(row)))
        self.assertFalse(hasattr(analise, "candidatas_reprovadas"))

    def test_conformidade(self) -> None:
        ok = {k: {"status": "PASS"} for k in ("P1", "P2", "I1", "I2", "I3", "I4", "I5")}
        self.assertEqual(conformidade(ok), "conforme")
        self.assertEqual(conformidade({**ok, "I2": {"status": "FAIL"}}), "nao_conforme")
        self.assertEqual(conformidade({**ok, "I3": {"status": "INCONCLUSIVO"}}), "inconclusivo")
        self.assertEqual(conformidade({**ok, "I3": {"status": "INCONCLUSIVO"}, "P1": {"status": "FAIL"}}), "nao_conforme")

    def test_analyse_run_writes_no_manual_coding_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw, processed, results = Path(temp) / "raw", Path(temp) / "processed", Path(temp) / "results"
            run = raw / "ensaio_x"
            run.mkdir(parents=True)
            (run / "runtime.json").write_text('{"phase":"rehearsal"}', encoding="utf-8")
            rows = [{"episode_id": e.episode_id, "profile_id": e.profile_id, "status": "INCONCLUSIVO", "turns": [], "audit": None, "cost_usd": 0.0, "error": None}
                    for e in planned_episodes(phase="rehearsal")]
            (run / "episodes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            with patch.object(analise, "DATA_ROOT", raw), patch.object(custos, "DATA_ROOT", raw), \
                 patch.object(analise, "PROCESSED_ROOT", processed), patch.object(analise, "RESULTS_ROOT", results):
                output = campanha.analyse_run("ensaio_x")
            self.assertFalse((output / "ficha_codificacao.csv").exists())
            self.assertFalse((output / "ficha_codificacao_turnos.csv").exists())
            self.assertFalse((output / "proposta_codificacao_turnos.json").exists())
            self.assertTrue((output / "matriz_requisito_episodio.csv").exists())
            header = (output / "matriz_requisito_episodio.csv").read_text(encoding="utf-8-sig").splitlines()[0]
            self.assertEqual(header, "episode_id,requirement_id,status,evidence_ref,reason")
            summary = json.loads((output / "resumo.json").read_text(encoding="utf-8"))
            self.assertNotIn("human_coding", summary)
            first = rows[0]["episode_id"]
            self.assertEqual(summary["conformidade"][first], "inconclusivo")
            self.assertEqual(summary["barreiras_internas"][first]["fallback_publicado"], 0)
            report = (results / "ensaio_x.md").read_text(encoding="utf-8")
            self.assertIn("juízes pendentes", report)
            self.assertIn("matriz_requisito_episodio.csv", report)
            self.assertNotIn("codificação autoral", report)


class DescriptiveSignalsTests(unittest.TestCase):
    def test_contract_gap_and_criteria_are_descriptive(self) -> None:
        import copy

        from instrumento_avaliacao.campanha import verify_episode
        from instrumento_avaliacao.campanha.analise import (
            contract_gap_invalid,
            contract_gap_signals,
            n1v_criteria_summary,
        )
        from tests.support import safe_episode, sync_hashes
        plain = safe_episode()
        row = copy.deepcopy(plain)
        events = row["audit"]["events"]
        events.append({"agent": "N2_transition", "turn": 2, "status": "ok", "contract_gap": {
            "present": True, "kind": "tema_novo", "evidence": [{"turn": 2, "quote": "outro tema"}]}})
        events.append({"agent": "N2_transition", "turn": 3, "status": "ok", "contract_gap": {"present": False, "kind": "nenhum", "evidence": []}})
        # Sinal incoerente (contract_gap_valid=false) não conta como sinal; vai para contract_gap_invalid.
        events.append({"agent": "N2_transition", "turn": 4, "status": "ok", "contract_gap_valid": False,
                       "contract_gap_issue": "citacao_inadmissivel", "contract_gap": {
                           "present": True, "kind": "outro", "evidence": [{"turn": 4, "quote": "nunca dito"}]}})
        events.append({"agent": "publication_barrier", "turn": 2, "status": "rejected", "n1v_criteria": {
            "lacuna_real": "atende", "nao_revela": "nao_atende", "ancorada": "atende", "categorizavel": "atende"}})
        events.append({"agent": "publication_barrier", "turn": 2, "status": "approved", "n1v_criteria": {
            "lacuna_real": "atende", "nao_revela": "atende", "ancorada": "atende", "categorizavel": "atende"}})
        # Fechamento (nao_se_aplica) e registros históricos sem o campo não contam.
        events.append({"agent": "publication_barrier", "turn": 4, "status": "approved", "n1v_criteria": {
            key: "nao_se_aplica" for key in ("lacuna_real", "nao_revela", "ancorada", "categorizavel")}})
        sync_hashes(row)
        self.assertEqual(len(contract_gap_signals(row)), 1)
        self.assertEqual(contract_gap_signals(row)[0]["kind"], "tema_novo")
        self.assertEqual(contract_gap_signals(plain), [])
        self.assertEqual(contract_gap_invalid(row), 1)
        self.assertEqual(contract_gap_invalid(plain), 0)
        summary = n1v_criteria_summary(row)
        self.assertEqual(summary["nao_revela_nao_atende"], 1)
        self.assertEqual(summary["nao_revela_atende"], 1)
        self.assertEqual(summary["lacuna_real_atende"], 2)
        self.assertEqual(n1v_criteria_summary(plain), {})
        self.assertEqual(verify_episode(row), verify_episode(plain))

    def test_resumo_descriptive_signals_shape_and_legacy_null(self) -> None:
        import copy
        from unittest.mock import patch

        from instrumento_avaliacao.aluno_simulado import planned_episodes
        from instrumento_avaliacao.campanha import analise, custos
        from tests.support import safe_episode
        planned = planned_episodes(phase="rehearsal")
        current = safe_episode()
        current["audit"]["resolution_contract_version"] = "5.0.0"
        current["audit"]["events"].extend([
            {"agent": "N2_transition", "turn": 2, "status": "ok", "contract_gap_valid": True, "contract_gap_issue": "",
             "contract_gap": {"present": True, "kind": "tema_novo", "evidence": [{"turn": 2, "quote": "Tentativa 2"}]}},
            {"agent": "N2_transition", "turn": 3, "status": "ok", "contract_gap_valid": False, "contract_gap_issue": "citacao_inadmissivel",
             "contract_gap": {"present": True, "kind": "outro", "evidence": [{"turn": 3, "quote": "nunca dito"}]}},
            {"agent": "publication_barrier", "turn": 2, "status": "approved", "n1v_criteria": {
                "lacuna_real": "atende", "nao_revela": "nao_atende", "ancorada": "atende", "categorizavel": "atende"}},
        ])
        empty = safe_episode()
        empty["audit"]["resolution_contract_version"] = "5.0.0"
        legacy = copy.deepcopy(current)
        legacy["audit"]["resolution_contract_version"] = "2.1.0"
        rows = []
        for index, item in enumerate(planned):
            if index < 3:
                row = (current, empty, legacy)[index]
                rows.append({**copy.deepcopy(row), "episode_id": item.episode_id, "profile_id": item.profile_id, "exercise_id": "E01"})
            else:
                rows.append({"episode_id": item.episode_id, "profile_id": item.profile_id, "status": "INCONCLUSIVO", "turns": [], "audit": None})
        with tempfile.TemporaryDirectory() as temp:
            raw, processed, results = Path(temp) / "raw", Path(temp) / "processed", Path(temp) / "results"
            run = raw / "ensaio_sinais"
            run.mkdir(parents=True)
            (run / "runtime.json").write_text('{"phase":"rehearsal"}', encoding="utf-8")
            (run / "episodes.jsonl").write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
            with patch.object(analise, "DATA_ROOT", raw), patch.object(custos, "DATA_ROOT", raw), \
                    patch.object(analise, "PROCESSED_ROOT", processed), patch.object(analise, "RESULTS_ROOT", results):
                output = analise.analyse_run("ensaio_sinais")
            signals = json.loads((output / "resumo.json").read_text(encoding="utf-8"))["descriptive_signals"]
        first, second, third, fourth = (signals[item.episode_id] for item in planned[:4])
        self.assertEqual(first, {
            "contract_gap_signals": 1, "contract_gap_invalid": 1,
            "n1v_criteria": {"lacuna_real_atende": 1, "nao_revela_nao_atende": 1, "ancorada_atende": 1, "categorizavel_atende": 1},
        })
        # 5.0.0 sem sinais (zero) é distinguível de registro anterior a 5.0.0 (null).
        self.assertEqual(second, {"contract_gap_signals": 0, "contract_gap_invalid": 0, "n1v_criteria": {}})
        self.assertEqual(third, {"contract_gap_signals": None, "contract_gap_invalid": None, "n1v_criteria": None})
        self.assertEqual(fourth, {"contract_gap_signals": None, "contract_gap_invalid": None, "n1v_criteria": None})


if __name__ == "__main__":
    unittest.main()
