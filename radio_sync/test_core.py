import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from scipy.io import wavfile
import core


R = core.RATE


def at(episode, t):
    return next(s for s in episode['segments'] if s['start'] <= t < s['end'])


def analyze_arrays(signals, audios, videos):
    with patch('core.decode', side_effect=lambda p, **k: signals[p]):
        return core.analyze(audios, videos)


class Tests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(42)

    def test_offset_noise_polarity(self):
        ref = self.rng.normal(size=40000)
        q = -ref[6000:22000] * .3 + self.rng.normal(0, .02, 16000)
        time, score = core.peaks(ref, q)[0]
        self.assertAlmostEqual(time, 3)
        self.assertGreater(score, .95)

    def test_silence_and_short(self):
        self.assertEqual(core.peaks(np.zeros(40000), np.zeros(16000)), [])
        self.assertEqual(core.peaks(np.ones(100), np.ones(20)), [])

    def test_repeated_ambiguous(self):
        q = self.rng.normal(size=16000)
        ref = np.concatenate([q, q])
        matches = core.peaks(ref, q)
        self.assertLess(abs(matches[0][1]-matches[1][1]), .001)

    def test_episode_assignment_and_gap(self):
        a = self.rng.normal(size=48000)
        b = self.rng.normal(size=16000)
        signals = {'a': a, 'b': b, 'v1': a[:16000], 'v2': a[32000:], 'v3': b}
        with patch('core.decode', side_effect=lambda p, **k: signals[p]):
            p = core.analyze(['a', 'b'], ['v1', 'v2', 'v3'])
        segments = p['episodes'][0]['segments']
        self.assertEqual([s['enabled'] for s in segments], [True, False, True])
        self.assertEqual(p['episodes'][1]['segments'][0]['video'], 'v3')

    def test_save_roundtrip(self):
        p = {'version': 1, 'episodes': [], 'note': '日本語'}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'p.json'
            core.save(p, path)
            self.assertEqual(core.load(path), p)

    def test_progress_and_cancel(self):
        a = self.rng.normal(size=48000)
        signals = {'a': a, 'v': a}
        seen = []
        with patch('core.decode', side_effect=lambda p, **k: signals[p]):
            core.analyze(['a'], ['v'], progress=seen.append)
            self.assertEqual(seen, sorted(seen))
            self.assertAlmostEqual(seen[-1], 1.0)
            with self.assertRaises(core.Cancelled):
                core.analyze(['a'], ['v'], cancelled=lambda: True)

    def test_keep_locked_segments(self):
        seg = lambda s, e, src, locked=False: {'start': s, 'end': e, 'video': 'v', 'source': src, 'score': 1,
                                               'status': 'manual' if locked else 'auto', 'enabled': True,
                                               'locked': locked, 'candidates': [{'video': 'v', 'source': src, 'score': 1}]}
        new = {'audio': 'a', 'duration': 24.0, 'segments': [seg(0, 8, 100), seg(8, 16, 108), seg(16, 24, 116)]}
        old = {'audio': 'a', 'duration': 24.0, 'segments': [seg(0, 10, 0), seg(10, 13, 50, True), seg(13, 24, 0)]}
        result = core.keep_locked(new, old)['segments']
        self.assertEqual([(s['start'], s['end']) for s in result], [(0, 8), (8, 10), (10, 13), (13, 16), (16, 24)])
        self.assertEqual([s['source'] for s in result], [100, 108, 50, 113, 116])
        self.assertEqual(result[3]['candidates'][0]['source'], 113)
        other = {**old, 'duration': 30.0}
        self.assertIs(core.keep_locked(new, other), new)

    def test_plan_parts_joins_continuous_video(self):
        seg = lambda s, e, video, src, on=True: {'start': s, 'end': e, 'video': video, 'source': src, 'enabled': on}
        parts = core.plan_parts([seg(0, 8, 'A', 3), seg(8, 16, 'A', 11), seg(16, 24, 'A', 50),
                                 seg(24, 32, 'B', 0, False), seg(32, 36, '', 0, False), seg(36, 40, 'B', 9)], 'logo.png')
        self.assertEqual(parts, [{'video': 'A', 'still': '', 'source': 3, 'duration': 16},
                                 {'video': 'A', 'still': '', 'source': 50, 'duration': 8},
                                 {'video': '', 'still': 'logo.png', 'source': 0.0, 'duration': 12},
                                 {'video': 'B', 'still': '', 'source': 9, 'duration': 4}])
        own = core.plan_parts([seg(0, 4, '', 0, False), {**seg(4, 8, '', 0, False), 'still': 'b.png'}], 'logo.png')
        self.assertEqual([(p['still'], p['duration']) for p in own], [('logo.png', 4), ('b.png', 4)])

    def test_decode_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'tone.wav'
            signal = self.rng.normal(0, .1, core.RATE*10).astype('float32')
            wavfile.write(path, core.RATE, signal)
            part = core.decode(path, 3, 2)
            self.assertEqual(len(part), core.RATE*2)
            self.assertGreater(np.corrcoef(part, signal[core.RATE*3:core.RATE*5])[0, 1], .99)

    def test_weak_but_consistent_camera_is_used(self):
        talk = self.rng.normal(size=R * 200)
        camera = np.concatenate([self.rng.normal(size=R * 10), talk * .25 + self.rng.normal(size=len(talk))])
        e = analyze_arrays({'radio': talk, 'cam': camera}, ['radio'], ['cam'])['episodes'][0]
        self.assertTrue(all(s['enabled'] and s['status'] == 'auto' and s['video'] == 'cam' for s in e['segments']))
        self.assertLess(max(abs(s['source'] - s['start'] - 10) for s in e['segments']), .01)
        self.assertLess(max(s['score'] for s in e['segments']), .5)

    def test_camera_stop_is_cut_at_the_file_end(self):
        talk = self.rng.normal(size=R * 150)
        camera = np.concatenate([self.rng.normal(size=R * 3), talk[:int(R * 100.5)]])
        e = analyze_arrays({'radio': talk, 'cam': camera}, ['radio'], ['cam'])['episodes'][0]
        last = max((s for s in e['segments'] if s['enabled']), key=lambda s: s['end'])
        self.assertAlmostEqual(last['end'], 100.5, delta=.04)
        self.assertLessEqual(last['source'] + last['end'] - last['start'], 103.5)
        self.assertFalse(at(e, 110)['enabled'])
        self.assertEqual(at(e, 110)['status'], 'auto')

    def test_radio_pause_switches_camera_position(self):
        talk = self.rng.normal(size=R * 300)
        radio = np.concatenate([talk[:R * 124], talk[R * 184:]])
        camera = np.concatenate([self.rng.normal(size=R * 5), talk])
        e = analyze_arrays({'radio': radio, 'cam': camera}, ['radio'], ['cam'])['episodes'][0]
        self.assertAlmostEqual(at(e, 60)['source'] - at(e, 60)['start'], 5, delta=.01)
        self.assertAlmostEqual(at(e, 200)['source'] - at(e, 200)['start'], 65, delta=.01)
        switch = next(s['start'] for s in e['segments'] if s['source'] - s['start'] > 30)
        self.assertAlmostEqual(switch, 124, delta=.5)

    def test_clock_drift_is_followed(self):
        talk = self.rng.normal(size=R * 400)
        drift = 1e-4
        t = np.arange(R * 420) / R
        camera = np.interp((t - 10) / (1 + drift), np.arange(len(talk)) / R, talk, left=0, right=0)
        camera += self.rng.normal(0, .5, len(camera))
        e = analyze_arrays({'radio': talk, 'cam': camera}, ['radio'], ['cam'])['episodes'][0]
        s = at(e, 392)
        self.assertAlmostEqual(s['source'], s['start'] * (1 + drift) + 10, delta=.005)

    def test_first_camera_has_priority(self):
        talk = self.rng.normal(size=R * 80)
        signals = {'radio': talk, 'weak': talk * .3 + self.rng.normal(size=len(talk)), 'strong': talk.copy()}
        e = analyze_arrays(signals, ['radio'], ['weak', 'strong'])['episodes'][0]
        self.assertTrue(all(s['video'] == 'weak' and s['enabled'] for s in e['segments']))

    def test_unmatched_opening_keeps_camera_but_needs_review(self):
        talk = self.rng.normal(size=R * 200)
        radio = np.concatenate([self.rng.normal(size=R * 16), talk[R * 16:]])  # a jingle only on the radio
        camera = np.concatenate([self.rng.normal(size=R * 10), talk * .3 + self.rng.normal(size=len(talk))])
        e = analyze_arrays({'radio': radio, 'cam': camera}, ['radio'], ['cam'])['episodes'][0]
        for t in (4, 12):
            self.assertTrue(at(e, t)['enabled'])
            self.assertEqual((at(e, t)['status'], at(e, t)['reason']), ('review', 'weak'))
        self.assertEqual(at(e, 40)['status'], 'auto')
        self.assertEqual(sum(s['status'] == 'review' for s in e['segments']), 2)

    def test_media_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            signal = (self.rng.normal(0, .1, core.RATE*16)).astype('float32')
            wavfile.write(root/'radio.wav', core.RATE, signal)
            wavfile.write(root/'camera.wav', core.RATE, signal[:core.RATE*8])
            core.run([core.binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=320x180:r=30', '-i', root/'camera.wav', '-t', '8',
                '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', root/'camera.mp4'])
            p = core.analyze([str(root/'radio.wav')], [str(root/'camera.mp4')])
            e = p['episodes'][0]
            self.assertTrue(at(e, 4)['enabled'])
            self.assertFalse(at(e, 12)['enabled'])
            core.export_episode(e, root/'out.mp4')
            duration, streams = core.probe(root/'out.mp4')
            self.assertAlmostEqual(duration, 16, delta=.1)
            self.assertEqual({s['codec_type'] for s in streams}, {'video', 'audio'})
            output_audio = core.decode(root/'out.mp4')[:len(signal)]
            self.assertGreater(np.corrcoef(signal, output_audio)[0, 1], .9)
            with self.assertRaises(ValueError):
                core.export_episode(e, root/'out.mp4')


