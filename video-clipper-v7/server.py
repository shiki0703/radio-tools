"""動画クリッパー v7: 操作画面のサーバー(Radio Sync と同じ作り)

処理の流れ:
  前処理 →(選択時)文字起こし → テロップ焼き込み →(選択時)盛り上がり検出 → 切り抜き
  完成後: テロップの文字・見た目を直して、焼き込み(と切り抜き)だけを作り直せる

- 127.0.0.1 だけで待ち受け、起動ごとの合言葉(トークン)が必要。このPCの中だけで使う
- 画面は Microsoft Edge の専用ウィンドウで開き、閉じて約3分たつと自動で終了する(処理中は終了しない)
- 動画はアップロードせず、PC内のファイルを元の場所から読む(元ファイルは変更しない)
- 処理ごとに results/日時/ を作り、project.json に内容を保存する(後から開いて修正を続けられる)

起動: start_windows.bat(初回は setup_windows.bat)
別のツールから動画を渡す: pythonw server.py --open "動画のパス"
"""
import ctypes
import hashlib
import http.server
import ipaddress
import socket
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
import webbrowser
from datetime import datetime

HERE = Path(__file__).resolve().parent
os.chdir(HERE)  # src/ は相対パスの作業フォルダを前提にしている
# 埋め込み版の Python で動かすときは、自分のフォルダが見えないので足しておく
# (「はじめる.bat」が用意する共通の Python はこの形。ふつうの Python では何も変わらない)
sys.path.insert(0, str(HERE))

from src.preprocess import prepare_video, extract_audio, probe, CANCEL_EVENT, ProcessCancelled  # noqa: E402
from src.subtitle import write_srt, burn_subtitles, normalize_style, FONT_MAP, FONT_LABELS, DEFAULT_STYLE  # noqa: E402
from src.highlight import detect_highlights  # noqa: E402
from src.title import make_topic_titles  # noqa: E402
from src.bookend import attach as attach_bookend  # noqa: E402
from src.clip import cut_clips  # noqa: E402
from src import chapters as chap  # noqa: E402
from src.logo import clean_logo, DEFAULT_LOGO, IMAGE_EXT  # noqa: E402

WEB = HERE / 'web'
DATA = HERE / 'data'
RESULTS = HERE / 'results'
THUMBS = DATA / 'thumbs'
SETTINGS = DATA / 'settings.json'
SECRETS = DATA / 'secrets.json'     # AI に使う API キー(この PC の中だけ。画面には返さない)
INSTANCE = DATA / 'instance.json'
LOG = DATA / 'server.log'
PREFERRED_PORT = 8766
IDLE_EXIT = 180
TOKEN = secrets.token_urlsafe(24)
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.mts', '.m2ts', '.wmv', '.webm'}
TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/mp4', '.webm': 'video/webm', '.jpg': 'image/jpeg',
         '.png': 'image/png', '.srt': 'text/plain; charset=utf-8', '.json': 'application/json; charset=utf-8',
         '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8'}
CLIP_STYLE = dict(DEFAULT_STYLE, pos_y=0.78)
DEFAULT_SETTINGS = {'do_transcribe': True, 'do_clip': True, 'accuracy': 'standard', 'hq': False, 'show_titles': False, 'title_scope': 'corner', 'title_maker': 'ai', 'intro': '', 'outro': '',
                    'logo': DEFAULT_LOGO, 'orientation': 'horizontal', 'clip_length': 30, 'style': {'main': DEFAULT_STYLE, 'clip': CLIP_STYLE}}

CGNAT = ipaddress.ip_network('100.64.0.0/10')    # 回線側 NAT の中(モバイル回線などで使われる)
# 同じ Wi-Fi のスマホに開けるときの状態(ふだんは閉じている)
LAN = {'open': False, 'pin': '', 'until': 0.0, 'tries': 0}
PIN_MINUTES = 10
UPLOADS = DATA / 'uploads'
UPLOADING = [0]          # 受け取り中のファイル数(この間は終了しない)

HOSTS = set()
ALLOWED = set()          # source videos the page may play
LAST_SEEN = [time.time()]
LAST_UI = [0.0]          # last time an open window checked in
PENDING = {'path': ''}   # a video handed over by another tool, waiting for the window to pick it up
LOCK = threading.Lock()
LOG_STREAM = [sys.stderr]

JOB = {'state': 'idle', 'kind': '', 'project': '', 'steps': [], 'step': -1, 'percent': 0, 'message': '',
       'started': 0.0, 'error': '', 'hint': '', 'detail': '',
       'wait': '', 'wait_note': '', 'paused': 0.0}   # wait='titles' … 見出しを作ってもらうのを待っている
# 見出しを待っているあいだ、画面からの「続ける」を受け取る
TITLE_GO = threading.Event()
TITLE_REPLY = {}
WEIGHTS = []

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
    if LOG_STREAM[0]:
        print(time.strftime('%Y-%m-%d %H:%M:%S'), message, file=LOG_STREAM[0], flush=True)


def key(path):
    return os.path.normcase(os.path.abspath(path))


