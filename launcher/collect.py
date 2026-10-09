# -*- coding: utf-8 -*-
"""配布用の ZIP を作る(開発用。配布物には入らない)。

ツールのプログラムは、このリポジトリにあるものを正とする。直すときはここを直接直して、
コミット・push する(push すると、利用者の次回起動時に更新が案内される)。

このスクリプトは、このフォルダの中身から ZIP を作るだけ。
このフォルダでツールを使っていても、作業データ(data / results / runtime など)は ZIP に入れない。
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'.venv', 'data', 'results', '__pycache__', '.git', '.claude', 'node_modules',
             '.pytest_cache', 'manual', 'manual-mac', 'tools', 'tests', 'uploads', 'proxy', 'runtime'}
SKIP_SUFFIX = {'.pyc', '.pyo', '.log', '.part', '.tmp'}
SKIP_NAMES = {'instance.json', 'hub_instance.json', 'autosave.json', 'exports.json', 'hub_config.json',
              'settings.json', 'common_settings.json', 'secrets.json', 'CLAUDE.md', 'settings.local.json', '.gitignore', '.gitattributes',
              'result.json', 'collect.py', 'collect_sources.json'}


def is_program(rel: Path, path: Path) -> bool:
    """配るプログラムのファイルか(作業データ・環境・開発用・一時ファイルではないか)"""
    if path.is_dir() or any(part in SKIP_DIRS for part in rel.parts):
        return False
    return path.suffix.lower() not in SKIP_SUFFIX and path.name not in SKIP_NAMES


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
        for path in sorted(root.rglob('*')):
            rel = path.relative_to(root)
            if is_program(rel, path):
                z.write(path, Path(root.name) / rel)
                n += 1
    return n


if __name__ == '__main__':
    print(f'PowerShell のファイルを整えました: {tidy_powershell(ROOT / "launcher")} 個')
    zip_path = ROOT.parent / 'ラジオ制作ツール.zip'
    n = make_zip(ROOT, zip_path)
    print(f'まとめました: {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB / {n} ファイル)')
