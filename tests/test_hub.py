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
