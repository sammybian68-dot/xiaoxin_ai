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