def save_json(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 書き込む人ごとに別の一時ファイルにする(画面と処理が同時に保存しても混ざらないように)
    temp = path.with_name(f'{path.name}.{threading.get_ident()}.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(40):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            # Windows では、ほかの処理が読んでいる最中のファイルは置き換えられない
            # (画面が1秒ごとに結果の一覧を読んでいる)。少し待ってやり直す
            if attempt == 39:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


# ---------- settings & projects ----------

def load_settings():
    try:
        saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    merged = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    style = saved.get('style') or {}
    merged['style'] = {'main': normalize_style(style.get('main')), 'clip': normalize_style(style.get('clip'), base=CLIP_STYLE)}
    merged['logo'] = allow_logo(clean_logo(merged.get('logo')))
    return merged


def clean_settings(raw):
    raw = raw if isinstance(raw, dict) else {}
    try:
        length = float(raw.get('clip_length', 30))
    except (TypeError, ValueError):
        length = 30.0
    style = raw.get('style') or {}
    return {
        'do_transcribe': bool(raw.get('do_transcribe', True)),
        'do_clip': bool(raw.get('do_clip', True)),
        'accuracy': raw.get('accuracy') if raw.get('accuracy') in ('standard', 'high') else 'standard',
        'hq': bool(raw.get('hq', False)),
        'show_titles': bool(raw.get('show_titles', False)),
        'title_scope': raw.get('title_scope') if raw.get('title_scope') in ('fine', 'corner', 'whole') else 'corner',
        'title_maker': raw.get('title_maker') if raw.get('title_maker') in ('ai', 'words') else 'ai',
        'intro': clean_clip_path(raw.get('intro')),
        'outro': clean_clip_path(raw.get('outro')),
        'logo': allow_logo(clean_logo(raw.get('logo'))),
        'orientation': raw.get('orientation') if raw.get('orientation') in ('horizontal', 'vertical') else 'horizontal',
        'clip_length': max(5.0, min(120.0, length)),
        'style': {'main': normalize_style(style.get('main')), 'clip': normalize_style(style.get('clip'), base=CLIP_STYLE)},
    }


def allow_logo(logo):
    """ロゴの画像を、画面で表示できるようにしておく"""
    if logo['path']:
        ALLOWED.add(key(logo['path']))
    return logo


def logo_of(project):
    """焼き込みに使うロゴ(重ねないときは None)"""
    logo = clean_logo(project['settings'].get('logo'))
    return logo if logo['on'] else None


def clean_clip_path(value):
    """前後に付ける動画のパス(無ければ空文字)"""
    path = Path(str(value or ''))
    return str(path) if str(value or '') and path.is_file() and path.suffix.lower() in VIDEO_EXT else ''


def project_dir(name):
    folder = (RESULTS / str(name)).resolve()
    if folder.parent != RESULTS.resolve() or not (folder / 'project.json').is_file():
        raise UserError('処理結果が見つかりません。results フォルダから移動・削除されていないか確認してください。')
    return folder


def load_project(name):
    return json.loads((project_dir(name) / 'project.json').read_text(encoding='utf-8'))


def save_project(project):
    save_json(project, RESULTS / project['name'] / 'project.json')


def recent_projects(limit=12):
    if not RESULTS.is_dir():
        return []
    items = []
    for folder in sorted((d for d in RESULTS.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
        try:
            p = json.loads((folder / 'project.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        items.append({'name': p['name'], 'source': p['source'], 'created': p['created'], 'status': p['status'],
                      'has_captioned': p.get('has_captioned', False), 'clips': len(p.get('clips', [])),
                      'settings': {k: p['settings'][k] for k in ('do_transcribe', 'do_clip', 'orientation')}})
        if len(items) >= limit:
            break
    return items


def mark_interrupted():
    """Projects left 'running' by a previous session that ended mid-way."""
    for item in recent_projects(limit=50):
        if item['status'] == 'running':
            p = load_project(item['name'])
            p['status'] = 'interrupted'
            save_project(p)


# ---------- media info ----------

def inspect(path):
    p = Path(str(path))
    if not p.is_file():
        raise UserError('動画ファイルが見つかりません。移動・削除されていないか確認してください。')
    if p.suffix.lower() not in VIDEO_EXT:
        raise UserError('動画ファイル(mp4 / mov など)を選んでください。')
    try:
        data = json.loads(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,width,height:format=duration,format_name',
             '-of', 'json', str(p)], capture_output=True, check=True, creationflags=NO_WINDOW).stdout)
    except FileNotFoundError as exc:
        raise UserError('動画加工ツール(ffmpeg)が見つかりません。README の手順で FFmpeg をインストールしてから、起動し直してください。') from exc
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise UserError('動画を読み込めませんでした。ファイルが壊れているか、対応していない形式の可能性があります。') from exc
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video:
        raise UserError('映像が入っていないファイルです。動画ファイルを選んでください。')
    containers = set(data.get('format', {}).get('format_name', '').split(','))
    ALLOWED.add(key(p))
    return {'path': str(p), 'name': p.name, 'folder': str(p.parent), 'size': p.stat().st_size,
            'duration': float(data.get('format', {}).get('duration') or 0), 'width': video.get('width', 0),
            'height': video.get('height', 0), 'has_audio': any(s.get('codec_type') == 'audio' for s in streams),
            'playable': bool(containers & {'mp4', 'mov'}) and video.get('codec_name') == 'h264',
            'hevc': bool(containers & {'mp4', 'mov'}) and video.get('codec_name') == 'hevc'}


def thumbnail(body):
    """A frame from the video, used as the background of the caption preview."""
    info = inspect(body.get('path'))
    stat = Path(info['path']).stat()
    digest = hashlib.sha1(f"{key(info['path'])}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:16]
    target = THUMBS / f'{digest}.jpg'
    if not target.is_file():
        THUMBS.mkdir(parents=True, exist_ok=True)
        at = max(0.0, info['duration'] * 0.3)
        color = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=color_transfer',
                                '-of', 'csv=p=0', info['path']], capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip()
        tonemap = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,'
                   if color in ('arib-std-b67', 'smpte2084') else '')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{at:.2f}', '-i', info['path'], '-frames:v', '1',
                        '-vf', tonemap + 'scale=960:-2,format=yuvj420p', '-q:v', '4', str(target)],
                       capture_output=True, creationflags=NO_WINDOW)
    if not target.is_file():
        return {'url': ''}
    return {'url': media_url(target)}


