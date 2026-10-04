# -*- coding: utf-8 -*-
"""概要欄のチャプター(src/chapters.py と、その窓口)のテスト

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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402
from src import chapters as chap  # noqa: E402


def segs(n=120, step=10.0):
    """10秒ごとの行(全部で20分)"""
    return [{'start': i * step, 'end': i * step + step - 0.5, 'text': f'行{i}'} for i in range(n)]


class ClockTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(chap.clock(171), '00:02:51')
        self.assertEqual(chap.clock(3725), '01:02:05')
        self.assertEqual(chap.clock(171, long=False), '2:51')

    def test_parse(self):
        for text, want in (('2:51', 171), ('02:51', 171), ('0:02:51', 171), ('171', 171), ('1:02:05', 3725)):
            self.assertEqual(chap.parse_clock(text), want, text)
        for bad in ('', 'abc', '1:2:3:4', '-5', None):
            self.assertIsNone(chap.parse_clock(bad), bad)


class TranscriptTest(unittest.TestCase):
    def test_blocks_and_times(self):
        text = chap.transcript_text([{'start': 0, 'end': 4, 'text': 'こんにちは'},
                                     {'start': 4, 'end': 11, 'text': '始めます'},
                                     {'start': 65, 'end': 70, 'text': '次の話'}])
        self.assertEqual(text.splitlines(), ['[0:00] こんにちは 始めます', '[1:05] 次の話'])

    def test_prompt_has_everything(self):
        prompt = chap.paste_prompt(segs(3))
        self.assertIn('主なトピック', prompt)
        self.assertIn('0:00 - 見出し', prompt)        # 答え方の見本
        self.assertIn('[0:00] 行0', prompt)


class ParseReplyTest(unittest.TestCase):
    def test_text_form(self):
        out = chap.parse_reply("""はい、作りました。

▼主なトピック
クモの糸 / ダンゴムシ / 南極の息

0:00 - オープニング
2:51 - 第1問 クモはなぜ巣に引っかからない?
05:00 – 第2問 ダンゴムシの迷路
1:02:05：最後の問題""")
        self.assertEqual(out['topics'], ['クモの糸', 'ダンゴムシ', '南極の息'])
        self.assertEqual([c['start'] for c in out['chapters']], [0, 171, 300, 3725])
        self.assertEqual(out['chapters'][1]['title'], '第1問 クモはなぜ巣に引っかからない?')

    def test_bullets_and_brackets(self):
        out = chap.parse_reply("- [0:00] はじまり\n・[3:10] 本題\n* 10:00 | 終わり")
        self.assertEqual([c['title'] for c in out['chapters']], ['はじまり', '本題', '終わり'])

    def test_chatgpt_style_markdown(self):
        """ChatGPT がよく返す形(見出し記号・太字・箇条書き・コードの囲み)"""
        out = chap.parse_reply("""以下が概要欄用の内容です。

### ▼主なトピック

- 吉見発案の常識クイズ
- クモの糸 / ダンゴムシ
- **南極の白い息**

### ▼チャプター

```
**0:00** - オープニング
**2:51** – 第1問 クモはなぜ巣に引っかからない?
1. 5:00 — 第2問 ダンゴムシの迷路
```

