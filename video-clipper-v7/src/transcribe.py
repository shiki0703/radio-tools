"""① 文字起こし:faster-whisper で音声を文字起こしする

テロップの表示タイミングを発話に合わせるため、単語単位のタイムスタンプ
(word_timestamps)を取得し、各区間の開始・終了を実際の発話へ揃える。
Whisperのセグメント時刻は無音パディング等で発話より早く始まることが
あるため、そのまま使うとテロップが音声より先に表示されてしまう。
"""
import re
from collections import Counter

import av
from faster_whisper import WhisperModel

from src.preprocess import check_cancelled


def _allow_new_pyav():
    """faster-whisper は音声を開くとき av.open(..., metadata_errors=...) を使うが、
    PyAV 19 でこの引数がなくなり、文字起こしが TypeError で止まる。
    なくなった引数だけを外して開き直す(古い PyAV ではそのまま通る)。"""
    original = getattr(av.open, '__wrapped__', av.open)

    def open_compat(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except TypeError as e:
            if 'metadata_errors' not in kwargs or 'metadata_errors' not in str(e):
                raise
            kwargs.pop('metadata_errors')
            return original(*args, **kwargs)

    open_compat.__wrapped__ = original
    av.open = open_compat


_allow_new_pyav()

# 一度読み込んだモデルを使い回す(2回目以降の処理が大幅に速くなる)
_model_cache = {}

# 精度モード: (モデルサイズ, ビーム幅)
# high は large-v3-turbo。medium より速くて正確なため置き換えた
# (34分の音声で medium 約20分・turbo 約7分。「遅くない?店舗間」→「遅くないテンポ感」など誤りも減る)
ACCURACY_MODES = {
    "standard": ("small", 5),               # 標準(モデル約0.5GB)
    "high": ("large-v3-turbo", 5),          # 高精度(モデル約1.6GB・初回だけダウンロード)
}

# --- テロップ表示タイミングの内部設定 ---
TAIL_SEC = 0.2      # 最後の単語が終わってから消えるまでの余韻
MIN_DUR_SEC = 0.8   # 最低表示時間(短い相づちが一瞬で消えないように)
MIN_GAP_SEC = 0.05  # 次のテロップとの最小間隔(重なり防止)
LEAD_SEC = 0.12     # 表示を少し遅らせる(単語の開始時刻は実際の発声よりわずかに早い)

# --- 長すぎるテロップの分割 ---
# Whisperは長い一続きの発話を1区間で返すことがある(実例: 26.8秒・約70文字)。
# テロップとして読める長さに、単語の切れ目で分ける。
MAX_DUR_SEC = 5.5    # これより長く表示される区間は分ける
MAX_CHARS = 22       # これより長い文字数の区間は分ける
MIN_PIECE_CHARS = 3  # 分けたあとの各行の最低文字数
_BREAK_AFTER = "、。!?！?…」』)"        # この文字の直後は切れ目として最優先
_BREAK_PARTICLES = "はがをにへでとやもねよかなけど"   # 助詞の直後も切れ目にしてよい


# 笑い声などに対してWhisperが出す「wwww」だけの行(音声の内容ではない)
_LAUGH_ONLY = re.compile(r"^[wWｗＷ]+$")

_HEAD_NAME = re.compile(r"^([一-龥々]{2,3}|[ァ-ヶー]{2,5})[ 　]+\S")


def strip_speaker_labels(segments: list) -> list:
    """Whisperが勝手に付ける架空の話者名(「樋口 あなるほどね」の「樋口 」など)を取り除く。

    学習データの話者名付き字幕の癖で、音声にない人名が行頭に「名前+空白」で現れる
    (「樋口」「深井」「ヤンヤン」など)。
    行頭に3回以上現れ、かつ出現の6割以上が「名前+空白」の形になっている
    漢字2〜3文字・カタカナ2〜5文字だけを
    話者名とみなす(日本語の文中の漢字語は普通、後ろに空白が付かない)。
    消した結果、文字が残らない区間は取り除く。
    """
    heads = Counter()
    for s in segments:
        m = _HEAD_NAME.match(s["text"])
        if m:
            heads[m.group(1)] += 1
    labels = []
    for name, n in heads.items():
        if n < 3:
            continue
        total = sum(s["text"].count(name) for s in segments)
        spaced = sum(len(re.findall(rf"(?:^|[ 　]){re.escape(name)}[ 　]", s["text"])) for s in segments)
        if spaced >= total * 0.6:
            labels.append(name)
    if not labels:
        return segments
    print(f"      架空の話者名を除去: {'、'.join(labels)}")
    pattern = re.compile(r"(?:^|(?<=[ 　]))(?:" + "|".join(map(re.escape, labels)) + r")[ 　]+")
    cleaned = []
    for s in segments:
        text = pattern.sub("", s["text"]).strip()
        if text:
            cleaned.append({**s, "text": text})
    return cleaned


def _text_of(words: list) -> str:
    """単語リストをつないで、行末の読点を落とした表示用の文字列にする"""
    return "".join(w[2] for w in words).strip().rstrip("、")


def split_long_segments(segments: list) -> list:
    """長すぎる区間を、単語の切れ目で読める長さに分ける。

    Whisperは一続きの発話をまとめて1区間で返すことがあり、そのままでは
    1つのテロップが何十秒も出たままになる。単語タイムスタンプがあるときだけ、
    句読点の直後・間の空いた所を優先して分ける(単語情報がなければ何もしない)。
    """
    out = []
    for seg in segments:
        out.extend(_split_segment(seg))
    return out


def _split_segment(seg: dict, depth: int = 0) -> list:
    words = seg.get("words") or []
    too_long = (seg["end"] - seg["start"]) > MAX_DUR_SEC or len(seg["text"]) > MAX_CHARS
    # 単語テキストが揃っていないもの(古いデータ)は分けられない
    if depth >= 6 or len(words) < 2 or not too_long or any(len(w) < 3 for w in words):
        return [seg]
    joined = "".join(w[2] for w in words).strip()
    if seg["text"] not in (joined, joined.rstrip("、")):
        return [seg]   # 話者名の除去などで文字が変わっている区間は触らない

    best, best_score = None, 0.0
    for i in range(1, len(words)):
        left, right = _text_of(words[:i]), _text_of(words[i:])
        if min(len(left), len(right)) < MIN_PIECE_CHARS:
            continue                       # 1〜2文字だけの行を作らない
        gap = max(0.0, words[i][0] - words[i - 1][1])
        tail = "".join(w[2] for w in words[:i]).strip()[-1]
        # 間が空いている所を第一に、句読点・助詞の直後を次に、最後に半分へ近さで選ぶ
        score = min(gap, 0.8) * 3
        if tail in _BREAK_AFTER:
            score += 2.0
        elif tail in _BREAK_PARTICLES:
            score += 0.8
        score += (1 - abs(len(left) - len(right)) / (len(left) + len(right))) * 0.5
        if score > best_score:
            best, best_score = i, score
    if best is None:
        return [seg]
    first = {**seg, "end": float(words[best - 1][1]), "text": _text_of(words[:best]), "words": words[:best]}
    second = {**seg, "start": float(words[best][0]), "text": _text_of(words[best:]), "words": words[best:]}
    return _split_segment(first, depth + 1) + _split_segment(second, depth + 1)


def align_to_words(raw_segments: list, lead: float = LEAD_SEC) -> list:
    """単語タイムスタンプを使って、テロップ区間を実際の発話に合わせる。

    raw_segments: [{"start", "end", "text", "words": [(開始, 終了), ...]}]
                  words が空の区間は元のセグメント時刻をそのまま使う。
    Returns:      [{"start", "end", "text"}](時刻は発話基準に補正済み)

    - 開始 = 最初の単語の発話開始(音声より先に表示しない)
    - 終了 = 最後の単語の発話終了 + 余韻(次のテロップ開始は跨がない)
    - 最低表示時間を確保するが、次の発話タイミングを壊さない
    """
    aligned = []
    for seg in raw_segments:
        words = seg.get("words") or []
        if words:
            # 単語の開始時刻は実際の発声よりわずかに早いので、少し遅らせて出す
            start = float(words[0][0]) + lead
            speech_end = float(words[-1][1])
        else:
            start = float(seg["start"]) + lead
            speech_end = float(seg["end"])
        aligned.append({"start": start, "end": speech_end, "text": seg["text"]})

    for i, seg in enumerate(aligned):
        # 余韻を足し、短すぎる場合は最低表示時間まで延ばす
        end = max(seg["end"] + TAIL_SEC, seg["start"] + MIN_DUR_SEC)
        # ただし次のテロップの発話開始より先には残さない(重なり・先行表示の防止)
        if i + 1 < len(aligned):
            end = min(end, aligned[i + 1]["start"] - MIN_GAP_SEC)
        # 万一の破綻防止(隣接発話が極端に近い場合でも表示時間ゼロにしない)
        end = max(end, seg["start"] + 0.2)
        seg["start"] = round(seg["start"], 2)
        seg["end"] = round(end, 2)
    return aligned


def _get_model(model_size: str) -> WhisperModel:
    if model_size not in _model_cache:
        # CPUで動作。GPUがあれば device="cuda" にすると更に高速化できる
        _model_cache[model_size] = WhisperModel(
            model_size, device="cpu", compute_type="int8")
    return _model_cache[model_size]


def transcribe(audio_path: str, accuracy: str = "standard", on_progress=None):
    """
    音声を文字起こしして、区間のリストを返す。

    accuracy: "standard"(速い) / "high"(高精度・低速)
    on_progress: 進捗(0.0〜1.0)を受け取るコールバック

    Returns:
        [{"start": 開始秒, "end": 終了秒, "text": "セリフ"}, ...]
        時刻は単語タイムスタンプで実際の発話に合わせてある
    """
    model_size, beam = ACCURACY_MODES.get(accuracy, ACCURACY_MODES["standard"])
    print(f"      文字起こし中... (モデル: {model_size}, beam={beam})")
    model = _get_model(model_size)

    segments, info = model.transcribe(
        audio_path,
        language="ja",
        beam_size=beam,
        vad_filter=True,                    # 無音区間を自動スキップ
        word_timestamps=True,               # 単語単位の時刻(テロップ同期用)
        initial_prompt="以下は日本語の会話です。句読点を付けてください。",
        # 直前の認識結果を次の区間のヒントにしない。
        # ヒントにすると一度の誤認識(架空の話者名「樋口」「深井」や行頭の「C」など)が
        # 以降の全区間に連鎖し、長い動画の後半ほど精度が落ちる。
        condition_on_previous_text=False,
        # 2秒以上の無音の中に出てきた「幻の発話」を捨てる
        hallucination_silence_threshold=2.0,
    )

    raw = []
    for seg in segments:
        check_cancelled()   # 区間ごとに中止要求を確認(文字起こしは逐次生成される)
        words = [(w.start, w.end, w.word) for w in (seg.words or [])]
        raw.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "words": words,
        })
        if on_progress and info.duration > 0:
            on_progress(min(seg.end / info.duration, 1.0))

    raw = [r for r in raw if not _LAUGH_ONLY.match(r["text"])]
    before = len(raw)
    # 分割は話者名の除去より先に行う(除去で文字が変わると単語と対応が取れなくなるため)
    raw = split_long_segments(raw)
    raw = strip_speaker_labels(raw)
    if len(raw) > before:
        print(f"      長すぎるテロップを分割: {before} → {len(raw)} 区間")
    results = align_to_words(raw)

    # ログ: 補正後の時刻を表示(0.2秒以上動いた場合だけ元の時刻も添える)
    for r, seg in zip(results, raw):
        shift = r["start"] - seg["start"]
        note = f"(発話に合わせ {shift:+.2f}s)" if abs(shift) >= 0.2 else ""
        print(f"  {r['start']:6.1f}s - {r['end']:6.1f}s : {r['text']} {note}")

    if on_progress:
        on_progress(1.0)
    print(f"  → {len(results)} 区間を認識しました")
    return results
