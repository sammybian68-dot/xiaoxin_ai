"""query_time：现在的北京时间。prompt 里也有分钟级的时间，这个给「现在几点」「还有多久到 X 点」要精确时用。"""

from __future__ import annotations

import datetime as dt
import json

from server import clock

WEEK = "一二三四五六日"
SPECS = [{"type": "function", "function": {
    "name": "query_time",
    "description": "查现在的北京时间（精确到秒），可选算到某个钟点还有多久。",
    "parameters": {"type": "object", "properties": {
        "until": {"type": "string", "description": "HH:MM，可选：算从现在到下一个这个钟点还有多久"},
    }},
}}]
NAMES = {"query_time"}


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
    except ValueError:
        args = {}
    t = clock.now()
    out = f"现在 {t:%Y-%m-%d %H:%M:%S} 星期{WEEK[t.weekday()]}（北京时间）"
    until = args.get("until") if isinstance(args, dict) else None
    if until:
        try:
            hhmm = clock._parse_time(until)
        except clock.ClockError as e:
            return f"{out}；失败：{e}"
        at = clock.next_fire({"time": hhmm, "repeat": [], "enabled": True}, t)
        out += f"；到 {clock.when_label(at, t)} 还有 {clock.fmt_duration((at - t).total_seconds())}"
    return out
