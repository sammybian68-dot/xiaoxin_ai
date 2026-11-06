"""实时总线：新消息推给能进这条线程的设备，兄妹互相收不到；断线后 ?since= 补齐；未登录连不上。"""

from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from server import store
from tests.test_members import family  # noqa: F401  fixture


def _next(ws, kind):
    while True:
        m = json.loads(ws.receive_text())
        if m["type"] == kind:
            return m


def test_hub_pushes_to_allowed_devices_only(family, fakes):
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    with family["爸爸"].websocket_connect("/api/hub") as dad, family["儿子"].websocket_connect("/api/hub") as son:
        hello = _next(dad, "hello")
        assert dau_t in hello["threads"] and len(hello["threads"]) == 2
        assert _next(son, "hello")["threads"] == [family["儿子"].get("/api/thread").json()["id"]]
        family["女儿"].post(f"/api/conversations/{dau_t}/messages", json={"text": "爸爸你好"})
        got = _next(dad, "msg")
        assert got["thread"] == dau_t and got["message"]["text"] == "爸爸你好"
        assert _next(dad, "msg")["message"]["role"] == "assistant"  # 小新答完也推
        # 儿子什么都没收到：再往儿子线程发一条，他收到的第一条 msg 就是这条
        son_t = family["儿子"].get("/api/thread").json()["id"]
        store.append(son_t, {"role": "user", "text": "我的"})
        assert _next(son, "msg")["message"]["text"] == "我的"


def test_notes_are_not_pushed(family):
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    with family["爸爸"].websocket_connect("/api/hub") as dad:
        _next(dad, "hello")
        store.append(dau_t, {"role": "note", "text": "[system] 现在大约是…"})
        store.append(dau_t, {"role": "user", "text": "第二条"})
        assert _next(dad, "msg")["message"]["text"] == "第二条"


def test_since_returns_only_newer(family):
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    a = store.append(dau_t, {"role": "user", "text": "一"})
    store.append(dau_t, {"role": "user", "text": "二"})
    store.append(dau_t, {"role": "user", "text": "三"})
    d = family["爸爸"].get(f"/api/thread?since={a['id']}").json()
    assert [m["text"] for m in d["messages"]] == ["二", "三"] and d["partial"]
    assert len(family["爸爸"].get("/api/thread?since=nope").json()["messages"]) == 3  # 找不到就给全部


def test_hub_needs_login(anon):
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/api/hub") as ws:
            ws.receive_text()


def test_reload_broadcast_admin_only(client):
    """远程重启：管理员发，推给所有在线连接；页面带版本号。"""
    from server.family import hub

    sent = []
    real = hub.broadcast
    hub.broadcast = lambda ev: sent.append(ev) or 3
    try:
        r = client.post("/api/reload", json={"why": "测试"}).json()
    finally:
        hub.broadcast = real
    assert r["sent"] == 3 and sent[0]["type"] == "reload" and sent[0]["version"] == r["version"]
    assert client.get("/api/version").json()["web"] == r["version"]
    assert f'name="xiaoxin-version" content="{r["version"]}"' in client.get("/").text


def test_static_cache(client):
    """首页给的 ?v= 和文件对得上才长缓存（手机网络卡时省掉 29 个「变了没」）；旧版本号、不带版本号照旧每次确认。"""
    import re

    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    url = re.search(r'static/app\.js\?v=\d+', page.text).group(0)
    assert "immutable" in client.get(url).headers["cache-control"]
    assert client.get("static/app.js?v=1").headers["cache-control"] == "no-cache"
    assert client.get("static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("static/../main.py?v=1").status_code == 404


def test_access_log_hides_key():
    import logging

    import server.main  # noqa: F401  装上过滤器

    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:1", "GET", "/?x=1&k=xd_secret&y=2", "1.1", 303), None)
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(rec)
    assert "xd_secret" not in rec.getMessage() and "k=***&y=2" in rec.getMessage()


def test_reload_not_for_parents(family):
    assert family["爸爸"].post("/api/reload", json={}).status_code == 403
    assert family["女儿"].post("/api/reload", json={}).status_code == 403