def media_url(path):
    return f'/media?t={urllib.parse.quote(TOKEN)}&p={urllib.parse.quote(str(path))}'


# ---------- processing ----------

def friendly_error(e):
    """技術的なエラーを、ユーザー向けの説明とヒントに変換する"""
    detail = f'{type(e).__name__}: {e}'
    low = str(e).lower()
    if isinstance(e, FileNotFoundError) and ('ffmpeg' in low or 'ffprobe' in low):
        return ('動画加工ツール(ffmpeg)が見つかりません',
                'README の手順で FFmpeg をインストールし、動画クリッパーを起動し直してください。', detail)
    if 'no space left' in low or 'errno 28' in low:
        return ('PCの空き容量が足りません', '不要なファイルを削除して空き容量を10GB以上確保してから、もう一度お試しください。', detail)
    if 'dll load failed' in low or '制御ポリシー' in str(e):
        return ('Windowsのセキュリティ機能がファイルをブロックしました',
                'README の「困ったとき」にある『アプリケーション制御ポリシー』の項目を確認してください。', detail)
    if isinstance(e, MemoryError):
        return ('メモリが不足しました', '他のアプリを閉じるか、より短い動画でお試しください。', detail)
    if 'moov atom not found' in low or 'invalid data' in low:
        return ('動画ファイルを読み込めませんでした',
                'ファイルが壊れているか、コピーの途中の可能性があります。iPhoneの動画はUSBでPCにコピーしてからお試しください。', detail)
    return ('処理中にエラーが発生しました', 'もう一度お試しください。解決しない場合は、下の詳細情報を配布者に伝えてください。', detail)


def begin(kind, project, steps, weights):
    total = sum(weights)
    WEIGHTS[:] = [w * 100 / total for w in weights]
    JOB.update(state='running', kind=kind, project=project['name'], steps=steps, step=-1, percent=0,
               message='準備しています…', started=time.time(), error='', hint='', detail='',
               wait='', wait_note='', paused=0.0)
    CANCEL_EVENT.clear()


def progress(step, fraction, message=None):
    index = JOB['steps'].index(step)
    JOB['step'] = index
    JOB['percent'] = round(sum(WEIGHTS[:index]) + WEIGHTS[index] * max(0.0, min(fraction, 1.0)))
    if message:
        JOB['message'] = message


def finish(project, state, error=None):
    """Record how the job ended, in the job status and in the project file."""
    if state == 'done':
        JOB.update(state='done', step=len(JOB['steps']), percent=100, message='完了しました')
        project['status'] = 'done'
    elif state == 'cancelled':
        log('処理を中止しました(ユーザー操作)')
        JOB.update(state='cancelled', message='処理を中止しました', step=-1, percent=0)
        project['status'] = 'done' if project.get('has_captioned') or project.get('clips') else 'cancelled'
    else:
        log(traceback.format_exc())
        title, hint, detail = friendly_error(error)
        JOB.update(state='error', message=title, hint=hint, detail=detail)
        project['status'] = 'done' if project.get('has_captioned') or project.get('clips') else 'error'
        project['error'] = title
    save_project(project)


def start_process(body):
    info = inspect(body.get('path'))
    if not info['has_audio']:
        raise UserError('音声が入っていない動画です。文字起こしや盛り上がり検出には音声が必要です。')
    settings = clean_settings(body.get('settings'))
    if not (settings['do_transcribe'] or settings['do_clip']):
        raise UserError('処理内容を1つ以上選んでください。')
    with LOCK:
        if JOB['state'] == 'running':
            raise UserError('別の処理が実行中です。終わるか中止してから、もう一度お試しください。')
        save_json(settings, SETTINGS)
        name = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        while (RESULTS / name).exists():
            time.sleep(1)
            name = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        (RESULTS / name).mkdir(parents=True)
        project = {'version': 1, 'name': name, 'created': time.time(), 'source': info['path'], 'duration': info['duration'],
                   'settings': settings, 'style': settings['style'], 'status': 'running', 'work_video': '',
                   'segments': [], 'titles': [], 'draft': None, 'highlights': [], 'clips': [],
                   'has_captioned': False}
        save_project(project)
        steps, weights = ['前処理'], [15]
        if settings['do_transcribe']:
            steps += ['文字起こし', 'テロップ焼き込み']
            weights += [35, 30]
        if settings['do_clip']:
            steps += ['盛り上がり検出', '切り抜き作成']
            weights += [5, 15]
        begin('process', project, steps, weights)
    threading.Thread(target=run_process, args=(project,), daemon=True).start()
    return {'ok': True, 'project': name}


def run_process(project):
    out_dir = RESULTS / project['name']
    settings = project['settings']
    wav = None
    try:
        progress('前処理', 0, '動画を確認しています')
        work_video, duration = prepare_video(project['source'], str(out_dir),
                                             on_message=lambda m: JOB.update(message=m),
                                             on_progress=lambda f: progress('前処理', f * 0.9), hq=settings['hq'])
        project.update(work_video=str(work_video), duration=duration)
        save_project(project)
        wav = extract_audio(work_video, str(out_dir))
        progress('前処理', 1.0)
        segments = None
        if settings['do_transcribe']:
            from src.transcribe import transcribe  # loads the speech model library only when needed
            progress('文字起こし', 0, '音声を文字起こししています(初回はAIモデルの取得で数分かかります)')
            segments = transcribe(wav, accuracy=settings['accuracy'],
                                  on_progress=lambda f: progress('文字起こし', f, f'音声を文字起こししています({round(f * 100)}%)'))
            project['segments'] = segments
            if settings['show_titles']:
                project['titles'] = topic_titles(project, segments)
            save_project(project)
            burn(project, segments, 'テロップ焼き込み', '字幕を動画に焼き込んでいます')
        if settings['do_clip']:
            progress('盛り上がり検出', 0, '盛り上がり箇所を分析しています')
            project['highlights'] = detect_highlights(wav, segments, str(out_dir), clip_length=settings['clip_length'], n_clips=5)
            progress('盛り上がり検出', 1.0)
            make_clips(project, segments, '切り抜き動画を書き出しています')
        finish(project, 'done')
    except ProcessCancelled:
        finish(project, 'cancelled')
    except Exception as exc:
        finish(project, 'error', exc)
    finally:
        if wav:
            Path(wav).unlink(missing_ok=True)