class EndingTests(unittest.TestCase):
    """話の後ろにエンディング曲を足す書き出し"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.dir.name)
        rng = np.random.default_rng(3)
        cls.radio = rng.normal(0, .1, core.RATE * 8).astype('float32')
        wavfile.write(root/'radio.wav', core.RATE, cls.radio)
        ff = core.binary('ffmpeg')
        # カメラ:青い画面が 20 秒(話は 2〜10 秒の所を使うので、後ろにまだ映像がある)
        core.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30', '-f', 'lavfi',
                  '-i', 'sine=frequency=200:sample_rate=48000', '-t', '20', '-c:v', 'libx264', '-threads', '2',
                  '-c:a', 'aac', root/'camera.mp4'])
        # 曲:440Hz が 3 秒
        core.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '3',
                  root/'song.mp3'])
        # 映像がない所の画像:赤
        core.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=100x50', '-frames:v', '1', root/'red.png'])

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def episode(self, source=2.0, video='camera.mp4'):
        return {'audio': str(self.root/'radio.wav'), 'duration': 8.0, 'segments': [
            {'start': 0.0, 'end': 8.0, 'video': str(self.root/video) if video else '', 'source': source,
             'score': 1, 'status': 'manual', 'enabled': bool(video), 'locked': False, 'candidates': []}]}

    def pixel(self, path, t, x, y):
        raw = core.run([core.binary('ffmpeg'), '-v', 'error', '-ss', f'{t:.3f}', '-i', path, '-frames:v', '1',
                        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(720, 1280, 3)
        return frame[y, x].astype(int)

    def is_blue(self, rgb):
        return rgb[2] > 150 and rgb[0] < 80

    def is_red(self, rgb):
        return rgb[0] > 150 and rgb[2] < 80

    def test_song_is_added_after_the_talk(self):
        out = self.root/'ending.mp4'
        core.export_episode(self.episode(), out, ending={'path': str(self.root/'song.mp3'), 'seconds': 3.0})
        duration, streams = core.probe(out)
        self.assertAlmostEqual(duration, 11, delta=.1)
        audio = core.decode(out)
        # 話の部分はラジオの音のまま
        self.assertGreater(np.corrcoef(self.radio, audio[:len(self.radio)])[0, 1], .9)
        # 後ろの 3 秒は曲(440Hz)で、カメラの音(200Hz)は入らない
        tail = audio[int(core.RATE * 8.2):int(core.RATE * 10.8)]
        spectrum = np.abs(np.fft.rfft(tail))
        freqs = np.fft.rfftfreq(len(tail), 1 / core.RATE)
        self.assertAlmostEqual(freqs[np.argmax(spectrum)], 440, delta=5)
        self.assertLess(spectrum[np.argmin(abs(freqs - 200))], spectrum.max() * .05)
        # 曲のあいだもカメラの映像が続く
        self.assertTrue(self.is_blue(self.pixel(out, 10.0, 640, 360)))
        # 動画クリッパーに、曲の長さを伝える
        tags = json.loads(core.run([core.binary('ffprobe'), '-v', 'error', '-show_entries', 'format_tags=comment',
                                    '-of', 'json', out]))['format']['tags']
        self.assertEqual(tags['comment'], 'radio-sync ending=3.000')

    def test_camera_that_already_stopped_holds_the_last_frame(self):
        out = self.root/'stopped.mp4'
        # カメラの残りは 1 秒だけ(19〜20 秒)。曲は 3 秒
        core.export_episode(self.episode(source=11.0), out, ending={'path': str(self.root/'song.mp3'), 'seconds': 3.0})
        duration, _ = core.probe(out)
        self.assertAlmostEqual(duration, 11, delta=.1)
        self.assertTrue(self.is_blue(self.pixel(out, 10.8, 640, 360)))

    def test_still_at_the_end_stays_during_the_song(self):
        out = self.root/'still.mp4'
        core.export_episode(self.episode(video=''), out, still=str(self.root/'red.png'),
                            ending={'path': str(self.root/'song.mp3'), 'seconds': 3.0})
        self.assertTrue(self.is_red(self.pixel(out, 10.0, 640, 360)))

    def test_without_ending_nothing_changes(self):
        out = self.root/'plain.mp4'
        core.export_episode(self.episode(), out)
        duration, _ = core.probe(out)
        self.assertAlmostEqual(duration, 8, delta=.1)



class TrimTests(unittest.TestCase):
    """いらない所を削ってから、残りを分けて書き出す処理"""

    def seg(self, start, end, video='', source=0.0):
        return {'start': start, 'end': end, 'video': video, 'source': source, 'score': .5,
                'status': 'auto', 'enabled': bool(video), 'locked': False, 'candidates': []}

    def episode(self, **extra):
        # カメラA(0〜100秒)→ 静止画(100〜110)→ カメラB(110〜200)
        return {'audio': 'a.mp3', 'duration': 200.0, 'segments': [
            self.seg(0, 100, 'A.MOV', 5.0), self.seg(100, 110), self.seg(110, 200, 'B.MOV', 0.0)], **extra}

    def check(self, part):
        """区間が 0 秒から隙間なく並んでいるか(書き出しが通る形か)"""
        self.assertEqual(part['segments'][0]['start'], 0.0)
        self.assertAlmostEqual(part['segments'][-1]['end'], part['duration'], places=3)
        for a, b in zip(part['segments'], part['segments'][1:]):
            self.assertAlmostEqual(a['end'], b['start'], places=3)
        self.assertAlmostEqual(sum(d for _, d in part['audio_parts']), part['duration'], places=3)

    def test_no_trim_means_one_whole_file(self):
        outs = core.plan_outputs(self.episode())
        self.assertEqual(len(outs), 1)
        self.assertAlmostEqual(outs[0]['duration'], 200.0, places=3)
        self.assertEqual(outs[0]['audio_parts'], [(0.0, 200.0)])
        self.check(outs[0])

    def test_trim_shortens_the_file(self):
        outs = core.plan_outputs(self.episode(trims=[{'start': 20.0, 'end': 50.0}]))
        self.assertEqual(len(outs), 1)
        self.assertAlmostEqual(outs[0]['duration'], 170.0, places=3)
        self.check(outs[0])

    def test_trim_keeps_the_source_position(self):
        # 20〜50秒を削ると、その後ろは素材を 30 秒進んだ所から使う
        outs = core.plan_outputs(self.episode(trims=[{'start': 20.0, 'end': 50.0}]))
        after = [s for s in outs[0]['segments'] if s['video'] == 'A.MOV'][1]
        self.assertAlmostEqual(after['start'], 20.0, places=3)
        self.assertAlmostEqual(after['source'], 55.0, places=3)

    def test_audio_is_cut_and_joined(self):
        outs = core.plan_outputs(self.episode(trims=[{'start': 20.0, 'end': 50.0}]))
        self.assertEqual(outs[0]['audio_parts'], [(0.0, 20.0), (50.0, 150.0)])

    def test_overlapping_trims_are_merged(self):
        trims = [{'start': 50.0, 'end': 70.0}, {'start': 60.0, 'end': 90.0}, {'start': 10.0, 'end': 20.0}]
        self.assertEqual(core.normalize_trims(trims, 200.0),
                         [{'start': 10.0, 'end': 20.0}, {'start': 50.0, 'end': 90.0}])
        outs = core.plan_outputs(self.episode(trims=trims))
        self.assertAlmostEqual(outs[0]['duration'], 150.0, places=3)

    def test_trim_then_split(self):
        # 20〜50秒を削ってから、静止画の真ん中(105秒)で分ける
        outs = core.plan_outputs(self.episode(trims=[{'start': 20.0, 'end': 50.0}], cuts=[105.0]))
        self.assertEqual(len(outs), 2)
        self.assertAlmostEqual(outs[0]['duration'], 75.0, places=3)     # 105 - 30
        self.assertAlmostEqual(outs[1]['duration'], 95.0, places=3)
        self.assertEqual(outs[0]['audio_parts'], [(0.0, 20.0), (50.0, 55.0)])
        self.assertEqual(outs[1]['audio_parts'], [(105.0, 95.0)])
        for part in outs:
            self.check(part)

    def test_split_point_inside_a_trim_is_ignored(self):
        outs = core.plan_outputs(self.episode(trims=[{'start': 100.0, 'end': 140.0}], cuts=[120.0]))
        self.assertEqual(len(outs), 1)
        self.assertAlmostEqual(outs[0]['duration'], 160.0, places=3)

    def test_trimming_a_whole_part_drops_that_file(self):
        outs = core.plan_outputs(self.episode(cuts=[105.0], trims=[{'start': 0.0, 'end': 105.0}]))
        self.assertEqual(len(outs), 1)
        self.assertAlmostEqual(outs[0]['duration'], 95.0, places=3)
        self.assertEqual(outs[0]['audio_parts'], [(105.0, 95.0)])

    def test_old_projects_keep_working(self):
        # 前の作りの「2本目は書き出さない」が、削る範囲として読み替えられる
        outs = core.plan_outputs(self.episode(cuts=[105.0], drop=[1]))
        self.assertEqual(len(outs), 1)
        self.assertAlmostEqual(outs[0]['duration'], 105.0, places=3)

    def test_everything_trimmed_makes_nothing(self):
        self.assertEqual(core.plan_outputs(self.episode(trims=[{'start': 0.0, 'end': 200.0}])), [])

    def test_total_length_matches_what_is_kept(self):
        episode = self.episode(cuts=[40.0, 105.0, 160.0],
                               trims=[{'start': 10.0, 'end': 30.0}, {'start': 120.0, 'end': 150.0}])
        outs = core.plan_outputs(episode)
        self.assertEqual(len(outs), 4)
        self.assertAlmostEqual(sum(o['duration'] for o in outs), 150.0, places=3)
        for part in outs:
            self.check(part)


class FrameCountTests(unittest.TestCase):
    """映像のコマ数(足りないと、音とだんだんずれる)"""

    def test_total_is_exact(self):
        lengths = [8.0, 5.4, 2.3, 12.367, 1.7333, 0.9, 119.633]
        counts = core.frame_counts(lengths)
        self.assertEqual(sum(counts), round(sum(lengths) * core.FPS))

    def test_no_drift_over_many_parts(self):
        # 半端な長さが続いても、切り捨てが積もらない
        counts = core.frame_counts([0.71] * 200)
        self.assertEqual(sum(counts), round(0.71 * 200 * core.FPS))
        self.assertTrue(all(c in (21, 22) for c in counts), counts)

    def test_whole_seconds_are_plain(self):
        self.assertEqual(core.frame_counts([1.0, 2.0, 8.0]), [30, 60, 240])

    def test_each_part_is_close_to_its_length(self):
        lengths = [3.333, 5.033, 21.324, 0.4, 16.0]
        for length, count in zip(lengths, core.frame_counts(lengths)):
            self.assertLess(abs(count - length * core.FPS), 1.0)

    def test_empty(self):
        self.assertEqual(core.frame_counts([]), [])


class QualityTests(unittest.TestCase):
    """書き出しの画質の選択"""

    def test_default_is_high(self):
        self.assertEqual(core.quality_of(None), core.QUALITY['high'])

    def test_unknown_name_falls_back(self):
        self.assertEqual(core.quality_of('ものすごい画質'), core.QUALITY[core.DEFAULT_QUALITY])

    def test_better_quality_means_smaller_crf(self):
        crfs = [int(core.QUALITY[k]['crf']) for k in ('standard', 'high', 'best')]
        self.assertEqual(crfs, sorted(crfs, reverse=True))

    def test_size_estimate_grows_with_quality(self):
        sizes = [core.QUALITY[k]['bytes'] for k in ('standard', 'high', 'best')]
        self.assertEqual(sizes, sorted(sizes))


class NetworkTests(unittest.TestCase):
    """スマホからつなぐときの、アドレスの見分け"""

    def setUp(self):
        import server
        self.server = server

    def test_same_wifi_is_allowed(self):
        for ip in ('192.168.1.5', '10.0.0.8', '172.16.3.9', '100.64.1.26'):
            self.assertTrue(self.server.same_network(ip), ip)

    def test_outside_is_refused(self):
        # 203.0.113.x など「説明用」の範囲は Python が私設扱いにするので、ここでは使わない
        for ip in ('8.8.8.8', '1.1.1.1', '52.10.20.30', '127.0.0.1', '169.254.1.1', 'example.com', ''):
            self.assertFalse(self.server.same_network(ip), ip)

    def test_pin_is_needed(self):
        self.server.LAN.update(open=False, pin='', until=0.0, tries=0)
        with self.assertRaises(self.server.UserError):
            self.server.pair({'pin': '123456'})

    def test_wrong_pin_is_refused_and_right_one_works(self):
        import time as clock
        self.server.LAN.update(open=True, pin='123456', until=clock.time() + 60, tries=0)
        with self.assertRaises(self.server.UserError):
            self.server.pair({'pin': '000000'})
        self.assertEqual(self.server.pair({'pin': '123456'})['token'], self.server.TOKEN)
        self.server.LAN.update(open=False, pin='', until=0.0, tries=0)

    def test_too_many_tries_closes_the_door(self):
        import time as clock
        self.server.LAN.update(open=True, pin='123456', until=clock.time() + 60, tries=0)
        for _ in range(11):
            with self.assertRaises(self.server.UserError):
                self.server.pair({'pin': '000000'})
        self.assertFalse(self.server.LAN['open'])

if __name__ == '__main__':
    unittest.main()


class SplitTests(unittest.TestCase):
    """1話を複数のファイルに分けて書き出す処理"""

    def seg(self, start, end, video='', source=0.0):
        return {'start': start, 'end': end, 'video': video, 'source': source, 'score': .5,
                'status': 'auto', 'enabled': bool(video), 'locked': False, 'candidates': []}

    def episode(self):
        # カメラA(0〜100秒)→ 撮り直しの静止画(100〜110)→ カメラB(110〜200)
        return {'audio': 'a.mp3', 'duration': 200.0, 'segments': [
            self.seg(0, 100, 'A.MOV', 5.0), self.seg(100, 110), self.seg(110, 200, 'B.MOV', 0.0)]}

    def test_change_点_is_between_cameras(self):
        self.assertEqual(core.camera_changes(self.episode()['segments']), [105.0])

    def test_direct_change_without_still(self):
        segs = [self.seg(0, 60, 'A.MOV'), self.seg(60, 120, 'B.MOV')]
        self.assertEqual(core.camera_changes(segs), [60.0])

    def test_no_change_for_one_camera(self):
        segs = [self.seg(0, 60, 'A.MOV', 0), self.seg(60, 120, 'A.MOV', 60)]
        self.assertEqual(core.camera_changes(segs), [])

    def test_split_makes_independent_episodes(self):
        parts = core.split_episode(self.episode(), [105.0])
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertEqual(part['segments'][0]['start'], 0.0)
            self.assertAlmostEqual(part['segments'][-1]['end'], part['duration'], places=3)
            for a, b in zip(part['segments'], part['segments'][1:]):
                self.assertAlmostEqual(a['end'], b['start'], places=3)
        self.assertAlmostEqual(parts[0]['duration'], 105.0, places=3)
        self.assertAlmostEqual(parts[1]['audio_start'], 105.0, places=3)

    def test_split_keeps_the_source_position(self):
        # 分けた2本目のカメラBは、元と同じ位置から使う
        parts = core.split_episode(self.episode(), [105.0])
        second = [s for s in parts[1]['segments'] if s['video'] == 'B.MOV'][0]
        self.assertAlmostEqual(second['source'], 0.0, places=3)
        # カメラAを途中で切った場合は、切った分だけ素材の位置も進む
        parts = core.split_episode(self.episode(), [50.0])
        first_of_second = parts[1]['segments'][0]
        self.assertAlmostEqual(first_of_second['source'], 55.0, places=3)

    def test_split_ignores_points_outside(self):
        parts = core.split_episode(self.episode(), [-5, 0, 200, 500])
        self.assertEqual(len(parts), 1)
        self.assertAlmostEqual(parts[0]['duration'], 200.0, places=3)

    def test_total_length_is_kept(self):
        parts = core.split_episode(self.episode(), [40.0, 105.0, 160.0])
        self.assertEqual(len(parts), 4)
        self.assertAlmostEqual(sum(p['duration'] for p in parts), 200.0, places=3)
