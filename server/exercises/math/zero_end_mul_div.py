"""末尾带 0 的乘除法口算（人教版三年级下「口算乘法」、四年级上「除数是整十数的除法」的口算）。

照用户给的卷子（2026-11-01 截图「末尾带0的乘除法口算练习（第5套）」）分 6 类，每类按比例混在一起：
  整十/整百 × 一位数   90×6  500×3  800×7        （也会把一位数放前面：8×80）
  十几 × 整十          16×20  17×60  14×90
  整十 × 整十          80×70  50×40  30×20
  整十/整百 ÷ 一位数   2800÷7  4200÷6  250÷5  120÷4   （都能除尽，商末尾带 0）
  整十/整百 ÷ 整十     540÷60  360÷90  160÷40         （商是一位数）
答案都是算出来的；同一份里不重复。
"""

from __future__ import annotations

import random

SUBJECT = "math"
TOPIC = "zero_end_mul_div"
NAME = "末尾带 0 的乘除法口算"
GRADE = "三年级下～四年级上"
LAYOUT = "math"
DEFAULT_COUNT = 150  # 用户说平时一套 150 道（2026-11-01）
MAX_COUNT = 180
HELP = "整十整百乘一位数、十几乘整十、整十乘整十、整十整百除以一位数、除以整十；ops 可只要「×」或「÷」"


def _tens_times_one(rnd):            # 90×6、500×3、8×80
    a = rnd.randint(1, 9) * rnd.choice((10, 10, 100))
    b = rnd.randint(2, 9)
    return (a, "×", b) if rnd.random() < 0.75 else (b, "×", a)


def _teen_times_tens(rnd):           # 16×20、17×60
    return rnd.randint(11, 19), "×", rnd.randint(2, 9) * 10


def _tens_times_tens(rnd):           # 80×70、50×40
    return rnd.randint(2, 9) * 10, "×", rnd.randint(2, 9) * 10


def _div_by_one(rnd):                # 2800÷7、250÷5、120÷4
    b = rnd.randint(2, 9)
    q = rnd.randint(1, 9) * rnd.choice((10, 10, 100))
    return b * q, "÷", b


def _div_by_tens(rnd):               # 540÷60、160÷40
    d = rnd.randint(2, 9) * 10
    return d * rnd.randint(2, 9), "÷", d


KINDS = [(_tens_times_one, 3), (_teen_times_tens, 2), (_tens_times_tens, 2), (_div_by_one, 3), (_div_by_tens, 2)]


def generate(count: int, rnd: random.Random, ops: str = "×÷", **_) -> tuple[str, list[dict]]:
    want = {o for o in str(ops or "×÷") if o in "×÷"} or {"×", "÷"}
    kinds = [(f, w) for f, w in KINDS if (f(random.Random(0))[1] in want)]
    funcs, weights = zip(*kinds)
    out, seen, tries = [], set(), 0
    while len(out) < count and tries < count * 100:
        tries += 1
        a, op, b = rnd.choices(funcs, weights)[0](rnd)
        if (a, op, b) in seen:
            continue
        seen.add((a, op, b))
        ans = a * b if op == "×" else a // b
        out.append({"q": f"{a}{op}{b} =", "a": str(ans)})
    title = "末尾带 0 的" + ("乘除法" if want == {"×", "÷"} else "乘法" if want == {"×"} else "除法") + "口算练习"
    return title, out
