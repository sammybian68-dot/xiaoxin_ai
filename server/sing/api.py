"""翻唱的接口：/api/sing/*。录声音、试听、删；封面；发到微信的视频；跟着伴奏唱（伴奏、真人版）；QQ 音乐扫码登录（只有家长、管理员）。"""

from __future__ import annotations

import asyncio
from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from server import store
from server.sing import job, karaoke, practice, qq, video, voices

router = APIRouter()
MAX_VOICE_BYTES = 8 * 1024 * 1024


def _who(request: Request):
    from server.main import who

    return who(request)


def _need_parent(request: Request):
    from server.main import need

    return need(request, "parent")


@router.get("/api/sing")
def sing_home(request: Request) -> dict:
    """设置页「唱歌」：录过谁、QQ 音乐登没登、唱歌服务开没开。"""
    _who(request)
    st = qq.status()
    return {"voices": voices.all_voices(), "qq": st, "service": job.service_up()}


@router.post("/api/sing/voices")
async def sing_voice_upload(request: Request, kid: str = "") -> dict:
    """录好的唱歌声音（wav），名字在 x-name（URL 编码）。存好后小新在线程里说一句怎么用。"""
    from server.main import kid_for

    w = _who(request)
    data = await request.body()
    if len(data) > MAX_VOICE_BYTES:
        raise HTTPException(413, "录音太大了")
    name = unquote(request.headers.get("x-name") or "")
    try:
        meta = await asyncio.to_thread(voices.save, name, data, w.id)
    except voices.VoiceError as e:
        raise HTTPException(400, str(e))
    try:
        k = kid_for(request, kid)
    except HTTPException:
        return meta
    store.append(k["thread"], {"role": "assistant", "model": "", "via": "sing",
                               "text": f"{meta['name']}唱歌的声音存好了（{meta['seconds']:.0f} 秒）。"
                                       f"说「小新，用{meta['name']}的声音唱江南」就能听啦。"})
    return meta


@router.get("/api/sing/voices/{vid}/audio")
def sing_voice_audio(request: Request, vid: str) -> FileResponse:
    _who(request)
    try:
        voices.get(vid)
        p = voices.path(vid)
    except KeyError:
        raise HTTPException(404, "没有这段录音")
    return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/api/sing/voices/{vid}")
def sing_voice_delete(request: Request, vid: str) -> dict:
    """家长都能删；孩子只能删自己录的。"""
    w = _who(request)
    try:
        v = voices.get(vid)
    except KeyError:
        raise HTTPException(404, "没有这段录音")
    if not w.at_least("parent") and v.get("by") != w.id and v.get("member") != w.id:
        raise HTTPException(403, "只能删自己录的")
    voices.delete(vid)
    return {"ok": True}


@router.get("/api/sing/cover/{album_mid}")
def sing_cover(request: Request, album_mid: str) -> FileResponse:
    _who(request)
    p = job.cover_path(album_mid)
    if p is None or not p.is_file():
        raise HTTPException(404, "没有封面")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


# ---------------------------------------------------------------- 录声音时跟着唱的歌

@router.get("/api/sing/practice")
def sing_practice_list(request: Request) -> dict:
    _who(request)
    return {"groups": [{"id": g, "name": n} for g, n in practice.GROUPS], "songs": practice.songs(), "listen": qq.usable()}


@router.get("/api/sing/practice/{sid}")
async def sing_practice_song(request: Request, sid: str) -> dict:
    """这首要唱的那段：lines 的 t 从这段开头算；start / end 是原唱里的秒（听原唱从这儿放到那儿）。"""
    _who(request)
    try:
        d = await asyncio.to_thread(practice.get, sid)
    except practice.PracticeError as e:
        raise HTTPException(404, str(e))
    return {**{k: d[k] for k in ("id", "title", "artist", "start", "end", "lines")}, "listen": qq.usable()}


