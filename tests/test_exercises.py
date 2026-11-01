"""出题 agent（server/exercises）：注册表、末尾带 0 的乘除法（6 类都有、答案对、末尾带 0、商是整数）、ops 过滤、
学科和题型对不上报错；make_exercise 工具出题后能打印。"""

from __future__ import annotations

import random
import re

import pytest

from server import exercises, store, worksheet
from server.exercises.math import zero_end_mul_div as zero


def _parse(q: str):
    a, op, b = re.match(r"(\d+)\s*([×÷])\s*(\d+)\s*=", q).groups()
    return int(a), op, int(b)


def test_registry_and_catalog():
    assert set(exercises.TOPICS) == {"zero_end_mul_div", "arithmetic", "pinyin_words", "dictation", "essay", "word_dictation"}
    cat = exercises.catalog()
    assert "zero_end_mul_div（数学·末尾带 0 的乘除法口算" in cat and "essay（语文·作文" in cat
    for m in exercises.TOPICS.values():
        assert m.SUBJECT in exercises.SUBJECTS and m.LAYOUT in worksheet.KINDS


@pytest.mark.parametrize("seed", range(5))
def test_zero_end_answers_and_shapes(seed):
    title, items = zero.generate(100, random.Random(seed))
    assert title == "末尾带 0 的乘除法口算练习" and len(items) == 100 and len({i["q"] for i in items}) == 100
    kinds = set()
    for it in items:
        a, op, b = _parse(it["q"])
        ans = int(it["a"])
        if op == "×":
            assert ans == a * b and (a % 10 == 0 or b % 10 == 0)
            kinds.add("teen×tens" if 11 <= min(a, b) <= 19 else "tens×tens" if a % 10 == b % 10 == 0 else "tens×one")
        else:
            assert a % b == 0 and ans == a // b and a % 10 == 0
            kinds.add("÷tens" if b % 10 == 0 else "÷one")
            if b % 10 == 0:
                assert 2 <= ans <= 9  # 除以整十：商是一位数
    assert kinds == {"tens×one", "teen×tens", "tens×tens", "÷one", "÷tens"}


def test_zero_end_ops_filter():
    _, mul = zero.generate(40, random.Random(1), ops="×")
    _, div = zero.generate(40, random.Random(1), ops="÷")
    assert all("×" in i["q"] for i in mul) and all("÷" in i["q"] for i in div)
    assert zero.generate(10, random.Random(1), ops="×")[0] == "末尾带 0 的乘法口算练习"


def test_create_checks_subject_and_topic():
    with pytest.raises(exercises.ExerciseError, match="没有这种题"):
        exercises.create("math", "fractions")
    with pytest.raises(exercises.ExerciseError, match="是数学的题"):
        exercises.create("chinese", "zero_end_mul_div")
    t, layout, items, _ = exercises.create("math", "zero_end_mul_div", count=500, seed=1)
    assert layout == "math" and len(items) == zero.MAX_COUNT  # 题数夹到上限


def test_make_exercise_tool_then_print(client, fakes):
    tid = client.get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "make_exercise", {"subject": "math", "topic": "zero_end_mul_div", "count": 60}),
                        ("tool", "print_file", {"file": "latest"}), ("text", "好的")]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "帮我出个末尾带0的乘除法卷子，打印出来"})
    acts = store.get(tid)["messages"][-1]["actions"]
    assert "末尾带 0 的乘除法口算练习" in acts[0]["result"] and "60 道口算" in acts[0]["result"]
    assert acts[1]["result"].startswith("已发给打印机") and "黑白" in acts[1]["result"]
    tools = {t["function"]["name"] for t in fakes.llm_requests[-1].get("tools") or []}
    assert not tools or "make_exercise" in tools
