"""对局和引擎（docs/board-games.md 8.2 的 E1–E4，加悔棋、提示、停一手、数子、异步应手）。用假 KataGo，不联网。
E5–E7（真 KataGo 的用时和棋力）在 tests/test_live.py，加 --live 才跑。"""

from __future__ import annotations

import threading

import pytest

from server.games import go
from server.games import go_rules as R
from server.games.katago import EngineError


class FakeKata:
    """按局面回一个固定格式的分析：最佳点 = 棋盘上第一个空的星位；humanPolicy 均匀给几个点。
    胜率、目差和真 KataGo 一样按「轮到谁下」报（SIDETOMOVE）：黑方视角固定是 60%、+2 目。"""

    def __init__(self):
        self.queries = []
        self.fail = 0
        self.top = None          # 指定最佳点（测 pass）
        self.policy_pts = None   # 指定 humanPolicy 的点
        self.pass_cost = 0.0     # 「只许停一手」比正常下少几目（测申请数棋）
        self.black_wr = 0.6      # 黑方视角的胜率（测认输、胜负已定）

    def query(self, body, timeout=60):
        self.queries.append(body)
        if self.fail:
            self.fail -= 1
            raise EngineError("小新的棋盘大脑挂了")
        b = R.replay(body["moves"], [s for _, s in body.get("initialStones", [])])
        free = [p for p in ["Q16", "D4", "Q4", "D16", "K10", "C3", "R17"] if b.at(*R.to_xy(p)) == "."]
        free = free or [R.to_gtp(x, y) for y in range(19) for x in range(19) if b.grid[y][x] == "."]   # 星位下满了
        top = self.top or free[0]
        pol = [0.0] * 362
        for p in (self.policy_pts or free[:3]):
            x, y = R.to_xy(p)
            pol[y * 19 + x] = 1.0
        moves = body["moves"]
        black = (moves[-1][0] == "W") if moves else body.get("initialPlayer", "B") == "B"
        wr, sc = (self.black_wr, 2.0) if black else (1 - self.black_wr, -2.0)
        if body.get("allowMoves"):   # 只许停一手：目差少 pass_cost
            sc -= self.pass_cost
            top = "pass"
        return {"rootInfo": {"winrate": wr, "scoreLead": sc, "visits": body["maxVisits"]},
                "moveInfos": [{"move": top, "order": 0, "winrate": wr, "scoreLead": sc, "visits": 10, "pv": [top, "C17"]}],
                "ownership": [0.0] * 361, "humanPolicy": pol, "policy": pol}


@pytest.fixture
def kata(monkeypatch):
    k = FakeKata()
    monkeypatch.setattr(go.engine, "query", k.query)
    monkeypatch.setattr(go, "rng", __import__("random").Random(7))
    yield k
    for t in [t for t in threading.enumerate() if t.name == "go-respond"]:
        t.join(5)


def _done(g):
    return go.wait(g["id"], "k1", len(g["moves"]), timeout=5)


def test_kid_move_lands_now_engine_answers_later(kata):
    g = go.new("k1", level="3k")
    assert not g["thinking"] and len(g["analysis"]) == 1
    g = go.play(g["id"], "k1", "Q16")
    assert g["thinking"] and g["moves"] == [["B", "Q16"]] and g["analysis"][-1] is None  # 孩子那手马上落
    g = _done(g)
    assert not g["thinking"] and len(g["moves"]) == 2 and g["moves"][1][0] == "W"
    assert len(g["analysis"]) == 3 and all(g["analysis"])  # 开局前、孩子那手后、小新那手后都分析了


def test_e1_human_level_samples_legal_policy_points(kata):
    kata.policy_pts = ["Q16", "C3", "R17"]
    g = go.new("k1", level="3k")
    g = _done(go.play(g["id"], "k1", "Q16"))  # Q16 已被孩子占了，不能抽到
    assert g["moves"][1][1] in ("C3", "R17")
    human = [q for q in kata.queries if "overrideSettings" in q]
    assert human and human[0]["overrideSettings"] == {"humanSLProfile": "rank_3k", "ignorePreRootHistory": False}
    assert human[0]["includePolicy"] is True


def test_human_level_passes_only_when_best_is_pass(kata):
    kata.top = "pass"
    g = _done(go.play(go.new("k1", level="1d")["id"], "k1", "Q16"))
    assert g["moves"][1] == ["W", "pass"]


