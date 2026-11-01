"""出题 agent：一个工具 make_exercise(subject, topic, …) 对外，题型按学科分目录，一个题型一个模块。

    server/exercises/
      __init__.py            注册表（TOPICS）、create()：出题 → 存成练习纸（server/worksheet.py 排版、打印）
      math/arithmetic.py     加减乘除口算
      math/zero_end_mul_div.py  末尾带 0 的乘除法
      chinese/words.py       看拼音写词语、听写
      chinese/essay.py       作文稿纸
      chinese/word_dictation.py  生词默写（三年级上册按单元：听写 + 古诗积累填空，练习模式里小新念）

加一个题型：在学科目录里新建 <topic>.py，写 SUBJECT、TOPIC、NAME（卷子标题）、GRADE、LAYOUT（math / tian / essay，
怎么排版）、DEFAULT_COUNT、MAX_COUNT、HELP（给小新看的参数说明）和 generate(count, rnd, **参数) -> (标题, [{q, a}])，
再在下面 TOPICS 登记。工具的 topic 枚举、说明都从这里来，不用改工具和 prompt。

数学题由程序按规则随机出，答案是算出来的，不让模型编（模型算错一道，孩子就白练了）；
语文的字词、作文题由小新按年级和课文挑好传进来，这里只排版。
"""

from __future__ import annotations

import random

from server.exercises.chinese import essay, word_dictation, words
from server.exercises.math import arithmetic, zero_end_mul_div

TOPICS = {m.TOPIC: m for m in (zero_end_mul_div, arithmetic, words.PINYIN, words.DICTATION, essay, word_dictation)}
SUBJECTS = {"math": "数学", "chinese": "语文"}


class ExerciseError(ValueError):
    pass


def catalog() -> str:
    """给小新看的题型表（拼进工具说明）。"""
    lines = []
    for t, m in TOPICS.items():
        lines.append(f"{t}（{SUBJECTS[m.SUBJECT]}·{m.NAME}，{m.GRADE}）：{m.HELP}")
    return "\n".join(lines)


def create(subject: str, kind: str, *, count=None, title: str = "", seed=None, **opts) -> tuple[str, str, list[dict], dict]:
    """出一套题。kind 是题型（TOPICS 的键）；opts 里的 topic 是作文题目。返回 (标题, 排版, 题目列表, 额外字段)。"""
    m = TOPICS.get(str(kind or ""))
    if m is None:
        raise ExerciseError(f"没有这种题：{kind}。有：" + "、".join(TOPICS))
    if subject and subject != m.SUBJECT:
        raise ExerciseError(f"{kind} 是{SUBJECTS[m.SUBJECT]}的题，不是{SUBJECTS.get(subject, subject)}")
    n = m.DEFAULT_COUNT
    if count:
        n = max(1 if m.LAYOUT == "dictation" else 10, min(int(count), m.MAX_COUNT))
    if "max" in opts:
        opts["limit"] = opts.pop("max")  # 工具参数叫 max，函数里叫 limit（不盖掉内置 max）
    rnd = random.Random(seed)
    auto_title, items = m.generate(n, rnd, **opts)
    if not items:
        raise ExerciseError("按这个条件出不来题，换个范围")
    extra = {}
    if m.LAYOUT == "essay":
        extra = {"topic": items[0]["q"], "words_count": max(100, min(int(opts.get("words_count") or 300), 800))}
    return (str(title or auto_title))[:30], m.LAYOUT, items, extra
