# -*- coding: utf-8 -*-
"""ロゴを重ねる焼き込み・切り抜きと、Radio Sync が付けたエンディング曲の扱いのテスト(FFmpeg を使う)

実行方法: python -m unittest discover tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402
from src.clip import cut_clips  # noqa: E402
from src.logo import clean_logo, logo_box, DEFAULT_LOGO  # noqa: E402
from src.subtitle import burn_subtitles, write_srt, DEFAULT_STYLE  # noqa: E402


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True, capture_output=True)


def pixel(path, t, x, y):
    out = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.2f}', '-i', str(path), '-frames:v', '1',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True).stdout
    size = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                      'stream=width,height', '-of', 'json', str(path)],
                                     check=True, capture_output=True).stdout)['streams'][0]
    return np.frombuffer(out, dtype=np.uint8).reshape(size['height'], size['width'], 3)[y, x].astype(int)


def is_red(rgb):
    return rgb[0] > 150 and rgb[1] < 90 and rgb[2] < 90


def is_blue(rgb):
    return rgb[2] > 150 and rgb[0] < 90


class LogoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.dir.name)
        # 640x360 の青い動画(音あり)と、赤いロゴ(2:1)
        ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=640x360:r=30', '-f', 'lavfi', '-i', 'sine=frequency=300',
               '-t', '4', '-c:v', 'libx264', '-c:a', 'aac', root / 'work.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=100x50', '-frames:v', '1', root / 'logo.png')
        cls.logo = clean_logo({'on': True, 'path': str(root / 'logo.png'), 'x': 0.8, 'y': 0.05, 'w': 0.1})
        cls.segments = [{'start': 0.5, 'end': 3.5, 'text': 'テロップ'}]

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_box_is_in_pixels_of_the_video(self):
        self.assertEqual(logo_box(self.logo, 640, 360), (64, 512, 18))
        self.assertEqual(logo_box(self.logo, 3840, 2160), (384, 3072, 108))

    def test_settings_are_checked(self):
        self.assertEqual(clean_logo({'on': True, 'path': str(self.root / 'nothing.png')})['on'], False)
        self.assertEqual(clean_logo({'on': True, 'path': str(self.root / 'work.mp4')})['path'], '')
        odd = clean_logo({'on': True, 'path': str(self.root / 'logo.png'), 'x': 'a', 'y': 9, 'w': 0})
        self.assertEqual((odd['x'], odd['y'], odd['w']), (DEFAULT_LOGO['x'], 1.0, 0.02))

    def test_burn_puts_the_logo_and_keeps_the_sound(self):
        srt = self.root / 'sub.srt'
        write_srt(self.segments, str(srt))
        out = self.root / 'captioned.mp4'
        burn_subtitles(str(self.root / 'work.mp4'), str(srt), str(out), duration=4, style=dict(DEFAULT_STYLE), logo=self.logo)
        # ロゴは (512, 18) から 64x32
        self.assertTrue(is_red(pixel(out, 2, 512 + 32, 18 + 16)))
        self.assertTrue(is_blue(pixel(out, 2, 512 - 8, 18 + 16)))
        self.assertTrue(is_blue(pixel(out, 2, 100, 100)))
        streams = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
                                             '-of', 'json', str(out)], capture_output=True, check=True).stdout)['streams']
        self.assertEqual(sorted(s['codec_type'] for s in streams), ['audio', 'video'])

    def test_burn_without_logo_is_unchanged(self):
        srt = self.root / 'sub2.srt'
        write_srt(self.segments, str(srt))
        out = self.root / 'plain.mp4'
        burn_subtitles(str(self.root / 'work.mp4'), str(srt), str(out), duration=4, style=dict(DEFAULT_STYLE))
        self.assertTrue(is_blue(pixel(out, 2, 512 + 32, 18 + 16)))

    def test_horizontal_clip_without_captions_gets_the_logo(self):
        out = self.root / 'h'
        out.mkdir()
        cut_clips(str(self.root / 'work.mp4'), [{'start': 1, 'end': 3}], str(out), work_video=str(self.root / 'work.mp4'),
                  logo=self.logo)
        self.assertTrue(is_red(pixel(out / 'clip_1.mp4', 1, 512 + 32, 18 + 16)))

    def test_vertical_clip_keeps_the_logo_inside_the_picture(self):
        out = self.root / 'v'
        out.mkdir()
        cut_clips(str(self.root / 'work.mp4'), [{'start': 1, 'end': 3}], str(out), orientation='vertical',
                  work_video=str(self.root / 'work.mp4'), segments=self.segments, style=dict(DEFAULT_STYLE), logo=self.logo)
        # 640x360 → 1080x608 にして、1920 の高さの真ん中(上から 656)に置く。ロゴは 1.6875 倍
        k = 1080 / 640
        x, y = round((512 + 32) * k), round(656 + (18 + 16) * k)
        self.assertTrue(is_red(pixel(out / 'clip_1.mp4', 1, x, y)))
        self.assertFalse(is_red(pixel(out / 'clip_1.mp4', 1, x, 20)))     # 上の黒帯には出ない


class EndingTest(unittest.TestCase):
    """Radio Sync で最後にエンディング曲を付けた動画"""

    def project(self, ending=22.0):
        return {'duration': 600.0, 'ending_seconds': ending, 'settings': {'intro': '', 'outro': ''},
                'outro_seconds': 0.0, 'segments': []}

    def test_body_ends_where_the_song_starts(self):
        self.assertEqual(server.body_seconds(self.project()), 578.0)
        self.assertEqual(server.bookend_seconds(self.project(), 'outro'), 22.0)
        self.assertEqual(server.body_seconds(self.project(0)), 600.0)

    def test_captions_in_the_song_are_dropped(self):
        segs = [{'start': 570.0, 'end': 579.0, 'text': '最後の話'},
                {'start': 581.0, 'end': 585.0, 'text': 'ご視聴ありがとうございました。'}]
        out = server.without_ending(segs, self.project())
        self.assertEqual([s['text'] for s in out], ['最後の話'])
        self.assertEqual(out[0]['end'], 578.0)
        self.assertEqual(server.without_ending(segs, self.project(0)), segs)

    def test_song_length_is_read_from_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            tagged, plain = Path(d) / 'tagged.mp4', Path(d) / 'plain.mp4'
            ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=30', '-t', '1', '-metadata',
                   'comment=radio-sync ending=22.104', tagged)
            ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=30', '-t', '1', plain)
            self.assertEqual(server.embedded_ending(tagged), 22.104)
            self.assertEqual(server.embedded_ending(plain), 0.0)
            self.assertEqual(server.embedded_ending(Path(d) / 'missing.mp4'), 0.0)


if __name__ == '__main__':
    unittest.main()