def topic_titles(project, segments):
    """左上に焼き込む見出しを作る。

    「AI で作る」のときは、概要欄のチャプターを先に作り、その短い見出しを使う(概要欄も同時にできる)。
    ふだんは、文字起こしが終わったところで一度止まり、画面で概要欄を作って
    (依頼文を ChatGPT などに貼る)もらってから続ける。API キーがあれば、止まらずに AI に頼む。
    焼き込みを1回で済ませるため(あとから見出しを直すと、作り直しが要る)。
    """
    settings = project['settings']
    if settings.get('title_maker', 'ai') != 'ai':
        project['titles_by'] = 'words'
        return make_topic_titles(segments, scope=settings.get('title_scope', 'corner'))
    note = ''
    if api_key():
        progress('文字起こし', 1.0, 'AI で話題の見出しを作っています(1分ほど)')
        try:
            body = body_seconds(project)
            data = chap.normalize(chap.ask_claude(segments, api_key()), segments, body)
            titles = chap.overlay_titles(data['chapters'], body)
            if titles:
                chapters_store(project, keep_bookend_names(project, data), 'ai')
                log(f"見出しを AI で作りました: {project['name']}({len(titles)}個)")
                project.update(titles_by='ai', title_note='')
                return titles
            note = 'AI の返事に使える見出しがありませんでした。'
        except chap.ChapterError as exc:
            log(f"AI で見出しを作れませんでした: {exc}")
            note = f'AI で見出しを作れませんでした。{exc}'
        except Exception:
            log(traceback.format_exc())
            note = 'AI で見出しを作れませんでした。'
    return wait_for_titles(project, segments, note)


def wait_for_titles(project, segments, note):
    """画面で見出し(概要欄)が作られるのを待つ。中止されたら止める"""
    TITLE_GO.clear()
    TITLE_REPLY.clear()
    save_project(project)                 # 文字起こしは保存しておく(画面の概要欄がこれを読む)
    JOB.update(wait='titles', wait_note=note, message='見出しを作ってください(下の手順)')
    since = time.time()
    try:
        while not TITLE_GO.wait(0.5):
            if CANCEL_EVENT.is_set():
                raise ProcessCancelled()
    finally:
        JOB.update(wait='', wait_note='', paused=JOB.get('paused', 0.0) + time.time() - since)
    reply = dict(TITLE_REPLY)
    if reply.get('mode') == 'chapters':
        titles = chap.overlay_titles(reply['chapters'], body_seconds(project))
        if titles:
            project.update(titles_by=reply.get('source') or 'edit', title_note='')
            return titles
    project.update(titles_by='words', title_note='')
    return make_topic_titles(segments, scope=project['settings'].get('title_scope', 'corner'))


def titles_continue(body):
    """「この見出しで続ける」「言葉を拾う方式で続ける」"""
    if JOB['wait'] != 'titles' or JOB['project'] != body.get('project'):
        raise UserError('いまは見出しを待っていません。画面を開き直してください。')
    if body.get('mode') == 'chapters':
        chapters_save(body)
        saved = load_chapters(load_project(body.get('project'))) or {}
        rows = [c for c in saved.get('chapters', []) if c['title'].strip()]
        if not rows:
            raise UserError('見出しがありません。作ってから続けるか、「言葉を拾う方式で続ける」を選んでください。')
        TITLE_REPLY.update(mode='chapters', chapters=rows, source=saved.get('source', 'edit'))
    else:
        TITLE_REPLY.update(mode='words')
    JOB['message'] = '焼き込みを続けています'
    TITLE_GO.set()
    return {'ok': True}


def write_titles_srt(project):
    """話題の見出しをSRTに書き出す(設定が入で、見出しがあるときだけ)"""
    titles = project.get('titles') or []
    if not project['settings'].get('show_titles') or not titles:
        return None
    path = RESULTS / project['name'] / 'titles.srt'
    write_srt(titles, str(path))
    return str(path)


def burn(project, segments, step, message):
    out_dir = RESULTS / project['name']
    srt = out_dir / 'subtitles.srt'
    write_srt(segments, str(srt))
    progress(step, 0, message)
    burn_subtitles(project['work_video'], str(srt), str(out_dir / 'captioned.mp4'), duration=project['duration'],
                   style=project['style']['main'], hq=project['settings']['hq'],
                   titles_srt=write_titles_srt(project), logo=logo_of(project),
                   on_progress=lambda f: progress(step, f, f'{message}({round(f * 100)}%)'))
    settings = project['settings']
    intro, outro = clean_clip_path(settings.get('intro')), clean_clip_path(settings.get('outro'))
    if intro or outro:
        progress(step, .98, 'オープニング・エンディングをつないでいます')
        attach_bookend(str(out_dir / 'captioned.mp4'), str(out_dir / 'captioned.mp4'),
                       intro=intro, outro=outro, hq=settings['hq'])
    project['intro_seconds'] = clip_seconds(intro)
    project['outro_seconds'] = clip_seconds(outro)
    project['has_captioned'] = True
    save_project(project)


