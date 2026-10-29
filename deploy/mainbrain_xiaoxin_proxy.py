"""把 https://gpu.cmbian.top:8183/xiaoxin/ 转到本机小新服务（127.0.0.1:8184）。

小新的代码在 ai_xiaoxin 仓库，这里只做转发：请求体和回包都边收边传，SSE 不攒包。
鉴权在小新那边（cookie 或 Bearer），这里原样带过去。
部署：ai_xiaoxin 的 deploy/gpu2.sh 会把本文件拷到 mainbrain 的 server/xiaoxin_proxy.py。
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

XIAOXIN_URL = os.environ.get("XIAOXIN_URL", "http://127.0.0.1:8184").rstrip("/")
PREFIX = "/xiaoxin"
# 逐跳头不转；content-length 交给下游重算
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length"}
_DROP_OUT = _HOP | {"date", "server"}  # 外层 uvicorn 自己会加

router = APIRouter()
_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=300, write=300, pool=10))


@router.get(PREFIX)
def xiaoxin_slash() -> RedirectResponse:
    return RedirectResponse(PREFIX + "/", status_code=307)


@router.websocket(PREFIX + "/{path:path}")
async def xiaoxin_ws(path: str, ws: WebSocket) -> None:
    """WebSocket 两头对转（按住说话的流式 PCM）。cookie、Origin、Authorization 原样带给小新，鉴权在那边。"""
    import asyncio

    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    url = XIAOXIN_URL.replace("http", "ws", 1) + "/" + path
    if ws.url.query:
        url += "?" + ws.url.query
    keep = ("cookie", "origin", "authorization", "user-agent")
    headers = {k: v for k, v in ws.headers.items() if k.lower() in keep}
    headers["x-forwarded-proto"] = "https"
    headers["x-forwarded-prefix"] = PREFIX
    try:
        up = await connect(url, additional_headers=headers, max_size=4 * 1024 * 1024, open_timeout=5,
                           origin=None)
    except InvalidStatus as e:
        await ws.close(code=4401 if e.response.status_code == 403 else 1011)
        return
    except Exception:
        await ws.close(code=1011)
        return
    await ws.accept()

    async def down() -> None:  # 小新 → 浏览器
        async for msg in up:
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_text(msg)

    async def upward() -> None:  # 浏览器 → 小新
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await up.send(msg["bytes"])
            elif msg.get("text") is not None:
                await up.send(msg["text"])

    tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except ConnectionClosed:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await up.close()
        try:
            await ws.close()
        except Exception:
            pass


@router.api_route(PREFIX + "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                  include_in_schema=False)
async def xiaoxin_proxy(path: str, request: Request) -> Response:
    url = f"{XIAOXIN_URL}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    headers["x-forwarded-proto"] = "https"
    headers["x-forwarded-prefix"] = PREFIX
    body = request.stream() if request.method in ("POST", "PUT", "PATCH") else None
    try:
        upstream = await _client.send(_client.build_request(request.method, url, headers=headers, content=body),
                                      stream=True)
    except httpx.HTTPError as e:
        return Response(f'{{"detail":"小新服务没起来：{type(e).__name__}"}}', 502, media_type="application/json")
    out_headers = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in _DROP_OUT]
    resp = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             background=BackgroundTask(upstream.aclose))
    resp.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
    return resp
