"""视频通话（阶段 0）：TURN 临时凭证和 coturn 的算法对得上、信令房间两端互转、第三个人进不来。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from server.family import call


def test_ice_without_secret_is_stun_only(client):
    d = client.get("/api/call/ice").json()
    assert d["ttl"] == 0 and len(d["iceServers"]) == 1 and d["iceServers"][0]["urls"][0].startswith("stun:")


def test_turn_credential_matches_coturn_rest_scheme(monkeypatch):
    monkeypatch.setenv("TURN_SECRET", "s3cret")
    d = call.ice_servers("Dad!", now=1000)
    turn = d["iceServers"][1]
    assert turn["username"] == f"{1000 + call.TTL_S}:dad"  # 过期时间:谁（只留安全字符）
    want = base64.b64encode(hmac.new(b"s3cret", turn["username"].encode(), hashlib.sha1).digest()).decode()
    assert turn["credential"] == want
    assert any("transport=udp" in u for u in turn["urls"]) and any("transport=tcp" in u for u in turn["urls"])


def test_signal_relays_between_two_and_rejects_third(client):
    with client.websocket_connect("/api/call/room1") as a:
        assert json.loads(a.receive_text()) == {"type": "peers", "count": 1, "you_first": True}
        with client.websocket_connect("/api/call/room1") as b:
            assert json.loads(a.receive_text())["count"] == 2
            assert json.loads(b.receive_text()) == {"type": "peers", "count": 2, "you_first": False}
            b.send_text(json.dumps({"type": "offer", "sdp": "x"}))
            assert json.loads(a.receive_text()) == {"type": "offer", "sdp": "x"}
            with client.websocket_connect("/api/call/room1") as c:
                assert json.loads(c.receive_text())["type"] == "full"
        assert json.loads(a.receive_text())["type"] == "bye"  # b 走了


def test_signal_needs_login_and_valid_room(anon):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/api/call/room1") as ws:
            ws.receive_text()


# ---------------------------------------------------------------- 来电（家长打给孩子 pad）

from tests.test_compact import at  # noqa: E402,F401  fixture
from tests.test_members import family  # noqa: E402,F401  fixture


def _next(ws, kind):
    while True:
        m = json.loads(ws.receive_text())
        if m["type"] == kind:
            return m


def _reset_calls():
    call._calls.clear()


def test_parent_calls_kid_answer_and_end(family, at):
    from server import store

    _reset_calls()
    at("2026-11-01 15:00")
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    with family["女儿"].websocket_connect("/api/hub") as pad:
        _next(pad, "hello")
        r = family["爸爸"].post("/api/call", json={"kid": family["ids"]["女儿"]})
        assert r.status_code == 200 and r.json()["ice"]["iceServers"]
        c = r.json()["call"]
        inv = _next(pad, "call.invite")["call"]
        assert inv["id"] == c["id"] and inv["caller_name"] == "爸爸" and inv["auto_answer_s"] == 5
        assert family["爸爸"].post("/api/call", json={"kid": family["ids"]["女儿"]}).status_code == 409  # 同时只一通
        assert family["女儿"].post(f"/api/call/{c['id']}/answer").json()["call"]["state"] == "connected"
        assert family["爸爸"].post(f"/api/call/{c['id']}/end").json()["call"]["state"] == "ended"
        assert _next(pad, "call.connected") and _next(pad, "call.ended")
    last = store.get(dau_t)["messages"][-1]
    assert last["via"] == "call" and last["text"].startswith("📹 视频通话") and last["from"] == family["ids"]["爸爸"]


def test_kid_rejects_and_quiet_hours(family, at):
    from server import store

    _reset_calls()
    at("2026-11-01 22:10")  # 免打扰：不自动接
    c = family["爸爸"].post("/api/call", json={"kid": family["ids"]["女儿"]}).json()["call"]
    assert c["auto_answer_s"] == 0
    assert family["女儿"].post(f"/api/call/{c['id']}/reject").json()["call"]["state"] == "rejected"
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    assert "没接（先不接）" in store.get(dau_t)["messages"][-1]["text"]
    assert family["爸爸"].post("/api/call", json={"kid": family["ids"]["女儿"]}).status_code == 200  # 结束了能再打


def test_only_parents_call_and_siblings_cant_touch(family):
    _reset_calls()
    assert family["儿子"].post("/api/call", json={}).status_code == 403
    c = family["爸爸"].post("/api/call", json={"kid": family["ids"]["女儿"]}).json()["call"]
    assert family["儿子"].post(f"/api/call/{c['id']}/answer").status_code == 404
    family["爸爸"].post(f"/api/call/{c['id']}/end")
