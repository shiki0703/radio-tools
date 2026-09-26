"""③ 話題の見出し:いま何の話をしているかを短い文字にして、画面の左上に出す

話のまとまりごとに、そのまとまりに集中して出てくる言葉を拾い、
「常識クイズ」「ドーナツ」のような見出しにする。
細かさは3段階(話ごと / コーナーごと / 動画に1つ)から選べる。

形態素解析(辞書)は使わず、漢字・カタカナ・英数字のかたまりを語の候補として扱う。
ひらがなだけの語は助詞や言い回しが多いので候補にしない。
自動なので外すこともあるため、画面から手で直せるようにしてある。
"""
import math
import re
from collections import Counter

from src.highlight import split_topics

# 語の候補になる文字のかたまり(漢字2文字以上・カタカナ3文字以上・英数2文字以上)
_CHUNK = re.compile(r"[一-龥々〆ヶ]{2,}|[ァ-ヴー]{2,}|[A-Za-z0-9０-９]{2,}")
# どの動画にも出てきて内容を表さない語
_KANJI_ONLY = re.compile(r"[一-龥々〆ヶ]+")
_STOP = {"自分", "本当", "感じ", "今日", "昨日", "明日", "最近", "普通", "全然", "結構", "多分",
         "一番", "今回", "前回", "今年", "去年", "我々", "俺ら", "皆さん", "みんな", "内容", "話題",
         "大丈夫", "無理", "確か", "絶対", "全部", "everyone", "ホント", "ヤバ", "ヤバい", "マジ",
         "ソレ", "コレ", "ナニ", "ドレ", "アレ", "ココ", "ソコ", "ドコ", "イマ", "ミンナ"}

MAX_CHARS = 16      # 見出しの長さの上限(左上に置くので短く)

# 見出しの細かさ。(まとまりの最短, 最長, 動画の長さを割る数)から目安の長さを決める
#   fine  … 話ごと(1〜4分)
#   corner… コーナーごと(3〜6分。既定)
#   whole … 動画に1つだけ(その回のテーマ)
SCOPES = {
    "fine": (60.0, 150.0, 12),
    "corner": (120.0, 300.0, 6),
}
DEFAULT_SCOPE = "corner"
# この文字が後ろに来ていれば、その語は名詞として使われている(「ダンゴムシは」「服装の」)。
# 「電車乗って」のように動詞が混ざったかたまりを見分けるために使う。
_NOUN_AFTER = set("のはがをにでともやへ、。!?！?」)』 　")


def _chunks(text: str) -> list:
    """語の候補を取り出す。隣り合うかたまりは連結したものも候補にする(女性+ホルモン→女性ホルモン)"""
    out = []
    for line in text.split("\n"):
        found = [(m.group(), m.start(), m.end()) for m in _CHUNK.finditer(line)]
        for i, (word, _, end) in enumerate(found):
            out.append(word)
            nxt = found[i + 1] if i + 1 < len(found) else None
            if nxt and nxt[1] == end and len(word) <= 3 and len(nxt[0]) <= 3:
                out.append(word + nxt[0])     # すきまなく続く短いかたまり同士だけつなげる
    return out


def _counts(segments: list) -> Counter:
    return Counter(_chunks("\n".join(s["text"] for s in segments)))


def _looks_like_noun(word: str, text: str) -> bool:
    """その語が文の中で名詞として使われているか(後ろに助詞・句読点・行末が来るか)"""
    start = 0
    while True:
        i = text.find(word, start)
        if i < 0:
            return False
        after = text[i + len(word):i + len(word) + 1]
        if after in ("", "\n") or after in _NOUN_AFTER:
            return True
        start = i + 1


def _ranked(counts: Counter, spread: Counter, n_topics: int, min_hits: int, text: str = "") -> list:
    """そのまとまりらしい語を、それらしい順に返す。

    spread は「その語がいくつのまとまりに出てくるか」。
    出演者の名前や口ぐせのように動画中へ散らばる語は下げ、
    ひとつのまとまりに集中する語(=その話の主題)を上げる。
    """
    scored = []
    for word, n in counts.items():
        if n < min_hits or word in _STOP:
            continue
        if any(stop in word for stop in _STOP):
            continue        # 「結構多分真剣」のような、言い回しが混ざったかたまりは使わない
        if text and not _looks_like_noun(word, text):
            continue        # 「電車乗(って)」のように動詞が混ざったかたまりは見出しにしない
        focus = math.log(max(n_topics, 2) / (spread.get(word, 0) + 0.5))
        if focus <= 0:
            continue        # どのまとまりにも出てくる語は主題にしない
        scored.append((n * focus * (len(word) ** 0.4), word))
    scored.sort(reverse=True)
    return [w for _, w in scored]


def topic_title(topic: list, spread: Counter, n_topics: int,
                limit: int = MAX_CHARS, fallback: str = "") -> str:
    """そのまとまりが何の話かを1語でとらえ、「◯◯の話」の形にして返す。

    そのまとまりに集中して出てくる語を主題とみなす。
    主題が見つからないときは fallback(動画全体の主題)を使う。
    """
    text = "\n".join(s["text"] for s in topic)
    counts = _counts(topic)
    words = _ranked(counts, spread, n_topics, 2, text)
    # 2回以上出る語がなければ、1回だけの語も見る(カタカナ語と2文字の漢語のみ)
    words += [w for w in _ranked(counts, spread, n_topics, 1, text)
              if w not in words and (not _KANJI_ONLY.fullmatch(w) or len(w) <= 2)]
    return next((w for w in words if len(w) <= limit), "") or fallback


def overall_subject(segments: list, limit: int = MAX_CHARS) -> str:
    """動画全体の主題(「常識クイズ」のような、その回のテーマ)を返す"""
    whole_text = "\n".join(s["text"] for s in segments)
    # 全体に何度も出てくる語ほどテーマに近い。長い語(「常識クイズ」)を「常識」より優先する
    ranked = sorted(((n * (len(w) ** 1.2), w) for w, n in _counts(segments).items()
                     if w not in _STOP and not any(stop in w for stop in _STOP)
                     and n >= 3 and _looks_like_noun(w, whole_text)), reverse=True)
    return next((w for _, w in ranked if len(w) <= limit), "")


def make_topic_titles(segments: list, scope: str = DEFAULT_SCOPE) -> list:
    """字幕から、話のまとまりごとの見出しを作る。

    scope … "fine"(話ごと)/ "corner"(コーナーごと)/ "whole"(動画に1つ)
    Returns: [{"start": 開始秒, "end": 終了秒, "text": 見出し}, ...]
             見出しになる言葉が見つからないまとまりは含めない。
    """
    if not segments:
        return []
    fallback = overall_subject(segments)
    if scope == "whole":
        return ([{"start": round(segments[0]["start"], 2), "end": round(segments[-1]["end"], 2),
                  "text": fallback}] if fallback else [])

    low, high, divisor = SCOPES.get(scope, SCOPES[DEFAULT_SCOPE])
    duration = segments[-1]["end"] - segments[0]["start"]
    topics = split_topics(segments, None, min(high, max(low, duration / divisor)))
    # 語ごとに「いくつのまとまりに出てくるか」を数える
    spread = Counter()
    for topic in topics:
        spread.update(set(_counts(topic)))
    titles = []
    for topic in topics:
        text = topic_title(topic, spread, len(topics), fallback=fallback)
        if text:
            titles.append({"start": round(topic[0]["start"], 2),
                           "end": round(topic[-1]["end"], 2), "text": text})
    return titles
