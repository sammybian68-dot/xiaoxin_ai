"""视频通话：TURN 临时凭证 + 信令转发。媒体不经过这里（P2P 直连，打不通走 ECS 上的 coturn），见 docs/architecture.md 8.3。

TURN 凭证：coturn 的 use-auth-secret（TURN REST API）。用户名 = "<过期时间戳>:<谁>"，密码 = base64(HMAC-SHA1(secret, 用户名))。
secret 只在 .env（TURN_SECRET）和 ECS 的 /etc/turnserver.conf 里；客户端拿到的凭证 10 分钟后作废。

信令：一个通话一个房间，最多两端。收到谁的消息就原样转给房间里另一端（offer / answer / ice / hangup 都一样），
这里不解析 SDP。现在是阶段 0 的最小版，阶段 6 换成 hub 里带身份的 call.* 事件。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

from fastapi import WebSocket

from src.voice import doubao

TURN_HOST = "47.116.13.227"
TURN_PORT = 3478
TTL_S = 600
ROOM = re.compile(r"^[a-z0-9]{3,32}$")
MAX_PEERS = 2


def ice_servers(who: str = "xiaoxin", now: float | None = None) -> dict:
    """给浏览器 RTCPeerConnection 的 iceServers。没配 TURN_SECRET 就只给 STUN（同一 Wi‑Fi 下够用）。"""
    stun = {"urls": [f"stun:{TURN_HOST}:{TURN_PORT}"]}
    secret = doubao.env("TURN_SECRET")
    if not secret:
        return {"iceServers": [stun], "ttl": 0}
    user = f"{int((now or time.time()) + TTL_S)}:{re.sub(r'[^a-z0-9_-]', '', who.lower())[:24] or 'x'}"
    pwd = base64.b64encode(hmac.new(secret.encode(), user.encode(), hashlib.sha1).digest()).decode()
    turn = {"urls": [f"turn:{TURN_HOST}:{TURN_PORT}?transport=udp", f"turn:{TURN_HOST}:{TURN_PORT}?transport=tcp"],
            "username": user, "credential": pwd}
    return {"iceServers": [stun, turn], "ttl": TTL_S}


_rooms: dict[str, list[WebSocket]] = {}


async def relay(ws: WebSocket, room: str) -> None:
    """一端连进来：通知双方人数，之后把它发的每条文本转给另一端。断开时告诉另一端 {"type":"bye"}。"""
    peers = _rooms.setdefault(room, [])
    if len(peers) >= MAX_PEERS:
        await ws.send_text(json.dumps({"type": "full"}))
        await ws.close()
        return
    peers.append(ws)
    try:
        for p in peers:
            await p.send_text(json.dumps({"type": "peers", "count": len(peers), "you_first": p is peers[0]}))
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            text = msg.get("text")
            if text is None or len(text) > 64_000:
                continue
            for p in list(peers):
                if p is not ws:
                    await p.send_text(text)
    finally:
        if ws in peers:
            peers.remove(ws)
        for p in list(peers):
            try:
                await p.send_text(json.dumps({"type": "bye"}))
            except Exception:
                pass
        if not peers:
            _rooms.pop(room, None)


# ---------------------------------------------------------------- 来电（家长打给孩子的 pad，docs/architecture.md 8.3）
# 家长 POST /api/call → 中枢开一个房间、hub 推 call.invite 给孩子的 pad（带倒计时秒数）→ pad 全屏来电、倒计时到 0 自动接通
# （或孩子先点接 / 拒）→ 双方各自打开 /calltest?room=…（WebRTC，媒体 P2P / ECS 中继）→ 任一方挂断 POST /api/call/{id}/end。
# 同时只允许一通（ECS 只有 5 Mbps）。通话本身不录，线程里只记一条：谁打的、接没接、多长。

import secrets as _secrets
import threading as _threading

_calls: dict[str, dict] = {}
_calls_lock = _threading.Lock()
RING_S = 45          # 响这么久没人接（倒计时关了或者 pad 不在）就算未接
AUTO_ANSWER_S = 5    # 默认倒计时 5 秒自动接通（设置里可改 3–10，关掉就只响铃）


def active() -> dict | None:
    with _calls_lock:
        for c in _calls.values():
            if c["state"] in ("ringing", "connected"):
                return c
    return None


def start(thread: str, caller: dict, kid: dict) -> dict:
    import time

    if active():
        raise ValueError("正在通话中，等这一通结束")
    cid = _secrets.token_hex(6)
    c = {"id": cid, "room": "call" + cid, "thread": thread, "caller": caller["id"], "caller_name": caller["name"],
         "kid": kid["id"], "state": "ringing", "created": time.time(), "answered": None, "ended": None}
    with _calls_lock:
        _calls[cid] = c
    return c


def get(cid: str) -> dict | None:
    return _calls.get(cid)


def update(cid: str, **fields) -> dict:
    with _calls_lock:
        c = _calls[cid]
        c.update(fields)
        return dict(c)
