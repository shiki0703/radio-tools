# -*- coding: utf-8 -*-
"""テロップの直し(src/fixes.py の誤字の候補・辞書と、焼き込む前の確認)のテスト

AI は実際には呼ばず、決まった返事をする身代わりで試す。
実行方法: .venv\\Scripts\\python.exe -m unittest discover tests
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402
from src import fixes  # noqa: E402
from test_chapters import FakeClient, segs  # noqa: E402


class TypoReplyTest(unittest.TestCase):
    def test_reads_lines_with_markup_and_reasons(self):
        text = ('はい、見つけました。\n'
                '**12: ヨシミ → 吉見 ※出演者の名前**\n'
                '- [48] 「機械が無い」→「機会が無い」(理由: 前後の話)\n'
                '3行目: 構成 -> 校正\n')
        self.assertEqual([(f['line'], f['wrong'], f['right'], f['reason']) for f in fixes.parse_typos(text, 60)],
                         [(3, '構成', '校正', ''), (12, 'ヨシミ', '吉見', '出演者の名前'), (48, '機械が無い', '機会が無い', '前後の話')])

    def test_drops_out_of_range_and_unchanged(self):
        self.assertEqual(fixes.parse_typos('99: a → b\n7: 同じ → 同じ\n2: x → y', 10),
                         [{'line': 2, 'wrong': 'x', 'right': 'y', 'reason': ''}])

    def test_none_found(self):
        self.assertEqual(fixes.parse_typos('なし', 10), [])

    def test_unreadable_reply(self):
        with self.assertRaises(fixes.ChapterError):
            fixes.parse_typos('よく分かりませんでした', 10)

    def test_json_reply(self):
        text = json.dumps({'fixes': [{'line': 1, 'wrong': 'A', 'right': 'B', 'reason': 'r'}]})
        self.assertEqual(fixes.parse_typos(text, 3)[0]['right'], 'B')

    def test_prompt_numbers_follow_screen_lines(self):
        """空の行は出さないが、番号は画面の行の順のまま"""
        prompt = fixes.typo_prompt(['あいう', '', 'えお'])
        self.assertIn('[1] あいう\n[3] えお', prompt)
        self.assertIn('12: 誤 → 正', prompt)

    def test_ai_with_fake_client(self):
        fake = FakeClient({'fixes': [{'line': 2, 'wrong': '機械', 'right': '機会', 'reason': '文脈'},
                                     {'line': 9, 'wrong': 'x', 'right': 'y', 'reason': ''}]})
        found = fixes.ask_typos(['a', '機械がない'], 'k', client=fake)
        self.assertEqual(found, [{'line': 2, 'wrong': '機械', 'right': '機会', 'reason': '文脈'}])
        self.assertEqual(fake.sent['output_config']['format']['schema'], fixes.TYPO_SCHEMA)
        self.assertIn('[2] 機械がない', fake.sent['messages'][0]['content'])


class DictionaryTest(unittest.TestCase):
    def test_apply_longest_first(self):
        out, count = fixes.apply_dict([{'start': 0, 'end': 1, 'text': 'ヨシミさんとヨシミカン'}],
                                      [{'from': 'ヨシミ', 'to': '吉見'}, {'from': 'ヨシミカン', 'to': '吉見館'}])
        self.assertEqual((out[0]['text'], count), ('吉見さんと吉見館', 2))

    def test_clean_keeps_latest(self):
        self.assertEqual(fixes.clean_entries([{'from': 'a', 'to': 'b'}, {'from': 'a', 'to': 'c'}, {'from': 'x', 'to': 'x'},
                                              {'from': '', 'to': 'y'}, 'bad']),
                         [{'from': 'a', 'to': 'c'}])

    def test_hotwords_newest_first_and_unique(self):
        self.assertEqual(fixes.hotwords([{'from': 'a', 'to': '吉見'}, {'from': 'b', 'to': '南極'}, {'from': 'c', 'to': '吉見'}]),
                         '吉見 南極')


class ServerFixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.saved = (server.RESULTS, server.DATA, server.SETTINGS, server.SECRETS, server.FIXES)
        server.RESULTS = root / 'results'
        server.DATA = root / 'data'
        server.SETTINGS = server.DATA / 'settings.json'
        server.SECRETS = server.DATA / 'secrets.json'
        server.FIXES = server.DATA / 'fixes.json'
        (server.RESULTS / 'p1').mkdir(parents=True)
        server.save_project({'name': 'p1', 'source': 'x.mp4', 'created': 0, 'status': 'running', 'duration': 1200,
                             'settings': {**server.DEFAULT_SETTINGS}, 'segments': segs(), 'titles': []})

    @classmethod
    def tearDownClass(cls):
        server.RESULTS, server.DATA, server.SETTINGS, server.SECRETS, server.FIXES = cls.saved
        cls.tmp.cleanup()

    def test_dictionary_add_and_remove(self):
        server.FIXES.unlink(missing_ok=True)
        server.fixes_add({'entries': [{'from': 'ヨシミ', 'to': '吉見'}]})
        view = server.fixes_add({'entries': [{'from': 'ヨシミ', 'to': '吉見さん'}, {'from': '構成', 'to': '校正'}]})
        self.assertEqual(view['entries'], [{'from': 'ヨシミ', 'to': '吉見さん'}, {'from': '構成', 'to': '校正'}])
        self.assertEqual(server.fixes_remove({'from': 'ヨシミ'})['entries'], [{'from': '構成', 'to': '校正'}])
        with self.assertRaises(server.UserError):
            server.fixes_add({'entries': []})

    def test_typos_need_lines_and_key(self):
        with self.assertRaises(server.UserError):
            server.typos_prompt({'lines': ['', ' ']})
        self.assertIn('[1] あ', server.typos_prompt({'lines': ['あ']})['text'])
        if not server.os.environ.get('ANTHROPIC_API_KEY'):
            with self.assertRaises(server.UserError):
                server.typos_ai({'lines': ['あ']})

    def wait_with(self, kind, answer):
        """焼き込む前に止まったところを、画面の代わりに answer で続けさせる"""
        project = {**server.load_project('p1'), 'settings': {**server.DEFAULT_SETTINGS, 'show_titles': True,
                                                             'title_maker': 'ai'}}
        server.JOB.update(steps=['文字起こし'], step=0, project='p1', wait='', paused=0.0)
        server.CANCEL_EVENT.clear()
        out = {}
        thread = threading.Thread(target=lambda: out.update(result=server.wait_for_titles(project, project['segments'], '', kind=kind)))
        thread.start()
        for _ in range(100):
            if server.JOB['wait']:
                break
            time.sleep(0.05)
        out['wait'] = server.JOB['wait']
        answer()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        return project, out

    def test_caption_check_takes_edited_lines(self):
        edited = [{'start': 0, 'end': 4, 'text': '直した行'}, {'start': 3, 'end': 8, 'text': '次の行'}, {'start': 9, 'end': 10, 'text': ' '}]
        project, out = self.wait_with('captions', lambda: server.titles_continue({'project': 'p1', 'segments': edited}))
        self.assertEqual(out['wait'], 'captions')
        self.assertIsNone(out['result'])                                   # 見出しは作らない
        self.assertEqual([s['text'] for s in project['segments']], ['直した行', '次の行'])
        self.assertLessEqual(project['segments'][0]['end'], project['segments'][1]['start'])   # 重なりを詰める
        self.assertEqual(server.JOB['wait'], '')

    def test_title_wait_also_takes_edited_lines(self):
        edited = [{'start': i * 10.0, 'end': i * 10 + 9.0, 'text': f'直{i}'} for i in range(120)]
        project, out = self.wait_with('titles', lambda: server.titles_continue({'project': 'p1', 'mode': 'words', 'segments': edited}))
        self.assertEqual(project['segments'][0]['text'], '直0')
        self.assertTrue(out['result'])                                     # 言葉を拾う方式の見出し

    def test_all_empty_lines_are_refused(self):
        def answer():
            with self.assertRaises(server.UserError):
                server.titles_continue({'project': 'p1', 'segments': [{'start': 0, 'end': 1, 'text': ''}]})
            server.titles_continue({'project': 'p1'})
        project, out = self.wait_with('captions', answer)
        self.assertEqual(project['segments'][0]['text'], '行0')            # 直さずに続けたので、そのまま

    def test_setting_is_kept(self):
        self.assertTrue(server.clean_settings({'check_captions': True})['check_captions'])
        self.assertFalse(server.clean_settings({})['check_captions'])


if __name__ == '__main__':
    unittest.main()
