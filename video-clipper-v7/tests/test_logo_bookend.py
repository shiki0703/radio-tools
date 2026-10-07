# -*- coding: utf-8 -*-
"""ロゴを重ねる焼き込み・切り抜きと、エンディング動画へのつなぎ方のテスト(FFmpeg を使う)

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

from src.bookend import attach, FADE_SEC  # noqa: E402
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


def brightness(path, t):
    out = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.2f}', '-i', str(path), '-frames:v', '1', '-vf', 'scale=64:36',
                          '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], check=True, capture_output=True).stdout
    return float(np.frombuffer(out, dtype=np.uint8).mean())


def loudness(path, t, length=0.3):
    out = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.2f}', '-t', str(length), '-i', str(path), '-ac', '1',
                          '-ar', '8000', '-f', 'f32le', '-'], check=True, capture_output=True).stdout
    data = np.frombuffer(out, dtype=np.float32)
    return float(np.sqrt((data ** 2).mean())) if len(data) else 0.0


class EndingJoinTest(unittest.TestCase):
    """本編の最後を徐々に暗く・静かにしてから、エンディング動画を始める"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.dir.name)
        # 本編:白い画面と音が 6 秒。エンディング:赤い画面(音なし)が 3 秒、大きさも違う
        ffmpeg('-f', 'lavfi', '-i', 'color=c=white:s=640x360:r=30', '-f', 'lavfi', '-i', 'sine=frequency=300:sample_rate=48000',
               '-t', '6', '-c:v', 'libx264', '-c:a', 'aac', root / 'main.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'color=c=red:s=320x240:r=24', '-t', '3', '-c:v', 'libx264', root / 'ending.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=640x360:r=30', '-f', 'lavfi', '-i', 'sine=frequency=500:sample_rate=44100',
               '-t', '2', '-c:v', 'libx264', '-c:a', 'aac', root / 'opening.mp4')

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def joined(self, name, **kw):
        out = self.root / name
        out.write_bytes((self.root / 'main.mp4').read_bytes())      # 本番と同じく、焼き込んだ動画を置き換える
        self.assertTrue(attach(str(out), str(out), **kw))
        return out

    def test_main_fades_to_black_then_the_ending_starts(self):
        out = self.joined('with_ending.mp4', outro=str(self.root / 'ending.mp4'))
        self.assertAlmostEqual(float(probe_duration(out)), 9.0, delta=0.15)
        self.assertGreater(brightness(out, 2.0), 230)                    # 暗くなる前は白いまま
        self.assertLess(brightness(out, 6.0 - FADE_SEC / 2), 200)        # 暗くなっていく途中
        self.assertGreater(brightness(out, 6.0 - FADE_SEC / 2), 50)
        self.assertLess(brightness(out, 5.95), 30)                       # 本編の終わりは、ほぼ真っ黒
        self.assertTrue(is_red(pixel(out, 7.5, 320, 180)))               # そのあとエンディング
        self.assertGreater(loudness(out, 2.0), 0.05)                     # 音も徐々に小さくなる
        self.assertLess(loudness(out, 5.75, 0.2), loudness(out, 2.0) * 0.3)
        size = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                          'stream=width,height', '-of', 'json', str(out)],
                                         check=True, capture_output=True).stdout)['streams'][0]
        self.assertEqual((size['width'], size['height']), (640, 360))

    def test_opening_alone_does_not_fade_the_main(self):
        out = self.joined('with_opening.mp4', intro=str(self.root / 'opening.mp4'))
        self.assertAlmostEqual(float(probe_duration(out)), 8.0, delta=0.15)
        self.assertTrue(is_blue(pixel(out, 1.0, 320, 180)))
        self.assertGreater(brightness(out, 7.9), 230)                    # エンディングがなければ暗くしない

    def test_nothing_to_join(self):
        self.assertFalse(attach(str(self.root / 'main.mp4'), str(self.root / 'x.mp4')))


def probe_duration(path):
    return json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                                     check=True, capture_output=True).stdout)['format']['duration']


if __name__ == '__main__':
    unittest.main()
