"""④ オープニング・エンディング:完成した動画の前後に、用意した映像をつなぐ

本編とは別に撮った(作った)OP・EDの動画をつなぐ。大きさ・コマ数・音声の形式は
本編にそろえてから結合するので、素材の解像度が違っていても問題ない。
OP・EDの音はそのまま残す(本編の音とつながる)。

エンディングをつなぐときは、本編の最後の FADE_SEC 秒で映像を徐々に暗く・音を徐々に小さくしてから、
エンディングを始める(ぷつっと切り替わらないように)。
"""
import json
import subprocess
from pathlib import Path

from src.preprocess import quality_args, probe, check_cancelled

FPS = 30
FADE_SEC = 2.0          # エンディングの前に、本編を暗くしていく長さ(秒)
NO_WINDOW = 0x08000000
AUDIO = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"


def _video_size(path: str) -> tuple:
    """動画の幅・高さ(取れなければ 1280x720)"""
    try:
        info = probe(path)
        return int(info.get("width") or 1280), int(info.get("height") or 720)
    except Exception:
        return 1280, 720


def _has_audio(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", path], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    try:
        return bool(json.loads(out or "{}").get("streams"))
    except ValueError:
        return False


def _normalize(src: str, out: str, width: int, height: int, hq: bool):
    """本編と同じ大きさ・コマ数・音声形式にそろえる(はみ出す分は黒帯で埋める。音がなければ無音を入れる)"""
    scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS}")
    if _has_audio(src):
        audio = ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        audio = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, *audio,
                    "-vf", scale, "-c:v", "libx264", *quality_args(hq),
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    out], check=True, capture_output=True, creationflags=NO_WINDOW)


def join_graph(count: int, main_index: int, main_seconds: float, fade: float, size: tuple) -> str:
    """-filter_complex 用:count 本をこの順につなぐ。fade > 0 なら、本編(main_index)の最後を暗く・静かにする"""
    width, height = size
    chains, labels = [], []
    for i in range(count):
        video = (f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={FPS},format=yuv420p")
        audio = f"[{i}:a]{AUDIO}"
        if i == main_index and fade > 0:
            start = max(0.0, main_seconds - fade)
            video += f",fade=t=out:st={start:.3f}:d={fade:.3f}"
            audio += f",afade=t=out:st={start:.3f}:d={fade:.3f}"
        chains += [f"{video}[v{i}]", f"{audio}[a{i}]"]
        labels.append(f"[v{i}][a{i}]")
    return ";".join(chains) + ";" + "".join(labels) + f"concat=n={count}:v=1:a=1[v][a]"


def attach(main_video: str, out_path: str, intro: str = "", outro: str = "",
           hq: bool = False, on_progress=None) -> bool:
    """main_video の前後に intro / outro をつないで out_path に書き出す。

    つなぐものが何もなければ False を返す(呼び出し側はそのままでよい)。
    """
    pieces = [p for p in (intro, main_video, outro) if p]
    if len(pieces) < 2:
        return False
    print("      オープニング・エンディングをつないでいます...")
    size = _video_size(main_video)
    main_seconds = float(probe(main_video)["duration"])
    work = Path(out_path).with_name("_bookend_tmp")
    work.mkdir(exist_ok=True)
    try:
        inputs = []
        for i, src in enumerate(pieces):
            check_cancelled()
            if src == main_video:
                inputs += ["-i", str(Path(src).resolve())]
                continue
            dst = work / f"{i}.mp4"
            _normalize(src, str(dst), *size, hq)
            inputs += ["-i", str(dst.resolve())]
            if on_progress:
                on_progress(min(0.9, (i + 1) / (len(pieces) + 1)))
        fade = min(FADE_SEC, main_seconds / 2) if outro else 0.0
        graph = join_graph(len(pieces), pieces.index(main_video), main_seconds, fade, size)
        tmp = Path(out_path).with_name("_" + Path(out_path).stem + "_joined.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph,
                        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", *quality_args(hq),
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
