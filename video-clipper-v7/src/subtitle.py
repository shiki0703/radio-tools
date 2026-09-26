"""① テロップ:SRT字幕ファイルの生成と、ffmpegによる動画への焼き込み

テロップの見た目は「スタイル辞書」で管理する(元動画用と切り抜き用で独立)。
  font          … FONT_MAP のキー
  size          … 文字サイズ(基準キャンバス高さ288に対する値。出力解像度へ自動スケール)
  pos_x / pos_y … 表示位置(0.0〜1.0 の画面内割合)
  color / outline_color … 文字色・縁取り色(#RRGGBB)
  outline / shadow      … 縁取りの太さ・影の強さ
  bold / box            … 太字・半透明の背景ボックス
これらを ffmpeg subtitles フィルタの force_style(ASS)に変換して反映する。
"""
import json
import re
import sys
from pathlib import Path

# 画面の選択値 → 実際のフォント名(Windows に標準搭載されているもの)
FONT_MAP = {
    "gothic": "Meiryo",           # 標準(メイリオ)
    "yu": "Yu Gothic UI",         # すっきり(游ゴシック)
    "mincho": "Yu Mincho",        # 上品(明朝)
    "msgothic": "MS Gothic",      # レトロ(MSゴシック)
}
# 画面に表示するフォント名(焼き込みで実際に使えるものだけを載せる)
FONT_LABELS = {
    "gothic": "標準(メイリオ)",
    "yu": "すっきり(游ゴシック)",
    "mincho": "上品(明朝)",
    "msgothic": "レトロ(MSゴシック)",
}
# 旧UI(3段階)との互換用
SIZE_MAP = {"small": 14, "medium": 18, "large": 26}

# libassがSRTを描画するときの既定キャンバスサイズ(PlayRes)。
# サイズ・マージンはこの座標系で指定すると、出力解像度に合わせて自動スケールされる。
PLAY_RES_X, PLAY_RES_Y = 384, 288

