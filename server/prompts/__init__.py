"""system prompt 按功能拆成 server/prompts/*.md，按 ORDER 拼起来。

每个 md 一个功能，写给模型看：短、无歧义、不写来龙去脉（那些放代码注释和 git 记录里）。
md 里的 {name} 由 build() 填，只填不常变的：ring_default（设置里的默认响铃提示）。
时间、闹钟状态这些会变的不放 system prompt（放了每轮 system 都变，KV cache 整段失效），
由 server/context.py 作为 `[system]` 消息插在对话里，见那边的说明。
voice.md 只在语音通话时拼上（main._llm_thread 的 extra_system）。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

DIR = Path(__file__).parent
ORDER = ["persona", "time", "search", "clock", "hanzi"]  # 文字对话、语音通话都有
VOICE = "voice"                          # 只有语音通话
WEEK = "一二三四五六日"
_SLOT = re.compile(r"\{(\w+)\}")


def read(name: str) -> str:
    return (DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def _slots() -> dict:
    from server import clock

    return {"ring_default": clock.ring_text()}


def kid_profile(kid: dict | None) -> str:
    """这条线程是哪个孩子的：名字、称呼、年级、说话要求。每个孩子不一样，拼在 system prompt 最后
    （同一个孩子一直不变，前缀缓存照样命中）。没有孩子（老路径、管理员的测试对话）就不加。"""
    if not kid:
        return ""
    lines = [f"# 你在陪谁\n这是{kid['name']}的小新。叫他「{kid.get('call') or kid['name']}」。"]
    if kid.get("grade"):
        lines.append(f"- 年级：{kid['grade']}，讲题、用词按这个程度。")
    if kid.get("style"):
        lines.append(f"- 家长的要求：{kid['style']}")
    return "\n".join(lines)


def fill(text: str, slots: dict) -> str:
    return _SLOT.sub(lambda m: str(slots.get(m.group(1), m.group(0))), text)


def build(voice: bool = False, kid: dict | None = None) -> str:
    """当前完整的 system prompt（填好默认响铃提示）；kid 给了就在最后加上这个孩子的档案。"""
    slots = _slots()
    names = ORDER + ([VOICE] if voice else [])
    parts = [fill(read(n), slots) for n in names]
    if kid:
        parts.append(kid_profile(kid))
    return "\n\n".join(parts)


def parts() -> list[dict]:
    """设置页「查看 prompt」用：每个 md 的原文（不填占位符）和什么时候生效。"""
    out = [{"name": n, "file": f"server/prompts/{n}.md", "when": "总是", "text": read(n)} for n in ORDER]
    out.append({"name": VOICE, "file": f"server/prompts/{VOICE}.md", "when": "语音通话", "text": read(VOICE)})
    return out
