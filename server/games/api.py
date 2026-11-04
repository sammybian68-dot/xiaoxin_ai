"""下棋的 HTTP 接口：/api/go/*（对局、复盘、看板）和 /api/games/*（设置、引擎状态）。main.py 里 include_router。
对局按孩子分（kid_for：孩子就是自己，家长 / 管理员按 ?kid=，默认第一个孩子）。设置只有管理员能改。"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Body, HTTPException, Request

from server.games import go, go_review, registry
from server.games.katago import engine

router = APIRouter()


def _kid(request: Request, kid: str = "") -> dict:
    from server.main import kid_for

    return kid_for(request, kid)


def _err(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _game(gid: str, kid: dict) -> dict:
    try:
        g = go.get(gid)
    except go.GameError as e:
        raise HTTPException(404, str(e))
    if g.get("kid") != kid["id"]:
        raise HTTPException(404, "没有这盘棋")
    return g


# ------------------------------------------------------------ 设置和引擎
@router.get("/api/games")
def games_settings(request: Request) -> dict:
    """设置 →「下棋」：有哪些棋种、围棋的设置、档位列表、KataGo 状态。"""
    _kid(request)
    return {"games": registry.GAMES, "go": registry.settings("go"),
            "levels": [{"id": i, "name": n} for i, n in go.LEVELS], "engine": engine.status()}


@router.put("/api/games/{game}")
def games_update(request: Request, game: str, body: dict = Body(...)) -> dict:
    from server.main import need

    need(request, "admin")
    try:
        registry.update(game, body)
    except ValueError as e:
        raise _err(e)
    return games_settings(request)


# 清空下棋记录要输家长定的密码。代码里只放 sha256，不放明文；比较用 compare_digest
CLEAR_PW_SHA256 = "961be95f6fe770b1682729bea5d2e63f97c7ff0a6100818794ee90953a01ed2b"


@router.post("/api/games/go/clear")
def games_clear(request: Request, body: dict = Body(...)) -> dict:
    """设置 →「下棋」→「清空所有下棋记录」：界面先二次确认，再输密码；密码在服务端核对，错了不删。"""
    from server.main import need

    need(request, "admin")
    pw = str(body.get("password") or "")
    if not hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), CLEAR_PW_SHA256):
        raise HTTPException(403, "密码不对，没有删")
    return {"deleted": go.clear_all()}


# ------------------------------------------------------------ 对局
@router.get("/api/go/current")
def go_current(request: Request, kid: str = "") -> dict:
    """还没下完的那盘（进「下棋」时接着下）。没有就 {}。"""
    g = go.current(_kid(request, kid)["id"])
    return go.view(g) if g else {}


@router.post("/api/go")
def go_new(request: Request, body: dict = Body(default={})) -> dict:
    """开一盘：level / color / handicap / komi 不给就用设置里的默认。还有没下完的那盘就先认输结束（只能有一盘在下）。"""
    k = _kid(request, str(body.get("kid") or ""))
    cfg = registry.settings("go")
    old = go.current(k["id"])
    if old:
        go.resign(old["id"], k["id"])
    try:
        g = go.new(k["id"], level=str(body.get("level") or cfg["level"]), color=str(body.get("color") or cfg["color"]),
                   komi=body.get("komi", cfg["komi"]), handicap=int(body.get("handicap", cfg["handicap"]) or 0))
    except (go.GameError, ValueError) as e:
        raise _err(e)
    except Exception as e:  # noqa: BLE001 引擎没起来
        raise HTTPException(503, str(e))
    return go.view(g)


@router.get("/api/go/{gid}")
def go_get(request: Request, gid: str, kid: str = "", wait: int = -1) -> dict:
    """wait=手数：小新在想就等到手数变了再回（长轮询，最多 25 秒）。"""
    k = _kid(request, kid)
    _game(gid, k)
    g = go.wait(gid, k["id"], wait, timeout=25) if wait >= 0 else go.get(gid)
    return go.view(g)


def _act(request: Request, gid: str, body: dict, fn, *args):
    k = _kid(request, str(body.get("kid") or ""))
    _game(gid, k)
    try:
        return fn(gid, k["id"], *args)
    except go.GameError as e:
        raise _err(e)


@router.post("/api/go/{gid}/move")
def go_move(request: Request, gid: str, body: dict = Body(...)) -> dict:
    mv = str(body.get("move") or "").strip()
    if not mv:
        raise HTTPException(400, "下在哪？")
    return go.view(_act(request, gid, body, go.play, mv))


@router.post("/api/go/{gid}/undo")
def go_undo(request: Request, gid: str, body: dict = Body(default={})) -> dict:
    if not registry.settings("go")["undo"]:
        raise HTTPException(400, "设置里没开悔棋")
    return go.view(_act(request, gid, body, go.undo))


@router.post("/api/go/{gid}/hint")
def go_hint(request: Request, gid: str, body: dict = Body(default={})) -> dict:
    if not registry.settings("go")["hint"]:
        raise HTTPException(400, "设置里没开提示")
    return {"hints": _act(request, gid, body, go.hint)}


@router.post("/api/go/{gid}/resign")
def go_resign(request: Request, gid: str, body: dict = Body(default={})) -> dict:
    return go.view(_act(request, gid, body, go.resign))


@router.post("/api/go/{gid}/count")
def go_count(request: Request, gid: str, body: dict = Body(default={})) -> dict:
    return go.view(_act(request, gid, body, go.count))


@router.post("/api/go/{gid}/retry")
def go_retry(request: Request, gid: str, body: dict = Body(default={})) -> dict:
    return go.view(_act(request, gid, body, go.retry))


# ------------------------------------------------------------ 复盘、看板
@router.get("/api/go/{gid}/review")
def go_review_get(request: Request, gid: str, kid: str = "") -> dict:
    """整盘复盘（关键步讲解第一次生成要十几秒，之后缓存在棋谱里）。没下完的也能看到目前为止的。"""
    k = _kid(request, kid)
    g = _game(gid, k)
    if any(a is None for a in g["analysis"]):
        raise HTTPException(409, "小新还在想，等这一手下完再复盘")
    with go.lock(gid):
        g = go.get(gid)
        r = go_review.review(g)
        go.save(g)   # 讲解缓存
    return {**r, "level_name": go.LEVEL_NAME[g["level"]], "result": g["result"], "created": g["created"],
            "komi": g["komi"]}


def summary_row(g: dict) -> dict:
    """看板里一盘：日期、档位、执色、结果、手数、坏棋数。"""
    me = g["kid_color"]
    ls = go_review.losses(g) if all(g["analysis"]) else []
    r = g.get("result") or {}
    return {"id": g["id"], "created": g["created"], "level": g["level"], "level_name": go.LEVEL_NAME[g["level"]],
            "color": me, "moves": len(g["moves"]), "result": r.get("text", "没下完"),
            "won": None if not r else r.get("winner") == me,
            "big": sum(x["grade"] == "big" for x in ls), "mid": sum(x["grade"] == "mid" for x in ls),
            "avg_loss": round(sum(max(0, x["loss_score"]) for x in ls) / max(1, len(ls)), 2) if ls else None}


def stats(kid: str, days: int = 90) -> dict:
    since = time.time() - days * 86400
    rows = [summary_row(g) for g in go.all_games(kid) if g["created"] >= since]
    done = [r for r in rows if r["won"] is not None]   # 认输的也算（早先「不到 20 手不算」，孩子认输了看板却是 0:0）
    by: dict[str, dict] = {}
    for r in done:
        b = by.setdefault(r["level"], {"level": r["level"], "name": r["level_name"], "won": 0, "lost": 0})
        b["won" if r["won"] else "lost"] += 1
    order = {i: n for n, (i, _) in enumerate(go.LEVELS)}
    won = sum(r["won"] for r in done)
    return {"games": rows, "total": len(done), "won": won, "lost": len(done) - won,
            "rate": round(won / len(done), 3) if done else None,
            "by_level": sorted(by.values(), key=lambda b: order[b["level"]]), "days": days}


@router.get("/api/go-stats")
def go_stats(request: Request, kid: str = "", days: int = 90) -> dict:
    return stats(_kid(request, kid)["id"], max(1, min(days, 3650)))
