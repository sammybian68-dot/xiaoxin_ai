"""翻唱的接口：/api/sing/*。录声音、试听、删；封面；发到微信的视频；QQ 音乐扫码登录（只有家长、管理员）。"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from server import store
from server.sing import job, practice, qq, video, voices

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


# ---------------------------------------------------------------- 发到微信的视频

def _sing_of(fid: str) -> dict | None:
    from server.family import members

    for k in members.kids(active_only=False):
        try:
            msgs = store.get(k["thread"])["messages"]
        except KeyError:
            continue
        for m in reversed(msgs):
            if (m.get("sing") or {}).get("file") == fid:
                return m["sing"]
    return None


def _sing_file(request: Request, fid: str):
    """翻唱的那个 mp3 和它的卡片字段；看不到这个文件（孩子看别人线程里的）一律当没有。"""
    from server.main import _can_file

    info = store.file_info(fid)
    if info is None or not info[0].is_file() or not _can_file(_who(request), info[1]):
        raise HTTPException(404, "没有这首歌")
    sing = _sing_of(fid)
    if sing is None:
        raise HTTPException(404, "这不是翻唱的歌")
    return info[0], sing


def _sec(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "时间不对")


@router.post("/api/sing/video")
async def sing_video(request: Request, body: dict = Body(...)) -> dict:
    """要一段分享用的视频：{file, start?, end?, off?, wait?}（秒；不给起止就是整首，off 是卡片上调的歌词提前量）。
    做好了 state=ready、带 url；还在做是 state=making + pct，再要一次（同一段不会重复做）；error 再要一次就重做。
    wait（最多 8 秒）：还在做就在这儿等着，做好了马上回。手机经公网一来一回有时要 3 秒，一秒问一次光问就要多花好几秒。"""
    fid = str(body.get("file") or "")
    audio, sing = await asyncio.to_thread(_sing_file, request, fid)
    start, end, off = _sec(body.get("start")), _sec(body.get("end")), _sec(body.get("off")) or 0.0
    try:
        st = video.ensure(fid, audio, sing, start, end, off)
    except video.VideoError as e:
        raise HTTPException(400, str(e))
    until = time.monotonic() + min(8.0, max(0.0, _sec(body.get("wait")) or 0.0))
    while st["state"] == "making" and time.monotonic() < until:
        await asyncio.sleep(0.2)
        st = video.state(st["key"])
        if st["state"] == "error":   # 等着的时候做坏了：照 ensure 的规矩只报这一次，下回再要就重做
            st = video.ensure(fid, audio, sing, start, end, off)
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
