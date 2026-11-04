"""下棋接口（docs/board-games.md 8.5 的 U1–U3、U8 在接口这一层）：开局、落子、长轮询等小新、悔棋、提示、
认输、复盘、看板、设置；孩子只能碰自己的棋，家长能看孩子的。用假 KataGo。"""

from __future__ import annotations

import threading

import pytest

from server.games import go
from tests.test_go_engine import FakeKata
from tests.test_members import family  # noqa: F401  fixture


@pytest.fixture
def kata(monkeypatch):
    k = FakeKata()
    monkeypatch.setattr(go.engine, "query", k.query)
    yield k
    for t in [t for t in threading.enumerate() if t.name == "go-respond"]:
        t.join(5)


def _settle(c, g):
    return c.get(f"/api/go/{g['id']}?wait={len(g['moves'])}").json()


def test_u1_play_flow(family, kata):
    kid = family["儿子"]
    g = kid.post("/api/go", json={"level": "3k"}).json()
    assert g["level_name"] == "3 级" and g["kid_color"] == "B" and g["to_move"] == "B" and len(g["board"]) == 19
    g = kid.post(f"/api/go/{g['id']}/move", json={"move": "Q16"}).json()
    assert g["thinking"] and g["moves"] == [["B", "Q16"]]
    g = _settle(kid, g)
    assert not g["thinking"] and len(g["moves"]) == 2 and g["to_move"] == "B"
    r = kid.post(f"/api/go/{g['id']}/move", json={"move": "Q16"})
    assert r.status_code == 400 and "已经有子" in r.json()["detail"]


def test_u2_current_game_survives_reload(family, kata):
    kid = family["儿子"]
    g = _settle(kid, kid.post(f"/api/go/{kid.post('/api/go', json={}).json()['id']}/move", json={"move": "C3"}).json())
    cur = kid.get("/api/go/current").json()
    assert cur["id"] == g["id"] and cur["moves"] == g["moves"] and cur["level"] == "2k"  # 设置里的默认 2 级


def test_u3_undo_hint_resign_count(family, kata):
    family["admin"].put("/api/games/go", json={"undo": True, "hint": True})
    kid = family["儿子"]
    g = _settle(kid, kid.post(f"/api/go/{kid.post('/api/go', json={'level': '3k'}).json()['id']}/move", json={"move": "C3"}).json())
    assert len(kid.post(f"/api/go/{g['id']}/hint", json={}).json()["hints"]) >= 1
    assert kid.post(f"/api/go/{g['id']}/undo", json={}).json()["moves"] == []
    g = _settle(kid, kid.post(f"/api/go/{g['id']}/move", json={"move": "C3"}).json())
    assert kid.post(f"/api/go/{g['id']}/count", json={}).json()["result"]["by"] == "count"
    g2 = kid.post("/api/go", json={"level": "1d"}).json()
    assert kid.post(f"/api/go/{g2['id']}/resign", json={}).json()["result"]["by"] == "resign"


def test_paused_game_blocks_new_until_resign(family, kata):
    """暂停不算结束：还有没下完的就不能直接开新的（409），只有明确 replace（界面上「再来一盘」）才把旧的算认输。"""
    kid = family["儿子"]
    a = kid.post("/api/go", json={}).json()
    r = kid.post("/api/go", json={})
    assert r.status_code == 409 and "没下完" in r.json()["detail"]
    assert kid.get("/api/go/current").json()["id"] == a["id"] and go.get(a["id"])["result"] is None
    b = kid.post("/api/go", json={"replace": True}).json()
    assert go.get(a["id"])["result"]["by"] == "resign" and kid.get("/api/go/current").json()["id"] == b["id"]
    kid.post(f"/api/go/{b['id']}/resign", json={})
    assert kid.get("/api/go/current").json() == {} and kid.post("/api/go", json={}).status_code == 200


def test_settings_toggle_undo_and_hint(family, kata):
    admin, kid = family["admin"], family["儿子"]
    s = admin.get("/api/games").json()
    assert [x["id"] for x in s["games"]][0] == "go" and s["go"]["level"] == "2k"
    # 辅助（胜率、提示、悔棋、提醒）默认全关：开着孩子会依赖；落子声默认开
    assert [s["go"][k] for k in ("winbar", "hint", "undo", "warn", "sound")] == [False, False, False, False, True]
    assert s["levels"][0] == {"id": "world", "name": "世界冠军"} and s["levels"][1]["name"] == "中国冠军"
    s = admin.put("/api/games/go", json={"undo": False, "hint": False, "level": "1d"}).json()
    assert s["go"]["undo"] is False and s["go"]["level"] == "1d"
    g = kid.post("/api/go", json={}).json()
    assert g["level"] == "1d"
    assert kid.post(f"/api/go/{g['id']}/undo", json={}).status_code == 400
    assert kid.post(f"/api/go/{g['id']}/hint", json={}).status_code == 400
    assert admin.put("/api/games/go", json={"level": "99d"}).status_code == 400
    assert kid.put("/api/games/go", json={"undo": True}).status_code == 403   # 孩子不能改设置