@pytest.mark.parametrize("level,visits", [("world", 3000), ("china", 1500)])
def test_e2_champions_take_best_with_visits(kata, level, visits):
    g = _done(go.play(go.new("k1", level=level)["id"], "k1", "C3"))
    assert g["moves"][1][1] == "Q16"
    assert any(q["maxVisits"] == visits and "overrideSettings" not in q for q in kata.queries)


def test_e3_engine_error_is_shown_and_retry_works(kata):
    g = go.new("k1", level="3k")
    kata.fail = 2   # 分析孩子那手、算应手都挂
    g = _done(go.play(g["id"], "k1", "Q16"))
    assert not g["thinking"] and "挂了" in g["error"] and len(g["moves"]) == 1
    g = _done(go.retry(g["id"], "k1"))
    assert g["error"] == "" and len(g["moves"]) == 2 and all(g["analysis"])


def test_cannot_move_while_thinking(kata):
    gate = threading.Event()
    real = kata.query

    def slow(body, timeout=60):
        gate.wait(5)
        return real(body, timeout)
    g = go.new("k1", level="3k")
    kata_query = go.engine.query
    go.engine.query = slow
    try:
        g = go.play(g["id"], "k1", "Q16")
        with pytest.raises(go.GameError, match="在想"):
            go.play(g["id"], "k1", "D4")
        with pytest.raises(go.GameError, match="在想"):
            go.undo(g["id"], "k1")
    finally:
        gate.set()
        go.engine.query = kata_query
    _done(g)


def test_illegal_move_message(kata):
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "Q16"))
    with pytest.raises(go.GameError, match="已经有子"):
        go.play(g["id"], "k1", "Q16")


def test_undo_two_moves(kata):
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "Q16"))
    g = _done(go.play(g["id"], "k1", "C3"))
    assert len(g["moves"]) == 4
    g = go.undo(g["id"], "k1")
    assert len(g["moves"]) == 2 and len(g["analysis"]) == 3 and g["undos"] == 1


def test_hint_returns_top_points(kata):
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "C3"))
    assert go.hint(g["id"], "k1")[0]["move"] and go.get(g["id"])["hints"] == 1


def test_white_and_handicap_engine_goes_first(kata):
    g = go.new("k1", level="3k", color="W")
    assert g["thinking"]
    g = _done(g)
    assert g["moves"][0][0] == "B" and g["kid_color"] == "W"
    h = _done(go.new("k1", level="3k", color="B", handicap=4))
    assert h["komi"] == 0.5 and h["moves"][0][0] == "W" and len(h["stones"]) == 4  # 让子：白先


def test_two_passes_end_with_count(kata):
    kata.top = "pass"
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "pass"))
    assert g["result"]["by"] == "count" and g["result"]["winner"] in ("B", "W")


def test_resign(kata):
    g = go.resign(go.new("k1", level="3k")["id"], "k1")
    assert g["result"]["winner"] == "W" and g["result"]["by"] == "resign"
    with pytest.raises(go.GameError, match="结束"):
        go.play(g["id"], "k1", "Q16")


def test_view_from_kid_side(kata):
    g = _done(go.play(go.new("k1", level="2k", color="B")["id"], "k1", "C3"))
    v = go.view(g)
    assert v["winrate"] == 0.6 and v["score"] == 2.0 and v["level_name"] == "2 级" and len(v["board"]) == 19
    w = _done(go.new("k1", level="2k", color="W"))
    assert go.view(w)["winrate"] == 0.4   # 白方视角


def test_other_kid_cannot_touch(kata):
    g = go.new("k1", level="3k")
    with pytest.raises(go.GameError):
        go.play(g["id"], "k2", "Q16")


def test_current_game_and_list(kata):
    a = go.new("k1", level="3k")
    go.resign(a["id"], "k1")
    b = go.new("k1", level="1d")
    assert go.current("k1")["id"] == b["id"] and [x["id"] for x in go.all_games("k1")] == [b["id"], a["id"]]


def test_levels_list():
    ids = [i for i, _ in go.LEVELS]
    assert ids[:2] == ["world", "china"] and ids[2] == "9d" and ids[10] == "1d" and ids[11] == "1k" and ids[-1] == "20k"
    assert go.LEVEL_NAME["world"] == "世界冠军" and go.LEVEL_NAME["china"] == "中国冠军"


