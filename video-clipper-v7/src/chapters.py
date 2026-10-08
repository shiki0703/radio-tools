"""⑤ 概要欄のチャプター:YouTube の概要欄に貼る「主なトピック」と時刻つきの見出しを作る

作り方は3つあり、どれも同じ形(topics / chapters)にそろえて返す。
  ・貼り付け … 依頼文をコピーして ChatGPT や claude.ai に貼り、返事を貼り戻す(ふだんはこれ)
  ・下書き … 話題の見出し(title.py)をそのまま並べる。AI を使わない
  ・AI     … 文字起こしを Claude に渡して、要約した見出しを作ってもらう(API キーが必要)

見出しは「本編の時刻」で持ち、画面と概要欄の文では、そのとき前後につないである
オープニング・エンディングを含めた時刻にする(timeline)。あとから付け替えても、ずれない。
YouTube はチャプターを「0:00 から始まる・3つ以上・それぞれ10秒以上」のときだけ作るので、
出す前にその形にそろえる。
"""
import json
import re

import unicodedata

from src.title import MAX_CHARS as LABEL_MAX, make_topic_titles

MODEL = "claude-opus-5-5"
MIN_GAP = 10.0          # YouTube が1つのチャプターに求める最短の長さ(秒)
MIN_COUNT = 3           # YouTube がチャプターを作るのに必要な数
BLOCK = 10.0            # AI に渡す文字起こしを、この秒数ずつまとめる(時刻の書き込みを減らす)
SNAP = 15.0             # AI が返した時刻を、近くの行の始まりにそろえる範囲(秒)
TITLE_MAX = 80
# 左上の見出しの長さ。LABEL_MAX(16)までなら、焼き込んだときに必ず1行に収まる。
# AI には余裕を持たせて、それより短く頼む(数え間違えても収まるように)
LABEL_ASK = 12

INSTRUCTIONS = """あなたはラジオ番組・トーク動画の編集担当です。下の文字起こしから、YouTube の概要欄に載せる「主なトピック」と「チャプター」を作ってください。

文字起こしについて
- 音声から自動で起こしたものなので、聞き間違い(固有名詞や言い回し)が混ざっています。前後から明らかなときは正しい言葉で書き、自信がない固有名詞は使わないでください。
- 各行の先頭の [分:秒] は、その行が話され始めた時刻です。

主なトピック
- この回で話していることを、短い名詞句で 6〜12 個。
- 見た人が「何の回か」を一目でつかめるように、具体的に書く。

チャプター
- 話題が切り替わるところで区切る。1つの長さは目安 1〜5 分。細かくしすぎず、話のまとまりを優先する。
- 1つ目は必ず 0:00 から始める。冒頭のあいさつや告知は、1つ目のチャプターにまとめてよい。
- 時刻は文字起こしにある [分:秒] の中から選ぶ(新しく作らない)。
- 見出しは1行、15〜30 字程度。何の話かが具体的に分かる言葉にする。「〜について」「トーク」のような、ぼんやりした言葉は避ける。
- 番組の雰囲気に合った言葉づかいにする。大げさな表現や、話していない内容は書かない。

左上の見出し
- チャプターごとに、動画の画面の左上に出す短い見出しも作る。いま何の話をしているかが一目で分かる言葉。
- 必ず12文字以内にする(全角で数える。英数字・記号は2文字で1文字)。画面の幅が決まっていて、超えると表示が崩れる。
- 「…」で省略したり、途中で切ったりしない。長くなりそうなときは言い換えて、12文字以内で言い切る形にする。
  悪い例:「吉見リクエスト「理科の常識クイ…」 良い例:「理科の常識クイズ」
- 「第1問」のような番号だけにせず、中身が分かる言葉にする(例:「クモの巣の秘密」)。かぎかっこは使わない。
- 書き終えたら、左上の見出しの文字数を1つずつ数え、12文字を超えていたら言い換えてから答える。"""

