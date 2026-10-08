"""テロップの直し:誤字の候補(AI)と、直し方の辞書

・誤字の候補 … テロップに行番号を付けて AI に渡し、「行番号: 誤 → 正」の形で候補を返してもらう。
               依頼文を ChatGPT などに貼る方法と、API キーで Claude に頼む方法がある。
               どれを採用するかは画面で選ぶ(ここでは読み取るだけで、テロップは直さない)。
・辞書       … 一度直した言い方(誤 → 正)を覚えておき、次の文字起こしのあとに自動で直す。
               正しい言い方は、文字起こし(Whisper)にも「出てきやすい言葉」として渡す。
"""
import json
import re
from pathlib import Path

from src.chapters import ChapterError, call_claude

MAX_ENTRIES = 500       # 辞書に覚えておく数の上限
WORD_MAX = 40           # 1つの言い方の長さの上限
HOTWORDS_MAX = 200      # 文字起こしに渡す言葉の長さの合計(長すぎると認識が乱れる)

TYPO_INSTRUCTIONS = """あなたはラジオ番組・トーク動画のテロップの校正担当です。下は音声から自動で起こしたテロップです。音声の聞き間違いや変換の誤り(誤字・同音異義語・固有名詞の表記)を探してください。

探すもの
- 前後の話から、明らかに違う言葉になっているところ(例:「機械が無い」→「機会が無い」)。
- 人名・地名・作品名などの固有名詞の書き間違い。番組の中で何度も出てくる名前は、表記をそろえる。

探さないもの
- 話し言葉の言い回し・口ぐせ・言いよどみ・文法の崩れ(話したとおりに出すテロップなので、直さない)。
- 句読点や「!」「?」の付け方、ひらがな・カタカナ・漢字の好みの違い。
- 自信がないもの。前後から誤りだと言い切れるものだけを出す。

行番号は、各行の先頭の [番号] です。
「誤」には、その行にある文字を、そのまま短く(直すところだけ)書いてください。"""

TYPO_FORMAT = """答え方
次の形だけで、1行に1つずつ答えてください(前置きや説明は要りません)。※のあとに短い理由を書いてください。
見つからなければ「なし」とだけ答えてください。

12: 誤 → 正 ※理由
48: 誤 → 正 ※理由"""

TYPO_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "description": "誤字の候補。行番号の順",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer", "description": "テロップの行番号([番号] の数字)"},
                    "wrong": {"type": "string", "description": "その行にある誤った文字(そのまま、短く)"},
                    "right": {"type": "string", "description": "正しい文字"},
                    "reason": {"type": "string", "description": "短い理由"},
                },
                "required": ["line", "wrong", "right", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fixes"],
    "additionalProperties": False,
}


# ---------- 誤字の候補 ----------

def numbered(lines: list) -> str:
    """テロップの各行に [行番号] を付ける(番号は画面の行の順。空の行は出さないが番号は飛ばす)"""
    return "\n".join(f"[{i}] {str(t).strip()}" for i, t in enumerate(lines, 1) if str(t or "").strip())


def typo_prompt(lines: list) -> str:
    """ChatGPT や claude.ai に貼る依頼文"""
    return f"{TYPO_INSTRUCTIONS}\n\n{TYPO_FORMAT}\n\n--- テロップ ---\n{numbered(lines)}"


_ARROW = r"(?:→|->|⇒|=>|➡|＞|>)"
_TYPO_LINE = re.compile(r"^\s*(?:[-・*•]\s*)?(?:行\s*)?[\[［(（]?\s*(\d{1,5})\s*[\]］)）]?\s*(?:行目?)?\s*[:：.、]?\s*(.+?)\s*"
                        + _ARROW + r"\s*(.+?)\s*$")
_QUOTES = "「」『』\"'“”`"


def _clean_word(text: str) -> str:
    text = re.sub(r"\*\*|__", "", str(text or "")).strip()
    return text.strip(_QUOTES).strip()


