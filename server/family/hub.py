"""实时总线（docs/architecture.md 第 7 节）：每台设备连一条 WSS /api/hub，收它能进的那几条线程的变化。

服务端 → 客户端：
  {"type":"hello", "who":…, "threads":[…]}           连上就发
  {"type":"msg", "thread":tid, "message":{…}}         有新消息（用户的、小新答完的、家长发的）
  {"type":"msg.update", "thread":tid, "message":{…}}  已有消息补了字段（原图传完了、首响时间）
  {"type":"ping"}                                     每 25 秒一次，顺便发现断线
客户端 → 服务端：{"type":"pong"}，别的忽略。断了客户端自己重连，带 ?since=<最后一条消息 id> 用 GET /api/thread 补齐。

推送来自线程池（store.append 在同步代码里调），所以 publish() 只往每个连接的 asyncio 队列里 call_soon_threadsafe，
真正的发送在连接自己的协程里做；一个连接慢不拖别人，队列满了就踢掉它（客户端重连后补齐）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field

log = logging.getLogger("xiaoxin.hub")

PING_S = 25
QUEUE_MAX = 200


@dataclass(eq=False)  # 按对象身份放进 set
class _Conn:
    who: object
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_MAX))
    dead: bool = False


_conns: set[_Conn] = set()
_lock = threading.Lock()


def publish(thread: str, event: dict) -> None:
    """给能进这条线程的每个在线连接推一条。线程安全，谁都能调（同步代码、线程池里都行）。"""
    from server.family import members

    with _lock:
        targets = [c for c in _conns if not c.dead]
    if not targets:
        return
    data = json.dumps({**event, "thread": thread}, ensure_ascii=False)
    for c in targets:
        try:
            if not members.can_thread(c.who, thread):
                continue
        except Exception:
            continue
        c.loop.call_soon_threadsafe(_offer, c, data)


def _offer(c: _Conn, data: str) -> None:
    if c.dead:
        return
    try:
        c.queue.put_nowait(data)
    except asyncio.QueueFull:  # 太慢了：踢掉（pump 看到 dead 就退出、连接关掉），它重连后自己补
        c.dead = True
        c.queue.put_nowait('{"type":"bye","reason":"slow"}') if not c.queue.full() else None


def online() -> list[str]:
    """哪些成员有设备在线（侧栏群成员的绿点）。管理员不算。"""
    with _lock:
        return sorted({c.who.id for c in _conns if not c.dead and c.who.kind == "member"})


def _presence() -> None:
    """有人上线 / 下线：告诉每条线程里的人（只说成员 id，前端自己对名单）。"""
    from server.family import members

    ids = online()
    for k in members.kids():
        publish(k["thread"], {"type": "presence", "online": ids})


async def serve(ws, who) -> None:
    from server.family import members

    c = _Conn(who, asyncio.get_running_loop())
    with _lock:
        _conns.add(c)
    _presence()
    threads = [k["thread"] for k in members.kids() if members.can_thread(who, k["thread"])]
    try:
        await ws.send_text(json.dumps({"type": "hello", "who": {"id": who.id, "role": who.role},
                                       "threads": threads}, ensure_ascii=False))

        async def pump():
            while not c.dead:
                try:
                    data = await asyncio.wait_for(c.queue.get(), PING_S)
                except asyncio.TimeoutError:
                    data = '{"type":"ping"}'
                await ws.send_text(data)

        async def drain():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if not t.cancelled() and t.exception():
                log.debug("hub conn %s ended: %r", who.id, t.exception())
    finally:
        with _lock:
            _conns.discard(c)
        _presence()
