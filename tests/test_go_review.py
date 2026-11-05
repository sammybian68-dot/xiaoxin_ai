"""复盘（docs/board-games.md 8.3 的 V1–V8）：关键步按数字挑、事实由代码算、讲解里的坐标和数字要在事实里。"""

from __future__ import annotations

from server.games import go_review as V
from server.games import go_rules as R


def _a(wr: float, score: float, best="D4", pv=None, own=None) -> dict:
    """一条分析（黑方视角）。"""
    return {"winrate": wr, "score": score, "ownership": own or [],
            "best": [{"move": best, "winrate": wr, "score": score, "visits": 400, "pv": pv or [best]}]}


def _game(moves, analysis, me="B") -> dict:
    return {"id": "g", "kid_color": me, "moves": moves, "analysis": analysis, "stones": [], "result": None}


def test_v1_big_drop_is_key_move():
    moves = [["B", "Q16"], ["W", "D4"], ["B", "A1"], ["W", "Q4"]]
    ana = [_a(.5, 0), _a(.5, .5), _a(.5, 0, best="D16"), _a(.2, -6), _a(.2, -6)]
    keys = V.key_moves(_game(moves, ana))
    assert [k["n"] for k in keys] == [3]
    k = keys[0]
    assert k["grade"] == "big" and k["loss_wr"] == 0.3 and k["before"] == 0.5 and k["after"] == 0.2


def test_white_view_flips():
    """孩子执白：胜率要按白方算（存的是黑方视角）。"""
    moves = [["B", "Q16"], ["W", "A1"]]
    ana = [_a(.5, 0), _a(.5, 0), _a(.8, 7)]   # 白下完黑胜率 80% → 白从 50% 掉到 20%
    k = V.key_moves(_game(moves, ana, me="W"))[0]
    assert k["n"] == 2 and k["before"] == 0.5 and k["after"] == 0.2 and k["score_after"] == -7


def test_v2_decided_game_uses_score():
    moves = [["B", "Q16"], ["W", "D4"]]
    ana = [_a(.02, -10), _a(.01, -25), _a(.01, -25)]
    k = V.key_moves(_game(moves, ana))[0]
    assert k["decided"] and k["grade"] == "big" and k["loss_score"] == 15


def test_small_loss_is_not_key():
    moves = [["B", "Q16"], ["W", "D4"]]
    ana = [_a(.5, 0), _a(.47, -1), _a(.47, -1)]
    assert V.key_moves(_game(moves, ana)) == []


def test_single_stone_atari_not_mentioned():
    b = R.Board()
    x, y = R.to_xy("A19"); b.grid[y][x] = "B"
    x, y = R.to_xy("A18"); b.grid[y][x] = "W"
    assert V._group_facts(b, [], "B") == []   # 一线上一颗子被叫吃：不值得说


def test_v3_atari_fact():
    # 黑 A1 B1 两子，白 A2 B2，白 C1 叫吃 → 黑只剩 … 构造：孩子（黑）下 Q16，盘上黑两子只剩 1 气
    b = R.Board()
    for p in ("A1", "B1"):
        x, y = R.to_xy(p); b.grid[y][x] = "B"
    for p in ("A2", "B2"):
        x, y = R.to_xy(p); b.grid[y][x] = "W"
    facts = V._group_facts(b, [], "B")
    assert any("A1 那 2 颗只剩 1 口气（在 C1）" in f and f.startswith("你的") for f in facts)


def test_v4_pv_capture_counted():
    b = R.Board()
    for p in ("A1", "B1"):
        x, y = R.to_xy(p); b.grid[y][x] = "B"
    for p in ("A2", "B2"):
        x, y = R.to_xy(p); b.grid[y][x] = "W"
    notes, kb, kw = V._pv_facts(b, ["Q16", "C1"], "B")
    assert kw == 2 and notes == ["第 2 手 白 C1 提掉 2 颗"]


def test_v5_territory_by_area():
    before = [0.0] * 361
    after = [0.0] * 361
    for y in range(0, 4):
        for x in range(15, 19):
            before[y * 19 + x] = 0.8     # 右上 16 个点原来偏黑（孩子）
            after[y * 19 + x] = 0.3      # 现在只剩一点点：合计少了 8 个点，没有一个点完全翻转
    assert V._territory(before, after, "B") == ["右上角你的地少了大约 8 个点"]
    assert V._territory(after, before, "W") == ["右上角你的地少了大约 8 个点"]  # 执白：黑多了 = 白少了
    small = [v + 0.01 for v in before]
    assert V._territory(before, small, "B") == []


def test_useless_move_fact():
    """一线角上的空着：周围归属没涨，目差掉了 15 → 说「几乎没起作用」。"""
    moves = [["B", "Q16"], ["W", "D4"], ["B", "A1"], ["W", "Q4"]]
    own = [0.0] * 361
    ana = [_a(.5, 0, own=own), _a(.5, 0, own=own), _a(.5, 0, best="D16", own=own), _a(.1, -15, own=own), _a(.1, -15, own=own)]
    fx = V.facts(_game(moves, ana), 3)
    assert "你这手（左下角的第 1 线）在它周围几乎没起作用，等于白白少下了一手。" in fx["lines"]


