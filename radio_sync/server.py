"""Browser UI for Radio Sync.

Normally only this PC can use it (a per-launch token blocks web sites, and
requests from other machines are refused). When the user presses
「スマホからつなぐ」, the same-Wi-Fi door opens for a while: a phone types the
6-digit PIN shown on the PC and gets the token. Media files never leave this PC.
"""
import hashlib
import ipaddress
import socket
import http.server
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import webbrowser
import numpy as np
# 埋め込み版の Python で動かすときは、自分のフォルダが見えないので足しておく
# (「はじめる.bat」が用意する共通の Python はこの形。ふつうの Python では何も変わらない)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import core

HERE = Path(__file__).resolve().parent
WEB = HERE / 'web'
# 動作確認のときだけ、別の場所を使えるようにする(ふだんは radio_sync/data)
DATA = Path(os.environ.get('RADIO_SYNC_DATA') or (HERE / 'data'))
PROXY = DATA / 'proxy'
AUTOSAVE = DATA / 'autosave.json'
INSTANCE = DATA / 'instance.json'
LOG = DATA / 'server.log'
PREFERRED_PORT = 8765
IDLE_EXIT = 180
TOKEN = secrets.token_urlsafe(24)
AUDIO_EXT = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg', '.opus', '.wma'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mts', '.m2ts', '.avi', '.mkv', '.wmv', '.mxf'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg'}
BROWSER_AUDIO_CONTAINERS = {'wav', 'mp3', 'mov', 'flac', 'ogg', 'aac'}
BROWSER_AUDIO_CODECS = {'mp3', 'aac', 'flac', 'opus', 'vorbis', 'pcm_u8', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le'}
BYTES_PER_SECOND = 400_000  # rough size of the 720p output, for the free-space check(標準画質)
TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/mp4', '.m4a': 'audio/mp4',
         '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.flac': 'audio/flac', '.ogg': 'audio/ogg',
         '.opus': 'audio/ogg', '.aac': 'audio/aac', '.png': 'image/png', '.jpg': 'image/jpeg',
         '.jpeg': 'image/jpeg', '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml'}

UPLOADS = DATA / 'uploads'
STARTED_CODE = [0.0]     # 起動したときの、プログラムの更新時刻
# 同じ Wi-Fi のスマホに開けるときの状態(ふだんは閉じている)
LAN = {'open': False, 'pin': '', 'until': 0.0, 'tries': 0}
PIN_MINUTES = 10

ALLOWED = set()          # media paths the page may stream
HOSTS = set()            # accepted Host headers (blocks DNS rebinding)
JOBS = {}
HEAVY = [None]           # the running analysis or export
PROXY_JOBS = {}
LOCK = threading.Lock()
PROXY_LOCK = threading.Lock()
LAST_SEEN = [time.time()]
SAVED = {'at': 0.0, 'by': ''}     # 最後に保存した時刻と端末(画面どうしの同期に使う)
UPLOADING = [0]          # 受け取り中のファイル数(この間は終了しない)
LOG_STREAM = [sys.stderr]


def use_bundled_ffmpeg():
    """一式フォルダに入れた ffmpeg を、このプロセスの中でだけ使えるようにする。

    PC の環境変数は変えない(「はじめる.bat」が runtime の bin に置いたものを拾うだけ)。
    自分で ffmpeg を入れている人は、そちらが優先される。
    """
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents[:2]):
        folder = base / 'runtime' / 'bin'
        if (folder / 'ffmpeg.exe').is_file() or (folder / 'ffmpeg').is_file():
            if shutil.which('ffmpeg') is None:
                os.environ['PATH'] = str(folder) + os.pathsep + os.environ.get('PATH', '')
            return


use_bundled_ffmpeg()


class UserError(Exception):
    """A problem the user can fix; the message is shown as is."""


def log(message):
    stream = LOG_STREAM[0]
    if stream:
        print(time.strftime('%Y-%m-%d %H:%M:%S'), message, file=stream, flush=True)


def code_time():
    """プログラム一式の、いちばん新しい更新時刻"""
    latest = 0.0
    for f in [HERE / 'core.py', HERE / 'server.py', *WEB.glob('*.js'), *WEB.glob('*.css'), *WEB.glob('*.html')]:
        try:
            latest = max(latest, f.stat().st_mtime)
        except OSError:
            pass
    return latest


def is_stale():
    """動かしたあとにプログラムが新しくなったか(その場合は起動し直しが必要)"""
    return code_time() > STARTED_CODE[0] + 1


def key(path):
    return os.path.normcase(os.path.abspath(path))


