"""④ オープニング・エンディング:完成した動画の前後に、用意した映像をつなぐ

本編とは別に撮った(作った)OP・EDの動画をつなぐ。大きさ・コマ数・音声の形式は
本編にそろえてから結合するので、素材の解像度が違っていても問題ない。
OP・EDの音はそのまま残す(本編の音とつながる)。
"""
import subprocess
from pathlib import Path

from src.preprocess import quality_args, probe, check_cancelled

FPS = 30
NO_WINDOW = 0x08000000


def _video_size(path: str) -> tuple:
    """動画の幅・高さ(取れなければ 1280x720)"""
    try:
        info = probe(path)
        return int(info.get("width") or 1280), int(info.get("height") or 720)
    except Exception:
        return 1280, 720


def _normalize(src: str, out: str, width: int, height: int, hq: bool):
    """本編と同じ大きさ・コマ数・音声形式にそろえる(はみ出す分は黒帯で埋める)"""
    scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS}")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                    "-vf", scale, "-c:v", "libx264", *quality_args(hq),
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    out], check=True, capture_output=True, creationflags=NO_WINDOW)


def attach(main_video: str, out_path: str, intro: str = "", outro: str = "",
           hq: bool = False, on_progress=None) -> bool:
    """main_video の前後に intro / outro をつないで out_path に書き出す。

    つなぐものが何もなければ False を返す(呼び出し側はそのままでよい)。
    """
    pieces = [p for p in (intro, main_video, outro) if p]
    if len(pieces) < 2:
        return False
    print("      オープニング・エンディングをつないでいます...")
    width, height = _video_size(main_video)
    work = Path(out_path).with_name("_bookend_tmp")
    work.mkdir(exist_ok=True)
    try:
        files = []
        for i, src in enumerate(pieces):
            check_cancelled()
            if src == main_video:
                files.append(Path(src).resolve())
                continue
            dst = work / f"{i}.mp4"
            _normalize(src, str(dst), width, height, hq)
            files.append(dst.resolve())
            if on_progress:
                on_progress(min(0.9, (i + 1) / (len(pieces) + 1)))
        manifest = work / "parts.txt"
        manifest.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
        tmp = Path(out_path).with_name("_" + Path(out_path).stem + "_joined.mp4")
        # 本編は作り直さず、つなぐだけ(音声だけ形式をそろえる)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(manifest), "-c:v", "libx264", *quality_args(hq),
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)],
                       check=True, capture_output=True, creationflags=NO_WINDOW)
        tmp.replace(out_path)
        if on_progress:
            on_progress(1.0)
        print(f"      → つなぎ終わりました: {out_path}")
        return True
    finally:
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        work.rmdir()
