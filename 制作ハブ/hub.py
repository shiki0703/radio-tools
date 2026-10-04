"""制作ハブ: Radio Sync と 動画クリッパー を1か所から使うための入口(このPC専用)

- 2つのツールの起動と、動いているかどうか・処理の進み具合を表示する
- Radio Sync で書き出した動画を、アップロードせずに動画クリッパーへ渡す
- 動画クリッパーの完成品フォルダを開く

127.0.0.1 だけで待ち受け、起動ごとの合言葉(トークン)が必要なので、
他のPCやWebサイトからは操作できない。外部ライブラリは使わない。
"""
import http.server
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'hub.html'
CONFIG = HERE / 'hub_config.json'
INSTANCE = HERE / 'hub_instance.json'
LOG = HERE / 'hub.log'
PREFERRED_PORT = 8790
IDLE_EXIT = 180
TOKEN = secrets.token_urlsafe(24)
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.mts', '.m2ts', '.wmv'}
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'results', 'uploads', 'data', 'proxy', 'manual', 'manual-mac'}

HOSTS = set()
LAST_SEEN = [time.time()]
BUSY = [0]  # requests that must keep the hub alive (e.g. waiting for a tool to start)
LOG_STREAM = [sys.stderr]


class UserError(Exception):
    """A problem the user can act on; shown as is."""


def log(message):
    if LOG_STREAM[0]:
        print(time.strftime('%Y-%m-%d %H:%M:%S'), message, file=LOG_STREAM[0], flush=True)


# ---------- where the tools are ----------

def shared_runtime():
    """一式フォルダに入れた共通の Python(「はじめる.bat」が用意する)。無ければ None。"""
    for base in (HERE, *HERE.parents[:2]):
        exe = base / 'runtime' / 'python' / 'pythonw.exe'
        if exe.is_file():
            return exe
    return None


def tool_python(folder):
    """そのツールを動かす Python。フォルダの中の .venv があればそれを使い、
    無ければ一式の共通ランタイムを使う(どちらも無ければ None)。"""
    local = Path(folder) / '.venv' / 'Scripts' / 'pythonw.exe'
    return local if local.is_file() else shared_runtime()


def is_radio_sync(p):
    return (p / 'server.py').is_file() and (p / 'core.py').is_file() and (p / 'web' / 'app.js').is_file()


def is_clipper(p):
    """動画クリッパー v7 以降(Radio Sync と同じ作りの版)"""
    return (p / 'server.py').is_file() and (p / 'src' / 'clip.py').is_file() and (p / 'web' / 'app.js').is_file()


def find_tool(test, marker):
    """Look for a tool near the hub (the hub folder's parent, three levels deep).

    Old copies often sit next to the current one (video-clipper-v5, -v6 ...): prefer the copy whose
    main file contains marker (a feature the hub needs), then the most recently changed one.
    """
    base = HERE.parent
    found = []
    for root, dirs, _ in os.walk(base):
        depth = len(Path(root).relative_to(base).parts)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')] if depth < 3 else []
        folder = Path(root)
        if test(folder):
            main_file = folder / marker[0]
            text = main_file.read_text(encoding='utf-8', errors='replace')
            found.append((marker[1] in text, main_file.stat().st_mtime, str(folder)))
    return max(found)[2] if found else ''


def load_config():
    try:
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        config = {}
    changed = False
    for key, test, marker in (('radio_sync', is_radio_sync, ('server.py', 'record_export')),
                              ('clipper', is_clipper, ('server.py', 'request-open'))):
        if not (config.get(key) and test(Path(config[key]))):
            found = find_tool(test, marker)
            if found:
                config[key] = found
                changed = True
    if changed:
        save_json(config, CONFIG)
    return config


def save_json(data, path):
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


# ---------- status ----------

def request_json(url, data=None, headers=None, timeout=1.5):
    body = json.dumps(data).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def radio_sync_status(config):
    folder = Path(config.get('radio_sync') or '')
    if not config.get('radio_sync') or not is_radio_sync(folder):
        return {'installed': False}
    info = {'installed': True, 'dir': str(folder), 'ready': tool_python(folder) is not None,
            'running': False}
    try:
        instance = json.loads((folder / 'data' / 'instance.json').read_text(encoding='utf-8'))
        hello = request_json(f"http://127.0.0.1:{instance['port']}/api/hello", headers={'X-Token': instance['token']})
        info['running'] = hello.get('app') == 'radio-sync'
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    return info


def clipper_status(config):
    folder = Path(config.get('clipper') or '')
    if not config.get('clipper') or not is_clipper(folder):
        return {'installed': False}
    info = {'installed': True, 'dir': str(folder), 'ready': tool_python(folder) is not None,
            'running': False}
    try:
        instance = json.loads((folder / 'data' / 'instance.json').read_text(encoding='utf-8'))
        hello = request_json(f"http://127.0.0.1:{instance['port']}/api/hello", headers={'X-Token': instance['token']})
        if hello.get('app') == 'video-clipper':
            info.update(running=True, state=hello.get('job'), percent=hello.get('percent', 0))
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    return info


