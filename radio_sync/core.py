"""Local-only experimental radio/video synchronizer. Times are seconds."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import numpy as np
from scipy.signal import correlate

RATE = 2000
BLOCK = 8.0
FPS = 30
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
OVERRUN = .25  # seconds a segment may run past the end of its video; the last frame is held

# 書き出しの画質。crf は小さいほどきれい(そのぶん重く、大きくなる)。
QUALITY = {
    'standard': {'label': '標準', 'preset': 'fast', 'crf': '20', 'bytes': 400_000},
    'high': {'label': '高画質', 'preset': 'medium', 'crf': '16', 'bytes': 800_000},
    'best': {'label': '最高画質', 'preset': 'slow', 'crf': '14', 'bytes': 1_100_000},
}
DEFAULT_QUALITY = 'high'


def quality_of(name):
    return QUALITY.get(name or DEFAULT_QUALITY, QUALITY[DEFAULT_QUALITY])


class Cancelled(Exception):
    """The user stopped a long operation."""


def never():
    return False


def _start(args):
    return subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=NO_WINDOW)


def _finish(process, errors, reader, cancelled):
    process.wait()
    reader.join()
    process.stdout.close()
    process.stderr.close()
    if cancelled():
        raise Cancelled()
    if process.returncode:
        raise RuntimeError(b''.join(errors).decode('utf-8', errors='replace')[-4000:])


def _drain(stream, sink):
    reader = threading.Thread(target=lambda: sink.append(stream.read()), daemon=True)
    reader.start()
    return reader


def run(args, cancelled=never):
    """Return stdout of a command; the process is killed once cancelled() is true."""
    process = _start(args)
    errors = []
    reader = _drain(process.stderr, errors)
    chunks = []
    while chunk := process.stdout.read1(1 << 20):
        chunks.append(chunk)
        if cancelled():
            process.kill()
            break
    _finish(process, errors, reader, cancelled)
    return b''.join(chunks)


def run_progress(args, duration, report=lambda f: None, cancelled=never):
    """Run FFmpeg, reporting the encoded fraction of duration seconds."""
    process = _start([args[0], '-nostats', '-progress', 'pipe:1', *args[1:]])
    errors = []
    reader = _drain(process.stderr, errors)
    for line in process.stdout:
        if cancelled():
            process.kill()
            break
        name, _, value = line.decode('ascii', errors='replace').strip().partition('=')
        if name == 'out_time_us' and value.isdigit() and duration > 0:
            report(min(1.0, int(value) / 1e6 / duration))
    _finish(process, errors, reader, cancelled)


def binary(name):
    local = Path(__file__).parent / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))
    found = str(local) if local.is_file() else shutil.which(name)
    if not found:
        raise RuntimeError(f'{name} がありません。bin フォルダか PATH に配置してください。')
    return found


def probe(path):
    data = json.loads(run([binary('ffprobe'), '-v', 'error', '-show_format',
                           '-show_streams', '-of', 'json', path]))
    return float(data['format']['duration']), data['streams']


def decode(path, start=0.0, duration=None, cancelled=never):
    """Mono analysis audio of the first audio track, optionally a window only."""
    args = [binary('ffmpeg'), '-v', 'error']
    if start > 0:
        args += ['-ss', f'{start:.4f}']
    args += ['-i', path]
    if duration is not None:
        args += ['-t', f'{duration:.4f}']
    return np.frombuffer(run([*args, '-map', '0:a:0', '-ac', '1', '-ar', RATE, '-f', 'f32le', '-'],
                             cancelled), dtype='<f4').astype(np.float64)


def peaks(reference, query, rate=RATE):
    """Normalized cross correlation, absolute score supports inverted polarity."""
    n = len(query)
    if n < rate or len(reference) < n:
        return []
    q = query - query.mean()
    energy = float(q @ q)
    if energy / n < 1e-10:
        return []
    sums = np.concatenate(([0.0], np.cumsum(reference)))
    squares = np.concatenate(([0.0], np.cumsum(reference ** 2)))
    variance = np.maximum(0, squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n)
    scores = np.abs(correlate(reference, q, mode='valid', method='fft')) / np.sqrt(np.maximum(variance * energy, 1e-20))
    scores[variance < 1e-10 * n] = 0
    result = []
    for _ in range(2):
        k = int(np.argmax(scores))
        result.append((k / rate, min(1.0, float(scores[k]))))
        scores[max(0, k-rate//2):k+rate//2+1] = 0
    return result


def analyze(audios, videos, notify=lambda s: None, progress=lambda f: None,
            cancelled=never, durations=None):
    """Match every BLOCK of each radio file against all camera audio.

    durations (seconds per audio, optional) only weights the progress report.
    """
    project = {'version': 1, 'audios': audios, 'videos': videos, 'still': '', 'episodes': []}
    weights = list(durations) if durations else [1.0] * len(audios)
    total = sum(weights) or 1.0
    decode_share = min(.3, .03 * len(videos))
    decoded = []
    for i, video in enumerate(videos):
        notify('動画の音声を読み込み中: ' + Path(video).name)
        decoded.append(decode(video, cancelled=cancelled))
        progress(decode_share * (i + 1) / len(videos))
    done = 0.0
    for audio, weight in zip(audios, weights):
        notify('照合中: ' + Path(audio).name)
        signal = decode(audio, cancelled=cancelled)
        duration = len(signal) / RATE
        blocks = []
        starts = np.arange(0, duration, BLOCK)
        for k, start in enumerate(starts):
            if cancelled():
                raise Cancelled()
            end = min(duration, start + BLOCK)
            query = signal[round(start*RATE):round(end*RATE)]
            candidates = []
            for video, samples in zip(videos, decoded):
                candidates.extend({'video': video, 'source': t, 'score': score} for t, score in peaks(samples, query))
            candidates.sort(key=lambda x: x['score'], reverse=True)
            blocks.append({'start': float(start), 'end': float(end), 'candidates': candidates})
            progress(decode_share + (1 - decode_share) * (done + weight * (k + 1) / len(starts)) / total)
        done += weight
        segments = assemble(duration, blocks, signal, dict(zip(videos, decoded)), videos)
        project['episodes'].append({'audio': audio, 'duration': duration, 'segments': segments})
    return project


MIN_ANCHOR = .12   # weakest block score that can anchor a camera run when neighbours agree
AGREE = .2         # seconds two offsets may differ and still belong to the same run
NEIGHBOURS = 4     # blocks on each side that may confirm an anchor
SPLIT_WINDOW = 2.0
SPLIT_STEP = .5
# Camera footage stays the default, but barely matching audio is flagged for review
# (e.g. a jingle only on the radio). Real studio material: matched stretches never fell below .11.
WEAK_EXTENDED = .1   # beyond the first/last matched block of a run
WEAK_MATCHED = .05   # between matched blocks


class Run:
    """A stretch where one camera file follows the radio: source = t + b + a*t.

    Inside one file the camera recorded continuously, so the relation also holds
    between and beyond the matched blocks, up to the ends of the file.
    """

    def __init__(self, video, rank, samples, anchors):
        self.video, self.rank, self.samples = video, rank, samples
        self.length = len(samples) / RATE
        t = np.array([p['start'] for p in anchors])
        o = np.array([p['offset'] for p in anchors])
        w = np.array([p['score'] for p in anchors])
        self.core0, self.core1 = float(t.min()), max(p['end'] for p in anchors)
        self.strength = float(w.sum())
        keep = np.ones(len(t), bool)
        a, b = 0.0, float(np.median(o))
        for _ in range(3):
            if keep.sum() >= 3 and np.ptp(t[keep]) >= 60:
                a, b = (float(x) for x in np.polyfit(t[keep], o[keep], 1, w=w[keep]))
            else:
                a, b = 0.0, float(np.median(o[keep]))
            if abs(a) > 5e-4:  # clock drift beyond 500 ppm is not physical; treat as outliers
                a, b = 0.0, float(np.median(o[keep]))
            fitted = np.abs(o - (b + a * t)) <= .08
            if not fitted.any():
                break
            keep = fitted
        self.a, self.b = a, b

    def source(self, t):
        return t + self.b + self.a * t

    def reaches(self, t0, t1):
        return (self.source(t0) >= -1e-6) & (self.source(t1) <= self.length + 1e-6)

    def score(self, signal, t0, t1, slack=.01):
        """Best match within +-slack seconds of the fitted position (the fit is only ms accurate)."""
        a0, a1 = round(t0 * RATE), round(t1 * RATE)
        at = round(self.source(t0) * RATE)
        lo = max(0, at - round(slack * RATE))
        hi = min(len(self.samples), at + (a1 - a0) + round(slack * RATE))
        if a1 - a0 < RATE or hi - lo < a1 - a0 or at < 0:
            return None
        found = peaks(self.samples[lo:hi], signal[a0:a1])
        return found[0][1] if found else 0.0


def _find_runs(blocks, samples, videos):
    runs = []
    for rank, video in enumerate(videos):
        options = [[(c['source'] - b['start'], c['score']) for c in b['candidates'] if c['video'] == video] for b in blocks]
        anchors = []
        for k, block in enumerate(blocks):
            ranked = block['candidates']
            best = None
            for offset, score in options[k]:
                if score < MIN_ANCHOR:
                    continue
                support = sum(any(abs(o - offset) <= AGREE and s >= MIN_ANCHOR * .8 for o, s in options[j])
                              for j in range(max(0, k - NEIGHBOURS), min(len(blocks), k + NEIGHBOURS + 1)) if j != k)
                strong = (ranked[0]['video'] == video and abs(ranked[0]['source'] - block['start'] - offset) < 1e-6
                          and score >= .5 and score - (ranked[1]['score'] if len(ranked) > 1 else 0) >= .12)
                if (strong or support >= 2 or (support >= 1 and score >= .25)) and (best is None or score > best['score']):
                    best = {'start': block['start'], 'end': block['end'], 'offset': offset, 'score': score, 'strong': strong}
            if best:
                anchors.append(best)
        chains = []
        for anchor in anchors:
            for chain in chains:
                if abs(chain[-1]['offset'] - anchor['offset']) <= AGREE:
                    chain.append(anchor)
                    break
            else:
                chains.append([anchor])
        runs.extend(Run(video, rank, samples[video], chain) for chain in chains
                    if len(chain) >= 2 or any(p['strong'] for p in chain))
    return runs


def _split_point(first, second, signal, t0, t1):
    """Where the timeline switches from run first to run second inside [t0, t1]."""
    centers = np.arange(t0 + SPLIT_WINDOW / 2, t1 - SPLIT_WINDOW / 2 + 1e-9, SPLIT_STEP)
    if len(centers) < 2:
        return (t0 + t1) / 2, False
    diff = []
    for c in centers:
        a = first.score(signal, c - SPLIT_WINDOW / 2, c + SPLIT_WINDOW / 2)
        b = second.score(signal, c - SPLIT_WINDOW / 2, c + SPLIT_WINDOW / 2)
        diff.append((a if a is not None else -1) - (b if b is not None else -1))
    diff = np.array(diff)
    cum = np.concatenate(([0.0], np.cumsum(diff)))
    m = int(np.argmax(2 * cum - cum[-1]))
    split = t0 if m == 0 else t1 if m == len(diff) else (centers[m - 1] + centers[m]) / 2
    sure = (m == 0 or diff[:m].mean() >= .05) and (m == len(diff) or -diff[m:].mean() >= .05)
    return round(split * FPS) / FPS, sure


def assemble(duration, blocks, signal, samples, videos):
    """Decide the camera (or still) for every frame of an episode and cut it into segments.

    Video order is the camera priority when several cameras cover the same moment.
    """
    runs = _find_runs(blocks, samples, videos)
    n = max(1, math.ceil(duration * FPS - 1e-6))
    c0 = np.arange(n) / FPS
    c1 = np.minimum(c0 + 1 / FPS, duration)
    label = np.full(n, -1)
    review = np.zeros(n, bool)
    for i in sorted(range(len(runs)), key=lambda i: (-runs[i].rank, runs[i].strength)):
        r = runs[i]
        label[(c0 >= r.core0 - 1e-9) & (c1 <= r.core1 + 1e-9) & r.reaches(c0, c1)] = i
    i = 0
    while i < n:
        if label[i] >= 0:
            i += 1
            continue
        j = i
        while j < n and label[j] < 0:
            j += 1
        _fill(label, review, i, j, runs, c0, c1, signal)
        i = j
    _refine_switches(label, review, runs, c0, c1, signal)
    if not runs:
        review[:] = True

    segments = []
    bounds = [0, *(np.flatnonzero((np.diff(label) != 0) | (np.diff(review.astype(int)) != 0)) + 1), n]
    for p, q in zip(bounds[:-1], bounds[1:]):
        t0, t1 = p / FPS, min(q / FPS, duration)
        run = runs[label[p]] if label[p] >= 0 else None
        for block in blocks:
            s, e = max(t0, block['start']), min(t1, block['end'])
            if e - s <= 1e-6:
                continue
            shift = s - block['start']
            candidates = [{**c, 'source': c['source'] + shift} for c in block['candidates'][:4]]
            reason = ('switch' if runs else 'nocamera') if review[p] else ''
            if run:
                score = run.score(signal, s, e)
                video, source, enabled = run.video, run.source(s), True
                matched = run.core0 - 1e-6 <= s and e <= run.core1 + 1e-6
                if not reason and (score is None and not matched or score is not None
                                   and score < (WEAK_MATCHED if matched else WEAK_EXTENDED)):
                    reason = 'weak'
                score = score if score is not None else (candidates[0]['score'] if candidates else 0.0)
            else:  # no camera file covers this moment; a stray best candidate would only mislead
                video, source, enabled = '', 0.0, False
                score = candidates[0]['score'] if candidates else 0.0
            segments.append({'start': s, 'end': e, 'video': video, 'source': round(max(0.0, source), 4),
                             'score': round(float(score), 4), 'status': 'review' if reason else 'auto',
                             'enabled': enabled, 'locked': False, 'candidates': candidates,
                             **({'reason': reason} if reason else {})})
    return segments


def _refine_switches(label, review, runs, c0, c1, signal):
    """A block that straddles a switch between two runs may have been given to either; find the real switch."""
    span = int(BLOCK * FPS)
    for p in np.flatnonzero(np.diff(label) != 0) + 1:
        first, second = label[p - 1], label[p]
        if first < 0 or second < 0 or first == second:
            continue
        lo, hi = max(0, p - span), min(len(label), p + span)
        cells = np.arange(lo, hi)
        mine = (label[cells] == first) | (label[cells] == second)
        both = mine & runs[first].reaches(c0[cells], c1[cells]) & runs[second].reaches(c0[cells], c1[cells])
        if both.sum() < SPLIT_WINDOW * FPS * 2:
            continue
        idx = cells[both]
        split, sure = _split_point(runs[first], runs[second], signal, c0[idx[0]], c1[idx[-1]])
        label[idx] = np.where(c0[idx] < split - 1e-9, first, second)
        if not sure:
            review[idx] |= np.abs(c0[idx] - split) <= BLOCK / 2


def _fill(label, review, i, j, runs, c0, c1, signal):
    """Label the unmatched cells [i, j) by extending the neighbouring runs through their camera files."""
    prev = label[i - 1] if i > 0 else -1
    nxt = label[j] if j < len(label) else -1
    cells = slice(i, j)
    fill = np.full(j - i, -1)
    reach_prev = runs[prev].reaches(c0[cells], c1[cells]) if prev >= 0 else np.zeros(j - i, bool)
    reach_next = runs[nxt].reaches(c0[cells], c1[cells]) if nxt >= 0 and nxt != prev else np.zeros(j - i, bool)
    fill[reach_prev] = prev
    fill[reach_next & ~reach_prev] = nxt
    both = np.flatnonzero(reach_prev & reach_next)
    if both.size:
        lo, hi = i + both[0], i + both[-1] + 1
        split, sure = _split_point(runs[prev], runs[nxt], signal, c0[lo], c1[hi - 1])
        fill[both] = np.where(c0[i + both] < split - 1e-9, prev, nxt)
        if not sure:
            near = np.abs(c0[cells] - split) <= BLOCK / 2
            review[cells] |= near & (reach_prev | reach_next)
    for k in sorted(range(len(runs)), key=lambda k: (runs[k].rank, -runs[k].strength)):
        free = fill < 0
        if not free.any():
            break
        fill[free & runs[k].reaches(c0[cells], c1[cells])] = k
    label[cells] = fill


def keep_locked(new, old):
    """Keep the user-fixed (locked) segments of old; fill the gaps from new."""
    if abs(new['duration'] - old['duration']) > .01:
        return new
    locked = sorted((s for s in old['segments'] if s.get('locked') and s['end'] <= new['duration'] + .001),
                    key=lambda s: s['start'])
    if not locked:
        return new
    segments = []

    def fill(lo, hi):
        for s in new['segments']:
            a, b = max(lo, s['start']), min(hi, s['end'])
            if b - a > 1e-6:
                shift = a - s['start']
                segments.append({**s, 'start': a, 'end': b, 'source': s['source'] + shift,
                                 'candidates': [{**c, 'source': c['source'] + shift} for c in s['candidates']]})

    cursor = 0.0
    for s in locked:
        if s['start'] < cursor - .001:
            continue
        fill(cursor, s['start'])
        segments.append(s)
        cursor = s['end']
    fill(cursor, new['duration'])
    return {**new, 'segments': segments}


def save(project, path):
    target = Path(path)
    fd, temp = tempfile.mkstemp(prefix=target.name, dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(project, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(path):
    with open(path, encoding='utf-8') as f:
        p = json.load(f)
    if p.get('version') != 1 or not isinstance(p.get('episodes'), list):
        raise ValueError('非対応のプロジェクトです。')
    return p


def plan_parts(segments, still=''):
    """Join neighbouring segments that continue the same video (or show the same still) into one encode.

    A still segment may name its own image in 'still'; otherwise the episode default is used.
    """
    parts = []
    for s in segments:
        video = s['video'] if s['enabled'] and s['video'] else ''
        image = '' if video else (s.get('still') or still)
        duration = s['end'] - s['start']
        last = parts[-1] if parts else None
        if (last and last['video'] == video and last['still'] == image
                and (not video or abs(last['source'] + last['duration'] - s['source']) < .001)):
            last['duration'] += duration
        else:
            parts.append({'video': video, 'still': image, 'source': s['source'] if video else 0.0, 'duration': duration})
    return parts


def camera_changes(segments):
    """カメラが入れ替わる時刻(分割位置の候補)。

    カメラを止めて撮り直した所では、あいだに静止画の区間ができる。
    その場合は静止画の真ん中で分けると、どちらの動画も欠けない。
    """
    runs = []
    for s in segments:
        video = s['video'] if s['enabled'] and s['video'] else ''
        if runs and runs[-1][0] == video:
            runs[-1][2] = s['end']
        else:
            runs.append([video, s['start'], s['end']])
    points = []
    for i, (video, start, end) in enumerate(runs):
        before = runs[i - 1][0] if i else ''
        after = runs[i + 1][0] if i + 1 < len(runs) else ''
        if not video and before and after and before != after:
            points.append((start + end) / 2)        # 撮り直しの切れ目
        elif video and before and before != video:
            points.append(start)                    # 映像が直接入れ替わる所
    return sorted({round(t, 3) for t in points})


def split_episode(episode, splits):
    """話を splits(秒)の位置で分け、書き出せる形の話の並びにして返す。

    それぞれが 0 秒から始まる独立した話になり、'audio_start' に
    元の音声のどこから始まるかが入る。
    """
    duration = episode['duration']
    points = [0.0, *sorted({round(float(t), 3) for t in splits if 0 < float(t) < duration}), duration]
    parts = []
    for t0, t1 in zip(points, points[1:]):
        if t1 - t0 < .2:
            continue
        segments = []
        for s in episode['segments']:
            a, b = max(s['start'], t0), min(s['end'], t1)
            if b - a <= 1e-6:
                continue
            segments.append({**s, 'start': round(a - t0, 6), 'end': round(b - t0, 6),
                             'source': round(s['source'] + (a - s['start']), 6) if s['video'] else s['source']})
        if not segments:
            continue
        segments[0]['start'] = 0.0
        segments[-1]['end'] = round(t1 - t0, 6)
        parts.append({**episode, 'duration': round(t1 - t0, 6), 'segments': segments,
                      'audio_start': round(t0, 6)})
    return parts


def normalize_trims(trims, duration):
    """削る範囲を、重なりをまとめて時間順に整える"""
    items = []
    for t in trims or []:
        try:
            start, end = max(0.0, float(t['start'])), min(float(t['end']), duration)
        except (KeyError, TypeError, ValueError):
            continue
        if end - start > 0.05:
            items.append({'start': round(start, 3), 'end': round(end, 3)})
    items.sort(key=lambda x: x['start'])
    merged = []
    for t in items:
        if merged and t['start'] <= merged[-1]['end'] + 0.01:
            merged[-1]['end'] = max(merged[-1]['end'], t['end'])
        else:
            merged.append(dict(t))
    return merged


def keep_ranges(duration, trims):
    """削ったあとに残る範囲([(開始, 終了), ...])"""
    ranges, cursor = [], 0.0
    for t in normalize_trims(trims, duration):
        if t['start'] - cursor > 0.05:
            ranges.append((round(cursor, 3), round(t['start'], 3)))
        cursor = max(cursor, t['end'])
    if duration - cursor > 0.05:
        ranges.append((round(cursor, 3), round(duration, 3)))
    return ranges


def map_time(t, keeps):
    """元の時刻が、削ったあとの何秒目になるか"""
    out = 0.0
    for a, b in keeps:
        if t < a:
            return round(out, 6)
        if t <= b:
            return round(out + (t - a), 6)
        out += b - a
    return round(out, 6)


def _audio_ranges(keeps, t0, t1):
    """削ったあとの [t0, t1] が、元の音声のどこにあたるか([(開始, 長さ), ...])"""
    out, acc = [], 0.0
    for a, b in keeps:
        length = b - a
        s, e = max(t0, acc), min(t1, acc + length)
        if e - s > 1e-6:
            out.append((round(a + (s - acc), 6), round(e - s, 6)))
        acc += length
    return out


def with_trims(episode):
    """古い作りの「この本は書き出さない」を、削る範囲として読み替える"""
    drop = episode.get('drop')
    if not drop:
        return episode
    duration = episode['duration']
    points = [0.0, *sorted({round(float(t), 3) for t in (episode.get('cuts') or []) if 0 < float(t) < duration}), duration]
    ranges = list(zip(points, points[1:]))
    trims = [dict(t) for t in (episode.get('trims') or [])]
    for k in drop:
        if 0 <= int(k) < len(ranges):
            a, b = ranges[int(k)]
            trims.append({'start': a, 'end': b})
    return {**episode, 'trims': trims, 'drop': []}


def plan_outputs(episode):
    """トリムと分割を当てはめて、書き出す動画の一覧を返す。

    それぞれが 0 秒から始まる独立した話になり、
    'audio_parts' に「元の音声のどこを使うか」が入る。
    """
    episode = with_trims(episode)
    duration = episode['duration']
    keeps = keep_ranges(duration, episode.get('trims'))
    if not keeps:
        return []
    # 削ったあとの、切れ目のない時間軸を作る
    segments, out_t = [], 0.0
    for a, b in keeps:
        for s in episode['segments']:
            x, y = max(s['start'], a), min(s['end'], b)
            if y - x <= 1e-6:
                continue
            segments.append({**s, 'start': round(out_t + (x - a), 6), 'end': round(out_t + (y - a), 6),
                             'source': round(s['source'] + (x - s['start']), 6) if s['video'] else s['source']})
        out_t += b - a
    total = round(out_t, 6)

    trims = normalize_trims(episode.get('trims'), duration)
    # 削る範囲の中にある分けめは、もう意味がないので外す
    cuts = sorted({map_time(t, keeps) for t in (episode.get('cuts') or [])
                   if not any(x['start'] <= t < x['end'] for x in trims)})
    points = [0.0, *[t for t in cuts if 0.2 < t < total - 0.2], total]
    outs = []
    for t0, t1 in zip(points, points[1:]):
        if t1 - t0 < 0.2:
            continue
        part = []
        for s in segments:
            x, y = max(s['start'], t0), min(s['end'], t1)
            if y - x <= 1e-6:
                continue
            part.append({**s, 'start': round(x - t0, 6), 'end': round(y - t0, 6),
                         'source': round(s['source'] + (x - s['start']), 6) if s['video'] else s['source']})
        if not part:
            continue
        part[0]['start'] = 0.0
        part[-1]['end'] = round(t1 - t0, 6)
        outs.append({**episode, 'duration': round(t1 - t0, 6), 'segments': part,
                     'audio_parts': _audio_ranges(keeps, t0, t1)})
    return outs


def encode_threads():
    """書き出しに使う CPU の数。

    ここを 2 に絞っていたため、多コアの PC でも 2 コアしか働いていなかった。
    画質はスレッド数で変わらないので、少しだけ余裕を残して全部使う。
    """
    return max(2, (os.cpu_count() or 4) - 4)


def frame_counts(durations, fps=FPS):
    """まとまりごとのコマ数。

    1 つずつ切り捨てると端数が積もって音とずれるので、通しの時刻から決める。
    合計は必ず round(全体の長さ × fps) になる。
    """
    counts, cursor = [], 0.0
    for d in durations:
        counts.append(round((cursor + d) * fps) - round(cursor * fps))
        cursor += d
    return counts


WIDTH, HEIGHT = 1280, 720


def ending_part(segments, seconds, still=''):
    """エンディング曲を流すあいだの映像(話の後ろに足す)。

    最後の区間のカメラを、その先もそのまま流す(カメラの音は使わない)。
    カメラがそれより先に止まっていれば、最後のコマで止めておく。最後が静止画なら、その静止画のまま。
    """
    last = segments[-1]
    if last['enabled'] and last['video']:
        return {'video': last['video'], 'still': '', 'source': last['source'] + (last['end'] - last['start']),
                'duration': seconds, 'ending': True}
    return {'video': '', 'still': last.get('still') or still, 'source': 0.0, 'duration': seconds, 'ending': True}


def export_episode(episode, output, still='', notify=lambda s: None, progress=lambda f: None,
                   cancelled=never, quality=None, ending=None):
    """Encode a full-length video, then mux the untouched radio timeline once.

    ending: {'path': 曲, 'seconds': 長さ} を渡すと、話の後ろに曲の長さだけ映像を足し、そこで曲を流す。
    """
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('出力先が既に存在します。別の名前を選んでください。')
    ffmpeg = binary('ffmpeg')
    look = quality_of(quality)
    segments = episode['segments']
    cursor = 0.0
    for s in segments:
        values = [s['start'], s['end'], s['source']]
        if not all(math.isfinite(x) for x in values) or abs(s['start']-cursor) > .001 or s['end'] <= s['start'] or s['source'] < 0:
            raise ValueError('区間が不正です。開始・終了・素材位置を確認してください。')
        cursor = s['end']
    if abs(cursor-episode['duration']) > .001:
        raise ValueError('区間が音声全体を覆っていません。')
    parts = plan_parts(segments, still)
    if ending:
        parts.append(ending_part(segments, float(ending['seconds']), still))
    body = episode['duration']
    full = body + (float(ending['seconds']) if ending else 0.0)
    total = full or 1.0
    with tempfile.TemporaryDirectory(prefix='radio-sync-', dir=output.parent) as temp:
        temp = Path(temp)
        encoded = 0.0
        counts = frame_counts([p['duration'] for p in parts])
        for i, part in enumerate(parts):
            notify('エンディングの映像を作成' if part.get('ending') else f'映像作成 {i+1}/{len(parts)}')
            duration = part['duration']
            count = counts[i]
            if part['video'] and part.get('ending'):
                length, _ = probe(part['video'])
                # カメラがもう止まっていれば、最後のあたりから始めて最後のコマで止める
                source = ['-ss', min(part['source'], max(0.0, length - .5)), '-i', part['video']]
                pad = f'tpad=stop_mode=clone:stop_duration={duration:.3f},'
            elif part['video']:
                length, streams = probe(part['video'])
                if not any(x['codec_type'] == 'video' for x in streams) or part['source']+duration > length+OVERRUN:
                    raise ValueError('動画の範囲外です: ' + part['video'])
                source = ['-ss', part['source'], '-i', part['video']]
                pad = 'tpad=stop_mode=clone:stop_duration=1,'  # audio may outlast the last frame slightly
            elif part['still']:
                source, pad = ['-loop', '1', '-i', part['still']], ''
            else:
                source, pad = ['-f', 'lavfi', '-i', f'color=c=0x17212b:s={WIDTH}x{HEIGHT}:r={FPS}'], ''
            fit = (pad + f'scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,'
                   f'pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}')
            run_progress([ffmpeg, '-v', 'error', *source, '-an', '-vf', fit,
                          '-frames:v', count, '-c:v', 'libx264', '-preset', look['preset'], '-crf', look['crf'],
                          '-pix_fmt', 'yuv420p',
                          '-threads', encode_threads(), temp / f'{i}.mp4'],
                         duration, lambda f, base=encoded, d=duration: progress(.97 * (base + f * d) / total), cancelled)
            encoded += duration
        manifest = temp / 'parts.txt'
        manifest.write_text(''.join(f"file '{i}.mp4'\n" for i in range(len(parts))), encoding='utf-8')
        intermediate = temp / 'complete.mp4'
        notify('音声と結合中')
        # 使う音声の区間(トリムで飛び飛びになることがある)
        parts_of_audio = episode.get('audio_parts') or [(float(episode.get('audio_start') or 0.0), episode['duration'])]
        extra = []
        if ending:
            # ラジオの後ろに曲をつなぐ。形式の違う音どうしをつなげるよう、48kHz ステレオにそろえる。
            # ラジオが話の長さより短くても曲の位置がずれないよう、話の長さまで無音で埋めてから曲を続ける
            same = 'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo'
            audio_in = ['-i', episode['audio'], '-i', ending['path']]
            chain = ''.join(f"[1:a]atrim=start={s:.4f}:duration={d:.4f},asetpts=N/SR/TB,{same}[a{i}];"
                            for i, (s, d) in enumerate(parts_of_audio))
            chain += ''.join(f'[a{i}]' for i in range(len(parts_of_audio)))
            chain += (f'concat=n={len(parts_of_audio)}:v=0:a=1,apad=whole_dur={body:.4f},atrim=duration={body:.4f}[radio];'
                      f'[2:a]{same},asetpts=N/SR/TB[song];[radio][song]concat=n=2:v=0:a=1[aout]')
            audio_map = ['-map', '[aout]']
            audio_filter = ['-filter_complex', chain]
            # 動画クリッパーが、ここは曲だと分かるように(字幕や切り抜きの対象から外す)
            extra = ['-metadata', f'comment=radio-sync ending={float(ending["seconds"]):.3f}']
        elif len(parts_of_audio) == 1:
            start = float(parts_of_audio[0][0])
            audio_in = (['-ss', f'{start:.4f}'] if start > 0 else []) + ['-i', episode['audio']]
            audio_map = ['-map', '1:a:0']
            audio_filter = []
        else:
            audio_in = ['-i', episode['audio']]
            chain = ''.join(f"[1:a]atrim=start={s:.4f}:duration={d:.4f},asetpts=N/SR/TB[a{i}];"
                            for i, (s, d) in enumerate(parts_of_audio))
            chain += ''.join(f'[a{i}]' for i in range(len(parts_of_audio)))
            chain += f'concat=n={len(parts_of_audio)}:v=0:a=1[aout]'
            audio_map = ['-map', '[aout]']
            audio_filter = ['-filter_complex', chain]
        run_progress([ffmpeg, '-v', 'error', '-f', 'concat', '-safe', '1', '-i', manifest,
                      *audio_in, *audio_filter, '-map', '0:v:0', *audio_map, '-c:v', 'copy',
                      '-c:a', 'aac', '-b:a', '192k', '-t', full, *extra, '-movflags', '+faststart', intermediate],
                     full, lambda f: progress(.97 + .03 * f), cancelled)
        # Exclusive creation prevents accidental overwrite, including races.
        with output.open('xb') as dst, intermediate.open('rb') as src:
            shutil.copyfileobj(src, dst)
    progress(1.0)
