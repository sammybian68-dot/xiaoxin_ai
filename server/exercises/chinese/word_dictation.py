"""生词默写（三年级上册，按单元）：一张卷子两部分——
  一、听写：只有题号和田字格，没有拼音。练习模式里小新每个词读两遍、每遍后等 5 秒（server/practice.py）；
  二、古诗和积累：照资料原样出填空（`{…}` 是空），孩子自己写。
词表、古诗、积累都在 data/grade3a_dictation.json（照爸爸拍的书后答案页录的）。

units：要哪几个单元。「第四单元」→ [4]；「第五单元之前」→ [1, 2, 3, 4]；「一到三单元」→ [1, 2, 3]。
count：听写抽几个词。一个单元全听写；几个单元默认抽 30 个（一个词读两遍等 5 秒约 15 秒，230 个词要一个小时）。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

SUBJECT = "chinese"
TOPIC = "word_dictation"
NAME = "生词默写"
GRADE = "三年级上册"
LAYOUT = "dictation"
DEFAULT_COUNT = 0  # 0 = 按单元数定（见 generate）
MAX_COUNT = 60
MULTI_DEFAULT = 30
HELP = ("units：单元号列表，如 [4]、[1,2,3,4]（「第五单元之前」= 1–4）；count：听写几个词（不给：一个单元全听，几个单元抽 30 个）；"
        "with_extras：要不要古诗和积累填空（默认要）。资料是《26 秋三年级上册语文·生字词默写》1–8 单元，字词由程序取，不用你给")
BLANK = re.compile(r"\{([^}]*)\}")
_DATA = None


def data() -> dict:
    global _DATA
    if _DATA is None:
        _DATA = json.loads((Path(__file__).parent / "data" / "grade3a_dictation.json").read_text(encoding="utf-8"))
    return _DATA


def units_of(units) -> list[int]:
    from server.exercises import ExerciseError

    if isinstance(units, (int, str)):
        text = str(units).translate(str.maketrans("一二三四五六七八", "12345678"))
        units = re.findall(r"\d+", text)
    have = {int(k) for k in data()["units"]}
    out = sorted({int(u) for u in units or [] if str(u).strip().isdigit()} & have)
    if not out:
        raise ExerciseError(f"要说是哪几个单元（这本有 {min(have)}–{max(have)} 单元）")
    return out


def generate(count: int, rnd: random.Random, units=None, with_extras=True, **_) -> tuple[str, list[dict]]:
    us = units_of(units)
    words = [(u, w) for u in us for w in data()["units"][str(u)]["words"].split()]
    n = count or (len(words) if len(us) == 1 else MULTI_DEFAULT)
    n = max(1, min(n, len(words), MAX_COUNT))
    pick = words if n >= len(words) else rnd.sample(words, n)
    if len(us) > 1:
        pick.sort(key=lambda x: (x[0], words.index(x)))  # 抽出来的按单元、书上的顺序排
    items = [{"q": f"{i + 1}.", "a": w, "unit": u, "part": "listen"} for i, (u, w) in enumerate(pick)]
    if with_extras is not False:
        for u in us:
            for ex in data()["units"][str(u)].get("extras") or []:
                items.append({"q": ex["title"], "by": ex.get("by", ""), "lines": ex["lines"], "unit": u, "part": "write",
                              "a": "；".join(b for line in ex["lines"] for b in BLANK.findall(line))})
    name = "第" + "、".join("一二三四五六七八"[u - 1] for u in us) + "单元" if len(us) <= 3 else f"第{us[0]}–{us[-1]}单元"
    return f"{name}生词默写", items


def blanks(items: list[dict]) -> int:
    """要孩子写的格子数：听写的字 + 填空的字（算限时用）。"""
    return sum(len(it["a"]) for it in items if it.get("part") == "listen") + \
        sum(len(b) for it in items if it.get("part") == "write" for line in it["lines"] for b in BLANK.findall(line))