def clean_fixes(rows, count: int = 0) -> list:
    """候補の形をそろえる(同じものは1つに。行番号が範囲の外のもの・誤と正が同じものは捨てる)"""
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        try:
            line = int(r.get("line"))
        except (TypeError, ValueError):
            continue
        wrong, right = _clean_word(r.get("wrong"))[:WORD_MAX * 2], _clean_word(r.get("right"))[:WORD_MAX * 2]
        if line < 1 or (count and line > count) or not wrong or wrong == right:
            continue
        if (line, wrong, right) in seen:
            continue
        seen.add((line, wrong, right))
        out.append({"line": line, "wrong": wrong, "right": right,
                    "reason": re.sub(r"\s+", " ", str(r.get("reason") or "")).strip()[:120]})
    out.sort(key=lambda r: r["line"])
    return out


def parse_typos(text: str, count: int = 0) -> list:
    """AI の返事(貼り付けられた文)を読む。JSON でも、決めた文の形でも読める"""
    text = str(text or "").strip()
    if not text:
        raise ChapterError("貼り付けられた文が空です。")
    if text.startswith("{"):
        try:
            return clean_fixes(json.loads(text).get("fixes"), count)
        except (ValueError, AttributeError):
            pass
    rows = []
    for raw in text.splitlines():
        m = _TYPO_LINE.match(re.sub(r"\*\*|__|`", "", raw))   # 太字などの記号を外してから読む
        if not m:
            continue
        # 「正 ※理由」「正 (理由: …)」のように、理由が後ろに付いていることがある
        parts = re.split(r"\s*(?:※|#|[(（]理由[:：]?)\s*", m.group(3), maxsplit=1)
        reason = parts[1].rstrip(")） ") if len(parts) > 1 else ""
        rows.append({"line": m.group(1), "wrong": m.group(2), "right": parts[0], "reason": reason})
    fixes = clean_fixes(rows, count)
    if not fixes and not re.search(r"なし|ありません|見つかりません", text):
        raise ChapterError("「12: 誤 → 正」の形の行が見つかりませんでした。AI の返事をまるごと貼ってください。")
    return fixes


def ask_typos(lines: list, api_key: str, client=None) -> list:
    """テロップを Claude に渡して、誤字の候補をもらう(送るのはテロップの文字だけ)"""
    data = call_claude(TYPO_INSTRUCTIONS, "--- テロップ ---\n" + numbered(lines), TYPO_SCHEMA, api_key, client)
    return clean_fixes(data.get("fixes"), len(lines))


# ---------- 直し方の辞書 ----------

def clean_entries(rows) -> list:
    """辞書の形をそろえる([{"from": 誤, "to": 正}]。同じ「誤」はあとのものを使う)"""
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        wrong = re.sub(r"\s+", " ", str(r.get("from") or "")).strip()[:WORD_MAX]
        right = re.sub(r"\s+", " ", str(r.get("to") or "")).strip()[:WORD_MAX]
        if wrong and wrong != right:
            out.pop(wrong, None)
            out[wrong] = right
    return [{"from": k, "to": v} for k, v in list(out.items())[-MAX_ENTRIES:]]


def load_dict(path) -> list:
    try:
        return clean_entries(json.loads(Path(path).read_text(encoding="utf-8")).get("entries"))
    except (OSError, ValueError, AttributeError):
        return []


def apply_dict(segments: list, entries: list) -> tuple:
    """辞書のとおりに直したテロップと、直した数を返す(長い言い方から先に直す)"""
    entries = sorted(clean_entries(entries), key=lambda e: -len(e["from"]))
    out, count = [], 0
    for seg in segments:
        text = str(seg.get("text") or "")
        for e in entries:
            if e["from"] in text:
                count += text.count(e["from"])
                text = text.replace(e["from"], e["to"])
        out.append({**seg, "text": text})
    return out, count


def hotwords(entries: list) -> str:
    """文字起こしに「出てきやすい言葉」として渡す、正しい言い方(新しく覚えたものから)"""
    words, total = [], 0
    for e in reversed(clean_entries(entries)):
        word = e["to"]
        if not word or word in words or total + len(word) > HOTWORDS_MAX:
            continue
        words.append(word)
        total += len(word) + 1
    return " ".join(words)
