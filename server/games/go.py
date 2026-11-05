"""围棋对局：开局、孩子落子、小新应手、悔棋、停一手、认输、数子；一盘一个文件。说明见 docs/board-games.md。

每一手（双方）落下后都用主模型分析一次（胜率、目差、最佳点、变化、归属），存进 analysis[i]（第 i 手之后的局面），
复盘直接用。小新的应手：冠军两档用主模型搜索取最佳点；段 / 级档用人类模型按 humanPolicy 抽一个（照 KataGo 的 Human SL 指南）。

孩子落子立刻生效（盘面、提子马上回给界面），小新的应手在后台线程算：「分析孩子这手」和「小新下哪」并行，
下完再分析一次。界面用 wait()（长轮询）等手数变了再画。算的时候 g["thinking"] = True，这时不能悔棋、不能落子。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from server import store
from server.games import go_rules as R
from server.games.katago import EngineError, engine

# 档位：id → (显示名, 怎么下)。顺序就是选择列表的顺序，强的在前
LEVELS = [("world", "世界冠军"), ("china", "中国冠军")] + \
         [(f"{d}d", f"{d} 段") for d in range(9, 0, -1)] + [(f"{k}k", f"{k} 级") for k in range(1, 21)]
LEVEL_NAME = dict(LEVELS)
CHAMP_VISITS = {"world": 3000, "china": 1500}
ANALYZE_VISITS = 400      # 每手分析（胜率曲线、亏了多少）：A100 上 ~0.3 秒
ADVICE_VISITS = 1500      # 复盘里的「小新建议」：要讲的那几手再用中国冠军档的搜索量重算一遍最好的点（~2 秒一手）
HUMAN_VISITS = 60         # 段级档落子前搜一下：只用来判断该不该 pass
KOMI = 7.5
# 申请数棋（全由 KataGo + 代码判，不用大模型）
DONE_GAIN = 1.5      # 双方「下一手最好的点」比「停一手」都多不到 1.5 目：棋下完了
DECIDED_WR, DECIDED_MIN, DECIDED_RUN = 0.03, 100, 10   # 下满 100 手，孩子胜率连续 10 手 < 3% 或 > 97%：胜负已定（家长 2026-11-05：3 手太少）
COUNT_VISITS = 400
# 胜率条、提示、悔棋、下坏了提醒是辅助，默认全关：开着孩子会依赖（家长 2026-11-04 要求），要用在设置里开
DEFAULTS = {"level": "2k", "color": "B", "komi": KOMI, "handicap": 0,
            "undo": False, "hint": False, "hint_max": 3, "winbar": False, "warn": False, "sound": True}

_locks: dict[str, threading.Lock] = {}
_changed = threading.Condition()   # 有对局变了（小新下完、出错）：叫醒 wait() 的
_active: set[str] = set()          # 正在后台算应手的对局（服务重启后这里是空的，见 _heal）
rng = random.Random()  # 段级档按人类策略抽子；测试里换成固定种子


class GameError(ValueError):
    """给孩子看的错。"""


def _dir():
    d = store.DATA / "games" / "go"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(gid: str):
    if not gid.isalnum() or len(gid) > 32:
        raise GameError("没有这盘棋")
    return _dir() / f"{gid}.json"


def save(g: dict) -> None:
    p = _path(g["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def get(gid: str) -> dict:
    p = _path(gid)
    if not p.is_file():
        raise GameError("没有这盘棋")
    return _heal(json.loads(p.read_text(encoding="utf-8")))


def all_games(kid: str = "") -> list[dict]:
    """新的在前。"""
    out = []
    for p in _dir().glob("*.json"):
        try:
            g = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not kid or g.get("kid") == kid:
            out.append(g)
    return sorted(out, key=lambda g: -g["created"])


def current(kid: str) -> dict | None:
    """这个孩子还没下完的那盘（最新的一盘）。"""
    for g in all_games(kid):
        if g.get("result") is None:
            return g
    return None


def clear_all() -> int:
    """删掉所有下棋记录（设置里「清空下棋记录」，接口那边校验过密码）。返回删了几盘。正在算应手的那盘也删。"""
    n = 0
    for p in _dir().glob("*.json"):
        p.unlink(missing_ok=True)
        n += 1
    _active.clear()
    _reviewing.clear()
    return n


_reviewing: set[str] = set()   # 正在后台生成复盘讲解的对局


def review_later(gid: str) -> bool:
    """后台把这盘的复盘讲解生成好（每好一手存一次盘、叫醒等着的界面）。已经在生成就不重复起。"""
    if gid in _reviewing:
        return False
    _reviewing.add(gid)

    def run():
        from server.games import go_review

        try:
            g = get(gid)
            if any(a is None for a in g["analysis"]):
                return

            def saved(n, c):
                with lock(gid):
                    cur = get(gid)
                    if cur["moves"][:n + 1] != g["moves"][:n + 1]:   # 生成期间悔棋了：这条作废
                        return
                    cur.setdefault("review", {})[str(n)] = c
                    save(cur)
                with _changed:
                    _changed.notify_all()
            go_review.fill(g, on_one=saved, deepen=deep_best)
        except Exception:  # noqa: BLE001 讲解没生成出来：下次打开复盘再试
            logging.getLogger("xiaoxin.go").exception("review %s", gid)
        finally:
            _reviewing.discard(gid)
            with _changed:
                _changed.notify_all()
    threading.Thread(target=run, daemon=True, name="go-review").start()
    return True


def reviewing(gid: str) -> bool:
    return gid in _reviewing


def wait_review(gid: str, have: int, timeout: float = 25) -> None:
    """长轮询：等讲解好的手数超过 have，或者生成结束。"""
    end = time.time() + timeout
    with _changed:
        while time.time() < end and gid in _reviewing:
            if sum("text" in c for c in (get(gid).get("review") or {}).values()) > have:
                return
            _changed.wait(min(1.0, end - time.time()))


def lock(gid: str) -> threading.Lock:
    return _locks.setdefault(gid, threading.Lock())


# ------------------------------------------------------------ 引擎
def _query(g: dict, moves: list, **kw) -> dict:
    body = {"moves": moves, "initialStones": [["B", s] for s in g.get("stones", [])],
            "initialPlayer": "W" if g.get("stones") else "B",
            "rules": "chinese", "komi": g["komi"], "boardXSize": R.N, "boardYSize": R.N}
    body.update(kw)
    return engine.query(body)


def _black_view(r: dict, to_move: str) -> dict:
    """KataGo 报的是「轮到谁下」那一方的胜率 / 目差 / 归属（SIDETOMOVE）。统一换成黑方视角存，复盘好算。"""
    s = 1 if to_move == "B" else -1
    ri = r["rootInfo"]
    top = sorted(r.get("moveInfos") or [], key=lambda m: m["order"])
    return {
        "winrate": round(ri["winrate"] if s == 1 else 1 - ri["winrate"], 4),
        "score": round(ri["scoreLead"] * s, 2),
        "best": [{"move": m["move"], "winrate": round(m["winrate"] if s == 1 else 1 - m["winrate"], 4),
                  "score": round(m["scoreLead"] * s, 2), "visits": m["visits"], "pv": m.get("pv", [])[:8]}
                 for m in top[:5]],
        "ownership": [round(v * s, 2) for v in r.get("ownership") or []],
        "visits": ri.get("visits", 0),
    }


def analyze(g: dict, upto: int) -> dict:
    """第 upto 手之后的局面（0 = 开局前）分析一次，黑方视角。"""
    moves = g["moves"][:upto]
    to_move = R.other(moves[-1][0]) if moves else ("W" if g.get("stones") else "B")
    r = _query(g, moves, maxVisits=ANALYZE_VISITS, includeOwnership=True)
    return _black_view(r, to_move)


def deep_best(g: dict, upto: int) -> list[dict]:
    """第 upto 手之后的局面，用 ADVICE_VISITS 次搜索算「最好的点」和变化（黑方视角，格式同 analyze 的 best）。
    建议只看这个，和对手是几级无关：每手分析和建议都用主模型（b28，最强的那个），不用人类模型。"""
    moves = g["moves"][:upto]
    to_move = R.other(moves[-1][0]) if moves else ("W" if g.get("stones") else "B")
    return _black_view(_query(g, moves, maxVisits=ADVICE_VISITS), to_move)["best"]


def engine_move(g: dict) -> str:
    """小新这一手下哪：坐标或 pass。"""
    moves = g["moves"]
    color = g["engine"]
    lv = g["level"]
    if lv in CHAMP_VISITS:
        r = _query(g, moves, maxVisits=CHAMP_VISITS[lv])
        return min(r["moveInfos"], key=lambda m: m["order"])["move"]
    r = _query(g, moves, maxVisits=HUMAN_VISITS, includePolicy=True,
               overrideSettings={"humanSLProfile": f"rank_{lv}", "ignorePreRootHistory": False})
    top = min(r["moveInfos"], key=lambda m: m["order"])
    if top["move"].lower() == "pass":
        return "pass"
    pol = r.get("humanPolicy") or r.get("policy") or []
    board = R.replay(moves, g.get("stones"))
    pts, wts = [], []
    for i, p in enumerate(pol[:R.N * R.N]):
        if p > 0 and board.legal(color, i % R.N, i // R.N):
            pts.append(R.to_gtp(i % R.N, i // R.N))
            wts.append(p)
    if not pts:
        return top["move"]
    return rng.choices(pts, wts)[0]


# ------------------------------------------------------------ 对局
def new(kid: str, *, level: str, color: str = "B", komi: float | None = None, handicap: int = 0) -> dict:
    if level not in LEVEL_NAME:
        raise GameError(f"没有这个档位：{level}")
    if color not in ("B", "W"):
        raise GameError("执黑还是执白？")
    handicap = max(0, min(int(handicap or 0), 9))
    if handicap == 1:
        handicap = 0
    stones = R.HANDICAP.get(handicap, [])
    g = {"id": uuid.uuid4().hex[:16], "kind": "go", "kid": kid, "created": time.time(), "level": level,
         "kid_color": color, "engine": R.other(color), "komi": 0.5 if stones else float(KOMI if komi is None else komi),
         "handicap": handicap, "stones": stones, "moves": [], "analysis": [], "result": None, "undos": 0, "hints": 0}
    g["analysis"].append(analyze(g, 0))
    g["thinking"] = _to_move(g) == g["engine"]   # 执白 / 让子：小新先下
    save(g)
    if g["thinking"]:
        _respond_later(g["id"])
    return g


def _to_move(g: dict) -> str:
    if g["moves"]:
        return R.other(g["moves"][-1][0])
    return "W" if g.get("stones") else "B"


def _respond_later(gid: str) -> None:
    _active.add(gid)
    threading.Thread(target=_respond, args=(gid,), daemon=True, name="go-respond").start()


def _heal(g: dict) -> dict:
    """存档里写着「在想」，可没有线程在算（服务中途重启了）：改成出错，界面上点「再试一次」接着算。"""
    if g.get("thinking") and g["id"] not in _active:
        g["thinking"], g["error"] = False, "小新刚才断了一下"
    return g


def _respond(gid: str) -> None:
    """后台：补上孩子那手的分析，小新应一手，再分析。出错记在 g["error"]，界面上能「再试一次」。"""
    try:
        g = get(gid)
        n = len(g["moves"])
        with ThreadPoolExecutor(2) as ex:
            need_ana = g["analysis"][-1] is None
            fa = ex.submit(analyze, g, n) if need_ana else None
            fm = ex.submit(engine_move, g) if g["result"] is None and not _passed_twice(g) else None
            ana = fa.result() if fa else None
            mv = fm.result() if fm else None
        with lock(gid):
            g = get(gid)
            if len(g["moves"]) != n:      # 期间被悔棋改了：作废
                return
            if ana is not None:
                g["analysis"][-1] = ana
                g["last_loss"] = _loss_of_last(g)
            if _passed_twice(g):
                finish_by_count(g)
            elif mv is not None and g["result"] is None:   # 小新从不认输（家长 2026-11-05：要下到底 / 申请数棋）
                _apply(g, g["engine"], mv)
                g["analysis"].append(None)
                save(g)
                g["analysis"][-1] = analyze(g, len(g["moves"]))
                if _passed_twice(g):
                    finish_by_count(g)
            g["thinking"], g["error"] = False, ""
            save(g)
        # 一盘结束（小新认输、双方停一手数子）要整盘讲解；没结束也起一下：刚才那手要是亏大了，
        # 趁孩子想下一手的时候就把它的讲解生成好，下完点复盘几乎不用等（fill 只补缺的，没缺的什么都不做）
        review_later(gid)
    except (EngineError, GameError, R.IllegalMove) as e:
        _fail(gid, str(e))
    except Exception as e:  # noqa: BLE001 线程里的错不能丢：记下来给界面看
        logging.getLogger("xiaoxin.go").exception("respond %s", gid)
        _fail(gid, f"小新出错了：{e}")
    finally:
        _active.discard(gid)
        with _changed:
            _changed.notify_all()


def _fail(gid: str, msg: str) -> None:
    with lock(gid):
        g = get(gid)
        g["thinking"], g["error"] = False, msg
        save(g)


def _passed_twice(g: dict) -> bool:
    return len(g["moves"]) >= 2 and all(m[1] == "pass" for m in g["moves"][-2:])


def _loss_of_last(g: dict) -> dict | None:
    """孩子刚下那手亏了多少（「下坏了提醒一句」用）。"""
    from server.games import go_review

    ls = go_review.losses(g)
    return ls[-1] if ls and ls[-1]["n"] == len(g["moves"]) else None


def wait(gid: str, kid: str, moves: int, timeout: float = 25) -> dict:
    """长轮询：等到不在想了（小新下完并分析完 / 出错），最多 timeout 秒。moves 是界面现在的手数（留着做校验用）。"""
    end = time.time() + timeout
    with _changed:
        while True:
            g = _own(gid, kid)
            if not g.get("thinking") or time.time() >= end:
                return g
            _changed.wait(min(1.0, max(0.05, end - time.time())))


def retry(gid: str, kid: str) -> dict:
    """应手出错后「再试一次」。"""
    with lock(gid):
        g = _own(gid, kid)
        stuck = _to_move(g) == g["engine"] or g["analysis"][-1] is None   # 该小新下了 / 孩子那手还没分析
        if g["result"] is not None or g.get("thinking") or not stuck:
            return g
        g["thinking"], g["error"] = True, ""
        save(g)
    _respond_later(gid)
    return g


def _apply(g: dict, color: str, mv: str) -> list:
    board = R.replay(g["moves"], g.get("stones"))
    dead = [] if mv.lower() == "pass" else board.play(color, *R.to_xy(mv))
    g["moves"].append([color, "pass" if mv.lower() == "pass" else mv.upper()])
    return dead


def play(gid: str, kid: str, mv: str) -> dict:
    """孩子落一手（坐标或 pass），小新接着应。返回最新对局。"""
    with lock(gid):
        g = _own(gid, kid)
        if g["result"] is not None:
            raise GameError("这盘已经结束了")
        if g.get("thinking"):
            raise GameError("小新在想，等一下")
        if _to_move(g) != g["kid_color"]:
            raise GameError("还没轮到你")
        try:
            _apply(g, g["kid_color"], mv)
        except R.IllegalMove as e:
            raise GameError(str(e))
        g["analysis"].append(None)       # 后台补
        g["thinking"], g["error"], g["last_loss"] = True, "", None
        save(g)
    _respond_later(gid)
    return g


def undo(gid: str, kid: str) -> dict:
    """悔棋：退到孩子上一手之前（一般退两手：小新一手 + 孩子一手）。"""
    with lock(gid):
        g = _own(gid, kid)
        if g["result"] is not None:
            raise GameError("这盘已经结束了")
        if g.get("thinking"):
            raise GameError("小新在想，等它下完再悔")
        while g["moves"] and g["moves"][-1][0] != g["kid_color"]:
            g["moves"].pop()
            g["analysis"].pop()
        if not g["moves"]:
            raise GameError("还没下呢，没有能悔的")
        g["moves"].pop()
        g["analysis"].pop()
        g["undos"] += 1
        g["last_loss"], g["error"] = None, ""
        # 悔掉的那几手（以及孩子最后一手：它的讲解用到了后面小新的应手）的讲解作废
        n = len(g["moves"])
        g["review"] = {k: v for k, v in (g.get("review") or {}).items() if int(k) < n}
        save(g)
        return g


def hint(gid: str, kid: str) -> list[dict]:
    """提示：当前局面 AI 推荐的前 3 个点（就是上一手之后那次分析的 best）。"""
    from server.games import registry

    g = _own(gid, kid)
    if g["result"] is not None or _to_move(g) != g["kid_color"] or g.get("thinking"):
        raise GameError("现在不是你下")
    cap = int(registry.settings("go").get("hint_max", 3) or 0)   # 0 = 不限
    if cap and g["hints"] >= cap:
        raise GameError(f"这盘的 {cap} 次提示用完了，自己想想吧")
    with lock(gid):
        g = get(gid)
        g["hints"] += 1
        save(g)
    best = g["analysis"][-1]["best"]
    return [{"move": b["move"]} for b in best if b["move"].lower() != "pass"][:3]


def resign(gid: str, kid: str) -> dict:
    with lock(gid):
        g = _own(gid, kid)
        if g["result"] is not None:
            raise GameError("这盘已经结束了")
        g["result"] = {"winner": g["engine"], "by": "resign", "text": f"{'白' if g['engine'] == 'W' else '黑'}中盘胜（认输）"}
        g["ended"] = time.time()
        save(g)
    review_later(gid)   # 结束就开始生成复盘讲解
    return g


def _zi(x: float) -> str:
    """目数 / 2 = 子数，按 ¼ 子写：3.75 → 「3¾」，0.5 → 「¼」……"""
    q = round(abs(x) / 2 * 4)
    whole, frac = divmod(q, 4)
    return (str(whole) if whole or not frac else "") + ["", "¼", "½", "¾"][frac]


def finish_by_count(g: dict) -> None:
    """数子结束：死子按最后一次分析的归属拿掉（归属 ≥ 0.6 判给对方的子当死子），空点按归属给到一方，再按中国规则数。
    结果里存每个点归谁（owner，界面画地盘）和死子（dead），文字按子说：「黑 185 子，白 176 子，黑贴 3¾ 子，黑胜 ¾ 子」。"""
    board = R.replay(g["moves"], g.get("stones"))
    own = next((a for a in reversed(g["analysis"]) if a), {}).get("ownership") or []
    dead, owner = [], []
    for y in range(R.N):
        for x in range(R.N):
            v = board.grid[y][x]
            o = own[y * R.N + x] if len(own) == R.N * R.N else 0.0
            if (v == "B" and o <= -0.6) or (v == "W" and o >= 0.6):
                dead.append(R.to_gtp(x, y))
                v = "."
            if v in ("B", "W"):
                owner.append(v)
            elif not own:
                owner.append(".")
            else:
                owner.append("B" if o >= 0.3 else "W" if o <= -0.3 else ".")
    if not own:   # 没有分析（不该发生）：按「只挨一方的空」数
        a = board.area()
        area = {"B": a["B"], "W": a["W"]}
    else:
        area = {"B": owner.count("B"), "W": owner.count("W")}
    diff = area["B"] - area["W"] - g["komi"]
    winner = "B" if diff > 0 else "W"
    wn = "黑" if winner == "B" else "白"
    text = (f"黑 {area['B']} 子，白 {area['W']} 子，黑贴 {_zi(g['komi'])} 子，{wn}胜 {_zi(diff)} 子")
    g["result"] = {"winner": winner, "by": "count", "margin": round(abs(diff), 1), "area": area, "dead": dead,
                   "owner": "".join(owner), "text": text}
    g["ended"] = time.time()


def _area_name(x: int, y: int) -> str:
    h = "左" if x < 6 else "右" if x > 12 else ""
    v = "上" if y < 6 else "下" if y > 12 else ""
    return f"{h}{v}角" if h and v else f"{h or v}边" if h or v else "中间"


def count_check(g: dict) -> dict:
    """孩子「申请数棋」：能不能数，由 KataGo 和代码判（不用大模型）。
    返回 {ok, reason（念给孩子听）, area: [没下完那片的点]}。
    - 胜负已定：下满 100 手，孩子胜率连续 10 手（双方各 5 手）< 3% 或 > 97%。
    - 棋下完了：轮到谁都一样——「下最好的一手」比「停一手」多不到 DONE_GAIN 目（双方各算一次，allowMoves 只许 pass）。
    - 都不是：说哪里没下完、谁在那里下一手还能多几目，标出那一片还没定归属的空点。"""
    me = g["kid_color"]
    recent = [a for a in g["analysis"] if a][-DECIDED_RUN:]
    mine = [(a["winrate"] if me == "B" else 1 - a["winrate"]) for a in recent]
    if len(g["moves"]) >= DECIDED_MIN and len(mine) == DECIDED_RUN and (all(w < DECIDED_WR for w in mine) or all(w > 1 - DECIDED_WR for w in mine)):
        return {"ok": True, "why": "decided", "reason": "胜负已经定了，可以数棋。", "area": []}

    def gain(moves: list, who: str) -> tuple[float, str]:
        base = _query(g, moves, maxVisits=COUNT_VISITS)
        ps = _query(g, moves, maxVisits=COUNT_VISITS, allowMoves=[{"player": who, "moves": ["pass"], "untilDepth": 1}])
        best = min(base["moveInfos"], key=lambda m: m["order"])["move"]
        return base["rootInfo"]["scoreLead"] - ps["rootInfo"]["scoreLead"], best

    g_me, best_me = gain(g["moves"], me)
    g_op, best_op = gain(g["moves"] + [[me, "pass"]], R.other(me))
    if max(g_me, g_op) < DONE_GAIN:
        return {"ok": True, "why": "finished", "reason": "棋已经下完了，可以数棋。", "area": []}
    who, val, best = ("你", g_me, best_me) if g_me >= g_op else ("小新", g_op, best_op)
    if best.lower() == "pass":
        return {"ok": True, "why": "finished", "reason": "棋已经下完了，可以数棋。", "area": []}
    bx, by = R.to_xy(best)
    # 没下完的那一片：最佳点附近（横竖 2 路内，5×5）还没定归属的空点（|归属| < 0.4），从最佳点连着的那些。
    # 原来 9×9、门槛 0.5：中盘时一标就是 60 多个点，孩子看不出是哪（2026-11-04 实测 69 点）
    board = R.replay(g["moves"], g.get("stones"))
    own = (g["analysis"][-1] or {}).get("ownership") or []
    near = {(x, y) for y in range(max(0, by - 2), min(R.N, by + 3)) for x in range(max(0, bx - 2), min(R.N, bx + 3))
            if board.grid[y][x] == "." and (len(own) != R.N * R.N or abs(own[y * R.N + x]) < 0.4)}
    zone, todo = {(bx, by)}, [(bx, by)]
    while todo:
        for nb in R.neighbors(*todo.pop()):
            if nb in near and nb not in zone:
                zone.add(nb)
                todo.append(nb)
    area = [R.to_gtp(*p) for p in sorted(zone)]
    n = max(1, round(val))
    return {"ok": False, "why": "unfinished",
            "reason": f"还不能数棋。{_area_name(bx, by)}这里还没下完，{who}在这里下一手还能多大约 {n} 目。下完了再申请数棋吧。",
            "area": area, "gain": round(val, 1)}


def count(gid: str, kid: str) -> tuple[dict, dict]:
    """孩子「申请数棋」：小新（KataGo）同意就数子结束，不同意原样返回并说原因。返回 (对局, 判定)。"""
    with lock(gid):
        g = _own(gid, kid)
        if g["result"] is not None:
            raise GameError("这盘已经结束了")
        if g.get("thinking") or g["analysis"][-1] is None:
            raise GameError("小新在想，等一下")
        if _to_move(g) != g["kid_color"]:
            raise GameError("轮到你下的时候才能申请数棋")
        check = count_check(g)
        g.setdefault("count_requests", []).append({"n": len(g["moves"]), "ok": check["ok"], "why": check["why"]})
        if check["ok"]:
            finish_by_count(g)
        save(g)
    if check["ok"]:
        review_later(gid)
    return g, check


def _own(gid: str, kid: str) -> dict:
    g = get(gid)
    if kid and g.get("kid") != kid:
        raise GameError("没有这盘棋")
    return g


# ------------------------------------------------------------ 给界面的
def _hint_max() -> int:
    from server.games import registry

    return int(registry.settings("go").get("hint_max", 3) or 0)


def view(g: dict) -> dict:
    """界面要的：盘面、轮到谁、提子、最后一手、胜率（孩子视角）、结果。不带每手的大块分析。"""
    board = R.replay(g["moves"], g.get("stones"))
    last = next((a for a in reversed(g["analysis"]) if a), None)
    me = g["kid_color"]
    wr = last["winrate"] if last else 0.5
    return {"id": g["id"], "level": g["level"], "level_name": LEVEL_NAME[g["level"]], "kid_color": me,
            "komi": g["komi"], "handicap": g["handicap"], "moves": g["moves"],
            "board": ["".join(r) for r in board.grid], "captures": board.captures, "ko": board.ko,
            "to_move": _to_move(g), "result": g["result"], "undos": g["undos"], "hints": g["hints"],
            "thinking": bool(g.get("thinking")), "error": g.get("error", ""), "last_loss": g.get("last_loss"),
            "hint_max": _hint_max(),
            "winrate": round(wr if me == "B" else 1 - wr, 3),
            "score": round((last["score"] if last else 0) * (1 if me == "B" else -1), 1),
            "created": g["created"]}
