"""動画クリッパー(モック版)エントリーポイント

処理の流れ:
  1. 文字起こし (Whisper)
  2. SRT字幕生成 → テロップ焼き込み (ffmpeg)
  3. 盛り上がり検出 → 候補5区間を選出
  4. 切り抜き5本を出力(縦 / 横 選択可)

使い方:
  python main.py input.mp4
  python main.py input.mp4 --format vertical --clip-length 20
"""
import argparse
from pathlib import Path

from src.preprocess import prepare_video, extract_audio
from src.transcribe import transcribe
from src.subtitle import write_srt, burn_subtitles
from src.highlight import detect_highlights
from src.clip import cut_clips


def main():
    parser = argparse.ArgumentParser(description="動画テロップ付与+切り抜き候補作成(モック)")
    parser.add_argument("video", help="入力動画ファイル (mp4など)")
    parser.add_argument("--format", choices=["horizontal", "vertical"],
                        default="horizontal", help="切り抜きの向き(デフォルト: horizontal)")
    parser.add_argument("--clip-length", type=float, default=30.0,
                        help="切り抜き1本の秒数(デフォルト: 30)")
    parser.add_argument("--model", default="small",
                        help="Whisperモデルサイズ: tiny/base/small/medium (精度と速度のトレードオフ)")
    args = parser.parse_args()

    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)

    # ⓪ 前処理:大きい動画は自動圧縮 + 音声を一括抽出
    print("[1/5] 前処理中...")
    work_video = prepare_video(args.video, str(out_dir))
    wav_path = extract_audio(work_video, str(out_dir))

    # ① 文字起こし
    segments = transcribe(wav_path, model_size=args.model)

    # ① テロップ付与
    srt_path = str(out_dir / "subtitles.srt")
    captioned = str(out_dir / "captioned.mp4")
    write_srt(segments, srt_path)
    burn_subtitles(work_video, srt_path, captioned)

    # ② 盛り上がり検出(候補5つ)
    highlights = detect_highlights(wav_path, segments, str(out_dir),
                                   clip_length=args.clip_length, n_clips=5)

    # ②③ 切り抜き(縦 / 横)
    cut_clips(captioned, highlights, str(out_dir), orientation=args.format)
    Path(wav_path).unlink(missing_ok=True)  # 中間ファイルの後片付け

    print("\n完了! output/ フォルダを確認してください。")


if __name__ == "__main__":
    main()
