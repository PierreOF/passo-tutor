"""Read-only review of preserved experiments, without database or LLM."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tutor_socratico.api.revisao_experimentos import ExperimentReview


class ExperimentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / 'dados_brutos'
        self.raw.mkdir()
        self.review = ExperimentReview(self.root)

    def episode(self, run='run-a', **overrides):
        folder = self.raw / run
        folder.mkdir(exist_ok=True)
        (folder / 'runtime.json').write_text(json.dumps({'phase': 'pilot', 'run_id': run}), encoding='utf-8')
        record = {'episode_id': 'E01-P01-R01', 'exercise_id': 'E01', 'profile_id': 'P01', 'repetition': 1,
                  'status': 'COMPLETO', 'turns': [{'turn': 1, 'student_message': 'Minha dúvida', 'student_code': 'x = 1', 'tutor_message': 'Qual regra?', 'state': {'mode': 'CLOSING_LIMIT'}}],
                  'audit': {'resolution_criterion': 'Explicar', 'prompt': 'SECRET'}, 'api_key': 'SECRET'}
        record.update(overrides)
        (folder / 'episodes.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')
        return folder

    def test_catalog_and_public_transcript_are_read_only_and_allowlisted(self):
        folder = self.episode()
        before = (folder / 'episodes.jsonl').read_bytes()
        runs = self.review.runs()
        self.assertEqual(runs['items'][0]['kind'], 'pilot')
        cases = self.review.cases('run-a')
        self.assertEqual(cases['total'], 1)
        self.assertNotIn('messages', cases['items'][0])
        detail = self.review.case('run-a', 'E01-P01-R01')
        self.assertEqual([m['role'] for m in detail['messages']], ['student', 'tutor'])
        self.assertEqual(detail['messages'][0]['code'], 'x = 1')
        self.assertNotIn('SECRET', json.dumps(detail))
        self.assertIsNone(detail['technical_result'])
        self.assertEqual(before, (folder / 'episodes.jsonl').read_bytes())

    def test_case_summary_includes_gap_summary(self):
        self.episode(audit={'gap_summary': {'gap_type': 'concepcao_equivocada', 'claim_text': 'acha que = compara', 'extra': 'SECRET'}})
        summary = self.review.cases('run-a')['items'][0]
        self.assertEqual(summary['gap_summary'], {'gap_type': 'concepcao_equivocada', 'claim_text': 'acha que = compara'})
        self.assertNotIn('SECRET', json.dumps(self.review.case('run-a', 'E01-P01-R01')))
        self.episode(run='run-b')
        self.assertIsNone(self.review.cases('run-b')['items'][0]['gap_summary'])

    def test_replay_uses_scenario_and_does_not_reconstruct_code(self):
        row = {'run_id': 'replay', 'case_id': 'E01-S01', 'exercise_id': 'E01', 'scenario': 'pedido_direto', 'turn': 1, 'student_message': 'Faça', 'published_text': 'Tente explicar.', 'student_code_sha256': 'abc'}
        (self.raw / 'replay.jsonl').write_text(json.dumps(row), encoding='utf-8')
        detail = self.review.case('replay', 'E01-S01')
        self.assertEqual(detail['scenario'], 'pedido_direto')
        self.assertIsNone(detail['profile_id'])
        self.assertTrue(detail['messages'][0]['code_unavailable'])

    def test_partial_tail_and_corruption_are_visible_and_isolated(self):
        folder = self.episode()
        with (folder / 'episodes.jsonl').open('a', encoding='utf-8') as stream:
            stream.write('{"unfinished":')
        self.assertTrue(self.review.cases('run-a')['warnings'])
        (self.raw / 'bad.jsonl').write_text('bad\n{}\n', encoding='utf-8')
        runs = self.review.runs()['items']
        self.assertEqual(len(runs), 2)
        self.assertTrue(next(r for r in runs if r['run_id'] == 'bad')['warnings'])

    def test_pending_input_and_missing_response_are_not_fabricated(self):
        self.episode(status='INCONCLUSIVO', pending_turn={'turn': 2, 'student_message': 'E agora?', 'student_code': ''})
        detail = self.review.case('run-a', 'E01-P01-R01')
        self.assertTrue(detail['messages'][-1]['pending'])
        self.assertEqual(detail['messages'][-1]['role'], 'student')

    def test_filters_pagination_and_ids(self):
        self.episode()
        self.assertEqual(self.review.cases('run-a', profile_id='P02')['total'], 0)
        self.assertEqual(self.review.cases('run-a', offset=1)['items'], [])
        for bad in ['../run-a', '..', 'x/y', 'x\\y']:
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.review.cases(bad)
        with self.assertRaises(FileNotFoundError):
            self.review.case('run-a', 'missing')

    def test_unknown_format_is_visible(self):
        (self.raw / 'old.jsonl').write_text('{"other": 1}', encoding='utf-8')
        self.assertEqual(self.review.runs()['items'][0]['kind'], 'unsupported')

    def test_missing_artifacts_empty(self):
        self.assertEqual(ExperimentReview(self.root / 'missing').runs()['items'], [])

    def test_external_symlinks_are_not_read(self):
        outside = self.root / 'outside.jsonl'
        outside.write_text('{"secret":1}', encoding='utf-8')
        try:
            (self.raw / 'escape.jsonl').symlink_to(outside)
        except OSError:
            self.skipTest('symlink permission unavailable')
        self.assertEqual(self.review.runs()['items'], [])

    def test_recorded_matrix_and_reconciliation_are_exposed_without_evaluation(self):
        self.episode()
        processed = self.root / 'dados_processados' / 'run-a'
        processed.mkdir(parents=True)
        (processed / 'resumo.json').write_text(json.dumps({'episode_status': [{'episode_id': 'E01-P01-R01', 'status_original': 'INCONCLUSIVO', 'status_efetivo': 'COMPLETO', 'motivo': 'custo_reconciliado'}]}), encoding='utf-8')
        (processed / 'matriz_requisito_episodio.csv').write_text('episode_id,requirement_id,status,evidence_ref,reason\nE01-P01-R01,VT-01,PASS,ref,registrado\n', encoding='utf-8')
        detail = self.review.case('run-a', 'E01-P01-R01')
        self.assertEqual(detail['technical_result'], 'PASS')
        self.assertEqual(detail['reconciliation']['status_original'], 'INCONCLUSIVO')
        self.assertEqual(detail['details']['checks'][0]['requirement_id'], 'VT-01')

    def test_malformed_optional_fields_do_not_break_other_cases(self):
        self.episode(audit={'resolution_requirements': None, 'events': None})
        self.assertEqual(self.review.runs()['items'][0]['case_count'], 1)

    def test_preserves_public_opening_and_timestamps_without_duplicating_turns(self):
        self.episode(audit={'session': {'messages': [
            {'role': 'tutor', 'text': 'Vamos começar.', 'createdAt': '2026-10-15T10:00:00Z'},
            {'role': 'student', 'text': 'Minha dúvida', 'createdAt': '2026-10-15T10:00:01Z'},
            {'role': 'tutor', 'text': 'Qual regra?', 'createdAt': '2026-10-15T10:00:02Z'}]}})
        detail = self.review.case('run-a', 'E01-P01-R01')
        self.assertEqual([m['text'] for m in detail['messages']], ['Vamos começar.', 'Minha dúvida', 'Qual regra?'])
        self.assertEqual(detail['messages'][1]['created_at'], '2026-10-15T10:00:01Z')

    def test_malformed_collections_keep_readable_case_and_warning(self):
        self.episode(audit={'events': 1, 'resolution_requirements': True})
        cases = self.review.cases('run-a')
        self.assertEqual(cases['total'], 1)
        self.assertTrue(cases['warnings'])

    def test_replay_retains_allowlisted_validation_events_and_hashes(self):
        row = {'case_id':'E01-S01', 'exercise_id':'E01', 'scenario':'direto', 'turn':1,
               'student_message':'Ajuda', 'published_text':'Pense.', 'scope_hash':'scope', 'diagnosis_hash':'diag',
               'agent_events':[{'agent':'publication_barrier', 'reason':'nivel_5_bloqueado', 'prompt':'SECRET'}]}
        (self.raw / 'replay.jsonl').write_text(json.dumps(row), encoding='utf-8')
        detail = self.review.case('replay', 'E01-S01')
        self.assertEqual(detail['details']['turns'][0]['scope_hash'], 'scope')
        self.assertEqual(detail['details']['events'][0]['reason'], 'nivel_5_bloqueado')
        self.assertNotIn('SECRET', json.dumps(detail))

    def test_audited_response_after_timeout_replaces_unconfirmed_pending(self):
        self.episode(turns=[], pending_turn={'turn':1, 'student_message':'Ajuda'}, audit={'session':{'messages':[
            {'role':'tutor','text':'Vamos começar.'}, {'role':'student','text':'Ajuda'}, {'role':'tutor','text':'Qual regra?'}]}})
        detail = self.review.case('run-a', 'E01-P01-R01')
        self.assertEqual(len(detail['messages']), 3)
        self.assertFalse(detail['messages'][1]['pending'])
        self.assertFalse(detail['messages'][1]['response_missing'])

    def test_complete_invalid_tail_is_corruption_not_active_write(self):
        self.episode()
        with (self.raw / 'run-a' / 'episodes.jsonl').open('a', encoding='utf-8') as stream:
            stream.write('{"a": invalid}')
        warnings = self.review.cases('run-a')['warnings']
        self.assertTrue(any('corrompido' in w for w in warnings))

    def test_run_listing_discovers_sources_once_and_does_not_build_transcripts(self):
        self.episode('run-a')
        self.episode('run-b')
        with patch.object(self.review, '_sources', wraps=self.review._sources) as discover, patch.object(self.review, '_episode', wraps=self.review._episode) as transcript:
            listing = self.review.runs()
        self.assertEqual(listing['total'], 2)
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(transcript.call_count, 0)
