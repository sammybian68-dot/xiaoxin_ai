"""作文：题目由小新按年级出，这里排方格稿纸（每满 100 字标一下）。写作提示只印在答案页（给家长）。"""

from __future__ import annotations

import random

SUBJECT = "chinese"
TOPIC = "essay"
NAME = "作文"
GRADE = "二年级～六年级"
LAYOUT = "essay"
DEFAULT_COUNT = 0
MAX_COUNT = 1
HELP = "topic：作文题目；words_count：字数 100–800；hint：写作提示（印在答案页）"


def generate(count: int, rnd: random.Random, topic: str = "", hint: str = "", **_) -> tuple[str, list[dict]]:
    from server.exercises import ExerciseError

    topic = str(topic or "").strip()[:40]
    if not topic:
        raise ExerciseError("作文要有题目")
    return "作文", [{"q": topic, "a": str(hint or "")[:200]}]