class Job:
    def __init__(self, kind, work):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = 'running'
        self.progress = 0.0
        self.message = ''
        self.result = None
        self.error = ''
        self.cancel = threading.Event()
        self.started = time.time()
        JOBS[self.id] = self
        threading.Thread(target=self._run, args=(work,), daemon=True).start()

    def _run(self, work):
        try:
            self.result = work(self)
            self.progress, self.state = 1.0, 'done'
        except core.Cancelled:
            self.state = 'cancelled'
        except (UserError, ValueError) as exc:
            self.error, self.state = str(exc), 'error'
        except RuntimeError as exc:
            log(traceback.format_exc())
            self.error, self.state = 'FFmpegの処理に失敗しました。\n' + str(exc)[-800:], 'error'
        except Exception as exc:
            log(traceback.format_exc())
            self.error, self.state = f'予期しないエラーです: {exc}', 'error'

    def report(self, fraction):
        self.progress = max(self.progress, min(1.0, float(fraction)))

    def note(self, message):
        self.message = message

    def view(self):
        elapsed = time.time() - self.started
        eta = None
        if self.state == 'running' and self.progress > .02 and elapsed > 3:
            eta = elapsed * (1 - self.progress) / self.progress
        return {'id': self.id, 'kind': self.kind, 'state': self.state, 'progress': self.progress,
                'message': self.message, 'error': self.error, 'eta': eta,
                'result': self.result if self.state == 'done' else None}


def start_heavy(kind, work):
    with LOCK:
        current = HEAVY[0]
        if current and current.state == 'running':
            raise UserError('別の処理(照合または書き出し)が実行中です。終わるか中止してから、もう一度お試しください。')
        HEAVY[0] = Job(kind, work)
        return HEAVY[0]


class Dialogs:
    """Native file dialogs, all run on one Tk thread."""

    def __init__(self):
        self.native = None
        try:
            import tkinter  # noqa: F401
        except ImportError:
            # 「はじめる」で入れる Python(埋め込み版)には tkinter が無い。Windows の標準の画面を使う
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import native_dialog
            self.native = native_dialog
            self.lock = threading.Lock()
            return
        self.requests = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        import tkinter as tk
        from tkinter import filedialog
        root = None
        while True:
            function, reply = self.requests.get()
            try:
                if root is None:
                    # An invisible, topmost owner keeps the dialog in front of the browser window.
                    root = tk.Tk()
                    root.title('Radio Sync')
                    root.protocol('WM_DELETE_WINDOW', lambda: None)
                    root.attributes('-alpha', 0.0)
                    root.attributes('-toolwindow', True)
                    root.attributes('-topmost', True)
                    root.geometry('1x1+200+200')
                root.deiconify()
                root.lift()
                root.focus_force()
                root.update()
                reply.put((True, function(root, filedialog)))
            except Exception as exc:
                reply.put((False, UserError(f'ファイル選択の画面を開けませんでした: {exc}')))
            try:
                root.withdraw()
                root.update()
            except Exception:
                root = None  # the owner window was destroyed; make a new one next time

    def ask(self, function):
        if self.native:
            with self.lock:                 # 画面は1つずつ出す
                try:
                    return function(None, self.native)
                except UserError:
                    raise
                except Exception as exc:
                    raise UserError(f'ファイル選択の画面を開けませんでした: {exc}') from exc
        reply = queue.Queue()
        self.requests.put((function, reply))
        ok, value = reply.get()
        if not ok:
            raise value
        return value


DIALOGS = []


def pick(kind):
    audio = ('音声ファイル', ' '.join('*' + e for e in sorted(AUDIO_EXT)))
    video = ('動画ファイル', ' '.join('*' + e for e in sorted(VIDEO_EXT)))
    everything = ('すべてのファイル', '*.*')

    def ask(root, dialog):
        if kind == 'audio':
            result = dialog.askopenfilenames(parent=root, title='ラジオ音声を選ぶ(複数選択できます)', filetypes=[audio, everything])
        elif kind == 'video':
            result = dialog.askopenfilenames(parent=root, title='カメラ動画を選ぶ(複数選択できます)', filetypes=[video, everything])
        elif kind == 'still':
            result = dialog.askopenfilename(parent=root, title='映像がない所に表示する画像', filetypes=[('画像', '*.png *.jpg *.jpeg')])
        elif kind == 'open':
            result = dialog.askopenfilename(parent=root, title='プロジェクトを開く', filetypes=[('プロジェクト', '*.json')])
        elif kind == 'save':
            result = dialog.asksaveasfilename(parent=root, title='プロジェクトを保存', defaultextension='.json',
                                              initialfile='radio_sync_project.json', filetypes=[('プロジェクト', '*.json')])
        elif kind == 'folder':
            result = dialog.askdirectory(parent=root, title='素材の入ったフォルダを選ぶ')
        elif kind == 'outdir':
            result = dialog.askdirectory(parent=root, title='書き出し先のフォルダを選ぶ')
        else:
            raise UserError('不明な選択です。')
        if isinstance(result, str):
            result = [result] if result else []
        return [os.path.normpath(x) for x in result]

    with LOCK:
        if not DIALOGS:
            DIALOGS.append(Dialogs())
    return {'paths': DIALOGS[0].ask(ask)}


