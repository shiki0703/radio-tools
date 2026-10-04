# -*- coding: utf-8 -*-
"""手元の3つのツールを、配るかたちに集めなおす(開発用。配布物には入らない)。

作業中のもの(data / results / .venv / 設定)は入れない。
プログラムのファイルだけを入れるので、上書きしても今までの作業は消えない。
"""
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 手元のツールの置き場所は人によって違うので、別ファイルに書く(このファイルは配らない)
CONFIG = Path(__file__).with_name('collect_sources.json')
SAMPLE = {
    'radio_sync': 'C:\\ここに\\radio_sync のパス',
    'video-clipper-v7': 'C:\\ここに\\video-clipper-v7 のパス',
    '制作ハブ': 'C:\\ここに\\制作ハブ のパス',
}
SKIP_DIRS = {'.venv', 'data', 'results', '__pycache__', '.git', '.claude', 'node_modules',
             '.pytest_cache', 'manual', 'manual-mac', 'tools', 'tests', 'uploads', 'proxy'}
SKIP_SUFFIX = {'.pyc', '.pyo', '.log', '.part', '.tmp'}
SKIP_NAMES = {'instance.json', 'hub_instance.json', 'autosave.json', 'exports.json',
              'hub_config.json', 'settings.json', 'CLAUDE.md', 'settings.local.json',
              '.gitignore', '.gitattributes', 'result.json'}
DEV_ONLY = {'collect.py', 'collect_sources.json'}   # 開発用なので配らない


def load_sources() -> dict:
    if not CONFIG.is_file():
        CONFIG.write_text(json.dumps(SAMPLE, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'{CONFIG.name} を作りました。中のパスを書き直してから、もう一度実行してください。')
        sys.exit(1)
    return {k: Path(v) for k, v in json.loads(CONFIG.read_text(encoding='utf-8')).items()}


def is_program(rel: Path, path: Path) -> bool:
    """配るプログラムのファイルか(作業データ・環境・一時ファイルではないか)"""
    if any(part in SKIP_DIRS for part in rel.parts) or path.is_dir():
        return False
    return path.suffix.lower() not in SKIP_SUFFIX and path.name not in SKIP_NAMES


def copy_program(src: Path, dst: Path) -> int:
    """プログラムのファイルだけを上書きする。

    このフォルダでツールを実際に使っていることもあるので、フォルダごと消して入れ直すことはしない。
    作業データの場所(data / results など)には一切触れない。
    """
    wanted = set()
    for path in src.rglob('*'):
        rel = path.relative_to(src)
        if not is_program(rel, path):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        wanted.add(rel)
    # 元のほうから無くなったプログラムのファイルだけを消す
    if dst.is_dir():
        for path in list(dst.rglob('*')):
            rel = path.relative_to(dst)
            if is_program(rel, path) and rel not in wanted:
                path.unlink()
    return len(wanted)


def tidy_powershell(folder: Path) -> int:
    """PowerShell 5.1 は BOM なしの UTF-8 を古い文字コードとして読むので日本語が化ける。
    改行も CRLF でないと、行の続き(`)がうまく効かない。配る前に整えておく。"""
    n = 0
    for path in folder.glob('*.ps1'):
        text = path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').replace('\n', '\r\n')
        path.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8'))
        n += 1
    return n


def make_zip(root: Path, out: Path) -> int:
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in root.rglob('*'):
            rel = path.relative_to(root)
            if not path.is_file() or rel.parts[0] in {'runtime', '.git'} or rel.name in DEV_ONLY:
                continue
            if not is_program(rel, path):      # このフォルダで使ったときの作業データは入れない
                continue
            z.write(path, Path(root.name) / rel)
            n += 1
    return n


if __name__ == '__main__':
    total = 0
    for name, src in load_sources().items():
        if not src.is_dir():
            print(f'{name}: 見つかりません({src})')
            sys.exit(1)
        n = copy_program(src, ROOT / name)
        total += n
        print(f'{name}: {n} ファイル')

    print(f'PowerShell のファイルを整えました: {tidy_powershell(ROOT / "launcher")} 個')

    zip_path = ROOT.parent / 'ラジオ制作ツール.zip'
    n = make_zip(ROOT, zip_path)
    print(f'\nまとめました: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB / {n} ファイル)')
