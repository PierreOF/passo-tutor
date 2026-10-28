import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tutor_socratico.api import Handler, ThreadingHTTPServer


class ReviewStorage:
    def resolve_web_session(self, token):
        if token not in {'admin', 'student', 'temporary'}:
            return None
        return ({'id': token, 'role': 'student' if token == 'student' else 'admin', 'must_change_password': token == 'temporary'}, '')


class ReviewApiTests(unittest.TestCase):
    def test_admin_endpoints_use_existing_authorization_without_tutor(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TUTOR_REVIEW_ROOT': directory}):
            raw = Path(directory) / 'dados_brutos'
            raw.mkdir()
            (raw / 'r.jsonl').write_text(json.dumps({'case_id': 'c', 'exercise_id': 'E01', 'scenario': 'direto', 'turn': 1, 'student_message': 'Ajuda', 'published_text': 'Qual regra?'}), encoding='utf-8')
            class ReviewHandler(Handler):
                storage = ReviewStorage()
                tutor = None
                def log_message(self, *_):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), ReviewHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ['/api/admin/experiments', '/api/admin/experiments/r/cases', '/api/admin/experiments/r/cases/c']:
                    for token, expected in [('', 401), ('student', 403), ('temporary', 403), ('admin', 200)]:
                        request = Request(f'http://127.0.0.1:{server.server_port}{path}', headers={'Cookie': f'passo_session={token}'})
                        try:
                            with urlopen(request) as response:
                                status = response.status
                                json.load(response)
                        except HTTPError as error:
                            status = error.code
                        self.assertEqual(status, expected, (path, token))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_without_review_root_the_review_is_unavailable(self):
        class ReviewHandler(Handler):
            storage = ReviewStorage()
            tutor = None
            def log_message(self, *_):
                pass
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('TUTOR_REVIEW_ROOT', None)
            server = ThreadingHTTPServer(('127.0.0.1', 0), ReviewHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(f'http://127.0.0.1:{server.server_port}/api/admin/experiments', headers={'Cookie': 'passo_session=admin'})
                with self.assertRaises(HTTPError) as context:
                    urlopen(request)
                self.assertEqual(context.exception.code, 503)
                body = json.load(context.exception)
                self.assertEqual(body['kind'], 'review_unavailable')
                self.assertIn('TUTOR_REVIEW_ROOT', body['message'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