def test_group_got_worse_fact():
    b = R.Board()
    for p in ("K10", "K11", "K12"):
        x, y = R.to_xy(p); b.grid[y][x] = "B"
    before, after = [0.0] * 361, [0.0] * 361
    for p in ("K10", "K11", "K12"):
        x, y = R.to_xy(p)
        before[y * 19 + x], after[y * 19 + x] = 0.7, -0.8
    facts = V._group_facts(b, after, "B", before)
    assert facts == ["你的K12 那 3 颗（中腹）这手之前还好好的，这手之后小新看很可能被吃"]


def _fx():
    return {"n": 47, "move": "Q16", "best": "R14", "before": .62, "after": .31,
            "lines": ["这是第 47 手，你（黑）下在 Q16。", "这手之前你的胜率 62%，之后 31%；目差从 你领先 3.0 目 变成 你落后 5.0 目。",
                      "小新建议下在 R14（绿圈）。", "你下完以后：你的R10 那 5 颗只剩 1 口气（在 S10），对方下一手就能提掉"]}


def test_v6_made_up_coord_rejected_then_fixed():
    said = iter(["你下在 Q16，应该下 C3。", "你下在 Q16，右边 R10 那 5 颗只剩 1 口气，小新建议下在 R14。"])
    r = V.explain(_fx(), chat=lambda msgs: next(said))
    assert r["source"] == "model" and "R14" in r["text"]
    assert V.check("你下在 Q16，应该下 C3。", _fx()) == ["坐标 C3 在事实里没有"]


def test_v7_wrong_number_rejected_twice_uses_template():
    r = V.explain(_fx(), chat=lambda msgs: "R10 那 6 颗只剩 1 口气。")
    assert r["source"] == "template" and r["problems"] == ["数字 6 在事实里没有"]
    assert r["text"] == "红圈这一手下完，胜率从 62% 掉到了 31%。小新建议下在绿圈那里，可以点「看小新的下法」一步步看。"


def test_model_down_uses_template():
    def boom(msgs):
        raise RuntimeError("502")
    assert V.explain(_fx(), chat=boom)["source"] == "template"


def test_v8_review_caches_explanations():
    moves = [["B", "Q16"], ["W", "D4"], ["B", "A1"], ["W", "Q4"]]
    ana = [_a(.5, 0), _a(.5, .5), _a(.5, 0, best="D16", pv=["D16", "Q4"]), _a(.2, -6, best="B1"), _a(.2, -6)]
    g = _game(moves, ana)
    calls = []

    def chat(msgs):
        calls.append(1)
        return "你下在 A1，小新建议下在 D16。"
    r1 = V.review(g, chat=chat)
    r2 = V.review(g, chat=chat)
    assert len(calls) == 1 and r1["keys"][0]["text"] == r2["keys"][0]["text"] == "你下在 A1，小新建议下在 D16。"
    assert r1["curve"] == [.5, .5, .5, .2, .2] and r1["stats"]["big"] == 1
    assert "第 3 手" in r1["summary"]
    r3 = V.review({**g, "review": {}}, chat=chat, wait=False)   # 不等：讲解没有，数字先给
    assert r3["pending"] == 1 and r3["keys"][0]["pending"] and "text" not in r3["keys"][0] and r3["keys"][0]["facts"]


def test_facts_full_game_uses_real_board():
    moves = [["B", "Q16"], ["W", "D4"], ["B", "A1"], ["W", "Q4"]]
    ana = [_a(.5, 0), _a(.5, .5), _a(.5, 0, best="D16", pv=["D16", "Q4", "C3"]), _a(.2, -6, best="B1", pv=["B1"]), _a(.2, -6)]
    fx = V.facts(_game(moves, ana), 3)
    text = "\n".join(fx["lines"])
    assert "第 3 手，你（黑）下在 A1" in text and "小新建议下在 D16（绿圈）" in text and "D16、Q4" not in text  # 整串变化不给模型
    assert "小新觉得对手最好的应手是 B1（只是看法），对手实际下的是 Q4。" in text
    assert "胜率 50%，之后 20%" in text and "你落后 6.0 目" in text


