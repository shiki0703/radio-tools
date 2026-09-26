"""② 盛り上がり検出(v4):話題のまとまりで切り出す + 根拠の提示

まず会話を「話題」に区切り、話題ごとに盛り上がりを評価する。
こうすると、話の途中から始まって途中で終わる切り抜きにならない。

  話題の切れ目 … 長い無音・話題転換の言い回し(「じゃあ」「さて」「第3問」など)・
                  使われる言葉の入れ替わり(前後で語彙が重ならなくなる所)

長い無音や語彙の重なりのしきい値は「何秒」と決め打ちにせず、その動画の中での
相対的な位置(分布の順位)で決める。番組のテンポや話し方の違いに左右されないため。

盛り上がりのスコアは4つの観点を組み合わせ、
「なぜこの区間が選ばれたか」の根拠(内訳と説明文)も返す。

  1. 音量        … 区間の平均的な声量(盛り上がりは声が大きい)
  2. 音量の急上昇 … 笑い声・歓声・ツッコミのような瞬間的なピーク
  3. キーワード   … 笑い系/驚き系/盛り上がり系の言葉の出現
  4. 発話密度     … テンポよく喋っている(=会話が弾んでいる)

文字起こしなしで実行された場合は話題が分からないため、
従来どおり一定の長さの窓をずらして音声だけ(1・2)で判定する。
"""
import json
import re
import wave
from pathlib import Path

import numpy as np

# 盛り上がりを示すキーワード(系統別)。自由に追加してOK
KEYWORD_GROUPS = {
    "笑い": ["笑", "www", "ww", "ウケ", "うけ", "面白", "おもしろ", "爆笑"],
    "驚き": ["すごい", "すご", "すげ", "やば", "ヤバ", "えぇ", "えー", "えっ",
             "うわ", "マジ", "まじ", "本当", "ほんと", "びっくり", "衝撃", "信じられ"],
    "盛り上がり": ["最高", "神", "天才", "優勝", "やった", "きた", "キタ",
                   "いいね", "熱い", "アツ", "いえ", "イエ"],
}

# スコアの重み(合計1.0)
WEIGHTS_FULL = {"volume": 0.30, "spike": 0.20, "keyword": 0.30, "density": 0.20}
WEIGHTS_AUDIO_ONLY = {"volume": 0.60, "spike": 0.40}

# --- 話題の切れ目の判定 ---
# 無音の長さ・語彙の重なりのしきい値は、動画ごとの分布から決める(番組のテンポに左右されないように)。
GAP_PERCENTILE = 90       # 行間の無音がこの順位より長ければ、話題の切れ目になりやすい
GAP_MIN, GAP_MAX = 1.0, 3.5        # ただしこの範囲に収める(秒)
VOCAB_LOW, VOCAB_MID = 15, 30      # 語彙の重なりがこの順位より小さければ、話題が変わったとみなす
TOPIC_MIN_RATIO = 0.6     # 話題の最短の長さ(1本の長さの目安に対する割合)
TOPIC_MIN_FLOOR = 10.0    # 話題の最短の長さの下限(秒)
TOPIC_WINDOW = 5          # 語彙の入れ替わりを見るときの前後の行数
TOPIC_SCORE = 1.2         # 切れ目と判定するスコア(小さくすると細かく切れる)
WHOLE_MAX = 2.0           # 目安の長さの何倍までなら話題を丸ごと切り出すか
PART_MIN, PART_MAX = 0.7, 1.8      # 話題の一部を切り出すときの長さの範囲(目安の長さの何倍か)
HEAD_BONUS = 0.30         # 話題の頭から始まる切り出しを優先する度合い

# 話題転換の言い回し(行の先頭に出てきたら切れ目とみなす)。ジャンルを問わない言い方を並べる
CUE_WORDS = ("じゃあ", "じゃ、", "では", "それでは", "さて", "ところで", "続いて", "次は", "次に",
             "というわけで", "という訳で", "そういえば", "話は変わ", "話変わ", "本題", "最後に",
             "まず", "はじめに", "初めに", "いきます", "行きます", "はい、次", "もう一つ", "もうひとつ",
             "ここからは", "改めて", "ちなみにさっき", "あと、", "それでね")
# 「第3問」「2つ目」のような番号付きの見出し(コーナー・章立て・クイズなどで共通)
NUMBERED_HEAD = re.compile(r"^(第[0-9０-９一二三四五六七八九十]+[問回話章節]|"
                           r"[0-9０-９一二三四五六七八九十]+(つ目|個目|問目|本目|番目|点目))")