def test_u8_kid_cannot_see_sibling_parent_can(family, kata):
    son, dau, dad = family["儿子"], family["女儿"], family["爸爸"]
    g = son.post("/api/go", json={}).json()
    assert dau.get(f"/api/go/{g['id']}").status_code == 404
    assert dau.post(f"/api/go/{g['id']}/move", json={"move": "Q16"}).status_code == 404
    sid = family["ids"]["儿子"]
    assert dad.get(f"/api/go/{g['id']}?kid={sid}").status_code == 200
    assert dad.get(f"/api/go-stats?kid={sid}").json()["games"][0]["id"] == g["id"]


def test_review_and_stats(family, kata):
    kid = family["儿子"]
    g = kid.post("/api/go", json={"level": "3k"}).json()
    for mv in ("C3", "R3", "C17"):
        g = _settle(kid, kid.post(f"/api/go/{g['id']}/move", json={"move": mv}).json())
    g = kid.post(f"/api/go/{g['id']}/resign", json={}).json()
    r = kid.get(f"/api/go/{g['id']}/review").json()
    assert len(r["curve"]) == len(r["moves"]) + 1 and r["level_name"] == "3 级" and r["result"]["by"] == "resign"
    st = kid.get("/api/go-stats").json()
    assert st["games"][0]["id"] == g["id"] and (st["total"], st["won"], st["lost"]) == (1, 0, 1)   # 下了几手就认输也算一盘输


def test_engine_down_gives_503(family, monkeypatch):
    def down(body, timeout=60):
        raise go.EngineError("小新的棋盘大脑没起来")
    monkeypatch.setattr(go.engine, "query", down)
    r = family["儿子"].post("/api/go", json={})
    assert r.status_code == 503 and "没起来" in r.json()["detail"]


# ---------------------------------------------------------------- 聊天里的工具（U7 在工具这一层）
@pytest.fixture
def as_kid():
    from server.tools import printer as printer_tools

    tok = printer_tools.ctx.set({"thread": "t1", "kid": "k1"})
    yield "k1"
    printer_tools.ctx.reset(tok)


def _tool(name, **args):
    import json

    from server.tools import games as games_tools

    return games_tools.run(name, json.dumps(args))


def test_tool_play_go_card(as_kid, kata):
    out = _tool("play_go", level="1d", color="W")
    assert "对手 1 段，你执白" in out and "播报：" in out
    assert "对手 2 级" in _tool("play_go")      # 默认档
    g = go.new("k1", level="3k")               # 有一盘暂停着：不出新卡，直接接着下
    out = _tool("play_go", level="1d")
    assert f"[go:{g['id']}]" in out and "接着下" in out and "认输" in out
    assert _tool("play_go", level="99d").startswith("失败")


def test_tool_stats_and_review(as_kid, kata, monkeypatch):
    import datetime as dt

    assert "还没和小新下过棋" in _tool("go_stats")
    assert "还没有能复盘的棋" in _tool("go_review")
    g = go.new("k1", level="3k")
    g = go.get(g["id"])
    g["moves"] = [["B" if i % 2 == 0 else "W", "pass"] for i in range(12)]
    g["analysis"] = [g["analysis"][0]] * 13
    g["result"] = {"winner": "B", "by": "count", "text": "黑胜 3.5 目"}
    go.save(g)
    today = dt.date.today().isoformat()
    one = _tool("go_review")
    assert f"[go:{g['id']}]" in one and "黑胜 3.5 目" in one
    h = go.new("k1", level="1d")
    h = go.get(h["id"]); h["moves"] = g["moves"]; h["analysis"] = g["analysis"]; go.save(h)
    two = _tool("go_review", date=today)
    assert "下了 2 盘" in two and f"[go:{g['id']}]" in two and f"[go:{h['id']}]" in two
    assert "没有下过棋" in _tool("go_review", date="2020-01-01")
    assert "下完 1 盘：1 胜 0 负" in _tool("go_stats")


def test_tool_needs_kid(kata):
    assert _tool("play_go").startswith("失败：这里不能下棋")


def test_clear_all_needs_password(family, kata, monkeypatch):
    import hashlib

    from server.games import api as games_api

    # 真密码不写进仓库：测试里换成一个测试密码的哈希
    monkeypatch.setattr(games_api, "CLEAR_PW_SHA256", hashlib.sha256(b"test-pw").hexdigest())
    admin, kid = family["admin"], family["儿子"]
    kid.post("/api/go", json={})
    assert admin.post("/api/games/go/clear", json={"password": "123"}).status_code == 403
    assert len(go.all_games()) == 1
    assert kid.post("/api/games/go/clear", json={"password": "test-pw"}).status_code == 403   # 只有管理员
    assert admin.post("/api/games/go/clear", json={"password": "test-pw"}).json() == {"deleted": 1}
    assert go.all_games() == [] and kid.get("/api/go-stats").json()["games"] == []
