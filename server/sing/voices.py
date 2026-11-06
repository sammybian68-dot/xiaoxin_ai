"""唱歌声音库：DATA/sing/voices/<id>.wav + index.json [{id, name, member, by, seconds, created}]。

一个名字一份（「爸爸」「卞依一」），重录就换掉旧的。name 是录的时候说的那个称呼；能对上家里的成员（名字或称呼一样）
就记下 member，之后叫名字、叫称呼都找得到。录音是 pad 上原样录的 wav（不开降噪、回声消除、自动增益，唱歌被降噪会发闷），
SoulX 那边自己转 44.1 kHz。全家共用：爸爸的声音在每个孩子的线程里都能点。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from server import store

MIN_S, MAX_S = 8.0, 40.0
_lock = threading.RLock()
_ID = re.compile(r"^[0-9a-f]{12}$")
_NOISE = re.compile(r"[\s，。,.!！?？、「」“”\"'（）()]+|的?(唱歌的?)?声音$|唱歌$")


class VoiceError(ValueError):
    pass


def _dir() -> Path:
    return store.DATA / "sing" / "voices"


def _index() -> Path:
    return _dir() / "index.json"


def _read() -> list[dict]:
    try:
        return json.loads(_index().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _write(items: list[dict]) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _index().with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_index())


def clean(name: str) -> str:
    """「爸爸的唱歌声音」「 爸爸 」→「爸爸」。"""
    return _NOISE.sub("", (name or "").strip())[:20]


def all_voices() -> list[dict]:
    with _lock:
        return sorted(_read(), key=lambda v: -v["created"])


def get(vid: str) -> dict:
    for v in all_voices():
        if v["id"] == vid:
            return v
    raise KeyError(vid)


def path(vid: str) -> Path:
    if not _ID.match(vid or ""):
        raise KeyError(vid)
    return _dir() / f"{vid}.wav"


def member_for(name: str) -> dict | None:
    """名字或称呼对得上的家庭成员。"""
    from server.family import members

    n = clean(name)
    if not n:
        return None
    for m in members.all_members():
        if n in (clean(m.get("name", "")), clean(m.get("call", ""))):
            return m
    return None


def find(name: str) -> dict | None:
    """按说法找声音：名字一样 → 同一个成员（叫名字或称呼都行）→ 互相包含（「依一」→「卞依一」）。"""
    n = clean(name)
    if not n:
        return None
    vs = all_voices()
    for v in vs:
        if clean(v["name"]) == n:
            return v
    m = member_for(n)
    if m:
        for v in vs:
            if v.get("member") == m["id"]:
                return v
    hits = [v for v in vs if n in clean(v["name"]) or clean(v["name"]) in n]
    return hits[0] if len(hits) == 1 else None


def save(name: str, data: bytes, by: str = "") -> dict:
    n = clean(name)
    if not n:
        raise VoiceError("要说清是谁的声音")
    try:
        _, _, seconds = store.wav_info(data)
    except store.StoreError as e:
        raise VoiceError(str(e)) from e
    if seconds < MIN_S:
        raise VoiceError(f"只录了 {seconds:.0f} 秒，至少唱 {MIN_S:.0f} 秒")
    if seconds > MAX_S:
        raise VoiceError(f"录了 {seconds:.0f} 秒，太长了（最多 {MAX_S:.0f} 秒）")
    m = member_for(n)
    vid = uuid.uuid4().hex[:12]
    with _lock:
        items = _read()
        old = [v for v in items if clean(v["name"]) == n or (m and v.get("member") == m["id"])]
        _dir().mkdir(parents=True, exist_ok=True)
        path(vid).write_bytes(data)
        meta = {"id": vid, "name": n, "member": m["id"] if m else "", "by": by, "seconds": round(seconds, 1),
                "created": time.time()}
        _write([v for v in items if v not in old] + [meta])
    for v in old:
        path(v["id"]).unlink(missing_ok=True)
    return meta


def delete(vid: str) -> None:
    with _lock:
        items = _read()
        if not any(v["id"] == vid for v in items):
            raise KeyError(vid)
        _write([v for v in items if v["id"] != vid])
    path(vid).unlink(missing_ok=True)


def names() -> str:
    """给模型看的：录过谁。"""
    return "、".join(v["name"] for v in all_voices()) or "还没有人录"
