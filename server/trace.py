"""调试轨迹：每次发给主脑的请求原样记下来（system、messages、tools、回了什么、用量、耗时），调试面板看。

照 carHer2「调试」那一栏：一轮回答可能有好几次请求（调了工具再问一次），一次一条，按轮次分组。
每段对话留最近 MAX_TURNS 轮。一轮结束落一份到 XIAOXIN_DATA/trace/<cid>.json（图片本体换成占位，不然太大），
部署重启后调试面板还能看到之前的；不进对话文件。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

MAX_TURNS = 20
MAX_CONVS = 30
_lock = threading.Lock()
_data: "OrderedDict[str, deque]" = OrderedDict()
_seq = int(time.time() * 1000)  # 重启后接着落盘的旧轮次往后编，id 不撞
DIR = Path(os.environ.get("XIAOXIN_DATA") or "/Data/cmbian/private_data/xiaoxin") / "trace"


def _safe(cid: str) -> bool:
    return bool(cid) and cid.replace("-", "").isalnum() and len(cid) <= 64


def _strip(x):
    """图片 data URL 换成占位：一张 2048 的图 base64 有几百 KB。"""
    if isinstance(x, dict):
        if x.get("type") == "image_url":
            return {"type": "image_url", "image_url": {"url": "[图片]"}}
        return {k: _strip(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


def _load(key: str) -> deque:
    """内存里没有这段对话（刚重启）：从盘上读回来。调用方持锁。"""
    q = deque(maxlen=MAX_TURNS)
    if _safe(key):
        try:
            q.extend(json.loads((DIR / f"{key}.json").read_text()))
        except (OSError, ValueError):
            pass
    _data[key] = q
    while len(_data) > MAX_CONVS:
        _data.popitem(last=False)
    return q


def _save(key: str) -> None:
    if not _safe(key):
        return
    with _lock:
        snap = _strip(list(_data.get(key, [])))
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = DIR / f"{key}.json.tmp"
        tmp.write_text(json.dumps(snap, ensure_ascii=False, default=str))
        tmp.replace(DIR / f"{key}.json")
    except OSError:
        pass


def new_turn(cid: str | None, *, model: str, voice: bool) -> dict:
    """一轮开始。返回这一轮的记录（请求往 calls 里追加）。cid 为空（还没建对话）也记，挂在 "-" 下。"""
    global _seq
    key = cid or "-"
    with _lock:
        _seq += 1
        turn = {"id": _seq, "at": time.time(), "model": model, "voice": voice, "calls": [], "done": False, "cid": key}
        q = _data[key] if key in _data else _load(key)
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
    _save(turn.get("cid") or "-")


def turns(cid: str) -> list[dict]:
    """最近几轮，新的在前。"""
    key = cid or "-"
    with _lock:
        q = _data[key] if key in _data else _load(key)
        return list(reversed(list(q)))


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
