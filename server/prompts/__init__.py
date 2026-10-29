"""system prompt 按功能拆成 server/prompts/*.md，按 ORDER 拼起来。

每个 md 一个功能，写给模型看：短、无歧义、不写来龙去脉（那些放代码注释和 git 记录里）。
md 里的 {name} 由 build() 填：now（当前北京时间）、clock_state（闹钟 / 倒计时 / 正计时的现状）。
voice.md 只在语音通话时拼上（main._llm_thread 的 extra_system）。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

DIR = Path(__file__).parent
ORDER = ["persona", "time", "search", "clock"]  # 文字对话、语音通话都有
VOICE = "voice"                          # 只有语音通话
WEEK = "一二三四五六日"
_SLOT = re.compile(r"\{(\w+)\}")


def read(name: str) -> str:
    return (DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def _slots() -> dict:
    from server import clock

    t = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    return {"now": f"{t:%Y-%m-%d %H:%M} 星期{WEEK[t.weekday()]}", "clock_state": clock.summary()}


def fill(text: str, slots: dict) -> str:
    return _SLOT.sub(lambda m: str(slots.get(m.group(1), m.group(0))), text)


def build(voice: bool = False) -> str:
    """当前完整的 system prompt（填好时间和闹钟状态）。"""
    slots = _slots()
    names = ORDER + ([VOICE] if voice else [])
    return "\n\n".join(fill(read(n), slots) for n in names)


def parts() -> list[dict]:
    """设置页「查看 prompt」用：每个 md 的原文（不填占位符）和什么时候生效。"""
    out = [{"name": n, "file": f"server/prompts/{n}.md", "when": "总是", "text": read(n)} for n in ORDER]
    out.append({"name": VOICE, "file": f"server/prompts/{VOICE}.md", "when": "语音通话", "text": read(VOICE)})
    return out
