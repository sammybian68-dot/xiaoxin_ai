"""看拼音写词语、听写：词语由小新给（按年级、课文挑），拼音本机字库查，多音字和查不到的用小新给的。"""

from __future__ import annotations

import random
import re

SUBJECT = "chinese"
GRADE = "一年级～六年级"
LAYOUT = "tian"


class _Topic:
    def __init__(self, topic: str, name: str, max_count: int, need_pinyin: bool):
        self.SUBJECT, self.TOPIC, self.NAME, self.GRADE, self.LAYOUT = SUBJECT, topic, name, GRADE, LAYOUT
        self.DEFAULT_COUNT, self.MAX_COUNT, self.need_pinyin = 0, max_count, need_pinyin
        self.HELP = f"words：词语（2–4 字为主，≤{max_count} 个）" + ("；pinyin：多音字给拼音，如 {\"银行\": \"yín háng\"}" if need_pinyin else "")

    def generate(self, count: int, rnd: random.Random, words=None, pinyin=None, **_) -> tuple[str, list[dict]]:
        from server.exercises import ExerciseError

        ws = clean(words)[:self.MAX_COUNT]
        if not ws:
            raise ExerciseError("要给几个词语（汉字）")
        given = pinyin if isinstance(pinyin, dict) else {}
        items = [{"q": given.get(w) or pinyin_of(w), "a": w} for w in ws]
        if self.need_pinyin and any(not it["q"] for it in items):
            raise ExerciseError("这些词查不到拼音，要在 pinyin 里给：" + "、".join(it["a"] for it in items if not it["q"]))
        if not self.need_pinyin:
            for i, it in enumerate(items):
                it["q"] = f"{i + 1}."
        return self.NAME, items


PINYIN = _Topic("pinyin_words", "看拼音，写词语", 30, True)
DICTATION = _Topic("dictation", "听写", 40, False)


def clean(words) -> list[str]:
    if isinstance(words, str):
        words = re.split(r"[\s,，、;；]+", words)
    out = []
    for w in words or []:
        w = re.sub(r"[^一-鿿]", "", str(w))
        if 1 <= len(w) <= 8 and w not in out:
            out.append(w)
    return out


def pinyin_of(word: str) -> str:
    from server import hanzi

    parts = []
    for ch in word:
        py = (hanzi.lookup(ch) or {}).get("pinyin") or []
        if not py:
            return ""
        parts.append(py[0])
    return " ".join(parts)
