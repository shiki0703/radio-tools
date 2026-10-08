# -*- coding: utf-8 -*-
"""重要ロジックの単体テスト(外部ツール不要で実行できる範囲)

実行方法: python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import re
from src.subtitle import (build_style, build_title_style, normalize_style, _ass_color,
                          DEFAULT_STYLE, FONT_MAP, FONT_LABELS, PLAY_RES_Y)

# 実フォント名はOSごとに変わるため、テストは対応表を参照して比較する
GOTHIC = FONT_MAP["gothic"]
from src.clip import _clip_segments
from src.preprocess import quality_args
from src.highlight import split_topics
from src.title import make_topic_titles, MAX_CHARS as MAX_TITLE_CHARS
from src.transcribe import (align_to_words, split_long_segments, strip_speaker_labels,
                            TAIL_SEC, MIN_DUR_SEC, MIN_GAP_SEC, MAX_DUR_SEC, MAX_CHARS)


class TestAlignToWords(unittest.TestCase):
    """テロップ時刻を発話(単語タイムスタンプ)に合わせる処理"""

    def test_start_snaps_to_first_word(self):
        # Whisperのセグメントが発話より0.6秒早く始まっているケース
        raw = [{"start": 1.0, "end": 4.0, "text": "こんにちは",
                "words": [(1.6, 2.2), (2.3, 3.8)]}]
        out = align_to_words(raw, lead=0)
        self.assertEqual(out[0]["start"], 1.6)   # 音声より先に表示しない

    def test_end_is_last_word_plus_tail(self):
        raw = [{"start": 0.0, "end": 6.0, "text": "テスト",
                "words": [(0.5, 1.0), (1.2, 4.0)]}]
        out = align_to_words(raw, lead=0)
        self.assertAlmostEqual(out[0]["end"], 4.0 + TAIL_SEC, places=2)

    def test_min_duration_for_short_word(self):
        # 一瞬の相づちでも読める長さまで表示を延ばす
        raw = [{"start": 2.0, "end": 2.3, "text": "はい",
                "words": [(2.0, 2.3)]}]
        out = align_to_words(raw, lead=0)
        self.assertAlmostEqual(out[0]["end"] - out[0]["start"], MIN_DUR_SEC, places=2)

    def test_tail_does_not_cross_next_speech(self):
        # 発話が連続する場合、余韻が次のテロップ開始を跨がない
        raw = [
            {"start": 0.0, "end": 2.0, "text": "a", "words": [(0.2, 1.95)]},
            {"start": 2.0, "end": 4.0, "text": "b", "words": [(2.0, 3.5)]},
        ]
        out = align_to_words(raw, lead=0)
        self.assertLessEqual(out[0]["end"], out[1]["start"] - MIN_GAP_SEC + 0.001)

    def test_silence_gap_keeps_next_at_speech_start(self):
        # 長い無音のあとの次のテロップは、発話開始まで表示されない
        raw = [
            {"start": 0.0, "end": 2.0, "text": "a", "words": [(0.2, 1.8)]},
            {"start": 8.0, "end": 12.0, "text": "b", "words": [(9.1, 11.0)]},
        ]
        out = align_to_words(raw, lead=0)
        self.assertAlmostEqual(out[0]["end"], 1.8 + TAIL_SEC, places=2)  # 無音へ間延びしない
        self.assertEqual(out[1]["start"], 9.1)                            # 先行表示しない

    def test_fallback_without_words(self):
        # 単語情報が取れなかった区間は従来のセグメント時刻を使う
        raw = [{"start": 1.0, "end": 3.0, "text": "a", "words": []}]
        out = align_to_words(raw, lead=0)
        self.assertEqual(out[0]["start"], 1.0)
        self.assertAlmostEqual(out[0]["end"], 3.0 + TAIL_SEC, places=2)

    def test_no_overlaps(self):
        raw = [
            {"start": 0.0, "end": 1.0, "text": "a", "words": [(0.1, 0.9)]},
            {"start": 1.0, "end": 1.4, "text": "b", "words": [(1.0, 1.3)]},
            {"start": 1.5, "end": 3.0, "text": "c", "words": [(1.5, 2.8)]},
        ]
        out = align_to_words(raw, lead=0)
        for a, b in zip(out, out[1:]):
            self.assertLessEqual(a["end"], b["start"])


class TestSplitLongSegments(unittest.TestCase):
    """長すぎるテロップを単語の切れ目で分ける処理"""

    def words(self, pairs, t0=0.0, step=0.4, gaps=None):
        """[(文字, 直前の無音)] から単語リストを作る"""
        out, t = [], t0
        for i, (w, gap) in enumerate(pairs):
            t += gap
            out.append((round(t, 2), round(t + step, 2), w))
            t += step
        return out

    def seg(self, words):
        return {"start": words[0][0], "end": words[-1][1],
                "text": "".join(w[2] for w in words).strip(), "words": words}

    def test_short_segment_is_kept(self):
        seg = self.seg(self.words([("はい", 0), ("そうですね", 0)]))
        self.assertEqual(split_long_segments([seg]), [seg])

    def test_long_segment_is_split(self):
        words = self.words([("これは", 0), ("とても長い", 0), ("テロップで", 0), ("読みきれません", 0),
                            ("だから", 0.7), ("分けます", 0)])
        out = split_long_segments([self.seg(words)])
        self.assertGreater(len(out), 1)
        for s in out:
            self.assertLessEqual(len(s["text"]), MAX_CHARS)
            self.assertLessEqual(s["end"] - s["start"], MAX_DUR_SEC + 0.5)
        self.assertEqual("".join(s["text"] for s in out), "これはとても長いテロップで読みきれませんだから分けます")

    def test_splits_at_the_silence(self):
        # 助詞も句読点もないときは、間が空いている所で分ける
        words = self.words([("あいうえおかき", 0), ("くけこさしすせ", 0), ("そたちつてとな", 1.0), ("にぬねのはひふ", 0)])
        out = split_long_segments([self.seg(words)])
        self.assertEqual([s["text"] for s in out], ["あいうえおかきくけこさしすせ", "そたちつてとなにぬねのはひふ"])

    def test_splits_after_punctuation(self):
        # 間の空き方が同じなら、句読点の直後で分ける
        words = self.words([("これはとても長い", 0), ("テストの文です。", 0), ("次の文もここに", 0), ("しっかり入っています", 0)])
        out = split_long_segments([self.seg(words)])
        self.assertEqual(out[0]["text"], "これはとても長いテストの文です。")

    def test_segment_without_words_is_kept(self):
        seg = {"start": 0.0, "end": 30.0, "text": "単語情報がない長い区間" * 3, "words": []}
        self.assertEqual(split_long_segments([seg]), [seg])

    def test_keeps_times_in_order(self):
        words = self.words([("あいうえお", 0), ("かきくけこ", 0.5), ("さしすせそ", 0), ("たちつてと", 0.5)])
        out = split_long_segments([self.seg(words)])
        for a, b in zip(out, out[1:]):
            self.assertLessEqual(a["end"], b["start"])


class TestLeadDelay(unittest.TestCase):
    """発声よりわずかに早い単語時刻を、少し遅らせて表示する"""

    def test_start_is_delayed(self):
        raw = [{"start": 1.0, "end": 4.0, "text": "こんにちは", "words": [(1.6, 2.2, "こん"), (2.3, 3.8, "にちは")]}]
        out = align_to_words(raw, lead=0.12)
        self.assertAlmostEqual(out[0]["start"], 1.72, places=2)


class TestStripSpeakerLabels(unittest.TestCase):
    """Whisperが付ける架空の話者名(「樋口 」「深井 」)の除去"""

    def seg(self, text):
        return {"start": 0.0, "end": 1.0, "text": text, "words": []}

    def test_removes_repeated_head_names(self):
        segs = [self.seg(t) for t in ["樋口 あなるほどね", "深井 それ常識クイズって言うんだよ", "樋口 はい",
                                      "深井 正解 Aです", "樋口 うん", "深井 忘れたわ", "知らねえよと思って",
                                      "深井 答え 樋口 多分覚えてないと思う"]]
        out = [s["text"] for s in strip_speaker_labels(segs)]
        self.assertEqual(out, ["あなるほどね", "それ常識クイズって言うんだよ", "はい", "正解 Aです", "うん",
                               "忘れたわ", "知らねえよと思って", "答え 多分覚えてないと思う"])

    def test_removes_katakana_names(self):
        segs = [self.seg(t) for t in ["ヤンヤン そうだけど今日は", "ヤンヤン 理科の常識クイズです",
                                      "樋口 保険すげーな ヤンヤン ゴリゴリ問題見てんね", "ヤンヤン はい", "トレビアクイズ"]]
        out = [s["text"] for s in strip_speaker_labels(segs)]
        self.assertEqual(out, ["そうだけど今日は", "理科の常識クイズです", "樋口 保険すげーな ゴリゴリ問題見てんね",
                               "はい", "トレビアクイズ"])

    def test_drops_segment_left_empty(self):
        segs = [self.seg("樋口 はい"), self.seg("樋口 うん"), self.seg("樋口 "), self.seg("樋口 そう")]
        self.assertEqual(len(strip_speaker_labels(segs)), 3)

    def test_keeps_normal_text(self):
        # 文中の漢字語や、空白を挟まない語、たまに1〜2回だけの形は消さない
        segs = [self.seg(t) for t in ["徳川家康", "徳川吉村は8代将軍", "理科 の問題", "理科の上式クイズです",
                                      "上式 だからね", "常識クイズ", "A 足がぬるぬるしているから"]]
        self.assertEqual(strip_speaker_labels(segs), segs)

    def test_name_also_used_in_sentences_is_kept(self):
        # 本当に会話に出てくる名前(文中でも使われる)は話者名扱いしない
        segs = [self.seg(t) for t in ["井上 久しぶり", "井上 そうね", "井上 はい", "井上くん久しぶりで",
                                      "井上さんが言ってた", "井上の家", "井上くんに聞いた"]]
        self.assertEqual(strip_speaker_labels(segs), segs)


class TestSplitTopics(unittest.TestCase):
    """会話を話題のまとまりに区切る処理"""

    def line(self, start, dur, text):
        return {"start": start, "end": start + dur, "text": text}

    def test_long_silence_starts_a_topic(self):
        segs = [self.line(0, 3, "きのう見た映画の話なんだけど"), self.line(4, 3, "すごく面白かったんだよ"),
                self.line(8, 3, "主人公がずっと走ってるだけの映画で"), self.line(12, 3, "でも最後まで見ちゃった"),
                self.line(16, 3, "映画館はガラガラだったけどね"), self.line(20, 3, "その映画のパンフレットも買った"),
                # ここで6秒の無音を挟んで別の話へ
                self.line(30, 3, "今日のごはんは何にしようか"), self.line(34, 3, "カレーが食べたい気分だな"),
                self.line(38, 3, "この前作ったカレーが余ってる"), self.line(42, 3, "ごはんを炊くだけでいいね"),
                self.line(46, 3, "じゃあ夜はカレーにしよう"), self.line(50, 3, "ごはん多めで頼むよ")]
        topics = split_topics(segs)
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[1][0]["text"], "今日のごはんは何にしようか")

    def test_short_topic_joins_the_previous_one(self):
        # 切れ目らしさがあっても、直前の話題が短すぎるならつなげる
        segs = [self.line(0, 2, "ちょっと聞いてよ"), self.line(3, 2, "うん"),
                self.line(10, 2, "じゃあ次の話をしようか"), self.line(13, 2, "いいよ")]
        self.assertEqual(len(split_topics(segs)), 1)

    def test_answer_stays_with_the_question(self):
        # 問いかけの直後と「正解」は、間が空いていても前の話題の続き
        segs = [self.line(0, 4, "ここで問題です"), self.line(5, 4, "日本で一番高い山はどこでしょう"),
                self.line(12, 3, "富士山"), self.line(16, 3, "正解です"),
                self.line(20, 3, "簡単すぎたね"), self.line(24, 3, "もっと難しいのがいいな")]
        self.assertEqual(len(split_topics(segs)), 1)

    def test_no_segments(self):
        self.assertEqual(split_topics([]), [])

    def test_every_line_is_kept(self):
        segs = [self.line(i * 5, 3, f"{i}行目の内容です") for i in range(20)]
        self.assertEqual(sum(len(t) for t in split_topics(segs)), len(segs))


class TestTopicsAreGeneric(unittest.TestCase):
    """話題の区切りが、特定のジャンルや話す速さに依存していないこと"""

    def talk(self, pairs, scale=1.0):
        """[(前の無音, 文)] から字幕を作る。scale で全体の速さを変える"""
        out, t = [], 0.0
        for gap, text in pairs:
            t += gap * scale
            dur = len(text) * 0.18 * scale
            out.append({"start": round(t, 2), "end": round(t + dur, 2), "text": text})
            t += dur
        return out

    SHOW = [(0, "きのう行ったラーメン屋の話をしていい"), (0.3, "すごく混んでたんだよね"),
            (0.3, "券売機の前で10分も並んだ"), (0.3, "でも味は文句なしだった"),
            (0.3, "スープが濃いのに後味は軽くて"), (0.3, "替え玉までしちゃった"),
            (3.0, "さて、話は変わりますが"), (0.3, "来月の旅行はどこにしようか"),
            (0.3, "海がいいか山がいいかで迷ってる"), (0.3, "去年は海に行ったよね"),
            (0.3, "じゃあ今年は山にしてみようか"), (0.3, "温泉も入りたいしね")]

    def test_same_result_when_the_talk_is_slower(self):
        # ゆっくり話す動画でも、同じ場所で話題が分かれる(秒数の決め打ちをしていない)
        fast = split_topics(self.talk(self.SHOW), None, 10.0)
        slow = split_topics(self.talk(self.SHOW, scale=2.0), None, 10.0)
        self.assertEqual(len(fast), len(slow))
        self.assertEqual([s["text"] for s in fast[-1]], [s["text"] for s in slow[-1]])

    def test_numbered_heading_starts_a_topic(self):
        segs = self.talk([(0, "ここからは順番に紹介していきます"), (0.3, "まずはこちらの商品から"),
                          (0.3, "見た目がとにかくかわいいんです"), (0.3, "値段も手ごろでした"),
                          (0.3, "毎日のように使っています"), (0.3, "買ってよかったと思います"),
                          (0.5, "2つ目はこの調理器具です"), (0.3, "使い方がとても簡単で"),
                          (0.3, "洗い物も少なくて済みます"), (0.3, "料理の時間が短くなりました"),
                          (0.3, "家族にも好評でした"), (0.3, "これもおすすめできます")])
        topics = split_topics(segs, None, 20.0)
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[1][0]["text"], "2つ目はこの調理器具です")

    def test_shorter_target_allows_shorter_topics(self):
        # 「1本の長さの目安」を短くすると、話題も細かく取れる
        segs = self.talk(self.SHOW)
        self.assertGreaterEqual(len(split_topics(segs, None, 10.0)), len(split_topics(segs, None, 60.0)))


class TestTopicTitles(unittest.TestCase):
    """話題の見出し(画面左上に出す「いま何の話か」)"""

    def line(self, start, dur, text):
        return {"start": start, "end": start + dur, "text": text}

    def talk(self):
        """ダンゴムシの話 → ラーメンの話、と変わる会話(各話題1分ほど)"""
        first = ["ダンゴムシの迷路の話をします", "ダンゴムシは交互に曲がります", "だから迷路はこう作ります",
                 "ダンゴムシは賢いですね", "迷路を抜けられます", "ダンゴムシはすごいですね",
                 "迷路の作り方が大事です", "ダンゴムシの動きは決まっています", "迷路の形を工夫します",
                 "ダンゴムシの迷路は面白いです", "迷路を抜けた記録もあります", "ダンゴムシは迷いません"]
        second = ["さて、ラーメンの話に変えます", "きのう食べたラーメンは家系でした", "ラーメンのスープが濃いんですよ",
                  "ラーメンの麺は太麺でした", "ラーメンは週に一度にします", "家系はやっぱりおいしい",
                  "ラーメンの替え玉もしました", "家系のライスも頼みます", "ラーメンの汁は残します",
                  "ラーメン屋の行列は長いです", "家系の味は濃いめが好きです", "ラーメンの話は尽きません"]
        lines, t = [], 0.0
        for text in first:
            lines.append(self.line(t, 4, text))
            t += 5
        t += 6                       # ここで話題が変わる(長めの無音)
        for text in second:
            lines.append(self.line(t, 4, text))
            t += 5
        return lines

    def test_whole_scope_gives_one_title(self):
        # 「動画に1つ」は、その回のテーマを最初から最後まで出す
        titles = make_topic_titles(self.talk(), scope="whole")
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["start"], 0)
        self.assertGreaterEqual(titles[0]["end"], 100)

    def test_title_has_no_suffix(self):
        for t in make_topic_titles(self.talk(), scope="fine"):
            self.assertFalse(t["text"].endswith("の話"))

    def test_title_uses_the_words_of_the_topic(self):
        titles = make_topic_titles(self.talk(), scope="fine")
        self.assertEqual(len(titles), 2)
        self.assertIn(titles[0]["text"], ("ダンゴムシ", "迷路"))     # その話の中心の言葉
        self.assertIn(titles[1]["text"], ("ラーメン", "家系"))

    def test_title_covers_the_topic_time(self):
        titles = make_topic_titles(self.talk(), scope="fine")
        self.assertEqual(titles[0]["start"], 0)
        self.assertGreater(titles[1]["end"], titles[1]["start"])
        self.assertLessEqual(titles[0]["end"], titles[1]["start"])

    def test_title_is_short(self):
        for t in make_topic_titles(self.talk(), scope="fine"):
            self.assertLessEqual(len(t["text"]), MAX_TITLE_CHARS)

    def test_no_segments(self):
        self.assertEqual(make_topic_titles([]), [])

    def test_title_style_is_top_left(self):
        style = build_title_style({"font": "gothic", "size": 24})
        self.assertIn("Alignment=5", style)      # 左上(SRT由来の字幕は旧SSA形式の配置指定)
        self.assertIn("BorderStyle=3", style)    # 背景つき
        self.assertIn(f"FontName={GOTHIC}", style)

    def test_title_is_smaller_than_the_caption(self):
        for size in (10, 24, 48):
            title_size = float(re.search(r"FontSize=([0-9.]+)", build_title_style({"size": size})).group(1))
            self.assertLessEqual(title_size, size)
            self.assertGreaterEqual(title_size, min(size, 11))


class TestQualityArgs(unittest.TestCase):
    def test_off_keeps_current_settings(self):
        # 画質優先OFFは従来と同じ設定(既存ユーザーの動作を変えない)
        self.assertEqual(quality_args(False), ["-crf", "23", "-preset", "veryfast"])

    def test_on_uses_higher_quality(self):
        args = quality_args(True)
        self.assertEqual(args, ["-crf", "18", "-preset", "medium"])
        # CRFは小さいほど高画質
        self.assertLess(int(args[1]), 23)


class TestAssColor(unittest.TestCase):
    def test_rgb_to_ass_bgr(self):
        self.assertEqual(_ass_color("#FF8800"), "&H000088FF")

    def test_alpha(self):
        self.assertEqual(_ass_color("#FFFFFF", 0x50), "&H50FFFFFF")


class TestNormalizeStyle(unittest.TestCase):
    def test_defaults_when_empty(self):
        s = normalize_style(None)
        self.assertEqual(s, DEFAULT_STYLE)
        self.assertIsNot(s, DEFAULT_STYLE)   # 既定値の辞書を共有しない

    def test_json_string_input(self):
        s = normalize_style('{"font": "mincho", "size": 30, "bold": true}')
        self.assertEqual(s["font"], "mincho")
        self.assertEqual(s["size"], 30.0)
        self.assertTrue(s["bold"])

    def test_invalid_values_fall_back(self):
        s = normalize_style({"font": "nope", "size": "huge", "color": "red",
                             "pos_x": "x", "outline": None})
        self.assertEqual(s["font"], DEFAULT_STYLE["font"])
        self.assertEqual(s["size"], DEFAULT_STYLE["size"])
        self.assertEqual(s["color"], DEFAULT_STYLE["color"])
        self.assertEqual(s["pos_x"], DEFAULT_STYLE["pos_x"])
        self.assertEqual(s["outline"], DEFAULT_STYLE["outline"])

    def test_clamps(self):
        s = normalize_style({"size": 999, "pos_y": 2.0, "outline": 99, "shadow": -3})
        self.assertEqual(s["size"], 80.0)
        self.assertEqual(s["pos_y"], 1.0)
        self.assertEqual(s["outline"], 10.0)
        self.assertEqual(s["shadow"], 0.0)

    def test_legacy_size_names(self):
        # 旧UI(小/中/大)からの互換変換
        self.assertEqual(normalize_style({"size": "large"})["size"], 26.0)

    def test_base_is_preserved_for_missing_keys(self):
        base = dict(DEFAULT_STYLE, pos_y=0.78, font="yu")
        s = normalize_style({"size": 22}, base=base)
        self.assertEqual(s["pos_y"], 0.78)
        self.assertEqual(s["font"], "yu")
        self.assertEqual(s["size"], 22.0)

    def test_color_normalized_to_upper_hex(self):
        s = normalize_style({"color": "#ff8800"})
        self.assertEqual(s["color"], "#FF8800")


class TestBuildStyle(unittest.TestCase):
    def test_default_bottom_center(self):
        style = build_style()
        self.assertIn(f"FontName={GOTHIC}", style)
        self.assertIn("FontSize=18", style)
        self.assertIn("Bold=0", style)
        self.assertIn("Alignment=2", style)
        self.assertIn("MarginV=29", style)   # (1-0.9)*288
        self.assertIn("MarginL=0", style)
        self.assertIn("MarginR=0", style)
        self.assertIn("BorderStyle=1", style)

    def test_top_position_clamped(self):
        style = build_style({"pos_y": 0.0})
        # 上端でも画面外にはみ出さないようにクランプされる
        self.assertIn(f"MarginV={PLAY_RES_Y - 40}", style)

    def test_left_shift_uses_right_margin(self):
        style = build_style({"pos_x": 0.2})
        self.assertIn("MarginL=0", style)
        self.assertIn("MarginR=230", style)  # 2*(0.5-0.2)*384=230.4→round

    def test_colors_and_bold(self):
        style = build_style({"color": "#FF0000", "outline_color": "#2563EB",
                             "bold": True, "outline": 3.5, "shadow": 2})
        self.assertIn("PrimaryColour=&H000000FF", style)
        self.assertIn("OutlineColour=&H00EB6325", style)
        self.assertIn("Bold=-1", style)
        self.assertIn("Outline=3.5", style)
        self.assertIn("Shadow=2", style)

    def test_box_mode(self):
        style = build_style({"box": True, "outline_color": "#000000", "outline": 6})
        self.assertIn("BorderStyle=3", style)
        self.assertIn("OutlineColour=&H50000000", style)  # 半透明の背景
        self.assertIn("Shadow=0", style)                  # ボックス時は影を使わない

    def test_font_maps_consistent(self):
        # 画面に出すフォントは、焼き込みで使えるものと完全に一致する
        self.assertEqual(set(FONT_MAP), set(FONT_LABELS))

    def test_font_keys_are_stable(self):
        # 保存済みのテロップ設定がそのまま使えるよう、選択肢のキーは変えない
        self.assertEqual(set(FONT_MAP), {"gothic", "yu", "mincho", "msgothic"})

    def test_every_font_key_renders(self):
        # どのフォントを選んでも、そのOSの実フォント名が指定される
        for key, name in FONT_MAP.items():
            self.assertIn(f"FontName={name}", build_style({"font": key}))


class TestClipSegments(unittest.TestCase):
    SEGS = [
        {"start": 0.0, "end": 5.0, "text": "A"},
        {"start": 8.0, "end": 12.0, "text": "B"},
        {"start": 20.0, "end": 25.0, "text": "C"},
    ]

    def test_overlap_only(self):
        local = _clip_segments(self.SEGS, 7.0, 15.0)
        self.assertEqual([s["text"] for s in local], ["B"])

    def test_times_shifted_to_clip_origin(self):
        local = _clip_segments(self.SEGS, 7.0, 15.0)
        self.assertEqual(local[0]["start"], 1.0)   # 8.0 - 7.0
        self.assertEqual(local[0]["end"], 5.0)     # 12.0 - 7.0

    def test_partial_overlap_is_trimmed(self):
        local = _clip_segments(self.SEGS, 3.0, 10.0)
        self.assertEqual(local[0]["text"], "A")
        self.assertEqual(local[0]["start"], 0.0)   # 開始前から出ていた字幕は0秒から
        self.assertEqual(local[0]["end"], 2.0)     # 5.0 - 3.0
        self.assertEqual(local[1]["end"], 7.0)     # クリップ終端(10.0)で打ち切り

    def test_no_overlap(self):
        self.assertEqual(_clip_segments(self.SEGS, 14.0, 19.0), [])


if __name__ == "__main__":
    unittest.main()