def test_restart_while_thinking_heals(kata):
    """存档里 thinking=True 但没有线程在算（服务重启）：读出来是「断了」，再试一次能接着下。"""
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "Q16"))
    raw = go.get(g["id"])
    raw["moves"].append(["B", "C3"]); raw["analysis"].append(None); raw["thinking"] = True
    go.save(raw)   # 模拟：孩子落了子，服务就挂了
    g = go.get(g["id"])
    assert not g["thinking"] and "断了" in g["error"]
    g = _done(go.retry(g["id"], "k1"))
    assert g["error"] == "" and len(g["moves"]) == 4 and all(g["analysis"])


# ---------------------------------------------------------------- 申请数棋（KataGo 判，不用大模型）
def test_count_refused_when_unfinished(kata):
    kata.pass_cost = 9.0
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "C3"))
    g, check = go.count(g["id"], "k1")
    assert g["result"] is None and not check["ok"] and check["why"] == "unfinished"
    assert "还不能数棋" in check["reason"] and "大约 9 目" in check["reason"] and check["area"]
    assert any(q.get("allowMoves") for q in kata.queries)
    assert go.get(g["id"])["count_requests"] == [{"n": 2, "ok": False, "why": "unfinished"}]


def test_count_ok_when_finished(kata):
    kata.pass_cost = 0.5
    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "C3"))
    g, check = go.count(g["id"], "k1")
    assert check["ok"] and check["why"] == "finished" and g["result"]["by"] == "count"
    r = g["result"]
    assert "黑贴 3¾ 子" in r["text"] and r["text"].startswith("黑 ") and len(r["owner"]) == 361


def test_count_ok_when_decided(kata):
    kata.pass_cost = 9.0
    kata.black_wr = 0.01
    g = go.new("k1", level="3k")
    raw = go.get(g["id"])
    raw["moves"] = [["B" if i % 2 == 0 else "W", "pass"] for i in range(100)]
    raw["analysis"] = [dict(raw["analysis"][0], winrate=0.01)] * 101
    go.save(raw)
    g, check = go.count(g["id"], "k1")
    assert check["ok"] and check["why"] == "decided"


def test_zi():
    assert [go._zi(x) for x in (7.5, 0.5, 1.5, 8, 0)] == ["3¾", "¼", "¾", "4", "0"]


def test_engine_never_resigns(kata):
    """小新从不认输：孩子大优、下满 60 手，小新照样接着下（只能孩子认输、双方停一手或申请数棋结束）。"""
    from server.games import go_rules as R

    kata.black_wr = 0.995
    g = go.new("k1", level="10k")
    raw = go.get(g["id"])
    pts = [R.to_gtp(x, y) for y in range(19) for x in range(19)][:60]
    raw["moves"] = [["B" if i % 2 == 0 else "W", p] for i, p in enumerate(pts)]
    raw["analysis"] = [dict(raw["analysis"][0], winrate=0.995)] * 61
    go.save(raw)
    g = _done(go.play(g["id"], "k1", "K10"))
    assert g["result"] is None and g["moves"][-1][0] == "W"


def test_hint_limit(kata, monkeypatch):
    from server.games import registry

    g = _done(go.play(go.new("k1", level="3k")["id"], "k1", "C3"))
    for _ in range(3):
        go.hint(g["id"], "k1")
    with pytest.raises(go.GameError, match="3 次提示用完了"):
        go.hint(g["id"], "k1")
    monkeypatch.setattr(registry, "settings", lambda game: {**go.DEFAULTS, "hint_max": 0})
    go.hint(g["id"], "k1")                       # 0 = 不限


def test_engine_does_not_resign_early(kata):
    kata.black_wr = 0.995
    g = _done(go.play(go.new("k1", level="10k")["id"], "k1", "K10"))
    assert g["result"] is None and len(g["moves"]) == 2   # 才 1 手，不认输


def test_explanation_made_during_game_and_dropped_on_undo(kata, monkeypatch):
    """下着棋就把亏大的那手讲解生成好；悔棋后作废。"""
    import threading as _th

    from server.games import go_review

    monkeypatch.setattr(go_review, "explain", lambda fx, chat=None: {"text": f"讲 {fx['n']}", "source": "model", "problems": []})
    g = go.new("k1", level="3k")
    kata.black_wr = 0.1              # 孩子落子后胜率从 60% 掉到 10%：大亏
    g = _done(go.play(g["id"], "k1", "C3"))
    for t in [t for t in _th.enumerate() if t.name == "go-review"]:
        t.join(5)
    assert go.get(g["id"])["review"]["1"]["text"] == "讲 1"
    g = go.undo(g["id"], "k1")
    assert go.get(g["id"])["review"] == {}