def proxy_path(path, kind):
    stat = Path(path).stat()
    digest = hashlib.sha1(f'{key(path)}|{stat.st_size}|{stat.st_mtime_ns}'.encode()).hexdigest()[:20]
    return PROXY / (digest + ('.mp4' if kind == 'video' else '.m4a'))


def inspect(path):
    p = Path(path)
    info = {'path': path, 'name': p.name, 'kind': 'missing', 'duration': 0.0, 'playable': False,
            'problem': '', 'proxy_ready': False, 'proxy_path': ''}
    if not p.is_file():
        info['problem'] = 'ファイルが見つかりません。移動・削除・名前変更されていないか確認してください。'
        return info
    ALLOWED.add(key(path))
    if p.suffix.lower() in IMAGE_EXT:
        return {**info, 'kind': 'image'}
    try:
        data = json.loads(core.run([core.binary('ffprobe'), '-v', 'error', '-show_format',
                                    '-show_streams', '-of', 'json', path]))
        fmt, streams = data.get('format', {}), data.get('streams', [])
        duration = float(fmt.get('duration') or 0)
    except Exception:
        return {**info, 'kind': 'broken', 'problem': '読み込めませんでした。対応していない形式か、ファイルが壊れている可能性があります。'}
    videos = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    containers = set(fmt.get('format_name', '').split(','))
    acodec = audios[0].get('codec_name') if audios else None
    kind = 'video' if videos else 'audio' if audios else 'broken'
    info.update(kind=kind, duration=duration)
    if kind == 'broken':
        info['problem'] = '音声も映像も入っていません。'
    elif not audios:
        info['problem'] = '音声が入っていないため、ラジオ音声と照合できません。'
    elif duration <= 0:
        info['problem'] = '長さを読み取れませんでした。'
    if kind == 'video':
        v = videos[0]
        info['playable'] = (bool(containers & {'mp4', 'mov'}) and v.get('codec_name') == 'h264'
                            and v.get('pix_fmt') in ('yuv420p', 'yuvj420p') and acodec in (None, 'aac', 'mp3'))
        # The page plays HEVC directly when the browser supports it, and falls back to a proxy.
        info['hevc'] = bool(containers & {'mp4', 'mov'}) and v.get('codec_name') == 'hevc'
    elif kind == 'audio':
        info['playable'] = bool(containers & BROWSER_AUDIO_CONTAINERS) and acodec in BROWSER_AUDIO_CODECS
    if kind in ('video', 'audio'):
        proxy = proxy_path(path, kind)
        info.update(proxy_path=str(proxy), proxy_ready=proxy.is_file())
    return info


def start_proxy(path, force=False):
    """プレビュー用の軽量版を用意する。

    force=True のときは、そのまま再生できる形式でも軽量版を作る
    (スマホには、元の大きな動画は重すぎて送れないため)。
    """
    info = inspect(path)
    if info['kind'] not in ('video', 'audio'):
        raise UserError(info['problem'] or 'プレビューできないファイルです。')
    if info['proxy_ready'] or (info['playable'] and not force):
        return {'ready': True, 'proxy_path': info['proxy_path'] if info['proxy_ready'] else ''}
    with LOCK:
        job = PROXY_JOBS.get(info['proxy_path'])
        if not job or job.state in ('error', 'cancelled'):
            job = PROXY_JOBS[info['proxy_path']] = Job('proxy', lambda j: make_proxy(j, info))
    return {'ready': False, 'job': job.id, 'proxy_path': info['proxy_path']}


def make_proxy(job, info):
    job.note('順番待ち')
    with PROXY_LOCK:
        PROXY.mkdir(parents=True, exist_ok=True)
        target = Path(info['proxy_path'])
        part = target.with_name(target.stem + '.part' + target.suffix)
        if info['kind'] == 'video':
            codec = ['-map', '0:v:0', '-map', '0:a:0?', '-vf', 'fps=30,scale=-2:360,format=yuv420p', '-c:v', 'libx264',
                     '-preset', 'veryfast', '-crf', '30', '-g', '30', '-c:a', 'aac', '-b:a', '96k']
        else:
            codec = ['-map', '0:a:0', '-vn', '-c:a', 'aac', '-b:a', '128k']
        job.note('プレビュー用の軽量版を作成中')
        try:
            decode = ['-hwaccel', 'auto'] if info['kind'] == 'video' else []  # GPU decoding when available (4K HEVC)
            core.run_progress([core.binary('ffmpeg'), '-y', '-v', 'error', *decode, '-i', info['path'], *codec,
                               '-movflags', '+faststart', part], info['duration'], job.report, job.cancel.is_set)
            os.replace(part, target)
        finally:
            if part.exists():
                part.unlink()
    return {'proxy_path': str(target)}


