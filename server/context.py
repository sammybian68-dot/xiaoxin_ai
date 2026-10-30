"""发给主脑的上下文：system prompt（不变）+ 压缩摘要 + 这一 epoch 的原文 + `[system]` 现状消息。

KV cache 怎么不坏（和 carHer2 一样严格）：
  - system prompt 只放不变的东西（人设、工具规则）。时间、闹钟状态不放进去，放了每轮 system 都变，前缀缓存整段失效。
  - 现状用 `[system] …` 的 user 消息，**存进对话记录、按原位置回放**：已经发过的那些字节以后每轮都一样，
    前缀一直命中；只有新插的那条在末尾。不在每轮最后临时拼（拼完下一轮它位置就变了，等于又一处不同）。
  - 不是每轮都插。时间：跨过整半小时、或离上次说过超过 30 分钟才插一条（「下午 1 点 30 分前后」这种粗粒度，
    够理解「今天」「下午」；要精确报时走 query_time 工具）。闹钟：状态摘要和上次说的不一样才插（工具改过、响过、到点关了）。
    第一轮一定插一条完整的。
  - 工具（alarm_set 之类）返回的结果本身就在对话里，比 `[system]` 新，模型以它为准（prompts/clock.md 写了）。
压缩（compact）换 epoch 时前缀缓存失效一次，之后新 epoch 照样稳定。
"""

from __future__ import annotations

import datetime as dt

from server import clock

TZ = dt.timezone(dt.timedelta(hours=8))
WEEK = "一二三四五六日"
TIME_EVERY_MIN = 30
PREFIX = "[system] "


def _period(h: int) -> str:
    return "凌晨" if h < 5 else "早上" if h < 9 else "上午" if h < 12 else "中午" if h < 13 else \
        "下午" if h < 18 else "晚上"


def time_line(now: dt.datetime) -> str:
    """粗到半小时：「2026-10-30 星期五 下午 1 点半左右」。"""
    h, m = now.hour, now.minute
    half = 15 <= m < 45
    period = _period(h)       # 时段按真实钟点（23:50 还是「晚上」）
    if m >= 45:
        h = (h + 1) % 24
    h12 = h % 12 or 12
    return f"{now:%Y-%m-%d} 星期{WEEK[now.weekday()]} {period} {h12} 点{'半' if half else ''}左右"


def note_for(conv: dict, now: dt.datetime | None = None) -> dict | None:
    """这一轮要不要插一条 `[system]`：要的话返回 {text, clock}（存成 role=note 的消息），不要返回 None。
    只看这段对话（这个 epoch 往前也算）上一条 note 说了什么。"""
    now = now or clock.now()
    last = next((m for m in reversed(conv.get("messages", [])) if m.get("role") == "note"), None)
    clock_state = clock.summary()
    parts = []
    stale_time = True
    if last:
        last_at = dt.datetime.fromtimestamp(last.get("created", 0), TZ)
        crossed = (now.hour, now.minute >= 30) != (last_at.hour, last_at.minute >= 30) or now.date() != last_at.date()
        stale_time = crossed or (now - last_at).total_seconds() >= TIME_EVERY_MIN * 60
    if stale_time:
        parts.append(f"现在大约是 {time_line(now)}（北京时间）。")
    if not last or last.get("clock") != clock_state:
        parts.append("闹钟和计时：\n" + clock_state)
    if not parts:
        return None
    return {"text": PREFIX + "\n".join(parts), "clock": clock_state}
