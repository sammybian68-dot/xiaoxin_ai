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