def scan(body):
    base = Path(body.get('folder') or '')
    if not base.is_dir():
        raise UserError('フォルダが見つかりません。')
    audios, videos, skipped = [], [], []
    for folder, subfolders, files in os.walk(base):
        subfolders.sort()
        if Path(folder) != base:
            subfolders[:] = []  # the folder itself and one level below
        for name in sorted(files):
            if Path(name).suffix.lower() not in AUDIO_EXT | VIDEO_EXT:
                continue
            info = inspect(str(Path(folder) / name))
            {'video': videos, 'audio': audios}.get(info['kind'], skipped).append(info)
            if len(audios) + len(videos) > 300:
                raise UserError('ファイルが多すぎます(300本超)。素材だけが入ったフォルダを選んでください。')
    return {'audios': audios, 'videos': videos, 'skipped': [i['name'] for i in skipped]}


def analyze(body):
    audios, videos = list(body.get('audios') or []), list(body.get('videos') or [])
    if not audios or not videos:
        raise UserError('ラジオ音声とカメラ動画をそれぞれ1本以上追加してください。')
    infos = {p: inspect(p) for p in audios + videos}
    problems = [f"・{i['name']}: {i['problem']}" for i in infos.values() if i['problem']]
    if problems:
        raise UserError('次のファイルを確認してください。\n' + '\n'.join(problems))
    durations = [max(infos[a]['duration'], 1.0) for a in audios]
    previous = {e['audio']: e for e in body.get('previous') or [] if isinstance(e, dict) and 'audio' in e}

    def work(job):
        project = core.analyze(audios, videos, job.note, job.report, job.cancel.is_set, durations)
        project['episodes'] = [core.keep_locked(e, previous[e['audio']]) if e['audio'] in previous else e
                               for e in project['episodes']]
        return project

    return {'job': start_heavy('analyze', work).id}