def test_best_saves_and_ataris():
    b = R.Board()
    for p in ("J6",):
        x, y = R.to_xy(p); b.grid[y][x] = "B"
    for p in ("J7", "K6"):
        x, y = R.to_xy(p); b.grid[y][x] = "W"
    # J6 黑一颗，气在 J5、H6：AI 建议 J5 → 两颗连起来
    assert V._best_saves(b, "J5", "B") == ["小新建议的 J5（绿圈）紧挨着你下边的J6，是给这块棋长气：原来只有 2 口气，下了以后有 4 口"]  # H6 H5 K5 J4
    c = R.Board()
    for p in ("A1", "B1"):
        x, y = R.to_xy(p); c.grid[y][x] = "W"
    x, y = R.to_xy("A2"); c.grid[y][x] = "B"
    # A2 黑只有 2 口气（A3、B2）：B2 同时是给它长气、又叫吃白两子
    assert V._best_saves(c, "B2", "B") == ["小新建议的 B2（绿圈）紧挨着你左下角的A2，是给这块棋长气：原来只有 2 口气，下了以后有 3 口",
                                           "小新建议的 B2（绿圈）能把对手的A1 那 2 颗紧到只剩 1 口气（叫吃）"]
    x, y = R.to_xy("C1"); c.grid[y][x] = "B"
    assert "小新建议的 B2（绿圈）能直接提掉对手的A1 那 2 颗" in V._best_saves(c, "B2", "B")


def test_check_rejects_meta_talk():
    assert V.check("小新建议下在 R14，不过事实里没写理由。", _fx()) == ["不要说「事实」"]


def test_missed_capture_fact():
    """对方一块棋这手之前归孩子（能吃），这手之后归对方（活了）。"""
    b = R.Board()
    for q in ("K10", "K11", "K12"):
        x, y = R.to_xy(q); b.grid[y][x] = "W"
    before, after = [0.0] * 361, [0.0] * 361
    for q in ("K10", "K11", "K12"):
        x, y = R.to_xy(q)
        before[y * 19 + x], after[y * 19 + x] = 0.8, -0.9   # 黑方视角：原来归黑（白棋是死的），现在归白
    assert V._group_facts(b, after, "B", before) == ["对手中腹一共 3 颗子（K12 一带），这手之前小新看你能吃掉，这手之后活了"]
    # 隔一路的另一块白棋：并成一片说
    for q in ("M10", "M11"):
        x, y = R.to_xy(q); b.grid[y][x] = "W"; before[y * 19 + x], after[y * 19 + x] = 0.8, -0.9
    assert V._group_facts(b, after, "B", before) == ["对手中腹一共 5 颗子（K12 一带），这手之前小新看你能吃掉，这手之后活了"]


def test_all_big_losses_no_limit():
    """「亏大发」的手全讲，不限手数：12 手大亏就讲 12 手；没有大亏时讲最大的 3 手小亏。"""
    moves, ana = [], [_a(.5, 0)]
    for i in range(1, 41):
        moves.append(["B" if i % 2 else "W", "pass"])
        ana.append(_a(.5, 0))
    big = list(range(1, 25, 2))                 # 12 手（孩子的手是奇数）
    for n in big:
        ana[n] = _a(.5, -9)                     # 每手亏 9 目
        ana[n + 1] = _a(.5, 0)                  # 小新那手又回来
    keys = [k["n"] for k in V.key_moves(_game(moves, ana))]
    assert keys == big
    ana2 = [_a(.5, 0)] * 41
    ana2 = list(ana2)
    for n, d in ((3, 5), (7, 6), (11, 4.5), (15, 7)):
        ana2[n] = _a(.5, -d)
    assert [k["n"] for k in V.key_moves(_game(moves, ana2))] == [3, 7, 15]


def test_best_connects_two_blocks():
    b = R.Board()
    for q in ("C11", "C13"):
        x, y = R.to_xy(q); b.grid[y][x] = "B"
    out = V._best_saves(b, "C12", "B")
    assert out[0] == "小新建议的 C12（绿圈）能把你左边的C11和C13连成一块"


def test_taken_point_and_weak_groups():
    """孩子（黑）第 1 手没下 C12，小新第 2 手就下在 C12，之后黑 C11、C13 归属从 0.98 掉到 0.1。"""
    moves = [["B", "Q16"], ["W", "C12"], ["B", "D4"]]
    own0 = [0.0] * 361
    own2 = [0.0] * 361
    for q in ("C11", "C13"):
        x, y = R.to_xy(q); own0[y * 19 + x] = 0.98; own2[y * 19 + x] = 0.1
    g = {"id": "g", "kid_color": "B", "moves": moves, "stones": [], "result": None,
         "analysis": [_a(.5, 0, own=own0), _a(.5, 0, own=own0), _a(.2, -6, own=own2), _a(.2, -6, own=own2)]}
    # 构造盘面：C11、C13 黑子放进初始子
    g["stones"] = ["C11", "C13"]
    out = V._taken(g, 1, "C12", "B")
    assert out[0] == "绿圈 C12 这个点双方都想要：你没下，对手紧接着就下在了这里"
    assert out[1] == "对手下了 C12 以后，你左边的C11、C13变得很弱，可能被吃"
    assert V._taken(g, 1, "D16", "B") == []   # 小新没下在那：不说


def test_check_rejects_ai_word():
    assert V.check("AI 建议下在 R14。", _fx()) == ["不要说「AI」"]