def recent_exports(config):
    log_file = Path(config.get('radio_sync') or '') / 'data' / 'exports.json'
    try:
        entries = json.loads(log_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    result = []
    for e in entries:
        p = Path(e.get('path', ''))
        if p.is_file():
            result.append({'path': str(p), 'name': p.name, 'folder': str(p.parent), 'duration': e.get('duration', 0),
                           'at': e.get('at', 0), 'size': p.stat().st_size})
        if len(result) >= 20:
            break
    return result


def recent_results(config):
    root = Path(config.get('clipper') or '') / 'results'
    if not root.is_dir():
        return []
    folders = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    result = []
    for d in folders:
        videos = [f.name for f in d.glob('*.mp4') if not f.name.startswith('_')]
        clips = [f for f in d.rglob('*.mp4') if f.parent != d]
        if videos or clips:
            result.append({'path': str(d), 'name': d.name, 'videos': len(videos) + len(clips)})
        if len(result) >= 6:
            break
    return result


def guides(config):
    found = {}
    for key, candidates in (('radio_sync', ('manual/web/radio-sync-guide.html', 'manual/Radio Sync ガイド.pdf')),
                            ('clipper', ('manual/web/video-clipper-guide.html', 'manual/動画クリッパー ガイド.pdf'))):
        base = Path(config.get(key) or '')
        found[key] = next((str(base / c) for c in candidates if config.get(key) and (base / c).is_file()), '')
    return found


def state():
    config = load_config()
    return {'radio_sync': radio_sync_status(config), 'clipper': clipper_status(config),
            'exports': recent_exports(config), 'results': recent_results(config), 'guides': guides(config)}


# ---------- actions ----------

def start_tool(body):
    config = load_config()
    tool = body.get('tool')
    if tool == 'radio_sync':
        info = radio_sync_status(config)
        if not info['installed']:
            raise UserError('Radio Sync のフォルダが見つかりません。下の「ツールの場所」で設定してください。')
        if not info['ready']:
            raise UserError('Radio Sync を動かす準備がまだです。一式フォルダの「はじめる.bat」を実行してください。')
        # Radio Sync opens its own window, or brings up the one already running
        subprocess.Popen([str(tool_python(info['dir'])), 'server.py'],
                         cwd=info['dir'], creationflags=NO_WINDOW)
        return {'ok': True}
    if tool == 'clipper':
        info = clipper_status(config)
        if not info['installed']:
            raise UserError('動画クリッパーのフォルダが見つかりません。下の「ツールの場所」で設定してください。')
        if not info['ready']:
            raise UserError('動画クリッパーを動かす準備がまだです。一式フォルダの「はじめる.bat」を実行してください。')
        run_clipper(info['dir'])
        return {'ok': True}
    raise UserError('不明なツールです。')


def run_clipper(folder, *args):
    """動画クリッパーは自分でウィンドウを開く(動いていれば、そのウィンドウを使う)"""
    subprocess.Popen([str(tool_python(folder)), 'server.py', *args],
                     cwd=folder, creationflags=NO_WINDOW)


def send_to_clipper(body):
    path = Path(str(body.get('path') or ''))
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
        raise UserError('動画ファイルが見つかりません。移動・削除されていないか確認してください。')
    info = clipper_status(load_config())
    if not info['installed']:
        raise UserError('動画クリッパーのフォルダが見つかりません。下の「ツールの場所」で設定してください。')
    if not info['ready']:
        raise UserError('動画クリッパーを動かす準備がまだです。一式フォルダの「はじめる.bat」を実行してください。')
    if info['running'] and info.get('state') == 'running':
        raise UserError(f"動画クリッパーは別の動画を処理中です({info.get('percent', 0)}%)。終わってから送ってください。")
    # the clipper starts if needed and shows the video in its window
    run_clipper(info['dir'], '--open', str(path))
    return {'ok': True, 'name': path.name}


class Dialogs:
    """Native file dialogs on one Tk thread, kept in front of the browser window."""

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
                    root.title('制作ハブ')
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


def ask(function):
    if not DIALOGS:
        DIALOGS.append(Dialogs())
    BUSY[0] += 1
    try:
        return DIALOGS[0].ask(function)
    finally:
        BUSY[0] -= 1


def pick_and_send(body):
    patterns = ' '.join('*' + e for e in sorted(VIDEO_EXT))
    path = ask(lambda root, d: d.askopenfilename(parent=root, title='テロップ・切り抜きに送る動画を選ぶ',
                                                 filetypes=[('動画ファイル', patterns), ('すべてのファイル', '*.*')]))
    if not path:
        return {'ok': False, 'cancelled': True}
    return send_to_clipper({'path': os.path.normpath(path)})


def set_tool_folder(body):
    tool = body.get('tool')
    test = {'radio_sync': is_radio_sync, 'clipper': is_clipper}.get(tool)
    if not test:
        raise UserError('不明なツールです。')
    label = 'Radio Sync(server.py がある)' if tool == 'radio_sync' else '動画クリッパー(app.py がある)'
    folder = ask(lambda root, d: d.askdirectory(parent=root, title=f'{label}フォルダを選ぶ'))
    if not folder:
        return {'ok': False, 'cancelled': True}
    if not test(Path(folder)):
        raise UserError(f'選んだフォルダは{label}フォルダではありません。')
    config = load_config()
    config[tool] = os.path.normpath(folder)
    save_json(config, CONFIG)
    return {'ok': True}


def open_path(body):
    """Open a folder, or show a file in Explorer. Only places the hub itself listed are allowed."""
    target = Path(str(body.get('path') or ''))
    info = state()
    allowed = {e['folder'] for e in info['exports']} | {e['path'] for e in info['exports']}
    allowed |= {r['path'] for r in info['results']} | {g for g in info['guides'].values() if g}
    allowed |= {t['dir'] for t in (info['radio_sync'], info['clipper']) if t.get('dir')}
    if str(target) not in allowed or not target.exists():
        raise UserError('開けませんでした。ファイルが移動・削除されていないか確認してください。')
    if target.is_file() and target.suffix.lower() in VIDEO_EXT:
        subprocess.Popen(['explorer', '/select,', str(target)])
    else:
        os.startfile(str(target))
    return {'ok': True}


POST = {
    '/api/ping': lambda b: {'ok': True},
    '/api/start': start_tool,
    '/api/send': send_to_clipper,
    '/api/pick-send': pick_and_send,
    '/api/tool-folder': set_tool_folder,
    '/api/open': open_path,
}


# ---------- web server ----------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'ProductionHub/1.0'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        try:
            if self.headers.get('Host', '') not in HOSTS:
                return self.send_json(403, {'error': 'forbidden'})
            url = urllib.parse.urlsplit(self.path)
            if url.path in ('/', '/index.html') and method == 'GET':
                return self.send_page()
            if url.path == '/api/bye' and secrets.compare_digest(urllib.parse.parse_qs(url.query).get('t', [''])[0], TOKEN):
                LAST_SEEN[0] = time.time() - IDLE_EXIT + 15
                return self.send_json(200, {'ok': True})
            if not url.path.startswith('/api/'):
                return self.send_json(404, {'error': 'not found'})
            if not secrets.compare_digest(self.headers.get('X-Token', ''), TOKEN):
                return self.send_json(403, {'error': 'ハブを start_hub.bat から起動し直してください。'})
            LAST_SEEN[0] = time.time()
            if method == 'GET' and url.path == '/api/state':
                return self.send_json(200, state())
            if method == 'POST' and url.path in POST:
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}') if length < 2**20 else {}
                return self.send_json(200, POST[url.path](body if isinstance(body, dict) else {}))
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

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def send_page(self):
        body = PAGE.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def open_window(url):
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
            edge = Path(base or '') / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            if base and edge.is_file():
                subprocess.Popen([str(edge), f'--app={url}', '--window-size=1180,900'], creationflags=NO_WINDOW)
                return
    webbrowser.open(url)