def make_clips(project, segments, message):
    out_dir = RESULTS / project['name']
    captioned = out_dir / 'captioned.mp4'
    progress('切り抜き作成', 0, message)
    cut_clips(str(captioned) if captioned.is_file() else project['work_video'], project['highlights'], str(out_dir),
              orientation=project['settings']['orientation'], work_video=project['work_video'],
              segments=segments if project['settings']['do_transcribe'] else None,
              style=project['style']['clip'], hq=project['settings']['hq'],
              titles=(project.get('titles') or []) if project['settings'].get('show_titles') else None,
              logo=logo_of(project),
              on_progress=lambda f: progress('切り抜き作成', f, f'{message}({round(f * 100)}%)'))
    project['clips'] = [f'clip_{i}.mp4' for i in range(1, len(project['highlights']) + 1)]
    save_project(project)


def clean_segments(raw, fallback):
    """画面から届いた字幕(分割・挿入・時刻のずらしを含む)を検証して並べ直す。

    文字が空の行は出さない。時刻がおかしい行は捨て、開始順に並べ、
    隣同士が重ならないように前の行の終わりを詰める。
    """
    out = []
    for s in raw if isinstance(raw, list) else []:
        text = str(s.get('text', '')).strip()
        if not text:
            continue
        try:
            start, end = max(0.0, float(s['start'])), float(s['end'])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({'start': round(start, 2), 'end': round(max(end, start + 0.2), 2), 'text': text})
    out.sort(key=lambda s: s['start'])
    for a, b in zip(out, out[1:]):
        if a['end'] > b['start']:
            a['end'] = round(max(b['start'] - 0.05, a['start'] + 0.2), 2)
    return out or [s for s in fallback if str(s.get('text', '')).strip()]


def start_reburn(body):
    project = load_project(body.get('project'))
    if not project.get('has_captioned'):
        raise UserError('テロップを修正できる完成動画がありません。')
    if not project.get('work_video') or not Path(project['work_video']).is_file():
        raise UserError('元の動画が見つからないため修正できません。元の動画が移動・削除されていないか確認してください。')
    segments = clean_segments(body.get('segments'), project['segments'])
    targets = body.get('targets') if isinstance(body.get('targets'), list) else ['main', 'clip']
    do_main = 'main' in targets
    do_clip = 'clip' in targets and bool(project.get('clips'))
    if not (do_main or do_clip):
        raise UserError('作り直す対象を1つ以上選んでください。')
    style = body.get('style') or {}
    with LOCK:
        if JOB['state'] == 'running':
            raise UserError('別の処理が実行中です。終わるか中止してから、もう一度お試しください。')
        project['segments'] = segments
        titles = body.get('titles')
        if isinstance(titles, list):
            project['titles'] = clean_segments(titles, project.get('titles') or [])
        if isinstance(body.get('show_titles'), bool):
            project['settings'] = {**project['settings'], 'show_titles': body['show_titles']}
        for key_name in ('intro', 'outro'):
            if isinstance(body.get(key_name), str):
                project['settings'] = {**project['settings'], key_name: clean_clip_path(body[key_name])}
        if isinstance(body.get('logo'), dict):
            project['settings'] = {**project['settings'], 'logo': clean_logo(body['logo'])}
        if body.get('title_scope') in ('fine', 'corner', 'whole'):
            # 見出しの細かさを変えたときは作り直す(手で直した見出しは上書きされる)
            project['settings'] = {**project['settings'], 'title_scope': body['title_scope']}
            project['titles'] = make_topic_titles(segments, scope=body['title_scope'])
        project['draft'] = None
        project['style'] = {'main': normalize_style(style.get('main'), base=project['style']['main']),
                            'clip': normalize_style(style.get('clip'), base=project['style']['clip'])}
        project['status'] = 'running'
        save_project(project)
        steps = (['テロップ焼き込み'] if do_main else []) + (['切り抜き作成'] if do_clip else [])
        begin('reburn', project, steps, ([70] if do_main else []) + ([30] if do_clip else []))
    threading.Thread(target=run_reburn, args=(project, segments, do_main, do_clip), daemon=True).start()
    return {'ok': True}


def run_reburn(project, segments, do_main, do_clip):
    try:
        if do_main:
            burn(project, segments, 'テロップ焼き込み', '修正した字幕を動画に焼き込んでいます')
        if do_clip:
            make_clips(project, segments, '切り抜き動画を作り直しています')
        finish(project, 'done')
    except ProcessCancelled:
        finish(project, 'cancelled')
    except Exception as exc:
        finish(project, 'error', exc)


def cancel(body):
    if JOB['state'] != 'running':
        raise UserError('実行中の処理がありません。')
    CANCEL_EVENT.set()
    JOB['message'] = '中止しています…'
    return {'ok': True}


# ---------- 概要欄のチャプター ----------
#
# 見出しは本編の時刻で chapters.json に持つ(オープニング・エンディングの名前も一緒に)。
# 画面に出すとき・概要欄の文を作るときに、その時点でつないであるオープニングの長さを足す。
# あとからオープニングを付けたり替えたりしても、時刻がずれないようにするため。

def clip_seconds(path):
    """動画の長さ(秒)。無い・読めないときは 0"""
    if not path:
        return 0.0
    try:
        return float(probe(path)['duration'])
    except Exception:
        return 0.0


def bookend_seconds(project, kind):
    """前後につないだ(つなぐ予定の)オープニング・エンディングの長さ(秒)。kind は 'intro' / 'outro'"""
    if isinstance(project.get(f'{kind}_seconds'), (int, float)):
        return float(project[f'{kind}_seconds'])
    # 長さを覚える前に作った結果や、まだ焼き込む前(見出し待ち)は、つなぐ動画からその場で測る
    return clip_seconds(clean_clip_path(project['settings'].get(kind)))


