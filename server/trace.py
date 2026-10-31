"""调试轨迹：每次发给主脑的请求原样记下来（system、messages、tools、回了什么、用量、耗时），调试面板看。

照 carHer2「调试」那一栏：一轮回答可能有好几次请求（调了工具再问一次），一次一条，按轮次分组。
只在内存里留最近 MAX_TURNS 轮（每段对话），不落盘：里面有完整上下文，量大，也不该进对话文件。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

MAX_TURNS = 20
MAX_CONVS = 30
_lock = threading.Lock()
_data: "OrderedDict[str, deque]" = OrderedDict()
_seq = 0


def new_turn(cid: str | None, *, model: str, voice: bool) -> dict:
    """一轮开始。返回这一轮的记录（请求往 calls 里追加）。cid 为空（还没建对话）也记，挂在 "-" 下。"""
    global _seq
    key = cid or "-"
    with _lock:
        _seq += 1
        turn = {"id": _seq, "at": time.time(), "model": model, "voice": voice, "calls": [], "done": False}
        q = _data.setdefault(key, deque(maxlen=MAX_TURNS))
        q.append(turn)
        _data.move_to_end(key)
        while len(_data) > MAX_CONVS:
            _data.popitem(last=False)
    return turn


def add_call(turn: dict | None, **rec) -> dict | None:
    if turn is None:
        return None
    rec.setdefault("at", time.time())
    with _lock:
        turn["calls"].append(rec)
    return rec


def finish(turn: dict | None, **fields) -> None:
    if turn is None:
        return
    with _lock:
        turn.update(fields, done=True)


def turns(cid: str) -> list[dict]:
    """最近几轮，新的在前。"""
    with _lock:
        return list(reversed(list(_data.get(cid or "-", []))))


def _is_note(m: dict) -> bool:
    return m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].startswith(("[system]", "前面的对话"))


def turn_tail(messages: list[dict]) -> tuple[int, list[dict]]:
    """这一轮自己的那段：从这一轮的用户消息（连同紧挨在它前面的 [system] 现状消息）起到最后。
    返回 (起点下标, 那段消息)。前面的历史每次都一样，调试面板只看这一段。"""
    k = next((i for i in range(len(messages) - 1, -1, -1)
              if messages[i].get("role") == "user" and not _is_note(messages[i])), 0)
    while k > 0 and _is_note(messages[k - 1]) and not messages[k - 1]["content"].startswith("前面的对话"):
        k -= 1
    return k, messages[k:]


def compact_turn(t: dict) -> dict:
    """给调试面板的一轮：每次请求只带这一轮的那段 messages（tail）和总条数，system 只在第一次请求上带一份。"""
    calls = []
    for i, c in enumerate(t["calls"]):
        start, tail = turn_tail(c.get("messages") or [])
        calls.append({**{k: v for k, v in c.items() if k not in ("messages", "system")},
                      "n_messages": len(c.get("messages") or []), "tail_start": start, "tail": tail,
                      "system_chars": len(c.get("system") or "")})
    out = {k: v for k, v in t.items() if k != "calls"}
    out["calls"] = calls
    out["system"] = t["calls"][0].get("system", "") if t["calls"] else ""
    return out
