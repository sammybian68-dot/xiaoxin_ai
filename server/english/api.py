"""英语场景对话的接口：/api/english/*。课的素材（背景、角色、配音）也从这里出，带孩子鉴权。"""

from __future__ import annotations

import json
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
def english_word(w: str, slow: int = 0) -> FileResponse:
    """一个词的标准读音（英音女声）。slow=1 慢读一遍（练读不准的词：一遍正常、一遍慢）。"""
    w = build.word_key(w)
    if not w:
        raise HTTPException(404, "没有这个词")
    p = drill.root() / "words" / (f"{w}.mp3" if not slow else f"{w}_slow.mp3")
    if not p.is_file():
        try:
            if slow:
                build.mp3(build._tts(w, build.WORD_VOICE, speech_rate=-50).pcm, p)
            else:
                build.word_audio(w)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"这个词读不出来：{e}")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=2592000"})


@router.post("/api/english/word/{w}/check")
async def english_word_check(request: Request, w: str, kid: str = "") -> dict:
    """单练一个词：她读一遍（16 kHz wav），讯飞按单词评分。不记进练习记录，只回分数。"""
    import asyncio

    from src.voice import xfyun_ise

    _kid(request, kid)
    w = build.word_key(w)
    if not w:
        raise HTTPException(404, "没有这个词")
    wav = await request.body()
    try:
        pcm = xfyun_ise.to_pcm16k(wav)
        if len(pcm) < 16000 * 2 * 0.3:
            raise ValueError("没听清，按住说完再松手")
        import array

        if max(abs(x) for x in array.array("h", pcm)[::8]) < 300:
            raise ValueError("没听到声音，靠近一点、大声说")
        r = await asyncio.to_thread(xfyun_ise.assess, w, pcm, category="read_word")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"打分没成功：{e}")
    total = r.get("total")
    return {"word": w, "total": total, "stars": drill.stars(total),
            "msg": "读得真准！" if (total or 0) >= drill.STAR3 else "不错，再跟着读一遍会更好。" if (total or 0) >= drill.STAR2
            else "再听一遍，跟着读，嘴巴张大一点。"}


@router.get("/api/english/say.mp3")
def english_say(text: str, slow: int = 0) -> FileResponse:
    """英音读一句（默写听写时小新念句子用）：英音女声，落盘缓存，同一句只合成一次。"""
    import hashlib

    t = " ".join(str(text).split())[:200]
    if not t or not re.fullmatch(r"[A-Za-z0-9 ,.!?'’\-:;]+", t):
        raise HTTPException(400, "只读英文句子")
    d = drill.root() / "say"
    d.mkdir(exist_ok=True)
    p = d / f"{hashlib.sha1((t + str(bool(slow))).encode()).hexdigest()[:16]}.mp3"
    if not p.is_file():
        try:
            build.say(t, build.WORD_VOICE, p, slow=bool(slow))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"读不出来：{e}")
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


@router.post("/api/english/dictation")
def english_dictation(request: Request, body: dict = Body(...)) -> dict:
    """出一张这课的默写卷（听写句子 + 补全对话），存成练习纸；以小新的身份发进群里（带练习纸卡片：打印、开始练习、
    拍照交卷批改都在卡片上，和语文生词默写同一套）。"""
    from server import store, worksheet

    k = _kid(request, str(body.get("kid") or ""))
    lid = str(body.get("lesson") or "")
    try:
        L = drill.lesson(lid)
        ws = worksheet.create("scene_dictation", subject="english", for_kid=k["id"], lesson=lid,
                              role=str(body.get("role") or ""), kid=k["id"])
    except KeyError:
        raise HTTPException(404, "没有这一课")
    except worksheet.WorksheetError as e:
        raise HTTPException(400, str(e))
    worksheet.page_file(ws, kid_name=k["name"])
    msg = store.append(k["thread"], {"role": "assistant", "via": "english", "model": "",
                                     "text": f"「{L['zh']}」的默写出好了：先听写句子，再补全对话。可以打印出来写，也可以点「开始练习」，我来念。",
                                     "actions": [{"name": "make_exercise", "arguments": json.dumps({"topic": "scene_dictation"}),
                                                  "result": f"已出好 {worksheet.summary(ws)}（w_{ws['id']}）"}]})
    return {"worksheet": ws["id"], "message": msg["id"]}


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