@router.get("/api/sing/practice/{sid}/audio")
async def sing_practice_audio(request: Request, sid: str) -> FileResponse:
    """原唱整首（支持 Range，前端直接跳到 start）。要 QQ 音乐登录。"""
    _who(request)
    try:
        p = await asyncio.to_thread(practice.original, sid)
    except practice.PracticeError as e:
        raise HTTPException(404, str(e))
    return FileResponse(p, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- 发到微信的视频、跟着伴奏唱

def _find(fid: str) -> tuple[str, dict, bool] | None:
    """这个文件是哪张翻唱卡片的：(线程, 消息, 是不是真人版)。"""
    from server.family import members

    for k in members.kids(active_only=False):
        try:
            msgs = store.get(k["thread"])["messages"]
        except KeyError:
            continue
        for m in reversed(msgs):
            s = m.get("sing") or {}
            if s.get("file") == fid or (s.get("real") or {}).get("file") == fid:
                return k["thread"], m, s.get("file") != fid
    return None


def _sing_file(request: Request, fid: str, real_ok: bool = True):
    """翻唱（或真人版）的 mp3、卡片字段（真人版换成真人那份）、线程和消息；看不到这个文件（孩子看别人线程里的）一律当没有。"""
    from server.main import _can_file

    info = store.file_info(fid)
    if info is None or not info[0].is_file() or not _can_file(_who(request), info[1]):
        raise HTTPException(404, "没有这首歌")
    found = _find(fid)
    if found is None or (found[2] and not real_ok):
        raise HTTPException(404, "这不是翻唱的歌")
    thread, m, real = found
    sing = m["sing"]
    if real:
        r = sing["real"]
        sing = {**sing, "file": fid, "singer": r["singer"], "seconds": r["seconds"], "real": True}
    return info[0], sing, thread, m


def _sec(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "时间不对")


@router.post("/api/sing/video")
async def sing_video(request: Request, body: dict = Body(...)) -> dict:
    """要一段分享用的视频：{file, start?, end?, off?}（秒；不给起止就是整首，off 是卡片上调的歌词提前量）。
    做好了 state=ready、带 url；还在做是 state=making + pct，隔一秒再要一次（同一段不会重复做）；error 再要一次就重做。"""
    fid = str(body.get("file") or "")
    audio, sing, _, _ = await asyncio.to_thread(_sing_file, request, fid)
    try:
        st = video.ensure(fid, audio, sing, _sec(body.get("start")), _sec(body.get("end")), _sec(body.get("off")) or 0.0)
    except video.VideoError as e:
        raise HTTPException(400, str(e))
    if st["state"] == "ready":
        st["url"] = f"api/sing/video/{st['key']}.mp4"
    return st


@router.get("/api/sing/video/{name}")
def sing_video_file(request: Request, name: str) -> FileResponse:
    from server.main import _can_file

    k = name.removesuffix(".mp4")
    p = video.path(k)
    info = store.file_info(k.split("_")[0]) if p else None
    if info is None or not _can_file(_who(request), info[1]):
        raise HTTPException(404, "视频还没做好")
    return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/sing/karaoke")
async def sing_karaoke(request: Request, body: dict = Body(...)) -> dict:
    """AI 翻唱那首去掉人声的伴奏：{file}（AI 版的文件 id）。好了 state=ready、带 url（原样的伴奏）、
    play（跟唱时放的：按段放大到和 AI 版一样响）和 vox（AI 人声，领唱用）；
    making 隔一秒再要；error 报一次，再要就重做。"""
    fid = str(body.get("file") or "")
    audio, _, _, _ = await asyncio.to_thread(_sing_file, request, fid, False)
    st = karaoke.ensure(fid, audio)
    if st["state"] == "ready":
        st["url"] = f"api/sing/karaoke/{fid}.mp3"
        try:
            await asyncio.to_thread(karaoke.extras, fid, audio)
        except karaoke.KaraokeError:
            pass
        if karaoke.acc_path(fid, "mon"):
            st["play"] = f"api/sing/karaoke/{fid}.mon.mp3"
        if karaoke.acc_path(fid, "vox"):
            st["vox"] = f"api/sing/karaoke/{fid}.vox.mp3"
    return st


def _guide(v: str) -> list[tuple[float, float]]:
    """「AI 领唱」开着的几段：12.3-40.1,50-60（歌里的秒）。"""
    out = []
    for part in filter(None, v.split(",")[:50]):
        try:
            a, b = (float(x) for x in part.split("-", 1))
        except ValueError:
            raise HTTPException(400, "领唱的时间不对")
        if b > a:
            out.append((a, b))
    return out


@router.get("/api/sing/karaoke/{name}")
def sing_karaoke_file(request: Request, name: str) -> FileResponse:
    from server.main import _can_file

    fid, _, part = name.removesuffix(".mp3").partition(".")
    p = karaoke.acc_path(fid, part) if part in ("", "vox", "mon") else None
    info = store.file_info(fid) if p else None
    if info is None or not _can_file(_who(request), info[1]):
        raise HTTPException(404, "伴奏还没好")
    return FileResponse(p, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/sing/real")
async def sing_real(request: Request, file: str, s0: float, singer: str = "", guide: str = "") -> dict:
    """跟着伴奏唱的录音（wav，原声）：file 是 AI 版的文件 id，s0 是录音第 0 个点对着歌里第几秒（前端拿播放进度估的，
    真正的延迟这边对出来），guide 是开着「AI 领唱」的几段。混成真人版挂到这张卡片上（再唱一次就换掉），返回新的 sing 字段。"""
    w = _who(request)
    data = await request.body()
    if len(data) > karaoke.MAX_REC_BYTES:
        raise HTTPException(413, "录音太大了")
    spans = _guide(guide)
    song, sing, thread, m = await asyncio.to_thread(_sing_file, request, file, False)
    acc = karaoke.acc_path(file)
    if acc is None:
        raise HTTPException(409, "伴奏还没好")
    try:
        mp3, info = await asyncio.to_thread(karaoke.make_real, data, acc, s0, song, spans, karaoke.acc_path(file, "mon"))
    except karaoke.KaraokeError as e:
        raise HTTPException(400, str(e))
    who = singer.strip()[:12] or sing["singer"]
    fid, p = store.allocate()
    try:
        p.write_bytes(mp3)
        meta = store.commit(fid, f"{who}亲自唱《{sing['title']}》.mp3", "audio/mpeg", len(mp3), owner="xiaoxin")
    except Exception:
        store.abort(fid)
        raise
    await asyncio.to_thread(karaoke.keep_recording, fid, data)
    old = (sing.get("real") or {}).get("file")
    real = {"file": fid, "singer": who, "by": w.id, "guide": bool(spans), **info}
    files = [f for f in (m.get("files") or []) if f.get("id") != old] + [meta]
    out = store.update_message(thread, m["id"], sing={**sing, "real": real}, files=files)
    return {"sing": out["sing"], "files": out["files"]}


# ---------------------------------------------------------------- QQ 音乐登录

@router.get("/api/sing/qq")
def sing_qq_status(request: Request, fresh: int = 0) -> dict:
    _who(request)
    return qq.status(fresh=bool(fresh))


@router.post("/api/sing/qq/login")
def sing_qq_login(request: Request, body: dict = Body(default={})) -> dict:
    """开始扫码：kind = wx（微信）| qq（手机 QQ）。返回会话 id 和二维码。"""
    _need_parent(request)
    try:
        sid = qq.login_start(str(body.get("kind") or "wx"))
    except qq.QQError as e:
        raise HTTPException(400, str(e))
    return {"sid": sid, **qq.login_state(sid)}


@router.get("/api/sing/qq/login/{sid}")
def sing_qq_login_state(request: Request, sid: str) -> dict:
    _need_parent(request)
    return qq.login_state(sid)


@router.delete("/api/sing/qq/login")
def sing_qq_login_cancel(request: Request) -> dict:
    _need_parent(request)
    qq.login_cancel()
    return {"ok": True}


@router.delete("/api/sing/qq")
def sing_qq_logout(request: Request) -> dict:
    _need_parent(request)
    qq.logout()
    return {"ok": True}
