"""通用口算：加减乘除随机混，范围、进位可调。

- 加法：结果不超过 max；减法不出负数；carry=False 时加法不进位、减法不退位（一年级上册）。
- 乘法：max ≤ 81 是表内（2–9 × 2–9），否则两位数 × 一位数。
- 除法：都能除尽；max ≤ 81 是表内除法（商 1–9），否则两位数 ÷ 一位数。
"""

from __future__ import annotations

import random

SUBJECT = "math"
TOPIC = "arithmetic"
NAME = "加减乘除口算"
GRADE = "一年级～三年级"
LAYOUT = "math"
DEFAULT_COUNT = 50
MAX_COUNT = 100
HELP = "ops「+-」「×」「÷」「+-×÷」；max：加减是结果上限（20 / 100 / 1000），乘除 9 = 表内；carry=false 不进位退位"

OPS = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "×": lambda a, b: a * b, "÷": lambda a, b: a // b}
OP_NAME = {"+": "加", "-": "减", "×": "乘", "÷": "除"}


def items(count: int, ops: str, max_n: int, carry: bool, rnd: random.Random) -> list[dict]:
    ops = "".join(o for o in ops if o in OPS) or "+"
    out, seen, tries = [], set(), 0
    while len(out) < count and tries < count * 200:
        tries += 1
        op = rnd.choice(ops)
        if op in "+-":
            a, b = rnd.randint(1, max_n), rnd.randint(1, max_n)
            if op == "+":
                if a + b > max_n or (not carry and a % 10 + b % 10 >= 10):
                    continue
            else:
                a, b = max(a, b), min(a, b)
                if a == b or (not carry and a % 10 < b % 10):
                    continue
        elif op == "×":
            a, b = rnd.randint(2, 9 if max_n <= 81 else 99), rnd.randint(2, 9)
        else:
            b = rnd.randint(2, 9)
            a = b * (rnd.randint(1, 9) if max_n <= 81 else rnd.randint(2, 99 // b))
        if (a, op, b) in seen:
            continue
        seen.add((a, op, b))
        out.append({"q": f"{a} {op} {b} =", "a": str(OPS[op](a, b))})
    return out


def generate(count: int, rnd: random.Random, ops: str = "+-", limit: int = 100, carry: bool = True, **_) -> tuple[str, list[dict]]:
    """limit：加减是结果上限，乘除 ≤81 是表内。工具参数里叫 max（见 exercises.create）。"""
    limit = min(1000, int(limit or 100))
    limit = 10 if limit < 10 else limit
    ops = str(ops or "+-")
    name = "".join(OP_NAME[o] for o in dict.fromkeys(ops) if o in OPS)
    return f"{limit} 以内{name}法口算", items(count, ops, limit, bool(carry), rnd)
