"""ロゴ:動画の上に画像(番組ロゴなど)を重ねる

位置と大きさは、画面に対する割合で持つ(x, y はロゴの左上、w はロゴの幅)。
動画の解像度が違っても(画質優先モードで元の 4K のままでも)、同じ見た目になる。
字幕・見出しはロゴより上に描く(重なったときに文字が隠れないように)。
"""
from pathlib import Path

IMAGE_EXT = {'.png', '.jpg', '.jpeg'}
DEFAULT_LOGO = {'on': False, 'path': '', 'x': 0.835, 'y': 0.04, 'w': 0.14}


def clean_logo(raw) -> dict:
    """画面や設定ファイルから来たロゴの設定を確かめる(画像が無ければ、重ねない)"""
    raw = raw if isinstance(raw, dict) else {}
    path = str(raw.get('path') or '')
    if not (path and Path(path).is_file() and Path(path).suffix.lower() in IMAGE_EXT):
        path = ''
    out = {'on': bool(raw.get('on')) and bool(path), 'path': path}
    for k in ('x', 'y', 'w'):
        try:
            value = float(raw.get(k, DEFAULT_LOGO[k]))
        except (TypeError, ValueError):
            value = DEFAULT_LOGO[k]
        if value != value or value in (float('inf'), float('-inf')):
            value = DEFAULT_LOGO[k]
        # 大きさは画面の幅の 2〜100%。位置は、少しなら画面の外にはみ出してもよい
        out[k] = round(min(1.0, max(0.02, value)) if k == 'w' else min(1.0, max(-0.5, value)), 4)
    return out


def logo_box(logo: dict, width: int, height: int) -> tuple:
    """ロゴの (幅, 左, 上) を、その動画のピクセルで返す(幅は偶数にそろえる)"""
    w = max(2, round(logo['w'] * width / 2) * 2)
    return w, round(logo['x'] * width), round(logo['y'] * height)


def overlay_graph(logo: dict, width: int, height: int, after=(), logo_input: str = '1:v') -> str:
    """-filter_complex 用:元の映像([0:v])にロゴを重ね、続けて after のフィルタ(字幕など)をかけて [v] にする"""
    w, x, y = logo_box(logo, width, height)
    chain = f'[0:v][logo]overlay={x}:{y}' + ''.join(',' + f for f in after)
    return f'[{logo_input}]scale={w}:-2,format=rgba[logo];{chain}[v]'