# 逆に「話の続き」を示す言い回し。ここでは切らない
# (接続詞は前の話を受けている合図。答え合わせ系の言い方もここに含める)
CONT_WORDS = ("つまり", "要するに", "だから", "なので", "それで", "そしたら", "それから", "でも", "だけど",
              "しかも", "ただ", "けど", "その", "これ", "あれ", "それ", "ていうか", "というか",
              "正解", "不正解", "答え", "せーの", "せいの", "解説", "ということです", "残念")
# 問いかけの終わり方。この直後は答えが続くので、話題の切れ目にしない
QUESTION_ENDS = ("でしょう", "でしょうか", "でしょう。", "ですか", "ですか?", "ますか", "?", "?",
                 "かな", "かな。", "どっち", "どれ", "なに", "何", "?」")


def _load_volumes(wav_path: str) -> np.ndarray:
    """WAVから1秒ごとの音量(RMS、0〜1正規化)を計算"""
    with wave.open(wav_path) as wf:
        rate = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    n_sec = max(len(data) // rate, 1)
    vols = np.array([
        np.sqrt(np.mean(data[i * rate:(i + 1) * rate].astype(np.float64) ** 2))
        for i in range(n_sec)
    ])
    return vols / vols.max() if vols.max() > 0 else vols


def _score_window(start: float, end: float, segments, volumes) -> dict:
    """ある時間窓のスコアと根拠を計算する"""
    s_idx, e_idx = int(start), min(int(end), len(volumes))
    window = volumes[s_idx:e_idx] if e_idx > s_idx else volumes[:1]
    overall_mean = float(np.mean(volumes)) or 1e-6

    # --- 1. 音量(全体平均との比較) ---
    vol_ratio = float(np.mean(window)) / overall_mean
    vol_score = min(vol_ratio / 1.5, 1.0)          # 平均の1.5倍で満点

    # --- 2. 音量の急上昇(瞬間ピーク) ---
    spike_ratio = float(np.max(window)) / overall_mean
    spike_score = min(max(spike_ratio - 1.0, 0) / 1.5, 1.0)  # 平均の2.5倍で満点

    reasons = []
    if vol_score >= 0.6:
        reasons.append(f"声量が全体平均の{vol_ratio:.1f}倍と大きい")
    if spike_score >= 0.5:
        reasons.append("瞬間的な音量ピークあり(笑い声・歓声の可能性)")

    if segments is None:
        # 文字起こしなし:音声のみで判定
        total = (vol_score * WEIGHTS_AUDIO_ONLY["volume"]
                 + spike_score * WEIGHTS_AUDIO_ONLY["spike"])
        return {
            "start": round(start, 1), "end": round(end, 1),
            "score": round(total, 3),
            "components": {"音量": round(vol_score, 2), "音量急上昇": round(spike_score, 2)},
            "matched_keywords": {},
            "reason": "、".join(reasons) or "音声の相対的な盛り上がり",
            "text_sample": "(文字起こしなし)",
        }

    # --- 3. キーワード ---
    texts = [seg["text"] for seg in segments
             if seg["start"] < end and seg["end"] > start]
    joined = "".join(texts)
    matched = {}
    kw_count = 0
    for group, words in KEYWORD_GROUPS.items():
        hits = [w for w in words if w in joined]
        if hits:
            matched[group] = hits
            kw_count += sum(joined.count(w) for w in hits)
    kw_score = min(kw_count / 3.0, 1.0)             # 3ヒットで満点
    if matched:
        parts = [f"{g}系「{'」「'.join(ws[:2])}」" for g, ws in matched.items()]
        reasons.append("キーワード: " + "、".join(parts))

    # --- 4. 発話密度 ---
    density = len(joined) / max(end - start, 1)
    density_score = min(density / 8.0, 1.0)          # 8文字/秒で満点
    if density_score >= 0.7:
        reasons.append(f"発話密度が高い({density:.1f}文字/秒、会話が弾んでいる)")

    total = (vol_score * WEIGHTS_FULL["volume"]
             + spike_score * WEIGHTS_FULL["spike"]
             + kw_score * WEIGHTS_FULL["keyword"]
             + density_score * WEIGHTS_FULL["density"])
    return {
        "start": round(start, 1), "end": round(end, 1),
        "score": round(total, 3),
        "components": {
            "音量": round(vol_score, 2),
            "音量急上昇": round(spike_score, 2),
            "キーワード": round(kw_score, 2),
            "発話密度": round(density_score, 2),
        },
        "matched_keywords": matched,
        "reason": "、".join(reasons) or "相対的にスコアが高い区間",
        "text_sample": joined[:80],
    }


def _bigrams(text: str) -> set:
    """語彙の入れ替わりを見るための文字2文字組(日本語は単語で切りにくいため)"""
    body = "".join(c for c in text if c not in "、。!?！?「」『』 　")
    return {body[i:i + 2] for i in range(len(body) - 1)}


def _overlaps(segments: list) -> list:
    """各行について、前後それぞれ数行の語彙がどれだけ重なっているかを返す(重ならない=話題が変わった)"""
    out = [None] * len(segments)
    for i in range(1, len(segments)):
        before = set().union(*(_bigrams(s["text"]) for s in segments[max(0, i - TOPIC_WINDOW):i]))
        after = set().union(*(_bigrams(s["text"]) for s in segments[i:i + TOPIC_WINDOW]))
        if before and after:
            out[i] = len(before & after) / len(before | after)
    return out


def _thresholds(segments: list) -> tuple:
    """その動画に合わせたしきい値(長い無音・語彙の重なり)を決める。

    早口の動画でもゆっくりした動画でも同じように切れ目を見つけられるよう、
    固定の秒数ではなく、その動画の中での相対的な位置で判断する。
    """
    gaps = [max(0.0, b["start"] - a["end"]) for a, b in zip(segments, segments[1:])]
    gap_hi = float(np.clip(np.percentile(gaps, GAP_PERCENTILE), GAP_MIN, GAP_MAX)) if gaps else GAP_MIN
    overlaps = _overlaps(segments)
    known = [o for o in overlaps if o is not None]
    vocab_low = float(np.percentile(known, VOCAB_LOW)) if known else 0.0
    vocab_mid = float(np.percentile(known, VOCAB_MID)) if known else 0.0
    return gap_hi, vocab_low, vocab_mid, overlaps


def _boundary_scores(segments: list) -> list:
    """各行について「ここから新しい話題が始まる度合い」を返す(先頭は必ず切れ目)"""
    gap_hi, vocab_low, vocab_mid, overlaps = _thresholds(segments)
    scores = [0.0] * len(segments)
    for i, seg in enumerate(segments):
        if i == 0:
            scores[i] = 99.0
            continue
        score = 0.0
        gap = seg["start"] - segments[i - 1]["end"]
        if gap >= gap_hi:
            score += 1.0
        elif gap >= gap_hi / 2:
            score += 0.25
        text = seg["text"].lstrip("　 ")
        if text.startswith(CUE_WORDS) or NUMBERED_HEAD.match(text):
            score += 1.0
        if text.startswith(CONT_WORDS):
            score -= 1.0       # 接続詞や答え合わせは、前の話の続き
        if segments[i - 1]["text"].rstrip("　 ").endswith(QUESTION_ENDS):
            score -= 1.0       # 問いかけの直後は答えが続く
        if overlaps[i] is not None:
            if overlaps[i] <= vocab_low:
                score += 0.6
            elif overlaps[i] <= vocab_mid:
                score += 0.3
        scores[i] = score
    return scores


def split_topics(segments: list, scores: list = None, target: float = 30.0) -> list:
    """字幕を話題のまとまりに分ける。戻り値は行のリストのリスト。

    短すぎるまとまり(切り出したい長さに対して短いもの)は直前の話題につなげる。
    相づちひとつで話題が切れてしまうのを防ぐため。
    """
    if not segments:
        return []
    scores = _boundary_scores(segments) if scores is None else scores
    min_sec = max(TOPIC_MIN_FLOOR, target * TOPIC_MIN_RATIO)
    topics, current = [], []
    for i, seg in enumerate(segments):
        starts_topic = scores[i] >= TOPIC_SCORE
        if starts_topic and current:
            # 直前の話題が短すぎるなら切らずに続ける
            if current[-1]["end"] - current[0]["start"] >= min_sec:
                topics.append(current)
                current = []
        current.append(seg)
    if current:
        if topics and current[-1]["end"] - current[0]["start"] < min_sec:
            topics[-1].extend(current)     # 最後の切れ端は前の話題に含める
        else:
            topics.append(current)
    return topics


def _best_part_of(topic: list, segments, volumes, target: float, head: float = None) -> dict:
    """長い話題の中から、目安の長さに収まる一番盛り上がっている連続部分を選ぶ。

    切れ目は必ず字幕の行の境目なので、文の途中から始まったり途中で終わったりしない。
    """
    head = topic[0]["start"] if head is None else head
    best, best_rank = None, -1.0
    for i in range(len(topic)):
        for j in range(i + 1, len(topic) + 1):
            start, end = topic[i]["start"], topic[j - 1]["end"]
            dur = end - start
            if dur > target * PART_MAX:
                break
            if dur < target * PART_MIN:
                continue
            cand = _score_window(start, end, segments, volumes)
            # 話の入り(クイズの問題文など)が抜けないよう、頭から始まるものを優先する
            rank = cand["score"] - HEAD_BONUS * min(1.0, (start - head) / 45.0)
            if rank > best_rank:
                best, best_rank = cand, rank
    return best


def _lead_in(segments: list, head: int, scores: list, gap_hi: float,
             max_extra: float = 9.0, max_lines: int = 3) -> float:
    """話題の切れ目がはっきりしないときだけ、直前の数行を頭に足して話の入りを補う。

    「少し間が空いた」程度の弱い切れ目だと、前の話の続きであることが多いため。
    head は話題の先頭の行が segments の何番目か。
    """
    if head == 0 or scores[head] >= 1.6:
        return segments[head]["start"]    # はっきり話題が変わっているので足さない
    start = segments[head]["start"]
    for k in range(1, max_lines + 1):
        if head - k < 0:
            break
        prev = segments[head - k]
        if start - prev["end"] > gap_hi or segments[head]["start"] - prev["start"] > max_extra:
            break
        start = prev["start"]
    return start


def _topic_candidates(segments: list, volumes, target: float) -> list:
    """話題ごとに1つの候補を作る(短い話題は丸ごと、長い話題は中の盛り上がり部分)"""
    candidates = []
    scores = _boundary_scores(segments)
    gap_hi = _thresholds(segments)[0]
    head = 0
    for topic in split_topics(segments, scores, target):
        lead = _lead_in(segments, head, scores, gap_hi)
        head += len(topic)
        if lead < topic[0]["start"]:
            topic = [s for s in segments[:head - len(topic)] if s["start"] >= lead] + topic
        start, end = topic[0]["start"], topic[-1]["end"]
        whole = end - start
        if whole <= target * WHOLE_MAX:
            cand = _score_window(start, end, segments, volumes)
            cand["reason"] = (cand["reason"] + "、" if cand["reason"] else "") + "話題のまとまりを丸ごと切り出し"
            cand["topic"] = "whole"
        else:
            cand = _best_part_of(topic, segments, volumes, target, start)
            if cand is None:
                continue
            cand["reason"] = (cand["reason"] + "、" if cand["reason"] else "") + "長い話題の中で盛り上がっている部分"
            cand["topic"] = "part"
        cand["topic_start"] = round(start, 1)
        cand["topic_end"] = round(end, 1)
        candidates.append(cand)
    return candidates


def detect_highlights(wav_path: str, segments, out_dir: str,
                      clip_length: float = 30.0, n_clips: int = 5) -> list:
    """盛り上がり上位 n_clips 区間を返す(重複しないようにずらす)

    segments: 文字起こし結果のリスト。None の場合は音声のみで判定
    """
    print("      盛り上がり箇所を検出中...")
    volumes = _load_volumes(wav_path)
    duration = len(volumes)

    def sliding():
        """一定の長さの窓をずらして候補を作る(話題が分からないとき・本数が足りないとき)"""
        out, t = [], 0.0
        while t + clip_length <= duration:
            out.append(_score_window(t, t + clip_length, segments, volumes))
            t += 5.0
        return out or [_score_window(0, duration, segments, volumes)]

    candidates = []
    if segments:
        # 会話を話題に区切り、話題ごとに評価する(話の途中で切らないため)
        candidates = _topic_candidates(segments, volumes, clip_length)
        print(f"      話題のまとまり: {len(candidates)} 個")
    if not candidates:
        candidates = sliding()

    def take(pool, picked):
        """スコアの高い順に、すでに選んだ区間と重ならないものを取る"""
        for c in sorted(pool, key=lambda c: c["score"], reverse=True):
            if len(picked) >= n_clips:
                break
            if all(c["end"] <= p["start"] or c["start"] >= p["end"] for p in picked):
                picked.append(c)
        return picked

    picked = take(candidates, [])
    if len(picked) < n_clips and segments:
        # 話題が少ない短い動画では、従来どおり窓をずらした候補で本数を補う
        picked = take(sliding(), picked)
    picked.sort(key=lambda c: c["start"])

    json_path = Path(out_dir) / "highlights.json"
    json_path.write_text(json.dumps(picked, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    for i, p in enumerate(picked, 1):
        print(f"  候補{i}: {p['start']}s〜{p['end']}s (score {p['score']}) {p['reason']}")
    return picked