def intro_seconds(project):
    return bookend_seconds(project, 'intro')


def api_key():
    try:
        saved = json.loads(SECRETS.read_text(encoding='utf-8')).get('anthropic_api_key', '')
    except (OSError, ValueError, AttributeError):
        saved = ''
    return saved or os.environ.get('ANTHROPIC_API_KEY', '')


def set_api_key(body):
    value = str(body.get('key') or '').strip()
    if value and (not value.startswith('sk-ant-') or len(value) < 30 or any(c.isspace() for c in value)):
        raise UserError('API キーの形が違うようです。「sk-ant-」で始まる文字列を、そのまま貼り付けてください。')
    if value:
        save_json({'anthropic_api_key': value}, SECRETS)
    else:
        SECRETS.unlink(missing_ok=True)
    return {'has_key': bool(api_key())}


def chapters_path(project):
    return RESULTS / project['name'] / 'chapters.json'


def chapter_segments(project):
    if not project.get('segments'):
        raise UserError('文字起こしがないため作れません。「文字起こし + テロップ」を入にして処理した結果で使えます。')
    return project['segments']


def body_seconds(project):
    segments = project.get('segments') or []
    return float(project.get('duration') or (segments[-1]['end'] if segments else 0))


def load_chapters(project):
    """保存してある案(本編の時刻)。無ければ None"""
    try:
        saved = json.loads(chapters_path(project).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if saved.get('timebase') != 'body':
        # 以前の形(できあがった動画の時刻)で保存したもの → 本編の時刻に直す
        rows = [{**c, 'kind': 'body'} for c in saved.get('chapters', [])]
        saved = {**saved, **chap.from_timeline(rows, intro_seconds(project), body_seconds(project),
                                               bookend_seconds(project, 'outro'))}
    return saved


def chapter_view(project, data, source):
    intro, outro, body = intro_seconds(project), bookend_seconds(project, 'outro'), body_seconds(project)
    rows = chap.timeline(data, intro, body, outro)
    return {'topics': data['topics'], 'chapters': rows, 'source': source,
            'problems': chap.problems(rows), 'text': chap.description(data['topics'], rows),
            'offset': intro, 'outro': outro, 'has_key': bool(api_key())}


def chapters_store(project, data, source):
    keep = {k: data[k] for k in ('topics', 'chapters', 'intro_title', 'outro_title') if k in data}
    save_json({**keep, 'source': source, 'timebase': 'body'}, chapters_path(project))
    return chapter_view(project, keep, source)


def chapters_get(body):
    """保存してある案。まだ無ければ、話題の見出しから作った下書き"""
    project = load_project(body.get('project'))
    segments = chapter_segments(project)
    saved = load_chapters(project)
    if saved is not None:
        return chapter_view(project, {**saved, 'topics': saved.get('topics', [])}, saved.get('source', 'edit'))
    return chapter_view(project, chap.draft(segments, project.get('titles'), body_seconds(project)), 'draft')


def keep_bookend_names(project, data):
    """新しく作り直した案でも、オープニング・エンディングの名前は前のものを引き継ぐ"""
    before = load_chapters(project) or {}
    for k in ('intro_title', 'outro_title'):
        if k in before:
            data[k] = before[k]
    return data


def chapters_ai(body):
    project = load_project(body.get('project'))
    segments = chapter_segments(project)
    key_value = api_key()
    if not key_value:
        raise UserError('API キーが設定されていません。「依頼文をコピー」して ChatGPT などに貼る方法で作れます。')
    try:
        raw = chap.ask_claude(segments, key_value)
    except chap.ChapterError as exc:
        raise UserError(str(exc)) from exc
    data = chap.normalize(raw, segments, body_seconds(project))
    if not data['chapters']:
        raise UserError('AI の返事に使える見出しがありませんでした。もう一度お試しください。')
    log(f"チャプターを AI で作りました: {project['name']}({len(data['chapters'])}個)")
    return chapters_store(project, keep_bookend_names(project, data), 'ai')


def chapters_prompt(body):
    """ChatGPT や claude.ai に貼る依頼文"""
    return {'text': chap.paste_prompt(chapter_segments(load_project(body.get('project'))))}


def chapters_paste(body):
    project = load_project(body.get('project'))
    segments = chapter_segments(project)
    try:
        raw = chap.parse_reply(body.get('text'))
    except chap.ChapterError as exc:
        raise UserError(str(exc)) from exc
    data = chap.normalize(raw, segments, body_seconds(project))
    if not data['chapters']:
        raise UserError('使える見出しがありませんでした。時刻が動画の長さを超えていないか確かめてください。')
    return chapters_store(project, keep_bookend_names(project, data), 'paste')


def chapters_save(body):
    """画面で直した内容を保存する(画面の時刻は、できあがった動画の時刻)"""
    project = load_project(body.get('project'))
    topics = [str(t).strip() for t in (body.get('topics') or []) if str(t).strip()][:20]
    data = chap.from_timeline(body.get('chapters') or [], intro_seconds(project), body_seconds(project),
                              bookend_seconds(project, 'outro'), load_chapters(project))
    source = body.get('source') if body.get('source') in ('ai', 'paste', 'edit', 'draft') else 'edit'
    return chapters_store(project, {**data, 'topics': topics}, source)


def chapters_reset(body):
    """保存した案を消して、下書きに戻す"""
    chapters_path(load_project(body.get('project'))).unlink(missing_ok=True)
    return chapters_get(body)


# ---------- screens ----------

def project_view(name):
    project = load_project(name)
    folder = RESULTS / project['name']
    stamp = f"&v={int(time.time())}"
    files = []
    for f in ('captioned.mp4', 'subtitles.srt', 'titles.srt', 'highlights.json', *project.get('clips', [])):
        if (folder / f).is_file():
            files.append({'name': f, 'path': str(folder / f), 'size': (folder / f).stat().st_size,
                          'url': media_url(folder / f) + stamp})
    allow_logo(clean_logo(project['settings'].get('logo')))
    return {**project, 'folder': str(folder), 'files': files, 'source_exists': Path(project['source']).is_file(),
            'lead': intro_seconds(project) if project.get('has_captioned') else 0.0,
            'can_fix': bool(project.get('work_video')) and Path(project['work_video']).is_file()}


def save_draft(body):
    project = load_project(body.get('project'))
    draft = body.get('draft')
    project['draft'] = draft if isinstance(draft, dict) else None
    save_project(project)
    return {'ok': True}


def state(body=None):
    LAST_UI[0] = time.time()
    job = dict(JOB)
    if job['state'] == 'running':
        elapsed = time.time() - job['started'] - job.get('paused', 0.0)
        job['elapsed'] = round(elapsed)
        job['eta'] = elapsed * (100 - job['percent']) / job['percent'] if job['percent'] >= 3 and elapsed > 5 else None
        if job.get('wait'):
            job['eta'] = None
    pending, PENDING['path'] = PENDING['path'], ''
    return {'job': job, 'settings': load_settings(), 'recent': recent_projects(),
            'fonts': [{'value': k, 'label': FONT_LABELS.get(k, k)} for k in FONT_MAP], 'pending': pending,
            'results': str(RESULTS), 'has_key': bool(api_key())}


# ---------- dialogs, folders ----------

class Dialogs:
    """Native file dialogs, all run on one Tk thread, kept in front of the window."""

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
                    root = tk.Tk()
                    root.title('動画クリッパー')
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
                root = None

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


def pick_video(body):
    with LOCK:
        if not DIALOGS:
            DIALOGS.append(Dialogs())
    kind = body.get('kind')
    if kind == 'logo':
        path = DIALOGS[0].ask(lambda root, d: d.askopenfilename(
            parent=root, title='重ねるロゴの画像を選ぶ(背景が透明な PNG がおすすめ)',
            filetypes=[('画像', ' '.join('*' + e for e in sorted(IMAGE_EXT)))]))
        path = os.path.normpath(path) if path else ''
        if path and Path(path).suffix.lower() not in IMAGE_EXT:
            raise UserError('PNG か JPEG の画像を選んでください。')
        if path:
            ALLOWED.add(key(path))
        return {'path': path}
    titles = {'intro': 'オープニングの動画を選ぶ', 'outro': 'エンディングの動画を選ぶ'}
    patterns = ' '.join('*' + e for e in sorted(VIDEO_EXT))
    path = DIALOGS[0].ask(lambda root, d: d.askopenfilename(
        parent=root, title=titles.get(kind, '処理する動画を選ぶ'), filetypes=[('動画ファイル', patterns), ('すべてのファイル', '*.*')]))
    if kind in titles:
        # 前後に付ける動画は、長さだけ確認して覚えておく
        path = os.path.normpath(path) if path else ''
        return {'path': path, 'info': inspect(path) if path else None}
    return {'info': inspect(os.path.normpath(path)) if path else None}


def open_place(body):
    """Open a result folder, or show a file in Explorer (only results and the chosen source videos)."""
    target = Path(str(body.get('path') or ''))
    inside_results = key(target).startswith(key(RESULTS) + os.sep) or key(target) == key(RESULTS)
    if not target.exists() or not (inside_results or key(target) in ALLOWED):
        raise UserError('開けませんでした。ファイルが移動・削除されていないか確認してください。')
    if target.is_file():
        subprocess.Popen(['explorer', '/select,', str(target)])
    else:
        os.startfile(str(target))
    return {'ok': True}


def request_open(body):
    """Another tool (the production hub) hands over a video to process."""
    info = inspect(body.get('path'))
    PENDING['path'] = info['path']
    if time.time() - LAST_UI[0] < 30:
        bring_to_front()
        return {'ok': True, 'window': 'existing'}
    open_window(f'http://127.0.0.1:{SERVER_PORT[0]}/#t={TOKEN}')
    return {'ok': True, 'window': 'new'}


def bring_to_front():
    """Raise the open 動画クリッパー window (best effort; Windows may only flash it in the taskbar)."""
    if os.name != 'nt':
        return
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buffer, 256)
        if user32.IsWindowVisible(hwnd) and buffer.value.startswith('動画クリッパー'):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    for hwnd in found[:1]:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


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
    try:
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
    return [n for i, n in enumerate(found) if same_network(n) and n not in found[:i]]


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
        log('スマホからつなげるようにしました')
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


