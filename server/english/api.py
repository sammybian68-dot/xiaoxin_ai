"""英语场景对话的接口：/api/english/*。课的素材（背景、角色、配音）也从这里出，带孩子鉴权。"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from server.english import build, drill

router = APIRouter()


def _kid(request: Request, kid: str = "") -> dict:
    from server.main import kid_for

    return kid_for(request, kid)


@router.get("/api/english")
def english_home(request: Request, kid: str = "") -> dict:
    """看板：每课熟练度、两周练习、读不准的词、最近练习、今天推荐；还有暂停着的那次。"""
    k = _kid(request, kid)
    b = drill.board(k["id"])
    cur = drill.current(k["id"])
    return {**b, "current": cur and {"id": cur["id"], "lesson": cur["lesson"], "role": cur["role"], "at": cur["at"]},
            "generating": sorted(GENERATING.get(k["id"], {}).values(), key=lambda g: g["at"])}


@router.get("/api/english/lesson/{lid}")
def english_lesson(request: Request, lid: str, kid: str = "") -> dict:
    k = _kid(request, kid)
    try:
        L = drill.lesson(lid)
    except KeyError:
        raise HTTPException(404, "没有这一课")
    hist = [{"id": s["id"], "role": s["role"], "at": s["started"], "secs": s.get("secs", 0), "summary": s.get("summary")}
            for s in drill.sessions(k["id"], lid) if s.get("done")]
    return {**L, "progress": drill.progress(k["id"]).get(lid) or {"level": 0, "times": 0}, "history": hist}


_FILE = re.compile(r"^(bg\.jpg|l\d{1,2}(_slow)?\.mp3)$")


@router.get("/api/english/asset/{lid}/{name}")
def english_asset(lid: str, name: str) -> FileResponse:
    """课的背景和配音（不是孩子的数据，同一个家里共用）。"""
    if not _FILE.match(name):
        raise HTTPException(404, "没有这个文件")
    try:
        drill.lesson(lid)
    except KeyError:
        raise HTTPException(404, "没有这一课")
    p = drill.root() / "lessons" / lid / name
    if not p.is_file():
        raise HTTPException(404, "没有这个文件")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=2592000"})


@router.get("/api/english/char/{name}.png")
def english_char(name: str) -> FileResponse:
    if not re.fullmatch(r"[a-z]{2,16}", name):
        raise HTTPException(404, "没有这个角色")
    p = drill.root() / "chars" / f"{name}.png"
    if not p.is_file():
        raise HTTPException(404, "没有这个角色")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=2592000"})


@router.get("/api/english/word/{w}.mp3")
def english_word(w: str) -> FileResponse:
    w = build.word_key(w)
    if not w:
        raise HTTPException(404, "没有这个词")
    p = drill.root() / "words" / f"{w}.mp3"
    if not p.is_file():
        try:
            build.word_audio(w)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"这个词读不出来：{e}")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=2592000"})


# ------------------------------------------------------------ 练习
@router.post("/api/english/session")
def english_start(request: Request, body: dict = Body(...)) -> dict:
    k = _kid(request, str(body.get("kid") or ""))
    try:
        return drill.start(k["id"], str(body.get("lesson") or ""), str(body.get("role") or ""))
    except KeyError:
        raise HTTPException(404, "没有这一课")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _own(request: Request, sid: str, kid: str = "") -> tuple[dict, dict]:
    k = _kid(request, kid)
    try:
        s = drill.get(sid)
    except KeyError:
        raise HTTPException(404, "没有这次练习")
    if s["kid"] != k["id"]:
        raise HTTPException(404, "没有这次练习")
    return k, s


@router.get("/api/english/session/{sid}")
def english_session(request: Request, sid: str, kid: str = "") -> dict:
    return _own(request, sid, kid)[1]


@router.post("/api/english/session/{sid}/line/{i}")
async def english_line(request: Request, sid: str, i: int, kid: str = "") -> dict:
    """她读了第 i 句：请求体是 16 kHz wav（界面的 Recorder 录的）。送讯飞评测，回分数和每个词。"""
    import asyncio

    k, _ = _own(request, sid, kid)
    wav = await request.body()
    if len(wav) > 8 * 1024 * 1024:
        raise HTTPException(413, "录音太长了")
    try:
        return await asyncio.to_thread(drill.score_line, sid, k["id"], i, wav)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 讯飞报错、网络
        raise HTTPException(502, f"打分没成功：{e}")


@router.get("/api/english/session/{sid}/audio/{i}/{n}.wav")
def english_audio(request: Request, sid: str, i: int, n: int, kid: str = "") -> FileResponse:
    k, _ = _own(request, sid, kid)
    try:
        return FileResponse(drill.audio(sid, k["id"], i, n), media_type="audio/wav")
    except KeyError:
        raise HTTPException(404, "没有这段录音")


@router.post("/api/english/session/{sid}/finish")
def english_finish(request: Request, sid: str, body: dict = Body(default={})) -> dict:
    k, _ = _own(request, sid, str(body.get("kid") or ""))
    s = drill.finish(sid, k["id"])
    _report(k, s)
    return s


def _report(k: dict, s: dict) -> None:
    """练完在群里发一条小结（和练习模式批改一样，是小新的一条消息）。"""
    from server import store

    L = drill.lesson(s["lesson"])
    sm = s["summary"]
    role = next((r["name"] for r in L["roles"] if r["id"] == s["role"]), s["role"])
    stars = "⭐" * round(sm["stars"]) if sm["said"] else ""
    weak = "、".join(w for w, _ in sm["weak"][:3])
    text = (f"{k['name']}练完了英语小剧场「{L['zh']}」（演 {role}）：{sm['said']}/{sm['of']} 句，平均 {sm['avg']} 分 {stars}，"
            f"用了 {s['secs'] // 60} 分 {s['secs'] % 60} 秒" + (f"，重读 {sm['retries']} 次" if sm["retries"] else "")
            + (f"。还要多练：{weak}" if weak else "。都读得很好！"))
    try:
        store.append(k["thread"], {"role": "assistant", "text": text, "model": "", "via": "english",
                                   "english": {"session": s["id"], "lesson": s["lesson"]}})
    except Exception:  # noqa: BLE001 发不出去不影响练习本身
        pass


# ------------------------------------------------------------ 现场生成一课（异步，和画图一样）
GENERATING: dict[str, dict] = {}   # kid → {gid: {ask, at}}


def generate_later(kid: dict, ask: str) -> str:
    """后台生成一课，好了以小新身份在群里发一条带卡片的消息。返回生成任务 id。"""
    import threading
    import uuid

    from server import store

    gid = uuid.uuid4().hex[:8]
    GENERATING.setdefault(kid["id"], {})[gid] = {"ask": ask, "at": time.time()}

    def run():
        try:
            L = build.custom(ask)
            store.append(kid["thread"], {"role": "assistant", "via": "english", "model": "",
                                         "text": f"{kid.get('call') or kid['name']}，「{L['zh']}」的对话场景做好了，我们一起练习吧！",
                                         "english": {"lesson": L["id"], "new": True}})
        except Exception as e:  # noqa: BLE001
            store.append(kid["thread"], {"role": "assistant", "via": "english", "model": "",
                                         "text": "这个对话场景没做出来，换个说法再试一次吧。", "error": str(e)[:300]})
        finally:
            GENERATING.get(kid["id"], {}).pop(gid, None)
    threading.Thread(target=run, daemon=True, name="english-gen").start()
    return gid