# テロップ設定の既定値(1項目でも欠けていたらここで補完する)
DEFAULT_STYLE = {
    "font": "gothic",
    "size": 18.0,
    "pos_x": 0.5, "pos_y": 0.9,
    "preset": "simple",
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "outline": 2.0,
    "shadow": 0.0,
    "bold": False,
    "box": False,
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def normalize_style(raw, base: dict = None) -> dict:
    """フォームやAPIから来たテロップ設定を検証し、既定値で補完して返す。

    raw はJSON文字列・辞書・Noneのいずれでもよい。
    不正な値は無視して base(省略時は DEFAULT_STYLE)の値を使う。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    s = dict(base or DEFAULT_STYLE)

    if raw.get("font") in FONT_MAP:
        s["font"] = raw["font"]
    size = raw.get("size")
    if isinstance(size, str) and size in SIZE_MAP:   # 旧「小/中/大」との互換
        size = SIZE_MAP[size]
    try:
        s["size"] = max(8.0, min(float(size), 60.0))
    except (TypeError, ValueError):
        pass
    for key in ("pos_x", "pos_y"):
        try:
            s[key] = max(0.0, min(float(raw[key]), 1.0))
        except (KeyError, TypeError, ValueError):
            pass
    for key, hi in (("outline", 10.0), ("shadow", 6.0)):
        try:
            s[key] = max(0.0, min(float(raw[key]), hi))
        except (KeyError, TypeError, ValueError):
            pass
    for key in ("color", "outline_color"):
        v = str(raw.get(key, ""))
        if _HEX_RE.match(v):
            s[key] = "#" + v.lstrip("#").upper()
    for key in ("bold", "box"):
        if isinstance(raw.get(key), bool):
            s[key] = raw[key]
    if isinstance(raw.get("preset"), str):
        s["preset"] = raw["preset"][:20]
    return s


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' を ASS の色形式 '&HAABBGGRR' に変換(AA=00 が不透明)"""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def build_style(style: dict = None) -> str:
    """スタイル辞書から force_style 文字列を組み立てる。

    位置は下中央アンカー(Alignment=2)を基準に、
    縦: MarginV(下からの余白)、横: MarginL/MarginR の差で表現する。
    box=True のときは BorderStyle=3(縁取り色を背景ボックスとして使い、
    縁取りの太さが背景の余白になる)。角丸はASSの制約で表現できない。
    """
    s = normalize_style(style or {})
    font_name = FONT_MAP.get(s["font"], "Meiryo")

    # 縦位置: pos_y=1.0 が最下部 → 下余白 0
    margin_v = round((1.0 - s["pos_y"]) * PLAY_RES_Y)
    margin_v = max(6, min(margin_v, PLAY_RES_Y - 40))
    # 横位置: 中央(0.5)からのずれを左右マージンの差で表す
    shift = round((s["pos_x"] - 0.5) * PLAY_RES_X)
    margin_l = max(0, 2 * shift)
    margin_r = max(0, -2 * shift)

    if s["box"]:
        border_style = 3
        outline = max(s["outline"], 2.0)                       # 背景の余白
        outline_colour = _ass_color(s["outline_color"], 0x50)  # 半透明の背景ボックス
        shadow = 0.0
    else:
        border_style = 1
        outline = s["outline"]
        outline_colour = _ass_color(s["outline_color"])
        shadow = s["shadow"]

    return (f"FontName={font_name},FontSize={s['size']:g},"
            f"Bold={-1 if s['bold'] else 0},"
            f"PrimaryColour={_ass_color(s['color'])},"
            f"OutlineColour={outline_colour},"
            f"BackColour={_ass_color('#000000', 0x60)},"      # 影の色(半透明の黒)
            f"BorderStyle={border_style},Outline={outline:g},Shadow={shadow:g},"
            f"Alignment=2,MarginV={margin_v},MarginL={margin_l},MarginR={margin_r}")


# --- 話題の見出し(画面左上) ---
TITLE_SIZE_RATIO = 0.62   # テロップの文字サイズに対する見出しの大きさ
TITLE_SIZE_MIN, TITLE_SIZE_MAX = 11.0, 20.0
TITLE_MARGIN = 10         # 画面の端からの余白(PlayRes 384x288 基準)


def build_title_style(style: dict = None) -> str:
    """話題の見出し用の force_style を作る。

    テロップ本体と同じフォントで、少し小さく、左上に半透明の黒帯で置く。
    ffmpeg が SRT から作る字幕は旧SSA形式の配置指定なので、左上は Alignment=5
    (7 は右上になる。実際に焼いて確認済み)。BorderStyle=3 が背景ボックス。
    """
    s = normalize_style(style or {})
    font_name = FONT_MAP.get(s["font"], "Meiryo")
    # テロップより小さく。ただし読めない大きさにはしない
    size = min(s["size"], max(TITLE_SIZE_MIN, min(s["size"] * TITLE_SIZE_RATIO, TITLE_SIZE_MAX)))
    return (f"FontName={font_name},FontSize={size:g},Bold=0,"
            f"PrimaryColour={_ass_color('#FFFFFF')},"
            f"OutlineColour={_ass_color('#000000', 0x40)},"     # 半透明の黒帯
            f"BackColour={_ass_color('#000000', 0x60)},"
            f"BorderStyle=3,Outline=3,Shadow=0,"
            f"Alignment=5,MarginV={TITLE_MARGIN},MarginL={TITLE_MARGIN},MarginR={TITLE_MARGIN}")


def _fmt_time(sec: float) -> str:
    """秒数を SRT の時刻形式 (HH:MM:SS,mmm) に変換"""
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list, srt_path: str):
    """文字起こし結果からSRT字幕ファイルを生成"""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_fmt_time(seg['start'])} --> {_fmt_time(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"      字幕ファイルを生成: {srt_path}")


def escape_srt_path(srt_path: str) -> str:
    """ffmpegのsubtitlesフィルタに渡すパスのエスケープ"""
    return str(srt_path).replace("\\", "/").replace(":", "\\:")


def subtitles_filters(srt_path: str, style: dict, titles_srt: str = None) -> list:
    """字幕(と、あれば話題の見出し)を焼き込む ffmpeg の -vf 用フィルタを作る"""
    parts = [f"subtitles='{escape_srt_path(srt_path)}':force_style='{build_style(style)}'"]
    if titles_srt:
        parts.append(f"subtitles='{escape_srt_path(titles_srt)}':force_style='{build_title_style(style)}'")
    return parts


def burn_subtitles(video_path: str, srt_path: str, out_path: str,
                   duration: float = 0, on_progress=None, style: dict = None,
                   hq: bool = False, titles_srt: str = None):
    """ffmpegで字幕を動画に焼き込む(テロップ化)。進捗を%で通知できる。

    titles_srt を渡すと、画面の左上に話題の見出しも焼き込む。
    hq(画質優先モード)のときは高画質寄りのエンコード設定を使う。
    """
    from src.preprocess import run_ffmpeg_progress, quality_args
    print("      テロップを焼き込み中...")

    # 失敗・中止したとき、書きかけのファイルが完成済み動画を上書きして
    # 壊さないよう、一時ファイルへ書き出してから置き換える
    tmp = Path(out_path).with_name("_" + Path(out_path).stem + "_tmp.mp4")

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", ",".join(subtitles_filters(srt_path, style, titles_srt)),
        "-c:v", "libx264", *quality_args(hq),
        "-c:a", "copy",
        str(tmp),
    ]
    try:
        run_ffmpeg_progress(cmd, duration, on_progress)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out_path)
    print(f"      → テロップ付き動画: {out_path}")
