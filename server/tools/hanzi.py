"""笔顺工具：查字的笔顺，同时在屏幕上一笔一划地演示。说明在 server/prompts/hanzi.md。"""

from __future__ import annotations

import json

from server import hanzi

MAX_CHARS = 4
SPECS = [{"type": "function", "function": {
    "name": "show_stroke_order",
    "description": "查汉字笔顺，并在屏幕上一笔一划演示动画。",
    "parameters": {"type": "object", "properties": {
        "chars": {"type": "string", "description": f"要查的字，1–{MAX_CHARS} 个，如「心」「森林」"},
    }, "required": ["chars"]},
}}]
NAMES = {"show_stroke_order"}


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return "失败：参数不是 JSON 对象"
    chars = hanzi.chars_of(args.get("chars") if isinstance(args, dict) else "")
    if not chars:
        return "失败：没有汉字，先问用户要查哪个字"
    chars = list(dict.fromkeys(chars))[:MAX_CHARS]
    lines = [hanzi.summary(c) for c in chars]
    ok = [c for c in chars if hanzi.lookup(c)]
    head = f"已在屏幕上演示「{''.join(ok)}」的笔顺。" if ok else "失败：这些字都没有笔顺数据。"
    return head + "\n" + "\n".join(lines)
