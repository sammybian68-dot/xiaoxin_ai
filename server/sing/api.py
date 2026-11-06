"""翻唱的接口：/api/sing/*。录声音、试听、删；封面；QQ 音乐扫码登录（只有家长、管理员）。"""

from __future__ import annotations

import asyncio
from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from server import store
from server.sing import job, qq, voices

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


# ---------------------------------------------------------------- QQ 音乐登录

@router.get("/api/sing/qq")
def sing_qq_status(request: Request, fresh: int = 0) -> dict:
    _who(request)
    return qq.status(fresh=bool(fresh))


@router.post("/api/sing/qq/login")
def sing_qq_login(request: Request, body: dict = Body(default={})) -> dict:
    """开始扫码：kind = mobile（QQ 音乐 App 扫）| qq（手机 QQ）| wx（微信）。返回会话 id 和二维码。"""
    _need_parent(request)
    try:
        sid = qq.login_start(str(body.get("kind") or "mobile"))
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