POST = {
    '/api/lan': lan_toggle,
    '/api/ping': lambda b: {'ok': True},
    '/api/pick': pick_video,
    '/api/inspect': lambda b: {'info': inspect(b.get('path'))},
    '/api/thumbnail': thumbnail,
    '/api/process': start_process,
    '/api/reburn': start_reburn,
    '/api/cancel': cancel,
    '/api/project': lambda b: project_view(b.get('project')),
    '/api/draft': save_draft,
    '/api/open': open_place,
    '/api/request-open': request_open,
    '/api/settings': lambda b: save_json(clean_settings(b.get('settings')), SETTINGS) or {'ok': True},
    '/api/chapters': chapters_get,
    '/api/chapters/ai': chapters_ai,
    '/api/chapters/prompt': chapters_prompt,
    '/api/chapters/paste': chapters_paste,
    '/api/chapters/save': chapters_save,
    '/api/chapters/reset': chapters_reset,
    '/api/apikey': set_api_key,
    '/api/titles-continue': titles_continue,
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'VideoClipper/7'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def from_here(self):
        return self.client_address[0] in ('127.0.0.1', '::1', '::ffff:127.0.0.1')

    def host_ok(self):
        host = self.headers.get('Host', '')
        if host in HOSTS:
            return True
        if not LAN['open']:
            return False
        name, _, port = host.rpartition(':')
        return port == str(self.server.server_address[1]) and bool(name) and same_network(name)

    def closed_page(self):
        """閉じているときに、ほかの端末から来た人への案内"""
        body = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>動画クリッパー</title><style>body{font-family:system-ui,sans-serif;margin:0;'
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
                LAST_SEEN[0] = time.time() - IDLE_EXIT + 15
                LAST_UI[0] = 0.0
                return self.send_json(200, {'ok': True})
            if url.path == '/media' and method == 'GET':
                return self.send_media(query)
            if url.path.startswith('/api/'):
                if not secrets.compare_digest(self.headers.get('X-Token', ''), TOKEN):
                    return self.send_json(403, {'error': '画面が古くなっています。動画クリッパーを起動し直してください。'})
                LAST_SEEN[0] = time.time()
                if method == 'GET' and url.path == '/api/hello':
                    return self.send_json(200, {'app': 'video-clipper', 'job': JOB['state'], 'percent': JOB['percent']})
                if method == 'GET' and url.path == '/api/state':
                    return self.send_json(200, state())
                if url.path == '/api/upload' and method == 'POST':
                    return self.send_json(200, self.receive_upload(query))
                if method == 'POST' and url.path in POST:
                    return self.send_json(200, POST[url.path](self.read_json()))
                return self.send_json(404, {'error': 'not found'})
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
        """スマホなどから送られてきた動画を、そのままファイルに書き出す"""
        sent = urllib.parse.unquote(query.get('name', [''])[0]).replace('\\', '/')
        name = os.path.basename(sent)
        suffix = Path(name).suffix.lower()
        if not name or len(name) > 120 or suffix not in VIDEO_EXT:
            raise UserError('この種類のファイルは受け取れません。')
        size = int(self.headers.get('Content-Length') or 0)
        if size <= 0:
            raise UserError('ファイルの中身がありません。')
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
        return {'path': str(target), 'name': target.name, 'size': size}

    def read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 32 * 2**20:
            raise UserError('データが大きすぎます。')
        data = json.loads(self.rfile.read(length) or b'{}')
        if not isinstance(data, dict):
            raise UserError('リクエストが正しくありません。')
        return data

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
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
        allowed = k.startswith(key(RESULTS) + os.sep) or k.startswith(key(THUMBS) + os.sep) or k in ALLOWED
        if not secrets.compare_digest(query.get('t', [''])[0], TOKEN) or not allowed or not os.path.isfile(path):
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
    def server_bind(self):
        # 0.0.0.0 で受けるので、127.0.0.1 だけで動いている別の起動と重ならないようにする
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    allow_reuse_address = False  # on Windows, reuse would let two servers share one port
    daemon_threads = True


SERVER_PORT = [0]


def instance_call(path, body):
    """Talk to a copy of the clipper that is already running (returns None if there is none)."""
    try:
        info = json.loads(INSTANCE.read_text(encoding='utf-8'))
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", headers={'X-Token': info['token'], 'Content-Type': 'application/json'},
                                         data=json.dumps(body).encode('utf-8') if body is not None else None)
        with urllib.request.urlopen(request, timeout=5) as response:
            return info, json.load(response)
    except Exception:
        return None


def open_window(url):
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
            edge = Path(base or '') / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            if base and edge.is_file():
                subprocess.Popen([str(edge), f'--app={url}', '--window-size=1440,920'], creationflags=NO_WINDOW)
                return
    webbrowser.open(url)


def watchdog(server):
    while True:
        time.sleep(5)
        if JOB['state'] != 'running' and UPLOADING[0] == 0 and time.time() - LAST_SEEN[0] > IDLE_EXIT:
            log('画面が閉じられたため終了します')
            server.shutdown()
            return


def main(argv):
    show = '--no-browser' not in argv
    handoff = argv[argv.index('--open') + 1] if '--open' in argv and argv.index('--open') + 1 < len(argv) else ''
    DATA.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    if LOG.is_file() and LOG.stat().st_size > 5 * 2**20:
        LOG.unlink()
    LOG_STREAM[0] = open(LOG, 'a', encoding='utf-8')
    sys.stdout = sys.stdout or LOG_STREAM[0]
    sys.stderr = sys.stderr or LOG_STREAM[0]
    running = instance_call('/api/hello', None)
    if running:
        info, _ = running
        if handoff:
            instance_call('/api/request-open', {'path': handoff})
        elif show:
            open_window(f"http://127.0.0.1:{info['port']}/#t={info['token']}")
        return
    server = None
    for port in (PREFERRED_PORT, 0):
        try:
            server = Server(('0.0.0.0', port), Handler)
            break
        except OSError:
            continue
    SERVER_PORT[0] = port = server.server_address[1]
    HOSTS.update({f'127.0.0.1:{port}', f'localhost:{port}'})
    mark_interrupted()
    if handoff and Path(handoff).is_file():
        PENDING['path'] = str(Path(handoff))
    save_json({'port': port, 'token': TOKEN, 'pid': os.getpid()}, INSTANCE)
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    log(f'起動しました http://127.0.0.1:{port}/')
    if show or handoff:
        open_window(f'http://127.0.0.1:{port}/#t={TOKEN}')
    try:
        server.serve_forever()
    finally:
        try:
            if json.loads(INSTANCE.read_text(encoding='utf-8')).get('token') == TOKEN:
                INSTANCE.unlink()
        except (OSError, ValueError):
            pass


if __name__ == '__main__':
    main(sys.argv[1:])
