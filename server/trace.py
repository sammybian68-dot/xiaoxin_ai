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


_desc_cache: "OrderedDict[str, dict]" = OrderedDict()


def _describe(url: str) -> dict:
    """一张 data URL 图片 → 调试面板看的：格式、尺寸、大小、160px 小缩略图（不存几百 KB 的原文）。"""
    key = url[:64] + str(len(url)) + url[-64:]
    if key in _desc_cache:
        return _desc_cache[key]
    d = {"url": "[图片]"}
    try:
        import base64
        import io

        from PIL import Image

        head, b64 = url.split(",", 1)
        raw = base64.b64decode(b64)
        d = {"url": f"[图片] {head[5:].split(';')[0]} {len(raw) // 1024} KB（base64 {len(b64) // 1024} KB）"}
        im = Image.open(io.BytesIO(raw))
        d["url"] = d["url"].replace("[图片] ", f"[图片] {im.width}×{im.height} ", 1)
        im.thumbnail((160, 160))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=60)
        d["thumb"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001  解不开的图：只留格式和大小
        pass
    with _lock:
        _desc_cache[key] = d
        while len(_desc_cache) > 64:
            _desc_cache.popitem(last=False)
    return d


def _strip(x):
    """图片 data URL 换成描述 + 小缩略图：一张 2048 的图 base64 有几百 KB，调试面板只要看格式。"""
    if isinstance(x, dict):
        if x.get("type") == "image_url":
            url = (x.get("image_url") or {}).get("url") or ""
            return {"type": "image_url", "image_url": _describe(url) if url.startswith("data:") else {"url": url}}
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
    if "messages" in rec:
        rec["messages"] = _strip(list(rec["messages"]))  # 记的时候就换掉图片本体，内存、落盘、给面板都小
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
    """这一轮自己的那段：从这一轮的用户消息（连同紧挨在它前面的 [system] 现状消息、家长不问小新时发的消息）起到最后。
    返回 (起点下标, 那段消息)。前面的历史每次都一样，调试面板只看这一段。"""
    k = next((i for i in range(len(messages) - 1, -1, -1)
              if messages[i].get("role") == "user" and not _is_note(messages[i])), 0)
    # 往前带上小新上次回答之后的所有 user（[system] 现状、家长发的图和话），不带开头的压缩摘要
    while k > 0 and messages[k - 1].get("role") == "user" and not (
            isinstance(messages[k - 1].get("content"), str) and messages[k - 1]["content"].startswith("前面的对话")):
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