PASTE_FORMAT = """答え方
次の形だけで答えてください(前置きや説明は要りません)。

▼主なトピック
トピック / トピック / トピック

0:00 - 見出し【左上の見出し】
2:51 - 見出し【左上の見出し】

※【】の中は、必ず12文字以内(「…」で省略しない)。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "description": "主なトピック。短い名詞句",
            "items": {"type": "string"},
        },
        "chapters": {
            "type": "array",
            "description": "チャプター。時間の順。1つ目は 0:00",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "文字起こしにある [分:秒] の時刻。例 12:34"},
                    "title": {"type": "string", "description": "1行の見出し"},
                    "label": {"type": "string", "description": "画面の左上に出す短い見出し。必ず12文字以内(全角で数える)。「…」で省略せず、言い換えて収める"},
                },
                "required": ["start", "title", "label"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topics", "chapters"],
    "additionalProperties": False,
}


class ChapterError(Exception):
    """利用者に見せる説明つきの失敗"""


# ---------- 時刻 ----------

def clock(seconds: float, long: bool = True) -> str:
    """秒 → 00:02:51(long=False なら 2:51)"""
    s = max(0, int(round(seconds)))
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    if long:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def parse_clock(text) -> float | None:
    """「2:51」「02:51」「0:02:51」「171」を秒にする。読めなければ None"""
    if isinstance(text, (int, float)):
        return float(text) if text >= 0 else None
    parts = str(text or "").strip().replace("：", ":").split(":")
    if not parts or not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
        return None
    value = 0
    for p in parts:
        value = value * 60 + int(p)
    return float(value)


# ---------- AI に渡す文字起こし ----------

def transcript_text(segments: list) -> str:
    """行を BLOCK 秒ずつまとめ、先頭に [分:秒] を付けた文字起こし"""
    lines, current, start = [], [], None
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        if start is None:
            start = seg["start"]
        current.append(text)
        if seg["end"] - start >= BLOCK:
            lines.append(f"[{clock(start, long=False)}] " + " ".join(current))
            current, start = [], None
    if current:
        lines.append(f"[{clock(start, long=False)}] " + " ".join(current))
    return "\n".join(lines)


def paste_prompt(segments: list) -> str:
    """ChatGPT や claude.ai に貼る依頼文(指示 + 答え方 + 文字起こし)"""
    return f"{INSTRUCTIONS}\n\n{PASTE_FORMAT}\n\n--- 文字起こし ---\n{transcript_text(segments)}"


# ---------- 返事を読む ----------

_LINE = re.compile(r"^\s*(?:[-・*>]\s*)?\[?((?:\d{1,2}:)?\d{1,2}:\d{2})\]?\s*(?:[-–—:：|｜]\s*)?(.+?)\s*$")
_TOPIC_HEAD = re.compile(r"主な?トピック|トピック")
_MARKUP = re.compile(r"\*\*|__|`")                                   # 太字・コードの記号
_BULLET = re.compile(r"^(?:#{1,6}\s*|>\s*|[-・*•]\s+|\d{1,2}[.)．]\s+)")  # 見出し・引用・箇条書き・番号
_SECTION = re.compile(r"^[▼■◆●【]|[:：]$|チャプター|タイムスタンプ|目次")


def _split_label(text: str) -> tuple:
    """「見出し【短い見出し】」を (見出し, 短い見出し) に分ける"""
    m = re.match(r"^(.*?)\s*[【\[]([^【】\[\]]{1,60})[】\]]\s*$", text)
    return (m.group(1).strip(), m.group(2).strip()) if m and m.group(1).strip() else (text.strip(), "")


_NUMBERING = re.compile(r"^(?:第\s*\d+\s*[問回話章部]|[QＱ問]\s*\d+)[\s:：.、。]*")


def label_width(text: str) -> float:
    """左上の見出しの長さ(全角を1、英数字などの半角を0.5で数える)"""
    return sum(1.0 if unicodedata.east_asian_width(ch) in "WFA" else 0.5 for ch in str(text or ""))


def label_too_long(text: str) -> bool:
    return label_width(text) > LABEL_MAX


def short_label(title: str, limit: int = LABEL_MAX) -> str:
    """左上に出す短い見出しが無いときに、見出しから作る。

    区切りのよいところで収まらなければ、切らずにそのまま返す(画面で「長すぎる」と知らせて直してもらう)。
    言葉の途中で切ると意味が通らなくなるため。
    """
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if label_width(title) <= limit:
        return title
    # 「第1問 」のような番号は、短くすると番号だけ残ってしまうので先に外す
    title = _NUMBERING.sub("", title) or title
    if label_width(title) <= limit:
        return title
    for sep in ("、", "。", "！", "？", "!", "?", "：", ":", " "):
        head = title.split(sep)[0].strip()
        if 2 <= label_width(head) <= limit:
            return head
    return title


def parse_reply(text: str) -> dict:
    """AI の返事(貼り付けられた文)を読む。JSON でも、決めた文の形でも読める"""
    text = str(text or "").strip()
    if not text:
        raise ChapterError("貼り付けられた文が空です。")
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return {"topics": [str(t) for t in data.get("topics", [])],
                    "chapters": [{"start": parse_clock(c.get("start")), "title": str(c.get("title", "")),
                                  "label": str(c.get("label", ""))}
                                 for c in data.get("chapters", []) if isinstance(c, dict)]}
        except (ValueError, AttributeError):
            pass

    topics, chapters, in_topics = [], [], False
    for raw in text.splitlines():
        # ChatGPT などは太字・見出し・箇条書きの記号を付けて返すことが多いので、先に外す
        line = _BULLET.sub("", _MARKUP.sub("", raw).strip()).strip()
        if not line:
            continue                 # 空行ではトピックの区切りを終わらせない(見出しの直後に空行が入ることがある)
        m = _LINE.match(line)
        if m:
            title, label = _split_label(m.group(2))
            chapters.append({"start": parse_clock(m.group(1)), "title": title, "label": label})
            in_topics = False
            continue
        if _TOPIC_HEAD.search(line) and len(line) <= 20:
            in_topics = True         # 「▼主なトピック」のあとの行がトピック(1行でも箇条書きでも)
            continue
        if _SECTION.search(line):
            in_topics = False        # 「▼チャプター」など、別のまとまりの見出し
            continue
        if in_topics or (not chapters and not topics and " / " in line):
            topics += [t.strip() for t in re.split(r"\s*[/／]\s*", line) if t.strip()]
    if not chapters:
        raise ChapterError("時刻つきの見出しが見つかりませんでした。「2:51 - 見出し」の形の行がある返事を貼ってください。")
    return {"topics": topics, "chapters": chapters}


# ---------- 形をそろえる ----------
#
# 見出しは「本編の時刻」で持つ。オープニング・エンディングの長さは後から変わることがあるので、
# 画面に出すときや概要欄の文を作るときに、その時点の長さを足して「できあがった動画の時刻」にする。

INTRO_TITLE = "オープニング"
OUTRO_TITLE = "エンディング"


def normalize(raw: dict, segments: list, body: float) -> dict:
    """AI や手で作った案を、本編の時刻のまま形をそろえる(1つ目は本編の 0:00、近すぎる区切りはまとめる)。

    body は本編の長さ。
    """
    starts = [s["start"] for s in segments]
    items = []
    for c in raw.get("chapters", []):
        t = parse_clock(c.get("start"))
        title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()[:TITLE_MAX]
        if t is None or not title:
            continue
        label = re.sub(r"\s+", " ", str(c.get("label") or "")).strip()
        label = label or short_label(title)        # 長すぎても切らない(画面で知らせる)
        near = min(starts, key=lambda s: abs(s - t)) if starts else t
        if abs(near - t) <= SNAP:
            t = near                 # 行の始まりにそろえる(話の途中から始まらないように)
        items.append([t, title, label])
    items.sort(key=lambda x: x[0])
    if items:
        items[0][0] = 0.0            # 本編の頭から始める
    kept = []
    for t, title, label in items:
        if t >= body > 0:
            continue
        if kept and t - kept[-1][0] < MIN_GAP:
            continue                 # 近すぎる区切りは前のものに含める
        kept.append([t, title, label])
    topics = [re.sub(r"\s+", " ", str(t)).strip() for t in raw.get("topics", [])]
    return {"topics": [t for t in topics if t][:20],
            "chapters": [{"start": round(t, 2), "title": title, "label": label} for t, title, label in kept]}


def timeline(data: dict, intro: float, body: float, outro: float) -> list:
    """本編の時刻で持っている見出しを、できあがった動画(オープニング・エンディング込み)の時刻の並びにする。

    オープニング・エンディングが10秒以上あれば、それぞれを1つのチャプターにする
    (名前は data の intro_title / outro_title。空なら出さない)。
    10秒より短いオープニングは、本編の1つ目のチャプターに含める(0:00 から始める)。
    """
    rows = []
    intro_title = data.get("intro_title", INTRO_TITLE)
    outro_title = data.get("outro_title", OUTRO_TITLE)
    if intro >= MIN_GAP and intro_title:
        rows.append({"start": 0.0, "title": intro_title, "label": "", "kind": "intro"})
    for c in data.get("chapters", []):
        start = round(float(c["start"]) + intro, 2)
        if not rows:
            start = 0.0
        rows.append({"start": start, "title": c["title"], "label": c.get("label", ""), "kind": "body"})
    if outro >= MIN_GAP and outro_title and body > 0:
        rows.append({"start": round(intro + body, 2), "title": outro_title, "label": "", "kind": "outro"})
    return rows


def from_timeline(rows: list, intro: float, body: float, outro: float, before: dict = None) -> dict:
    """画面で直した並び(できあがった動画の時刻)を、本編の時刻に戻す"""
    before = before or {}
    chapters, intro_title, outro_title = [], None, None
    for c in rows:
        if not isinstance(c, dict):
            continue
        title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()[:TITLE_MAX]
        kind = c.get("kind") or "body"
        if kind == "intro":
            intro_title = title
            continue
        if kind == "outro":
            outro_title = title
            continue
        t = parse_clock(c.get("start"))
        if t is None:
            continue
        t = max(0.0, t - intro)
        if body > 0 and t >= body:
            continue
        chapters.append({"start": round(t, 2), "title": title,
                         "label": re.sub(r"\s+", " ", str(c.get("label") or "")).strip()[:TITLE_MAX]})
    chapters.sort(key=lambda c: c["start"])
    if chapters:
        chapters[0]["start"] = 0.0
    out = {"chapters": chapters}
    # 画面に出ていたのに消された → 空(出さない)。そもそも出ていなかった → 前のまま
    out["intro_title"] = intro_title if intro_title is not None else (
        "" if intro >= MIN_GAP else before.get("intro_title", INTRO_TITLE))
    out["outro_title"] = outro_title if outro_title is not None else (
        "" if outro >= MIN_GAP else before.get("outro_title", OUTRO_TITLE))
    return out


def problems(chapters: list) -> list:
    """YouTube がチャプターとして扱わない形になっていないか"""
    out = []
    if len(chapters) < MIN_COUNT:
        out.append(f"YouTube は見出しが{MIN_COUNT}つ以上ないとチャプターにしません(いま{len(chapters)}つ)。")
    if chapters and chapters[0]["start"] != 0:
        out.append("1つ目の見出しは 00:00:00 にしてください。")
    for a, b in zip(chapters, chapters[1:]):
        if b["start"] <= a["start"]:
            out.append(f"「{b['title']}」の時刻が、前の見出しより前になっています。")
        elif b["start"] - a["start"] < MIN_GAP:
            out.append(f"「{a['title']}」が{MIN_GAP:.0f}秒より短くなっています。")
    if any(not c["title"].strip() for c in chapters):
        out.append("見出しが空の行があります。")
    for c in chapters:
        label = str(c.get("label") or "")
        if c.get("kind", "body") == "body" and label_too_long(label):
            out.append(f"左上の見出し「{label}」が長すぎます({label_width(label):g}文字)。"
                       f"{LABEL_MAX}文字以内に直すと1行に収まります。")
    return out


def description(topics: list, chapters: list) -> str:
    """概要欄にそのまま貼れる文(chapters はできあがった動画の時刻)"""
    parts = []
    if topics:
        parts.append("▼主なトピック\n" + " / ".join(topics))
    rows = [c for c in chapters if str(c.get("title") or "").strip()]
    if rows:
        parts.append("\n".join(f"{clock(c['start'])} - {c['title']}" for c in rows))
    return "\n\n".join(parts)


def overlay_titles(chapters: list, body_end: float) -> list:
    """本編の時刻の見出しを、左上に焼き込む見出しにする(オープニング・エンディングには出さない)"""
    rows = [c for c in chapters if str(c.get("title") or "").strip()]
    out = []
    for i, c in enumerate(rows):
        start = max(0.0, float(c["start"]))
        end = float(rows[i + 1]["start"]) if i + 1 < len(rows) else body_end
        text = str(c.get("label") or "").strip() or short_label(c["title"])
        if end - start >= 1.0 and text:
            out.append({"start": round(start, 2), "end": round(min(end, body_end), 2), "text": text})
    return out


# ---------- 下書き(AI を使わない) ----------

def draft(segments: list, titles: list, body: float) -> dict:
    """話題の見出しをそのまま並べた下書き(本編の時刻)"""
    titles = [t for t in (titles or []) if str(t.get("text") or "").strip()] or make_topic_titles(segments, "corner")
    chapters = [{"start": t["start"], "title": t["text"], "label": t["text"]} for t in titles]
    if chapters and chapters[0]["start"] >= 60:
        # 最初の見出しまでが長いときだけ、冒頭を別のチャプターにする(短ければ1つ目を 0:00 に寄せる)
        chapters.insert(0, {"start": 0, "title": "はじまり", "label": "はじまり"})
    seen, topics = set(), []
    for c in chapters[1:]:
        if c["title"] not in seen:
            seen.add(c["title"])
            topics.append(c["title"])
    return normalize({"topics": topics[:12], "chapters": chapters}, segments, body)


# ---------- AI ----------

def ask_claude(segments: list, api_key: str, client=None) -> dict:
    """文字起こしを Claude に渡して、トピックとチャプターの案をもらう(本編の時刻のまま返す)。

    送るのは文字起こしの文字だけ。動画や音声は送らない。
    """
    data = call_claude(INSTRUCTIONS, "--- 文字起こし ---\n" + transcript_text(segments), SCHEMA, api_key, client)
    return {"topics": data.get("topics", []), "chapters": data.get("chapters", [])}


def call_claude(system: str, text: str, schema: dict, api_key: str, client=None) -> dict:
    """Claude に頼んで、schema の形の JSON をもらう(失敗は利用者に見せる説明つきの ChapterError)"""
    try:
        import anthropic
    except ImportError as exc:
        raise ChapterError("AI を使うための部品が入っていません。一式フォルダの「はじめる」を一度実行してください(自動で入ります)。") from exc

    client = client or anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=300.0)
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",            # 断られたときは、別のモデルで自動でやり直してもらう
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.AuthenticationError as exc:
        raise ChapterError("API キーが正しくないようです。「API キーの設定」で入れ直してください。") from exc
    except anthropic.PermissionDeniedError as exc:
        raise ChapterError("この API キーでは使えませんでした。キーの権限を確かめてください。") from exc
    except anthropic.RateLimitError as exc:
        raise ChapterError("いま混み合っています。1分ほど待ってから、もう一度お試しください。") from exc
    except anthropic.BadRequestError as exc:
        if "credit" in str(exc).lower():
            raise ChapterError("API の残高が足りません。Claude Console(console.anthropic.com)で残高を追加してください。") from exc
        raise ChapterError(f"AI に断られました({exc.message})。貼り付け方式をお試しください。") from exc
    except anthropic.APIConnectionError as exc:
        raise ChapterError("インターネットにつながっていないようです。つながっているか確かめてください。") from exc
    except anthropic.APIStatusError as exc:
        raise ChapterError(f"AI 側で問題が起きました({exc.status_code})。少し待ってから、もう一度お試しください。") from exc

    if response.stop_reason == "refusal":
        raise ChapterError("AI が答えませんでした。貼り付け方式をお試しください。")
    if response.stop_reason == "max_tokens":
        raise ChapterError("AI の返事が途中で切れました。もう一度お試しください。")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ChapterError("AI の返事を読み取れませんでした。もう一度お試しください。") from exc
    if not isinstance(data, dict):
        raise ChapterError("AI の返事を読み取れませんでした。もう一度お試しください。")
    return data
