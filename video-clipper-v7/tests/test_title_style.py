# -*- coding: utf-8 -*-
"""話題の見出しの見た目(テロップとは別の設定)と、元動画・切り抜きで別のロゴのテスト

実行方法: .venv\\Scripts\\python.exe -m unittest discover tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402
from src.subtitle import build_title_style, normalize_style, DEFAULT_STYLE  # noqa: E402


def fields(style_text):
    return dict(part.split('=', 1) for part in style_text.split(','))


class TitleStyleTest(unittest.TestCase):
    def test_caption_size_does_not_change_title(self):
        a = fields(build_title_style({**DEFAULT_STYLE, 'size': 18}))
        b = fields(build_title_style({**DEFAULT_STYLE, 'size': 40}))
        self.assertEqual(a['FontSize'], b['FontSize'])

    def test_title_settings_are_used(self):
        f = fields(build_title_style({**DEFAULT_STYLE, 't_font': 'mincho', 't_size': 15, 't_color': '#ffee00', 't_bold': True,
                                      't_bg': '#1D4ED8', 't_bg_opacity': 0.5, 't_pos': 'br', 't_mx': 20.4, 't_my': 31}))
        self.assertEqual((f['FontName'], f['FontSize'], f['Bold']), ('Yu Mincho', '15', '-1'))
        self.assertEqual(f['PrimaryColour'], '&H0000EEFF')
        self.assertEqual(f['OutlineColour'], '&H80D84E1D')        # 帯(半分の濃さ)
        self.assertEqual((f['Alignment'], f['MarginL'], f['MarginR'], f['MarginV']), ('3', '20', '20', '31'))

    def test_spots(self):
        for pos, align in (('tl', '5'), ('tc', '6'), ('tr', '7'), ('bl', '1'), ('br', '3')):
            self.assertEqual(fields(build_title_style({**DEFAULT_STYLE, 't_pos': pos}))['Alignment'], align)

    def test_old_settings_keep_their_look(self):
        """見出しを別に持つ前の設定は、これまでどおりテロップの大きさ・フォントから決める"""
        old = {k: v for k, v in DEFAULT_STYLE.items() if not k.startswith('t_')}
        s = normalize_style({**old, 'size': 26, 'font': 'yu'})
        self.assertEqual((s['t_size'], s['t_font'], s['t_pos']), (16.1, 'yu', 'tl'))

    def test_bad_values_fall_back(self):
        s = normalize_style({**DEFAULT_STYLE, 't_pos': 'middle', 't_size': 'x', 't_bg_opacity': 5, 't_color': 'red'})
        self.assertEqual((s['t_pos'], s['t_size'], s['t_bg_opacity'], s['t_color']), ('tl', 11.0, 1.0, '#FFFFFF'))


class LogoPerVideoTest(unittest.TestCase):
    LOGO = {'on': True, 'path': '', 'x': 0.8, 'y': 0.04, 'w': 0.14}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.png = str(Path(self.tmp.name) / 'a.png')
        Path(self.png).write_bytes(b'png')               # ロゴは画像のファイルがあるときだけ使う

    def tearDown(self):
        self.tmp.cleanup()

    def test_clip_logo_is_separate(self):
        main = {**self.LOGO, 'path': self.png}
        clip = {**self.LOGO, 'on': False}
        project = {'settings': {'logo': main, 'logo_clip': clip}}
        self.assertEqual(server.logo_of(project)['path'], self.png)
        self.assertIsNone(server.logo_of(project, 'clip'))

    def test_old_project_uses_main_logo_for_clips(self):
        project = {'settings': {'logo': {**self.LOGO, 'path': self.png}}}
        self.assertEqual(server.logo_of(project, 'clip')['path'], self.png)

    def test_settings_copy_main_logo_when_missing(self):
        settings = server.clean_settings({'logo': {**self.LOGO, 'path': self.png}})
        self.assertEqual(settings['logo_clip']['path'], self.png)


if __name__ == '__main__':
    unittest.main()