def envelope(body):
    start, duration = max(0.0, float(body['start'])), min(float(body['duration']), 120.0)
    samples = core.decode(body['path'], start, duration)
    hop = core.RATE // 100
    n = len(samples) // hop
    values = []
    if n:
        env = np.sqrt((samples[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
        scale = float(np.percentile(env, 98)) or 1.0
        values = np.round(np.clip(env / scale, 0, 1), 3).tolist()
    return {'t0': start, 'rate': 100, 'values': values}


OVERVIEW = {}        # 音声パス -> 話全体の音の大きさ(粗い波形)


def overview(body):
    """話全体の音の大きさを粗く返す(カット画面のタイムライン用)"""
    path = str(body.get('path') or '')
    if not Path(path).is_file():
        raise UserError('音声ファイルが見つかりません。')
    points = max(200, min(int(body.get('points') or 900), 2000))
    key = (os.path.normcase(path), Path(path).stat().st_mtime_ns, points)
    if key not in OVERVIEW:
        samples = core.decode(path)
        duration = len(samples) / core.RATE
        hop = max(1, len(samples) // points)
        n = len(samples) // hop
        values = []
        if n:
            env = np.sqrt((samples[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
            scale = float(np.percentile(env, 98)) or 1.0
            values = np.round(np.clip(env / scale, 0, 1), 3).tolist()
        OVERVIEW.clear() if len(OVERVIEW) > 8 else None
        OVERVIEW[key] = {'duration': duration, 'values': values}
    return OVERVIEW[key]


def frame(body):
    """Save the camera picture at a moment as a still image candidate (JPEG under data/stills)."""
    path, time_at = body.get('path') or '', max(0.0, float(body.get('time') or 0))
    if not Path(path).is_file():
        raise UserError('動画が見つかりません。')
    stills = DATA / 'stills'
    stills.mkdir(parents=True, exist_ok=True)
    target = stills / f"{Path(path).stem}_{int(time_at * 1000):09d}.jpg"
    if not target.is_file():
        # HDR (HLG / PQ) iPhone video is tone mapped so the picture does not look washed out
        color = json.loads(core.run([core.binary('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                     'stream=color_transfer', '-of', 'json', path])).get('streams', [{}])
        hdr = (color[0] if color else {}).get('color_transfer') in ('arib-std-b67', 'smpte2084')
        tonemap = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,'
                   'zscale=t=bt709:m=bt709:r=tv,') if hdr else ''
        core.run([core.binary('ffmpeg'), '-v', 'error', '-ss', f'{time_at:.3f}', '-i', path, '-frames:v', '1',
                  '-vf', tonemap + "scale='min(1920,iw)':-2,format=yuvj420p", '-q:v', '2', target])
    ALLOWED.add(key(target))
    return {'path': str(target)}


def rematch(body):
    start, end = float(body['start']), float(body['end'])
    if end - start < 1:
        raise UserError('1秒未満の区間は自動で探せません。')
    radius = min(float(body.get('radius', 20)), 300.0)
    window = max(0.0, float(body['center']) - radius)
    reference = core.decode(body['video'], window, 2 * radius + (end - start))
    query = core.decode(body['audio'], start, end - start)
    return {'candidates': [{'video': body['video'], 'source': round(window + t, 4), 'score': score}
                           for t, score in core.peaks(reference, query)]}


def check_project(project):
    if not isinstance(project, dict) or project.get('version') != 1 or not isinstance(project.get('episodes'), list):
        raise UserError('プロジェクトの内容が正しくありません。')
    return project


def save_project(body):
    path = body.get('path') or ''
    if not path.lower().endswith('.json'):
        raise UserError('保存先は .json ファイルにしてください。')
    core.save(check_project(body.get('project')), path)
    return {'ok': True}


def load_project(body):
    try:
        return {'project': core.load(body['path']), 'path': body['path']}
    except (OSError, ValueError) as exc:
        raise UserError('プロジェクトを開けませんでした。壊れているか、別の種類のファイルです。\n' + str(exc)) from exc


def autosave(body):
    DATA.mkdir(exist_ok=True)
    now = time.time()
    core.save({'saved_at': now, 'path': body.get('path'), 'project': check_project(body.get('project'))}, AUTOSAVE)
    SAVED.update(at=now, by=str(body.get('client') or ''))      # どの端末が保存したか
    return {'ok': True, 'saved_at': now}


def state():
    saved = None
    if AUTOSAVE.is_file():
        try:
            saved = json.loads(AUTOSAVE.read_text(encoding='utf-8'))
        except ValueError:
            saved = None
    return {'autosave': saved, 'jobs': [j.view() for j in JOBS.values() if j.state == 'running' and j.kind != 'proxy']}


def clear_autosave(body):
    AUTOSAVE.unlink(missing_ok=True)
    SAVED.update(at=time.time(), by=str(body.get('client') or ''))
    return {'ok': True}


def cut_suggest(body):
    """話ごとの「カメラが替わる所」。カットの初期位置に使う"""
    project = check_project(body.get('project'))
    return {'suggestions': [core.camera_changes(e['segments']) for e in project['episodes']]}


CGNAT = ipaddress.ip_network('100.64.0.0/10')    # 回線側 NAT の中(モバイル回線などで使われる)


def same_network(name):
    """同じ LAN のアドレスとして扱ってよいか(外に出ているアドレスは断る)"""
    try:
        ip = ipaddress.ip_address(str(name).strip('[]'))
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    return ip.is_private or (ip.version == 4 and ip in CGNAT)


def lan_addresses():
    """同じ Wi-Fi から見える、この PC のアドレス"""
    found = []
    try:                                     # 外に出るときに使うアドレス(通信はしない)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('8.8.8.8', 80))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [name for i, name in enumerate(found) if same_network(name) and name not in found[:i]]


def lan_state(port):
    left = max(0, LAN['until'] - time.time()) if LAN['open'] else 0
    return {'open': bool(LAN['open']), 'pin': LAN['pin'] if left else '', 'seconds': int(left),
            'urls': [f'http://{ip}:{port}/' for ip in lan_addresses()]}


def lan_toggle(body):
    """「スマホからつなぐ」の開け閉め。開くと 6 桁の合言葉を出す。"""
    port = int(body.get('port') or PREFERRED_PORT)
    if body.get('open'):
        LAN.update(open=True, pin=f'{secrets.randbelow(10 ** 6):06d}',
                   until=time.time() + PIN_MINUTES * 60, tries=0)
        log('スマホからつなげるようにしました(合言葉は ' + LAN['pin'] + ')')
    else:
        LAN.update(open=False, pin='', until=0.0, tries=0)
        log('スマホからの接続を閉じました')
    return lan_state(port)


def pair(body):
    """スマホが合言葉を送ってきたら、この起動用の鍵を渡す"""
    if not LAN['open'] or time.time() > LAN['until']:
        raise UserError('合言葉の有効期限が切れています。PC の画面で「スマホからつなぐ」を押し直してください。')
    LAN['tries'] += 1
    if LAN['tries'] > 10:
        LAN.update(open=False, pin='', until=0.0)
        raise UserError('合言葉を何度も間違えたため、いったん閉じました。PC の画面で開き直してください。')
    if not secrets.compare_digest(str(body.get('pin') or ''), LAN['pin']):
        raise UserError(f"合言葉が違います(あと {11 - LAN['tries']} 回)。")
    LAN['tries'] = 0
    return {'token': TOKEN}


def export_plan(body):
    project = check_project(body.get('project'))
    folder = Path(body.get('folder') or '')
    if not folder.is_dir():
        raise UserError('書き出し先のフォルダが見つかりません。')
    items, used = [], set()
    for i, e in enumerate(project['episodes']):
        stem = Path(e['audio']).stem
        parts = core.plan_outputs(e)
        for k, part in enumerate(parts, 1):
            base = f'{i+1:02d}_{stem}' + (f'_{k}' if len(parts) > 1 else '')
            name, n = f'{base}.mp4', 1
            while name.lower() in used or (folder / name).exists():
                n += 1
                name = f'{base} ({n}).mp4'
            used.add(name.lower())
            items.append({'index': i, 'part': k - 1, 'parts': len(parts), 'name': name,
                          'duration': part['duration'], 'renamed': n > 1})
    durations = [x['duration'] for x in items] or [0]
    per_second = core.quality_of(body.get('quality'))['bytes']
    referenced = {e['audio'] for e in project['episodes']}
    referenced |= {s['video'] for e in project['episodes'] for s in e['segments'] if s['enabled'] and s['video']}
    referenced |= {s['still'] for e in project['episodes'] for s in e['segments'] if not s['enabled'] and s.get('still')}
    if project.get('still'):
        referenced.add(project['still'])
    return {'items': items, 'stale': is_stale(), 'free': shutil.disk_usage(folder).free,
            'need': int((sum(durations) + 1.2 * max(durations)) * per_second),
            'sizes': [int(x['duration'] * per_second) for x in items],
            'missing': sorted(p for p in referenced if not Path(p).is_file())}


def export(body):
    project = check_project(body.get('project'))
    folder = Path(body.get('folder') or '')
    if not folder.is_dir():
        raise UserError('書き出し先のフォルダが見つかりません。')
    still = project.get('still') or ''
    if still and not Path(still).is_file():
        raise UserError('映像がない所の画像が見つかりません。設定し直すか、外してください。')
    quality = body.get('quality') or project.get('quality')
    parts_of = {}
    jobs = []
    for item in body.get('items') or []:
        name = str(item['name'])
        if Path(name).name != name or not name.lower().endswith('.mp4'):
            raise UserError('出力ファイル名が正しくありません。')
        index = int(item['index'])
        if index not in parts_of:
            parts_of[index] = core.plan_outputs(project['episodes'][index])
        parts = parts_of[index]
        part = int(item.get('part') or 0)
        if part >= len(parts):
            raise UserError('分ける位置が変わっています。「4 カット」を開き直してください。')
        jobs.append((parts[part], name))
    total = sum(e['duration'] for e, _ in jobs) or 1.0

    def work(job):
        done, files = 0.0, []
        for n, (episode, name) in enumerate(jobs, 1):
            job.note(f'{n}/{len(jobs)} 個目を書き出し中: {name}')
            core.export_episode(episode, folder / name, still, progress=lambda f, base=done, d=episode['duration']:
                                job.report((base + f * d) / total), cancelled=job.cancel.is_set, quality=quality)
            files.append(name)
            record_export(folder / name, episode)
            done += episode['duration']
        return {'folder': str(folder), 'files': files}

    return {'job': start_heavy('export', work).id}


def record_export(path, episode):
    """Remember finished files (newest first) so the production hub can hand them to the next tool."""
    log = DATA / 'exports.json'
    try:
        entries = json.loads(log.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        entries = []
    entries = [e for e in entries if e.get('path') != str(path)]
    entries.insert(0, {'path': str(path), 'name': Path(path).name, 'audio': episode['audio'],
                       'duration': episode['duration'], 'at': time.time()})
    DATA.mkdir(exist_ok=True)
    core.save(entries[:50], log)


def cancel(body):
    job = JOBS.get(body.get('job'))
    if job:
        job.cancel.set()
    return {'ok': True}


def open_folder(body):
    path = body.get('path') or ''
    if os.name == 'nt' and os.path.isdir(path):
        os.startfile(path)
    return {'ok': True}


POST = {
    '/api/ping': lambda b: {'ok': True, 'stale': is_stale(), 'saved': dict(SAVED)},
    '/api/pick': lambda b: pick(b.get('kind')),
    '/api/inspect': lambda b: {'items': [inspect(p) for p in b.get('paths') or []]},
    '/api/scan': scan,
    '/api/analyze': analyze,
    '/api/proxy': lambda b: start_proxy(b.get('path') or '', bool(b.get('force'))),
    '/api/envelope': envelope,
    '/api/overview': overview,
    '/api/rematch': rematch,
    '/api/frame': frame,
    '/api/save': save_project,
    '/api/load': load_project,
    '/api/autosave': autosave,
    '/api/autosave/clear': clear_autosave,
    '/api/cut/suggest': cut_suggest,
    '/api/export/plan': export_plan,
    '/api/export': export,
    '/api/cancel': cancel,
    '/api/open-folder': open_folder,
    '/api/lan': lan_toggle,
}
GET = {'/api/state': state, '/api/hello': lambda: {'app': 'radio-sync'}}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'RadioSync/0.2'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def from_here(self):
        return self.client_address[0] in ('127.0.0.1', '::1', '::ffff:127.0.0.1')

    def host_ok(self):
        """Host ヘッダの確かめ(外のサイトに名前を書き換えられても入れないように)"""
        host = self.headers.get('Host', '')
        if host in HOSTS:
            return True
        if not LAN['open']:
            return False
        name, _, port = host.rpartition(':')
        if port != str(self.server.server_address[1]) or not name:
            return False
        return same_network(name)             # 同じ Wi-Fi のアドレス直打ちだけを通す

    def closed_page(self):
        """閉じているときに、ほかの端末から来た人への案内"""
        body = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Radio Sync</title><style>body{font-family:system-ui,sans-serif;margin:0;'
                'display:grid;place-items:center;min-height:100vh;background:#f4f5f7;color:#1d2330}'
                'div{max-width:340px;padding:24px;text-align:center;line-height:1.8}'
                'b{font-size:17px}p{color:#667085;font-size:14px}</style></head><body><div>'
                '<b>いまは、この PC からしか使えません</b>'
                '<p>PC の画面で「📱 スマホ」を押して、出てきた QR を読み取るか、'
                '合言葉を入れてください。</p>'
                '<p>すでに押してある場合は、この画面を再読み込みしてください。</p>'
                '</div></body></html>').encode('utf-8')
        self.send_response(403)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, method):
        try:
            if not self.from_here() and not LAN['open']:
                if method == 'GET' and not self.path.startswith('/api/'):
                    return self.closed_page()
                return self.send_json(403, {'error': 'forbidden'})
            if not self.host_ok():
                return self.send_json(403, {'error': 'forbidden'})
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == '/api/pair' and method == 'POST':      # 合言葉は鍵なしで受ける
                return self.send_json(200, pair(self.read_json()))
            if url.path == '/api/bye' and secrets.compare_digest(query.get('t', [''])[0], TOKEN):
                LAST_SEEN[0] = time.time() - IDLE_EXIT + 15  # exit soon unless the page comes back (reload)
                return self.send_json(200, {'ok': True})
            if url.path == '/media' and method == 'GET':
                return self.send_media(query)
            if url.path.startswith('/api/'):
                if not secrets.compare_digest(self.headers.get('X-Token', ''), TOKEN):
                    return self.send_json(403, {'error': '画面が古くなっています。start_windows.bat から起動し直してください。'})
                LAST_SEEN[0] = time.time()
                if url.path == '/api/upload' and method == 'POST':
                    return self.send_json(200, self.receive_upload(query))
                if method == 'GET' and url.path.startswith('/api/jobs/'):
                    job = JOBS.get(url.path.rsplit('/', 1)[-1])
                    return self.send_json(200, job.view()) if job else self.send_json(404, {'error': '処理が見つかりません。'})
                routes = GET if method == 'GET' else POST
                if url.path not in routes:
                    return self.send_json(404, {'error': 'not found'})
                result = routes[url.path]() if method == 'GET' else routes[url.path](self.read_json())
                return self.send_json(200, result)
            if method == 'GET':
                return self.send_static(url.path)
            self.send_json(404, {'error': 'not found'})
        except UserError as exc:
            self.send_json(400, {'error': str(exc)})
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as exc:
            log(traceback.format_exc())
            try:
                self.send_json(500, {'error': f'予期しないエラーです: {exc}'})
            except OSError:
                pass

    def receive_upload(self, query):
        """スマホなどから送られてきた素材を、そのままファイルに書き出す"""
        sent = urllib.parse.unquote(query.get('name', [''])[0]).replace('\\', '/')
        name = os.path.basename(sent)
        suffix = Path(name).suffix.lower()
        if not name or len(name) > 120 or suffix not in (AUDIO_EXT | VIDEO_EXT | IMAGE_EXT):
            raise UserError('この種類のファイルは受け取れません。')
        size = int(self.headers.get('Content-Length') or 0)
        if size <= 0:
            raise UserError('ファイルの中身がありません。')
        UPLOADS.mkdir(parents=True, exist_ok=True)
        folder = UPLOADS / time.strftime('%Y%m%d')
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free < size + 2 * 2**30:
            raise UserError('空き容量が足りません。')
        target, n = folder / name, 1
        while target.exists():
            n += 1
            target = folder / f'{Path(name).stem} ({n}){suffix}'
        temp = target.with_name(target.name + '.part')
        UPLOADING[0] += 1
        try:
            with temp.open('wb') as f:
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                    LAST_SEEN[0] = time.time()
            if left > 0:
                temp.unlink(missing_ok=True)
                raise UserError('送信が途中で切れました。もう一度お試しください。')
            temp.replace(target)
        finally:
            UPLOADING[0] -= 1
        log(f'受け取りました: {target}({size / 2**20:.0f} MB)')
        return {'item': inspect(str(target))}

    def read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 64 * 2**20:
            raise UserError('データが大きすぎます。')
        data = json.loads(self.rfile.read(length) or b'{}')
        if not isinstance(data, dict):
            raise UserError('リクエストが正しくありません。')
        return data

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path):
        name = 'index.html' if path in ('', '/') else urllib.parse.unquote(path).lstrip('/')
        file = (WEB / name).resolve()
        if WEB.resolve() not in file.parents or not file.is_file():
            return self.send_json(404, {'error': 'not found'})
        body = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', TYPES.get(file.suffix.lower(), 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def send_media(self, query):
        path = query.get('p', [''])[0]
        k = key(path) if path else ''
        if (not secrets.compare_digest(query.get('t', [''])[0], TOKEN)
                or not (k in ALLOWED or k.startswith(key(PROXY) + os.sep)) or not os.path.isfile(path)):
            return self.send_json(404, {'error': 'not found'})
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        header = self.headers.get('Range', '')
        if header.startswith('bytes='):
            first, _, last = header[6:].split(',')[0].strip().partition('-')
            try:
                if first:
                    start, end = int(first), min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header('Content-Type', TYPES.get(Path(path).suffix.lower(), 'application/octet-stream'))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with open(path, 'rb') as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows, reuse would let two servers share one port
    daemon_threads = True

    def server_bind(self):
        # 0.0.0.0 で受けるので、127.0.0.1 だけで動いている別の起動と重ならないようにする
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def running_instance():
    try:
        info = json.loads(INSTANCE.read_text(encoding='utf-8'))
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/hello", headers={'X-Token': info['token']})
        with urllib.request.urlopen(request, timeout=2) as response:
            if json.load(response).get('app') == 'radio-sync':
                return f"http://127.0.0.1:{info['port']}/#t={info['token']}"
    except Exception:
        pass
    return None


def open_ui(url):
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
            edge = Path(base or '') / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            if base and edge.is_file():
                subprocess.Popen([str(edge), f'--app={url}', '--window-size=1440,920'], creationflags=core.NO_WINDOW)
                return
    webbrowser.open(url)


def watchdog(server):
    while True:
        time.sleep(5)
        heavy = (HEAVY[0] and HEAVY[0].state == 'running') or UPLOADING[0] > 0
        if not heavy and time.time() - LAST_SEEN[0] > IDLE_EXIT:
            log('画面が閉じられたため終了します')
            server.shutdown()
            return


def main(argv):
    show = '--no-browser' not in argv
    DATA.mkdir(exist_ok=True)
    if LOG.is_file() and LOG.stat().st_size > 5 * 2**20:
        LOG.unlink()
    LOG_STREAM[0] = open(LOG, 'a', encoding='utf-8')
    STARTED_CODE[0] = code_time()
    try:
        SAVED['at'] = float(json.loads(AUTOSAVE.read_text(encoding='utf-8')).get('saved_at') or 0)
    except (OSError, ValueError):
        pass
    sys.stdout = sys.stdout or LOG_STREAM[0]
    sys.stderr = sys.stderr or LOG_STREAM[0]
    url = running_instance()
    if url:
        if show:
            open_ui(url)
        return
    server = None
    for port in (PREFERRED_PORT, 0):
        try:
            server = Server(('0.0.0.0', port), Handler)
            break
        except OSError:
            continue
    port = server.server_address[1]
    HOSTS.update({f'127.0.0.1:{port}', f'localhost:{port}'})
    core.save({'port': port, 'token': TOKEN, 'pid': os.getpid()}, INSTANCE)
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    log(f'起動しました http://127.0.0.1:{port}/')
    if show:
        open_ui(f'http://127.0.0.1:{port}/#t={TOKEN}')
    try:
        server.serve_forever()
    finally:
        for job in list(JOBS.values()):
            job.cancel.set()
        deadline = time.time() + 5
        while time.time() < deadline and any(j.state == 'running' for j in JOBS.values()):
            time.sleep(.1)
        try:
            if json.loads(INSTANCE.read_text(encoding='utf-8')).get('token') == TOKEN:
                INSTANCE.unlink()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    main(sys.argv[1:])
