"""⓪ 前処理:巨大な動画の自動圧縮と、音声の一括抽出(高速化のキモ)

- スマホの4K動画などをそのまま処理すると、テロップ焼き込みで
  何時間もかかるため、大きい動画は先にHD(1280px幅)へ自動圧縮する。
- 文字起こしと盛り上がり検出の両方で使う音声(16kHzモノラルWAV)を
  ここで1回だけ抽出し、後段で使い回す。
- run_ffmpeg_progress: ffmpegの進捗(%)を読み取る共通ヘルパー。
"""
import json
import subprocess
import threading
from pathlib import Path

# 処理の中止フラグ。app.py の /cancel で立てられ、各処理が節目ごとに確認する
CANCEL_EVENT = threading.Event()


class ProcessCancelled(Exception):
    """ユーザー操作による処理の中止"""


def check_cancelled():
    """中止が要求されていたら ProcessCancelled を投げる"""
    if CANCEL_EVENT.is_set():
        raise ProcessCancelled()

# この幅を超える動画は自動で縮小する(1280 = HD相当。切り抜き用途には十分)
MAX_WIDTH = 1280
# このサイズ(MB)を超える動画は解像度に関わらず再圧縮する
MAX_SIZE_MB = 800


def quality_args(hq: bool = False) -> list:
    """画質優先モードに応じたx264のエンコード設定を返す。

    OFF: 従来どおりの速度優先(CRF 23 / veryfast)
    ON:  高画質寄り(CRF 18 / medium)。時間とサイズは増えるが
         テロップや輪郭の崩れが減る
    """
    if hq:
        return ["-crf", "18", "-preset", "medium"]
    return ["-crf", "23", "-preset", "veryfast"]


def probe(video_path: str) -> dict:
    """ffprobeで動画の幅・高さ・長さを取得"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", video_path,
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    stream = info["streams"][0]
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "duration": float(info["format"].get("duration", 0)),
    }


def run_ffmpeg_progress(cmd: list, duration: float, on_progress=None):
    """ffmpegを実行しながら進捗(0.0〜1.0)をコールバックで通知する。

    cmd は ["ffmpeg", ...] 形式。失敗時はエラー内容を添えて例外を投げる。
    """
    full = [cmd[0], "-y", "-progress", "pipe:1", "-nostats",
            "-loglevel", "error"] + cmd[1:]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if CANCEL_EVENT.is_set():          # 中止要求が来たらffmpegを止める
            proc.kill()
            proc.wait()
            raise ProcessCancelled()
        line = line.strip()
        if line.startswith(("out_time_ms=", "out_time_us=")):
            try:
                us = int(line.split("=")[1])
                if duration > 0 and on_progress:
                    on_progress(min(us / 1_000_000 / duration, 1.0))
            except ValueError:
                pass
    proc.wait()
    check_cancelled()
    if proc.returncode != 0:
        err = proc.stderr.read()[-500:]
        raise RuntimeError(f"ffmpegの処理に失敗しました: {err}")
    if on_progress:
        on_progress(1.0)


def prepare_video(video_path: str, out_dir: str,
                  on_message=None, on_progress=None, hq: bool = False) -> tuple:
    """
    必要なら動画を圧縮し、(処理に使う動画のパス, 動画の長さ秒) を返す。
    小さい動画はそのまま返す(圧縮スキップ)。
    hq(画質優先モード)のときは圧縮せず、元の解像度のまま処理する。
    """
    def say(msg):
        print(f"      {msg}")
        if on_message:
            on_message(msg)

    info = probe(video_path)
    size_mb = Path(video_path).stat().st_size / 1024 / 1024
    say(f"動画情報: {info['width']}x{info['height']}, "
        f"{info['duration']:.0f}秒, {size_mb:.0f}MB")

    if hq:
        # 画質優先: 事前圧縮による再エンコードを行わず、元の画質を保つ
        say("画質優先モード: 圧縮せず元の解像度のまま処理します(時間がかかります)")
        if on_progress:
            on_progress(1.0)
        return video_path, info["duration"]

    if info["width"] <= MAX_WIDTH and size_mb <= MAX_SIZE_MB:
        say("十分軽量なため、圧縮をスキップします")
        if on_progress:
            on_progress(1.0)
        return video_path, info["duration"]

    compressed = str(Path(out_dir) / "_compressed.mp4")
    say("大きい動画のため、先にHD画質へ自動圧縮します")
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"scale='min({MAX_WIDTH},iw)':-2",
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "128k",
        compressed,
    ]
    run_ffmpeg_progress(cmd, info["duration"], on_progress)
    new_mb = Path(compressed).stat().st_size / 1024 / 1024
    say(f"圧縮完了: {size_mb:.0f}MB → {new_mb:.0f}MB")
    return compressed, info["duration"]


def extract_audio(video_path: str, out_dir: str) -> str:
    """文字起こし・盛り上がり検出で共用する音声WAVを1回だけ抽出"""
    wav_path = str(Path(out_dir) / "_audio.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-ac", "1", "-ar", "16000", "-vn", wav_path],
        check=True, capture_output=True,
    )
    return wav_path
