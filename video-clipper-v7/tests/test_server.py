# -*- coding: utf-8 -*-
"""操作画面サーバー(server.py)のテスト: 合言葉・接続元の確認、設定の検証、処理結果の保存と一覧

実行方法: .venv\\Scripts\\python.exe -m unittest discover tests
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402


class CleanSegmentsTest(unittest.TestCase):
    """画面から届いた字幕(分割・追加・時刻のずらし)の検証"""

    def test_drops_empty_and_sorts(self):
        out = server.clean_segments(
            [{'start': 3, 'end': 4, 'text': '後の行'}, {'start': 1, 'end': 2, 'text': ' 先の行 '},
             {'start': 5, 'end': 6, 'text': '   '}], [])
        self.assertEqual([s['text'] for s in out], ['先の行', '後の行'])

    def test_fixes_bad_times(self):
        out = server.clean_segments(
            [{'start': -5, 'end': -1, 'text': 'マイナス'}, {'start': 9, 'end': 9, 'text': '長さゼロ'},
             {'start': 'x', 'end': 1, 'text': '数字でない'}, {'text': '時刻なし'}], [])
        self.assertEqual([s['text'] for s in out], ['マイナス', '長さゼロ'])
        self.assertEqual(out[0]['start'], 0.0)
        self.assertGreater(out[1]['end'], out[1]['start'])

    def test_trims_overlap(self):
        out = server.clean_segments(
            [{'start': 0, 'end': 5, 'text': 'A'}, {'start': 2, 'end': 6, 'text': 'B'}], [])
        self.assertLessEqual(out[0]['end'], out[1]['start'])

    def test_falls_back_to_saved_segments(self):
        saved = [{'start': 0, 'end': 1, 'text': '元のまま'}]
        self.assertEqual(server.clean_segments(None, saved), saved)
        self.assertEqual(server.clean_segments([], saved), saved)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        server.RESULTS = root / 'results'
        server.DATA = root / 'data'
        server.SETTINGS = server.DATA / 'settings.json'
        server.THUMBS = server.DATA / 'thumbs'
        server.RESULTS.mkdir()
        cls.httpd = server.Server(('127.0.0.1', 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        server.HOSTS.update({f'127.0.0.1:{cls.port}'})
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def call(self, path, body=None, token=None, host=None):
        headers = {'Content-Type': 'application/json', 'X-Token': server.TOKEN if token is None else token}
        if host:
            headers['Host'] = host
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_requires_the_token_and_the_local_host_name(self):
        self.assertEqual(self.call('/api/state', token='wrong')[0], 403)
        self.assertEqual(self.call('/api/state', host='evil.example:80')[0], 403)
        status, data = self.call('/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(data['job']['state'], 'idle')
        self.assertTrue(data['fonts'])

    def test_settings_are_validated_and_remembered(self):
        status, _ = self.call('/api/settings', {'settings': {'do_clip': False, 'clip_length': 999, 'orientation': 'sideways',
                                                              'style': {'main': {'size': 500, 'color': 'red'}}}})
        self.assertEqual(status, 200)
        saved = self.call('/api/state')[1]['settings']
        self.assertFalse(saved['do_clip'])
        self.assertEqual(saved['clip_length'], 120.0)
        self.assertEqual(saved['orientation'], 'horizontal')
        self.assertLessEqual(saved['style']['main']['size'], 80)
        self.assertEqual(saved['style']['main']['color'], '#FFFFFF')

    def test_rejects_missing_and_non_video_files(self):
        status, data = self.call('/api/inspect', {'path': str(Path(self.tmp.name) / 'none.mp4')})
        self.assertEqual(status, 400)
        self.assertIn('見つかりません', data['error'])
        memo = Path(self.tmp.name) / 'memo.txt'
        memo.write_text('x', encoding='utf-8')
        self.assertEqual(self.call('/api/inspect', {'path': str(memo)})[0], 400)

    def test_projects_are_listed_and_protected(self):
        project = {'version': 1, 'name': '2026-01-02_030405', 'created': 1.0, 'source': 'C:/video.mp4', 'duration': 10,
                   'settings': server.clean_settings({}), 'style': server.load_settings()['style'], 'status': 'running',
                   'work_video': '', 'segments': [{'start': 0, 'end': 1, 'text': 'こんにちは'}], 'draft': None,
                   'highlights': [], 'clips': [], 'has_captioned': False}
        (server.RESULTS / project['name']).mkdir()
        server.save_project(project)
        server.mark_interrupted()
        recent = self.call('/api/state')[1]['recent']
        self.assertIn((project['name'], 'interrupted'), [(p['name'], p['status']) for p in recent])
        self.assertEqual(self.call('/api/draft', {'project': project['name'], 'draft': {'texts': ['こんばんは']}})[0], 200)
        view = self.call('/api/project', {'project': project['name']})[1]
        self.assertEqual(view['draft']['texts'], ['こんばんは'])
        self.assertFalse(view['can_fix'])
        self.assertEqual(self.call('/api/project', {'project': '../data'})[0], 400)
        self.assertEqual(self.call('/api/reburn', {'project': project['name']})[0], 400)

    def test_clip_length_is_parsed_safely(self):
        self.assertEqual(server.clean_settings({'clip_length': '30'})['clip_length'], 30.0)
        self.assertEqual(server.clean_settings({'clip_length': 'abc'})['clip_length'], 30.0)
        self.assertEqual(server.clean_settings({'clip_length': None})['clip_length'], 30.0)
        self.assertEqual(server.clean_settings({'clip_length': '-3'})['clip_length'], 5.0)

    def make_finished(self, name, clips=True):
        work = Path(self.tmp.name) / f'{name}_work.mp4'
        work.write_bytes(b'x')
        project = {'version': 1, 'name': name, 'created': 1.0, 'source': str(work), 'duration': 1,
                   'settings': server.clean_settings({}), 'style': server.load_settings()['style'], 'status': 'error',
                   'work_video': str(work), 'segments': [{'start': 0, 'end': 1, 'text': 'a'}], 'draft': None,
                   'highlights': [], 'clips': ['clip_1.mp4'] if clips else [], 'has_captioned': True}
        (server.RESULTS / name).mkdir()
        server.save_project(project)
        return project

    def test_reburn_needs_a_valid_target(self):
        # a failed or cancelled rebuild can be retried: the guard lets it reach the target check
        self.make_finished('2026-01-03_000001')
        status, data = self.call('/api/reburn', {'project': '2026-01-03_000001', 'targets': ['nope']})
        self.assertEqual(status, 400)
        self.assertIn('対象', data['error'])

    def test_clip_only_reburn_needs_clips(self):
        self.make_finished('2026-01-03_000002', clips=False)
        self.assertEqual(self.call('/api/reburn', {'project': '2026-01-03_000002', 'targets': ['clip']})[0], 400)

    def test_cancel_only_while_running(self):
        from src.preprocess import CANCEL_EVENT
        CANCEL_EVENT.clear()
        self.assertEqual(self.call('/api/cancel', {})[0], 400)
        self.assertFalse(CANCEL_EVENT.is_set())
        server.JOB['state'] = 'running'
        try:
            self.assertEqual(self.call('/api/cancel', {})[0], 200)
            self.assertTrue(CANCEL_EVENT.is_set())
        finally:
            server.JOB['state'] = 'idle'
            CANCEL_EVENT.clear()

    def test_media_is_limited_to_results(self):
        outside = Path(self.tmp.name) / 'secret.mp4'
        outside.write_bytes(b'x')
        url = f'http://127.0.0.1:{self.port}/media?t={server.TOKEN}&p={outside}'
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(url, timeout=5)


if __name__ == '__main__':
    unittest.main()