ご確認ください。""")
        self.assertEqual(out['topics'], ['吉見発案の常識クイズ', 'クモの糸', 'ダンゴムシ', '南極の白い息'])
        self.assertEqual([(c['start'], c['title']) for c in out['chapters']],
                         [(0, 'オープニング'), (171, '第1問 クモはなぜ巣に引っかからない?'), (300, '第2問 ダンゴムシの迷路')])

    def test_json_form(self):
        out = chap.parse_reply(json.dumps({'topics': ['a'], 'chapters': [{'start': '1:00', 'title': 'x'}]}))
        self.assertEqual(out['chapters'], [{'start': 60, 'title': 'x', 'label': ''}])

    def test_nothing_usable(self):
        with self.assertRaises(chap.ChapterError):
            chap.parse_reply('すみません、作れませんでした')
        with self.assertRaises(chap.ChapterError):
            chap.parse_reply('   ')


class NormalizeTest(unittest.TestCase):
    def test_first_is_zero_and_sorted(self):
        out = chap.normalize({'topics': [' a ', ''], 'chapters': [
            {'start': '5:00', 'title': 'B'}, {'start': '0:16', 'title': 'A'}]}, segs(), 1200)
        self.assertEqual(out['chapters'], [{'start': 0.0, 'title': 'A', 'label': 'A'}, {'start': 300.0, 'title': 'B', 'label': 'B'}])
        self.assertEqual(out['topics'], ['a'])

    def test_snaps_to_line_start(self):
        out = chap.normalize({'chapters': [{'start': 0, 'title': 'A'}, {'start': 123, 'title': 'B'}]}, segs(), 1200)
        self.assertEqual(out['chapters'][1]['start'], 120.0)

    def test_drops_too_close_and_past_end(self):
        out = chap.normalize({'chapters': [
            {'start': 0, 'title': 'A'}, {'start': 60, 'title': 'B'}, {'start': 65, 'title': '近すぎ'},
            {'start': 5000, 'title': '長さより後'}, {'start': 'x', 'title': '読めない'}, {'start': 90, 'title': ''}]},
            segs(), 1200)
        self.assertEqual([c['title'] for c in out['chapters']], ['A', 'B'])

    def test_problems(self):
        self.assertTrue(chap.problems([{'start': 0, 'title': 'A'}]))                      # 3つ未満
        self.assertTrue(chap.problems([{'start': 5, 'title': 'A'}, {'start': 60, 'title': 'B'},
                                       {'start': 120, 'title': 'C'}]))                     # 0:00 始まりでない
        self.assertTrue(chap.problems([{'start': 0, 'title': 'A'}, {'start': 5, 'title': 'B'},
                                       {'start': 120, 'title': 'C'}]))                     # 10秒未満
        self.assertEqual(chap.problems([{'start': 0, 'title': 'A'}, {'start': 60, 'title': 'B'},
                                        {'start': 120, 'title': 'C'}]), [])

    def test_description(self):
        text = chap.description(['クモ', '南極'], [{'start': 0, 'title': 'はじまり'}, {'start': 171, 'title': 'クモ'},
                                                   {'start': 200, 'title': ''}])
        self.assertEqual(text, '▼主なトピック\nクモ / 南極\n\n00:00:00 - はじまり\n00:02:51 - クモ')


class DraftTest(unittest.TestCase):
    def test_from_titles(self):
        titles = [{'start': 16, 'end': 160, 'text': '配信'}, {'start': 165, 'end': 300, 'text': 'クモ'},
                  {'start': 300, 'end': 500, 'text': '南極'}]
        out = chap.draft(segs(), titles, 1200)
        self.assertEqual([c['title'] for c in out['chapters']], ['配信', 'クモ', '南極'])
        self.assertEqual(out['chapters'][0]['start'], 0.0)
        self.assertEqual(out['topics'], ['クモ', '南極'])


class FakeClient:
    """Claude の身代わり。受け取った依頼を覚えておき、決まった返事をする"""

    def __init__(self, reply, stop='end_turn'):
        self.sent = None
        self.reply, self.stop = reply, stop
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.sent = kwargs
        return SimpleNamespace(stop_reason=self.stop,
                               content=[SimpleNamespace(type='text', text=json.dumps(self.reply, ensure_ascii=False))])


class AskClaudeTest(unittest.TestCase):
    def test_request_shape(self):
        fake = FakeClient({'topics': ['t'], 'chapters': [{'start': '0:00', 'title': 'A'}]})
        out = chap.ask_claude(segs(5), 'sk-ant-test', client=fake)
        self.assertEqual(out['chapters'][0]['title'], 'A')
        sent = fake.sent
        self.assertEqual(sent['model'], 'claude-opus-5-5')
        self.assertEqual(sent['fallbacks'], 'default')
        self.assertIn('server-side-fallback-2026-07-01', sent['betas'])
        self.assertEqual(sent['output_config']['format']['type'], 'json_schema')
        self.assertIn('[0:00] 行0', sent['messages'][0]['content'])   # 文字起こしだけを送る

    def test_refusal(self):
        with self.assertRaises(chap.ChapterError):
            chap.ask_claude(segs(5), 'k', client=FakeClient({}, stop='refusal'))


class LabelTest(unittest.TestCase):
    """左上に焼き込む短い見出し"""

    def test_label_in_reply(self):
        out = chap.parse_reply('0:00 - オープニング【オープニング】\n2:51 - 第1問 クモはなぜ巣に引っかからない?【クモの巣の秘密】\n5:00 - 角かっこなし')
        self.assertEqual([(c['title'], c['label']) for c in out['chapters']],
                         [('オープニング', 'オープニング'), ('第1問 クモはなぜ巣に引っかからない?', 'クモの巣の秘密'),
                          ('角かっこなし', '')])

    def test_label_made_when_missing_but_never_cut(self):
        """無いときは見出しから作る。長すぎても「…」で切らない(画面で知らせて直してもらう)"""
        out = chap.normalize({'chapters': [
            {'start': 0, 'title': '悲しい涙と嬉しい涙、味がちがう理由'},
            {'start': 60, 'title': 'A', 'label': 'これは十六文字をこえてしまう長すぎる見出しです'},
            {'start': 120, 'title': '第3問 南極で息を吐くと白くなる?', 'label': '南極の白い息'}]}, segs(), 1200)
        self.assertEqual([c['label'] for c in out['chapters']],
                         ['悲しい涙と嬉しい涙', 'これは十六文字をこえてしまう長すぎる見出しです', '南極の白い息'])
        self.assertFalse(any('…' in c['label'] for c in out['chapters']))

    def test_long_label_is_reported(self):
        rows = [{'start': 0, 'title': 'A', 'label': '理科の常識クイズ'},
                {'start': 60, 'title': 'B', 'label': '吉見リクエスト「理科の常識クイズ」開幕'},
                {'start': 120, 'title': 'C', 'label': 'ABC'},
                {'start': 180, 'title': 'エンディング', 'label': '', 'kind': 'outro'}]
        probs = chap.problems(rows)
        self.assertEqual(len(probs), 1)
        self.assertIn('吉見リクエスト「理科の常識クイズ」開幕', probs[0])
        self.assertIn('19文字', probs[0])

    def test_width(self):
        self.assertEqual(chap.label_width('クモの巣の秘密'), 7)
        self.assertEqual(chap.label_width('ABCクイズ2026'), 6.5)                 # 英数字は半分
        self.assertFalse(chap.label_too_long('あ' * 16))
        self.assertTrue(chap.label_too_long('あ' * 17))
        self.assertFalse(chap.label_too_long('A' * 32))

    def test_prompt_asks_to_fit(self):
        """依頼文で、左上の見出しを必ず収まる長さにするよう頼んでいる"""
        prompt = chap.paste_prompt(segs(3))
        for words in ('必ず12文字以内', '「…」で省略しない', '文字数を1つずつ数え', '※【】の中は、必ず12文字以内'):
            self.assertIn(words, prompt)
        self.assertIn('12文字以内', chap.SCHEMA['properties']['chapters']['items']['properties']['label']['description'])
        self.assertLess(chap.LABEL_ASK, chap.LABEL_MAX)                       # 数え間違えても収まる余裕

    def test_long_label_in_brackets_is_not_mixed_into_title(self):
        out = chap.parse_reply('2:51 - 見出し【これは三十文字をこえてしまうとても長い左上の見出しの例ですよね】')
        self.assertEqual(out['chapters'][0]['title'], '見出し')

    def test_short_label_drops_numbering(self):
        self.assertEqual(chap.short_label('第2問 ダンゴムシの迷路'), '第2問 ダンゴムシの迷路')   # 収まるならそのまま
        # 区切りのよいところで収まらなければ、切らずにそのまま(番号だけは外す)
        self.assertEqual(chap.short_label('第2問 ダンゴムシをゴールへ導く迷路の作り方'), 'ダンゴムシをゴールへ導く迷路の作り方')
        self.assertEqual(chap.short_label('クモの巣'), 'クモの巣')

    def test_overlay_titles(self):
        """本編の時刻のまま焼き込む。終わりは次の見出しの始まり。左上が空なら見出しから作る"""
        rows = [{'start': 0, 'title': 'A', 'label': 'はじまり'}, {'start': 120, 'title': 'B', 'label': 'クモ'},
                {'start': 600, 'title': 'C', 'label': ''}]
        out = chap.overlay_titles(rows, 1200)
        self.assertEqual(out, [{'start': 0.0, 'end': 120.0, 'text': 'はじまり'},
                               {'start': 120.0, 'end': 600.0, 'text': 'クモ'},
                               {'start': 600.0, 'end': 1200.0, 'text': 'C'}])


class TimelineTest(unittest.TestCase):
    """オープニング・エンディング込みの時刻の並び"""
    BODY = {'topics': [], 'chapters': [{'start': 0, 'title': 'A', 'label': 'a'}, {'start': 120, 'title': 'B', 'label': 'b'}]}

    def test_with_opening_and_ending(self):
        rows = chap.timeline(self.BODY, 30, 1200, 20)
        self.assertEqual([(r['kind'], r['start'], r['title']) for r in rows],
                         [('intro', 0.0, 'オープニング'), ('body', 30.0, 'A'), ('body', 150.0, 'B'),
                          ('outro', 1230.0, 'エンディング')])

    def test_short_opening_is_part_of_first(self):
        """10秒未満のオープニングはチャプターにしない(YouTube の決まり)。1つ目を 0:00 にする"""
        rows = chap.timeline(self.BODY, 6, 1200, 5)
        self.assertEqual([(r['kind'], r['start']) for r in rows], [('body', 0.0), ('body', 126.0)])

    def test_names_and_removal(self):
        rows = chap.timeline({**self.BODY, 'intro_title': 'OP 映像', 'outro_title': ''}, 30, 1200, 20)
        self.assertEqual([r['title'] for r in rows], ['OP 映像', 'A', 'B'])          # 空のエンディングは出さない

    def test_round_trip(self):
        rows = chap.timeline(self.BODY, 30, 1200, 20)
        rows[0]['title'] = 'OP'
        rows[2]['start'] = 180                              # 画面で B を 3:00 に動かした
        back = chap.from_timeline(rows, 30, 1200, 20)
        self.assertEqual([c['start'] for c in back['chapters']], [0.0, 150.0])      # 本編の時刻に戻る
        self.assertEqual((back['intro_title'], back['outro_title']), ('OP', 'エンディング'))

    def test_removed_bookend_rows(self):
        rows = [r for r in chap.timeline(self.BODY, 30, 1200, 20) if r['kind'] == 'body']
        back = chap.from_timeline(rows, 30, 1200, 20)
        self.assertEqual((back['intro_title'], back['outro_title']), ('', ''))       # 消した → 出さない
        back = chap.from_timeline(rows, 0, 1200, 0, before={'intro_title': 'OP'})
        self.assertEqual(back['intro_title'], 'OP')                                  # 出ていなかった → 前のまま


class SaveJsonTest(unittest.TestCase):
    def test_retries_while_file_is_being_read(self):
        """Windows で、ほかの処理が読んでいる最中で置き換えを断られても、待ってやり直す"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'project.json'
            real, calls = server.os.replace, []

            def busy_twice(a, b):
                calls.append(1)
                if len(calls) <= 2:
                    raise PermissionError(5, 'アクセスが拒否されました')
                return real(a, b)

            server.os.replace = busy_twice
            try:
                server.save_json({'a': 1}, path)
            finally:
                server.os.replace = real
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'a': 1})
            self.assertEqual(len(calls), 3)
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])     # 一時ファイルが残らない


class ServerChaptersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.saved = (server.RESULTS, server.DATA, server.SETTINGS, server.SECRETS)
        server.RESULTS = root / 'results'
        server.DATA = root / 'data'
        server.SETTINGS = server.DATA / 'settings.json'
        server.SECRETS = server.DATA / 'secrets.json'
        server.RESULTS.mkdir()
        project = {'name': 'p1', 'source': 'x.mp4', 'created': 0, 'status': 'done', 'duration': 1200,
                   'settings': {**server.DEFAULT_SETTINGS}, 'segments': segs(), 'has_captioned': True,
                   'intro_seconds': 12.0,
                   'titles': [{'start': 16, 'end': 160, 'text': '配信'}, {'start': 165, 'end': 300, 'text': 'クモ'},
                              {'start': 300, 'end': 600, 'text': '南極'}]}
        (server.RESULTS / 'p1').mkdir()
        server.save_project(project)

    @classmethod
    def tearDownClass(cls):
        server.RESULTS, server.DATA, server.SETTINGS, server.SECRETS = cls.saved
        cls.tmp.cleanup()

    def setUp(self):
        server.chapters_path({'name': 'p1'}).unlink(missing_ok=True)
        server.SECRETS.unlink(missing_ok=True)

    def test_draft_shifts_by_opening(self):
        view = server.chapters_get({'project': 'p1'})
        self.assertEqual(view['source'], 'draft')
        # オープニング(12秒)が1つ目のチャプター、本編は 12秒うしろから
        self.assertEqual([(c['kind'], c['start']) for c in view['chapters']],
                         [('intro', 0.0), ('body', 12.0), ('body', 172.0), ('body', 312.0)])
        self.assertFalse(view['has_key'])
        self.assertIn('00:00:00 - オープニング\n00:00:12 - 配信\n00:02:52 - クモ', view['text'])

    def test_opening_added_later_moves_times(self):
        """あとからオープニングを付け替えても、保存した見出しの時刻が追従する"""
        server.chapters_paste({'project': 'p1', 'text': '0:00 - 始まり\n2:00 - 本題\n10:00 - 終わり'})
        project = server.load_project('p1')
        try:
            server.save_project({**project, 'intro_seconds': 45.0, 'outro_seconds': 20.0})
            view = server.chapters_get({'project': 'p1'})
            self.assertEqual([(c['kind'], c['start']) for c in view['chapters']],
                             [('intro', 0.0), ('body', 45.0), ('body', 165.0), ('body', 645.0), ('outro', 1245.0)])
            self.assertIn('00:20:45 - エンディング', view['text'])
        finally:
            server.save_project(project)

    def test_paste_then_edit_then_reset(self):
        view = server.chapters_paste({'project': 'p1', 'text': '▼主なトピック\nA / B\n\n0:00 - 始まり\n2:00 - 本題\n10:00 - 終わり'})
        self.assertEqual(view['source'], 'paste')
        self.assertEqual([c['start'] for c in view['chapters']], [0.0, 12.0, 132.0, 612.0])
        self.assertEqual(view['problems'], [])
        saved = json.loads(server.chapters_path({'name': 'p1'}).read_text(encoding='utf-8'))
        self.assertEqual(saved['timebase'], 'body')                                  # 本編の時刻で保存
        self.assertEqual([c['start'] for c in saved['chapters']], [0.0, 120.0, 600.0])

        view = server.chapters_save({'project': 'p1', 'topics': ['A'],
                                     'chapters': [{'start': '0:00', 'title': '始まり'}, {'start': '0:05', 'title': '近い'}]})
        self.assertEqual(view['source'], 'edit')
        self.assertTrue(view['problems'])                        # 直した結果の問題は知らせる(勝手に消さない)
        self.assertEqual(server.chapters_get({'project': 'p1'})['source'], 'edit')

        self.assertEqual(server.chapters_reset({'project': 'p1'})['source'], 'draft')

    def test_api_key_is_kept_but_never_returned(self):
        with self.assertRaises(server.UserError):
            server.set_api_key({'key': 'これはキーではありません'})
        out = server.set_api_key({'key': 'sk-ant-api03-' + 'x' * 40})
        self.assertEqual(out, {'has_key': True})                 # キーそのものは返さない
        self.assertNotIn('sk-ant', json.dumps(server.chapters_get({'project': 'p1'})))
        self.assertEqual(server.set_api_key({'key': ''}), {'has_key': bool(server.os.environ.get('ANTHROPIC_API_KEY'))})

    def test_ai_needs_key(self):
        if server.os.environ.get('ANTHROPIC_API_KEY'):
            self.skipTest('この PC には API キーが設定されている')
        with self.assertRaises(server.UserError):
            server.chapters_ai({'project': 'p1'})

    def test_ai_with_fake_client(self):
        server.set_api_key({'key': 'sk-ant-api03-' + 'x' * 40})
        fake = FakeClient({'topics': ['クモ'], 'chapters': [{'start': '0:00', 'title': 'オープニング'},
                                                         {'start': '2:00', 'title': 'クモの話'},
                                                         {'start': '8:00', 'title': '南極の話'}]})
        real = chap.ask_claude
        chap.ask_claude = lambda s, k: real(s, k, client=fake)
        try:
            view = server.chapters_ai({'project': 'p1'})
        finally:
            chap.ask_claude = real
        self.assertEqual(view['source'], 'ai')
        self.assertEqual([c['start'] for c in view['chapters']], [0.0, 12.0, 132.0, 492.0])

    def topic_titles_with(self, fake, maker='ai', key=True, answer=None):
        """処理の途中で見出しを作るところを、Claude の身代わりで試す。

        見出し待ちで止まったら、画面の代わりに answer(JOB を受け取る)で続けさせる。
        """
        if key:
            server.set_api_key({'key': 'sk-ant-api03-' + 'x' * 40})
        project = {**server.load_project('p1'), 'name': 'p1',
                   'settings': {**server.DEFAULT_SETTINGS, 'show_titles': True, 'title_maker': maker}}
        server.JOB.update(steps=['文字起こし'], step=0, project='p1', wait='', paused=0.0)
        server.WEIGHTS[:] = [100]
        server.CANCEL_EVENT.clear()
        real = chap.ask_claude
        chap.ask_claude = (lambda s, k: real(s, k, client=fake)) if fake else real
        out = {}

        def work():
            try:
                out['titles'] = server.topic_titles(project, project['segments'])
            except Exception as exc:          # 中止など
                out['error'] = exc

        thread = threading.Thread(target=work)
        try:
            thread.start()
            for _ in range(100):                 # 止まるか、終わるまで待つ
                if server.JOB['wait'] or not thread.is_alive():
                    break
                time.sleep(0.05)
            out['waited'] = server.JOB['wait'] == 'titles'
            out['note'] = server.JOB['wait_note']
            if out['waited']:
                self.assertIsNotNone(answer, '見出し待ちで止まったのに、続け方が決めてない')
                answer()
            thread.join(10)
            self.assertFalse(thread.is_alive(), '止まったままになっている')
        finally:
            chap.ask_claude = real
            server.CANCEL_EVENT.clear()
        return project, out

    def test_titles_by_ai_before_burning(self):
        fake = FakeClient({'topics': ['クモ'], 'chapters': [
            {'start': '0:00', 'title': 'オープニング', 'label': 'はじまり'},
            {'start': '2:00', 'title': '第1問 クモの巣の話', 'label': 'クモの巣の秘密'},
            {'start': '8:00', 'title': '南極の話', 'label': '南極の白い息'}]})
        project, out = self.topic_titles_with(fake)
        self.assertFalse(out['waited'])                                     # キーがあれば止まらない
        titles = out['titles']
        self.assertEqual(project['titles_by'], 'ai')
        self.assertEqual([t['text'] for t in titles], ['はじまり', 'クモの巣の秘密', '南極の白い息'])
        self.assertEqual(titles[1]['start'], 120.0)                         # 本編の時刻(焼き込みはオープニングをつなぐ前)
        saved = json.loads(server.chapters_path(project).read_text(encoding='utf-8'))
        self.assertEqual(saved['source'], 'ai')                            # 概要欄も同時にできている

    def test_without_key_waits_for_pasted_chapters(self):
        """キーが無ければ文字起こしのあとで止まり、画面で作った見出しで焼き込みに進む"""
        def answer():
            server.titles_continue({'project': 'p1', 'mode': 'chapters', 'source': 'paste', 'topics': ['クモ'],
                                    'chapters': [{'start': '0:00', 'title': 'オープニング', 'label': 'はじまり'},
                                                 {'start': '2:12', 'title': 'クモの話', 'label': 'クモの巣の秘密'},
                                                 {'start': '8:12', 'title': '南極の話', 'label': ''}]})
        project, out = self.topic_titles_with(None, key=False, answer=answer)
        self.assertTrue(out['waited'])
        self.assertEqual(out['note'], '')
        # 画面の時刻はオープニング(12秒)込み。焼き込む見出しは本編の時刻
        self.assertEqual([(t['start'], t['text']) for t in out['titles']],
                         [(0.0, 'はじまり'), (120.0, 'クモの巣の秘密'), (480.0, '南極の話')])
        self.assertEqual(project['titles_by'], 'paste')
        self.assertTrue(server.chapters_path(project).is_file())           # 概要欄も保存されている
        self.assertEqual(server.JOB['wait'], '')

    def test_ai_failure_waits_with_reason(self):
        project, out = self.topic_titles_with(
            FakeClient({}, stop='refusal'),
            answer=lambda: server.titles_continue({'project': 'p1', 'mode': 'words'}))
        self.assertTrue(out['waited'])
        self.assertIn('AI で見出しを作れませんでした', out['note'])
        self.assertEqual(project['titles_by'], 'words')                   # 「言葉を拾う方式で続ける」
        self.assertTrue(out['titles'])

    def test_cancel_while_waiting(self):
        project, out = self.topic_titles_with(None, key=False, answer=server.CANCEL_EVENT.set)
        self.assertIsInstance(out.get('error'), server.ProcessCancelled)
        self.assertEqual(server.JOB['wait'], '')

    def test_continue_only_while_waiting(self):
        server.JOB.update(wait='', project='p1')
        with self.assertRaises(server.UserError):
            server.titles_continue({'project': 'p1', 'mode': 'words'})

    def test_words_never_waits(self):
        project, out = self.topic_titles_with(FakeClient({}), maker='words')
        self.assertFalse(out['waited'])
        self.assertEqual(project['titles_by'], 'words')

    def test_needs_transcript(self):
        (server.RESULTS / 'p2').mkdir(exist_ok=True)
        server.save_project({**server.load_project('p1'), 'name': 'p2', 'segments': []})
        with self.assertRaises(server.UserError):
            server.chapters_get({'project': 'p2'})


if __name__ == '__main__':
    unittest.main()
