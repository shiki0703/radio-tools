"""②③ 切り抜き:候補区間の切り出しと、縦/横フォーマットへの変換

切り抜きのテロップは、元動画(テロップなし)に対して切り抜き専用の
スタイル辞書で焼き込む。元動画側の設定とは完全に独立している。

horizontal … テロップあり: 元動画から切り出して切り抜き用スタイルで焼き込み
             テロップなし: テロップ付き(または元)動画から無劣化コピー
vertical   … 9:16の黒キャンバス(1080x1920)の中央に横動画をそのまま配置し、
             動画全体が見える形にする(クロップしない)。
             テロップは黒帯の上にも置ける。
"""
import subprocess
from pathlib import Path

from src.preprocess import quality_args, check_cancelled
from src.subtitle import write_srt, build_style, build_title_style, escape_srt_path

# 縦動画の出力キャンバス
V_WIDTH, V_HEIGHT = 1080, 1920


def _clip_segments(segments: list, start: float, end: float) -> list:
    """クリップ範囲に重なる字幕だけを、クリップ先頭を0秒とする時刻に変換して返す"""
    local = []
    for s in segments:
        if s["end"] > start and s["start"] < end:
            local.append({
                "start": max(0.0, s["start"] - start),
                "end": min(end, s["end"]) - start,
                "text": s["text"],
            })
    return local


def cut_clips(captioned_video: str, highlights: list, out_dir: str,
              orientation: str = "horizontal", on_progress=None,
              work_video: str = None, segments: list = None,
              style: dict = None, hq: bool = False, titles: list = None):
    """
    盛り上がり候補を切り抜いて clip_1.mp4 〜 clip_5.mp4 を出力。

    captioned_video: テロップ焼き込み済み動画(字幕なし運用時の切り出し元)
    work_video:      テロップなしの元動画(切り抜き用テロップを焼き込む元)
    segments:        字幕データ。切り抜き用スタイルで焼き込む(Noneなら字幕なし)
    style:           切り抜き用のテロップ設定辞書(src.subtitle.DEFAULT_STYLE 参照)
    titles:          話題の見出し([{start,end,text}])。渡すと画面左上にも焼き込む
    hq:              画質優先モード(高画質エンコード+高品質な縮小補間)
    on_progress:     進捗(0.0〜1.0)を受け取るコールバック
    """
    print(f"[5/5] 切り抜き動画を作成中... (形式: {orientation})")

    for i, h in enumerate(highlights, 1):
        check_cancelled()   # クリップごとに中止要求を確認
        final_path = Path(out_dir) / f"clip_{i}.mp4"
        # 失敗・中止時に完成済みクリップを書きかけで壊さないよう一時ファイルへ
        tmp_path = Path(out_dir) / f"_clip_{i}_tmp.mp4"
        out_path = str(tmp_path)

        # 切り抜き範囲に合わせて時刻をずらした字幕(テロップを焼く場合のみ)
        local = _clip_segments(segments, h["start"], h["end"]) \
            if (segments and work_video) else []

        # 話題の見出しも同じように時刻をずらす
        local_titles = _clip_segments(titles, h["start"], h["end"]) if (titles and work_video) else []

        tmp_srt = tmp_title_srt = None
        vf_parts = []
        if orientation == "vertical":
            # 縦: 元動画を幅1080に合わせ、9:16キャンバスの中央へ配置(上下黒帯)
            scale_flags = ":flags=lanczos" if hq else ""   # 画質優先時は高品質な補間
            vf_parts.append(f"scale={V_WIDTH}:-2{scale_flags},"
                            f"pad={V_WIDTH}:{V_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black")

        if local:
            tmp_srt = str(Path(out_dir) / f"_clip_{i}.srt")
            write_srt(local, tmp_srt)
            vf_parts.append(f"subtitles='{escape_srt_path(tmp_srt)}'"
                            f":force_style='{build_style(style)}'")

        if local_titles and work_video:
            tmp_title_srt = str(Path(out_dir) / f"_clip_{i}_title.srt")
            write_srt(local_titles, tmp_title_srt)
            vf_parts.append(f"subtitles='{escape_srt_path(tmp_title_srt)}'"
                            f":force_style='{build_title_style(style)}'")

        if vf_parts:
            # 再エンコードして切り出し(縦レイアウト化 か テロップ焼き込みがある場合)
            src = work_video if (local or local_titles) else (work_video or captioned_video)
            audio = ["-c:a", "aac"] + (["-b:a", "192k"] if hq else [])
            cmd = ["ffmpeg", "-y",
                   "-ss", str(h["start"]), "-to", str(h["end"]),
                   "-i", src,
                   "-vf", ",".join(vf_parts),
                   "-c:v", "libx264", *quality_args(hq),
                   *audio, out_path]
        else:
            # 横・テロップなしは無劣化コピー(超高速)
            # ※キーフレーム単位で切るため、開始位置が1〜2秒前後することがあります
            cmd = ["ffmpeg", "-y",
                   "-ss", str(h["start"]), "-to", str(h["end"]),
                   "-i", captioned_video,
                   "-c", "copy", out_path]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            for tmp_file in (tmp_srt, tmp_title_srt):
                if tmp_file:
                    Path(tmp_file).unlink(missing_ok=True)
        tmp_path.replace(final_path)
        if on_progress:
            on_progress(i / len(highlights))
        print(f"  → {final_path} ({h['start']}s〜{h['end']}s)")
