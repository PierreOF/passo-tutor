"""Read-only, allowlisted views of preserved TCC experimental artifacts.

This module never calls the tutor, evaluator, simulator or database.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

MAX_FILE = 64 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_TEXT = MAX_LINE


def pick(record: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {key: record[key] for key in keys if key in record and isinstance(record[key], (str, int, float, bool, type(None)))}


def text(value: Any) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ''


def records(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def events(value: Any) -> list[dict]:
    return [pick(e, ('agent', 'turn', 'status', 'guard_rule', 'guard_requirement_id', 'blocked', 'resolution_check', 'gap_resolved', 'interruption_category', 'reason', 'allowed_exposure_level', 'observed_exposure_level', 'exposure_level', 'generation_id', 'diagnosis_sha256', 'scope_sha256')) for e in records(value)]


class ExperimentReview:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.raw = self.root / 'dados_brutos'
        self.processed = self.root / 'dados_processados'

    @staticmethod
    def _id(value: str) -> str:
        if not value or value in {'.', '..'} or any(c in value for c in '/\\\x00'):
            raise ValueError('Identificador inválido.')
        return value

    def _within(self, path: Path, base: Path) -> bool:
        return base.resolve().is_relative_to(self.root) and path.resolve().is_relative_to(base.resolve()) and not path.is_symlink() and not base.is_symlink()

    def _json(self, path: Path, base: Path) -> dict:
        if not self._within(path, base) or not path.is_file():
            return {}
        if path.stat().st_size > MAX_FILE:
            raise ValueError('Artefato excede o limite de leitura.')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('Objeto JSON esperado.')
        return value

    def _sources(self) -> dict[str, Path]:
        if not self.raw.is_dir():
            return {}
        sources = {}
        for path in sorted(self.raw.iterdir()):
            if not self._within(path, self.raw):
                continue
            if path.is_dir():
                source = path / 'episodes.jsonl'
                if source.exists() and self._within(source, self.raw):
                    sources[path.name] = source
            elif path.suffix == '.jsonl':
                sources[path.stem] = path
        return sources

    def _rows(self, source: Path) -> tuple[list[tuple[int, dict]], list[str]]:
        if source.stat().st_size > MAX_FILE:
            return [], ['Artefato excede o limite de 64 MiB; leitura indisponível.']
        rows, warnings = [], []
        with source.open('rb') as stream:
            line_number = 0
            while line := stream.readline(MAX_LINE + 1):
                line_number += 1
                if len(line) > MAX_LINE:
                    warnings.append('Linha excede o limite de 2 MiB; leitura interrompida.')
                    break
                if not line.strip():
                    continue
                try:
                    row = json.loads(line.decode('utf-8-sig'))
                    if not isinstance(row, dict):
                        raise ValueError('object expected')
                    rows.append((line_number, row))
                except (ValueError, UnicodeError) as error:
                    incomplete = isinstance(error, json.JSONDecodeError) and (error.pos >= len(error.doc.rstrip()) - 1 or error.msg.startswith('Unterminated string'))
                    incomplete = incomplete or (isinstance(error, UnicodeDecodeError) and error.reason == 'unexpected end of data')
                    if incomplete and not line.endswith(b'\n') and not stream.peek(1):
                        warnings.append('Última linha incompleta; execução pode estar em gravação.')
                    else:
                        warnings.append(f'Linha {line_number} inválida; artefato corrompido.')
        return rows, warnings

    def _load(self, run_id: str, *, source: Path | None = None, overview: bool = False) -> tuple[dict, list[dict]]:
        self._id(run_id)
        source = source or self._sources().get(run_id)
        if source is None:
            raise FileNotFoundError(run_id)
        rows, warnings = self._rows(source)
        runtime, summary = {}, {}
        try:
            if source.name == 'episodes.jsonl':
                runtime = self._json(source.parent / 'runtime.json', self.raw)
            if not overview:
                summary = self._json(self.processed / run_id / 'resumo.json', self.processed)
        except (ValueError, OSError):
            warnings.append('Metadados indisponíveis ou inválidos.')
        episode_rows = [(n, r) for n, r in rows if isinstance(r.get('episode_id'), str)]
        replay_rows = [(n, r) for n, r in rows if isinstance(r.get('case_id'), str) and 'student_message' in r and 'published_text' in r]
        phase = runtime.get('phase') if isinstance(runtime.get('phase'), str) else None
        kind = phase if phase in {'pilot', 'campaign'} else 'historical'
        if overview:
            identifiers = {r['episode_id'] for _, r in episode_rows}
            if not episode_rows and replay_rows:
                kind = 'replay'
                identifiers = {r['case_id'] for _, r in replay_rows}
            elif not episode_rows and (rows or warnings):
                kind = 'unsupported'
                warnings.append('Formato não suportado ou sem casos legíveis.')
            return {
                'run_id': run_id, 'kind': kind, 'version': text(runtime.get('version')) or None,
                'started_at': text(runtime.get('started_at')) or None,
                'case_count': len(identifiers), 'warnings': warnings,
            }, []
        cases = []
        if episode_rows:
            for line, row in episode_rows:
                cases.append(self._episode(run_id, row, f'dados_brutos/{run_id}/episodes.jsonl:{line}'))
                audit = row.get('audit')
                if isinstance(audit, dict) and any(audit.get(k) is not None and not isinstance(audit[k], list) for k in ('events', 'resolution_requirements')):
                    warnings.append(f'Caso {row["episode_id"]}: coleção de auditoria inválida; mensagens preservadas.')
        elif replay_rows:
            kind = 'replay'
            groups: dict[str, list] = {}
            for n, row in replay_rows:
                groups.setdefault(row['case_id'], []).append((n, row))
            cases = [self._replay(run_id, values) for values in groups.values()]
        elif rows or warnings:
            kind = 'unsupported'
            warnings.append('Formato não suportado ou sem casos legíveis.')
        status_map = summary.get('episode_status', [])
        if isinstance(status_map, dict):
            status_map = [dict(v, episode_id=k) for k, v in status_map.items() if isinstance(v, dict)]
        for case in cases:
            match = next((s for s in status_map if isinstance(s, dict) and s.get('episode_id') == case['case_id']), None) if isinstance(status_map, list) else None
            if match:
                case['reconciliation'] = {**pick(match, ('status_original', 'status_efetivo', 'motivo')), 'source_ref': f'dados_processados/{run_id}/resumo.json'}
        # Read already-produced matrices; do not call any empirical verifier.
        for filename in ('matriz_requisito_episodio.csv', 'matriz_requisito_episodio_final.csv'):
            matrix_path = self.processed / run_id / filename
            if not self._within(matrix_path, self.processed) or not matrix_path.is_file():
                continue
            try:
                if matrix_path.stat().st_size > MAX_FILE:
                    raise ValueError('matrix too large')
                with matrix_path.open(encoding='utf-8-sig', newline='') as stream:
                    matrix = list(csv.DictReader(stream))
                for case in cases:
                    checks = [pick(r, ('requirement_id', 'status', 'evidence_ref', 'reason')) for r in matrix if r.get('episode_id') == case['case_id']]
                    if not checks:
                        continue
                    if filename.endswith('_final.csv') and 'checks' in case['details']:
                        case['details']['original_checks'] = case['details']['checks']
                        case['details']['original_checks_source'] = case['details']['checks_source']
                    case['details']['checks'] = checks
                    case['details']['checks_source'] = f'dados_processados/{run_id}/{filename}'
                    statuses = [r.get('status') for r in checks]
                    case['technical_result'] = 'FAIL' if 'FAIL' in statuses else 'INCONCLUSIVO' if 'INCONCLUSIVO' in statuses else 'PASS' if all(s == 'PASS' for s in statuses) else None
            except (ValueError, OSError, csv.Error):
                warnings.append('Matriz de resultados indisponível ou inválida.')
        # Missing records are shown only from a manifest carried by this exact run.
        try:
            manifest = self._json(source.parent / 'manifesto.json', self.raw) if source.name == 'episodes.jsonl' else {}
            if manifest.get('run_id') == run_id and isinstance(manifest.get('episode_order'), list):
                seen = {c['case_id'] for c in cases}
                for identifier in manifest['episode_order']:
                    if isinstance(identifier, str) and identifier not in seen:
                        cases.append(self._episode(run_id, {'episode_id': identifier, 'status': 'SEM_REGISTRO'}, f'dados_brutos/{run_id}/manifesto.json'))
        except (ValueError, OSError):
            warnings.append('Manifesto local inválido.')
        metadata = {
            'run_id': run_id, 'kind': kind, 'version': text(runtime.get('version')) or None,
            'started_at': text(runtime.get('started_at')) or None,
            'case_count': len(cases), 'warnings': warnings,
            'runtime': {name: pick(runtime.get(name), ('model', 'provider', 'temperature', 'resolution_contract_version')) for name in ('tutor', 'student')},
            'recorded_results': pick(summary, ('overall', 'status', 'pass', 'fail', 'inconclusive')),
        }
        return metadata, cases

    @staticmethod
    def _message(turn: dict, *, replay=False, pending=False) -> list[dict]:
        number = turn.get('turn')
        messages = []
        if isinstance(turn.get('student_message'), str):
            messages.append({'role': 'student', 'text': text(turn['student_message']), 'turn': number,
                             'code': text(turn.get('student_code')), 'pending': pending,
                             'code_unavailable': bool(replay and turn.get('student_code_sha256'))})
            timestamp = turn.get('submitted_at') or (turn.get('log') or {}).get('createdAt')
            if isinstance(timestamp, str):
                messages[-1]['created_at'] = timestamp
        response = turn.get('published_text' if replay else 'tutor_message')
        if isinstance(response, str) and response:
            messages.append({'role': 'tutor', 'text': text(response), 'turn': number, 'pending': False})
        elif messages:
            messages[-1]['response_missing'] = True
        return messages

    def _public_messages(self, row: dict, fallback: list[dict]) -> list[dict]:
        audit = row.get('audit')
        session = audit.get('session') if isinstance(audit, dict) else None
        public = session.get('messages') if isinstance(session, dict) else None
        if not isinstance(public, list) or not public:
            return fallback
        result, student_index, turn = [], None, 0
        turns = row.get('turns') if isinstance(row.get('turns'), list) else []
        pending = row.get('pending_turn')
        sources = [t for t in turns if isinstance(t, dict)] + ([pending] if isinstance(pending, dict) else [])
        for item in public:
            if not isinstance(item, dict) or item.get('role') not in {'student', 'tutor'} or not isinstance(item.get('text'), str):
                continue
            role = item['role']
            if role == 'student':
                turn += 1
                source = next((s for s in sources if s.get('turn') == turn and s.get('student_message') == item['text']), {})
                message = {'role': role, 'text': text(item['text']), 'turn': turn, 'code': text(source.get('student_code')), 'response_missing': True, 'pending': source is pending}
                student_index = len(result)
            else:
                message = {'role': role, 'text': text(item['text']), 'turn': turn, 'pending': False}
                if student_index is not None:
                    result[student_index]['response_missing'] = False
                    result[student_index]['pending'] = False
            if isinstance(item.get('createdAt'), str):
                message['created_at'] = item['createdAt']
            result.append(message)
        if not result:
            return fallback
        # A pending input may not yet be committed to the audited public session.
        if isinstance(pending, dict) and not any(m['role'] == 'student' and m['turn'] == pending.get('turn') and m['text'] == pending.get('student_message') for m in result):
            result.extend(self._message(pending, pending=True))
        return result

    def _episode(self, run_id: str, row: dict, reference: str) -> dict:
        turns = [t for t in row.get('turns', []) if isinstance(t, dict)] if isinstance(row.get('turns'), list) else []
        turns.sort(key=lambda t: t.get('turn') if isinstance(t.get('turn'), int) else 0)
        messages = [m for t in turns for m in self._message(t)]
        pending = row.get('pending_turn')
        if isinstance(pending, dict) and not any(t.get('turn') == pending.get('turn') for t in turns):
            messages.extend(self._message(pending, pending=True))
        messages = self._public_messages(row, messages)
        audit = row.get('audit') or {}
        details = pick(audit, ('resolution_criterion', 'requires_code_change', 'gap_id', 'catalog_version', 'catalog_sha256', 'resolution_contract_version'))
        if isinstance(audit, dict):
            details['requirements'] = [pick(r, ('requirement_id', 'description', 'evidence_source')) for r in records(audit.get('resolution_requirements'))]
            details['events'] = events(audit.get('events'))
        return {
            'run_id': run_id, 'case_id': text(row.get('episode_id')), 'exercise_id': text(row.get('exercise_id')) or None,
            'profile_id': text(row.get('profile_id')) or None, 'scenario': None,
            'repetition': row.get('repetition') if isinstance(row.get('repetition'), int) else None,
            'status': text(row.get('status')) or 'SEM_CONCLUSAO', 'technical_result': None,
            'turn_count': max(len(turns), sum(m['role'] == 'student' for m in messages)), 'messages': messages, 'source_ref': reference,
            'cost_usd': row.get('cost_usd') if isinstance(row.get('cost_usd'), (int, float)) else None,
            'closing': pick(turns[-1].get('state'), ('mode', 'status')) if turns else {},
            'error': pick(row.get('error'), ('kind', 'stage', 'guard_rule')),
            'details': details,
            'gap_summary': pick(audit.get('gap_summary'), ('gap_type', 'claim_text')) or None if isinstance(audit, dict) else None,
        }

    def _replay(self, run_id: str, values: list) -> dict:
        values.sort(key=lambda pair: pair[1].get('turn') if isinstance(pair[1].get('turn'), int) else 0)
        row = values[0][1]
        case = self._episode(run_id, {'episode_id': row['case_id'], 'exercise_id': row.get('exercise_id')}, f'dados_brutos/{run_id}.jsonl:{values[0][0]}')
        case.update(scenario=text(row.get('scenario')) or None, status='SEM_CONCLUSAO', turn_count=len(values), messages=[m for _, r in values for m in self._message(r, replay=True)])
        case['closing'] = pick(values[-1][1], ('mode', 'status'))
        case['details']['turns'] = [pick(r, ('turn', 'scope_hash', 'diagnosis_hash', 'plan_hash', 'mode', 'status', 'allowed_exposure_level', 'final_exposure_level', 'sanitizer_reason', 'resolution_evidence_hash')) for _, r in values]
        case['details']['events'] = [e for _, r in values for e in events(r.get('agent_events'))]
        checks = values[-1][1].get('case_checks')
        if isinstance(checks, dict):
            outcomes = [v.get('status') if isinstance(v, dict) else v for v in checks.values()]
            if 'FAIL' in outcomes:
                case['technical_result'] = 'FAIL'
            elif outcomes and all(v == 'PASS' for v in outcomes):
                case['technical_result'] = 'PASS'
            elif 'INCONCLUSIVO' in outcomes:
                case['technical_result'] = 'INCONCLUSIVO'
            case['details']['checks'] = {str(k): pick(v, ('status', 'reason')) if isinstance(v, dict) else v for k, v in checks.items() if isinstance(v, (dict, str, bool))}
        return case

    def runs(self, *, offset: int = 0, limit: int = 100) -> dict:
        items = []
        for run_id, source in self._sources().items():
            try:
                metadata, _ = self._load(run_id, source=source, overview=True)
                items.append(metadata)
            except (OSError, ValueError, TypeError):
                items.append({'run_id': run_id, 'kind': 'unsupported', 'case_count': 0, 'version': None, 'warnings': ['Artefato inválido; leitura indisponível.']})
        items.sort(key=lambda r: r.get('started_at') or r['run_id'], reverse=True)
        return self._page(items, offset, limit)

    @staticmethod
    def _page(items: list, offset: int, limit: int) -> dict:
        offset, limit = max(0, offset), max(1, min(200, limit))
        return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit}

    def cases(self, run_id: str, *, exercise_id='', profile_id='', scenario='', status='', search='', offset=0, limit=100) -> dict:
        metadata, cases = self._load(run_id)
        cases = [c for c in cases if all(not value or c[key] == value for key, value in (('exercise_id', exercise_id), ('profile_id', profile_id), ('scenario', scenario), ('status', status))) and search.casefold() in c['case_id'].casefold()]
        summaries = [{k: v for k, v in c.items() if k not in {'messages', 'details'}} for c in cases]
        return {**self._page(summaries, offset, limit), 'warnings': metadata['warnings']}

    def case(self, run_id: str, case_id: str) -> dict:
        self._id(case_id)
        metadata, cases = self._load(run_id)
        case = next((c for c in cases if c['case_id'] == case_id), None)
        if case is None:
            raise FileNotFoundError(case_id)
        return {**case, 'warnings': metadata['warnings'], 'runtime': metadata.get('runtime', {})}