def running_hub():
    try:
        info = json.loads(INSTANCE.read_text(encoding='utf-8'))
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/state", headers={'X-Token': info['token']})
        with urllib.request.urlopen(req, timeout=3):
            return f"http://127.0.0.1:{info['port']}/#t={info['token']}"
    except Exception:
        return None


def watchdog(server):
    while True:
        time.sleep(5)
        if not BUSY[0] and time.time() - LAST_SEEN[0] > IDLE_EXIT:
            log('画面が閉じられたため終了します')
            server.shutdown()
            return


def main(argv):
    show = '--no-browser' not in argv
    LOG_STREAM[0] = open(LOG, 'a', encoding='utf-8')
    sys.stdout = sys.stdout or LOG_STREAM[0]
    sys.stderr = sys.stderr or LOG_STREAM[0]
    url = running_hub()
    if url:
        if show:
            open_window(url)
        return
    server = None
    for port in (PREFERRED_PORT, 0):
        try:
            server = Server(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    port = server.server_address[1]
    HOSTS.update({f'127.0.0.1:{port}', f'localhost:{port}'})
    save_json({'port': port, 'token': TOKEN, 'pid': os.getpid()}, INSTANCE)
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    log(f'起動しました http://127.0.0.1:{port}/')
    if show:
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
