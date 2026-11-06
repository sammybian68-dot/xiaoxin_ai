"""QQ 音乐命令行：server/sing/qq.py 用子进程调它，每次输出一行 JSON。

单独跑在 .venv-qqmusic 里（qqmusic-api-python 要更新版的 pydantic、cryptography，还带 urllib3-future
会顶替 requests 用的 urllib3，不能装进小新的环境），所以这里只用标准库和 qqmusic_api，不 import server。

  login <qq|wx|mobile> <dir>   扫码登录：二维码写到 <dir>/qr.png|jpg，状态写 <dir>/state.json，登上了存凭证
  refresh                       刷新凭证（musickey 有效期 3 天，拿到 20 小时以后才真去刷）
  status                        有没有登录、凭证还能不能用
  search <关键字> [条数]          搜歌（不用登录）
  lyric <mid>                   LRC 歌词（不用登录）
  fetch <mid> <out>             下载整首（要会员凭证），先试 320k，拿不到退到 128k

凭证路径在环境变量 QQMUSIC_CRED，文件权限 600。设备信息（IMEI、MAC 这些）存在旁边的 device.json，每次都用同一台：
不传 device_path 时库每个进程随机造一台新设备，2026-11-06 扫码登上后调了十几次就「登录设备超限」，凭证被挤掉。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
STREAM = "https://ws.stream.qqmusic.qq.com/"
HEAD = {"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
CRED = Path(os.environ.get("QQMUSIC_CRED") or "credential.json")
DEVICE = CRED.with_name("device.json")
LOGIN_TIMEOUT = 180
REFRESH_AFTER_S = 20 * 3600   # musickey 3 天过期；拿到不到 20 小时就不刷（每次部署重启都会叫一次 refresh）


def out(**data) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _cgi(module: str, method: str, param: dict) -> dict:
    payload = {"comm": {"ct": 19, "cv": 1859, "uin": "0"}, "req": {"module": module, "method": method, "param": param}}
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), headers=HEAD)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["req"]


def _load():
    from qqmusic_api.models.request import Credential

    if not CRED.is_file():
        return None
    return Credential.model_validate_json(CRED.read_text(encoding="utf-8"))


def _save(cred) -> None:
    CRED.parent.mkdir(parents=True, exist_ok=True)
    _write(CRED, cred.model_dump_json(by_alias=True))


def _client(cred=None):
    from qqmusic_api import Client

    DEVICE.parent.mkdir(parents=True, exist_ok=True)
    return Client(credential=cred, device_path=str(DEVICE))


def _lock_device() -> None:
    for p in DEVICE.parent.glob("device*.json"):
        os.chmod(p, 0o600)


def _expires_at(cred) -> int:
    return int(cred.musickey_create_time or 0) + int(cred.key_expires_in or 0)


# ---------------------------------------------------------------- 不用登录的

def search(word: str, n: int = 5) -> None:
    body = _cgi("music.search.SearchCgiService", "DoSearchForQQMusicDesktop",
                {"query": word, "num_per_page": n, "page_num": 1, "search_type": 0})["data"]["body"]
    songs = []
    for s in body["song"]["list"][:n]:
        album = s.get("album") or {}
        songs.append({"mid": s["mid"], "title": s.get("name") or s.get("title") or "",
                      "artist": " / ".join(x.get("name", "") for x in s.get("singer") or []),
                      "album": album.get("name") or "", "album_mid": album.get("mid") or "",
                      "seconds": int(s.get("interval") or 0), "vip": bool((s.get("pay") or {}).get("pay_play"))})
    out(ok=True, songs=songs)


def lyric(mid: str) -> None:
    data = _cgi("music.musichallSong.PlayLyricInfo", "GetPlayLyricInfo", {"songMID": mid})["data"]
    text = base64.b64decode(data.get("lyric") or "").decode("utf-8", "replace")
    out(ok=True, lrc=text)


# ---------------------------------------------------------------- 要登录的

async def login(kind: str, d: Path) -> None:
    from qqmusic_api.models.login import QRCodeLoginEvents
    from qqmusic_api.modules.login import QRLoginType
    from qqmusic_api.modules.login_utils import QRCodeLoginSession

    d.mkdir(parents=True, exist_ok=True)
    state = d / "state.json"

    def put(s: str, **extra) -> None:
        _write(state, json.dumps({"state": s, "kind": kind, "at": time.time(), **extra}, ensure_ascii=False))

    names = {"qq": QRLoginType.QQ, "wx": QRLoginType.WX, "mobile": QRLoginType.MOBILE}
    session = QRCodeLoginSession(_client().login, names[kind], timeout_seconds=LOGIN_TIMEOUT)
    qr = await session.get_qrcode()
    ext = "png" if qr.mimetype == "image/png" else "jpg"
    (d / f"qr.{ext}").write_bytes(qr.data)
    put("waiting", qr=f"qr.{ext}", expires=time.time() + LOGIN_TIMEOUT)
    seen = {QRCodeLoginEvents.SCAN: "waiting", QRCodeLoginEvents.CONF: "scanned"}
    try:
        async for ev in session:
            if ev.event in seen:
                put(seen[ev.event], qr=f"qr.{ext}", expires=time.time() + LOGIN_TIMEOUT)
            elif ev.event == QRCodeLoginEvents.DONE and ev.credential is not None:
                _save(ev.credential)
                put("done")
                out(ok=True)
                return
            elif ev.event == QRCodeLoginEvents.REFUSE:
                put("refused")
                break
            elif ev.event == QRCodeLoginEvents.TIMEOUT:
                put("timeout")
                break
        else:
            put("error", detail="QQ 音乐那边断开了，没等到登录结果")
    except Exception as e:  # noqa: BLE001 写进 state，设置页才看得到；stdout 没人收
        put("error", detail=f"{type(e).__name__}: {str(e)[:200]}")
        raise
    out(ok=False)


async def refresh() -> None:
    cred = _load()
    if cred is None:
        out(ok=False, why="no_login")
        return
    if cred.musickey_create_time and time.time() - cred.musickey_create_time < REFRESH_AFTER_S:
        out(ok=True, refreshed=False, expires_at=_expires_at(cred))
        return
    client = _client(cred)
    try:
        fresh = await client.login.refresh_credential(cred)
    except Exception as e:  # noqa: BLE001 刷新失败：看旧的还能不能用
        expired = await client.login.check_expired(cred)
        out(ok=not expired, refreshed=False, expired=expired, error=f"{type(e).__name__}: {str(e)[:160]}")
        return
    _save(fresh)
    out(ok=True, refreshed=True, expires_at=_expires_at(fresh))


async def status() -> None:
    cred = _load()
    if cred is None:
        out(ok=True, logged_in=False)
        return
    uin = str(cred.musicid or cred.str_musicid or "")
    masked = uin[:2] + "*" * max(0, len(uin) - 4) + uin[-2:] if len(uin) > 4 else uin
    expired = await _client(cred).login.check_expired(cred)
    out(ok=True, logged_in=True, expired=expired, account=masked, expires_at=_expires_at(cred),
        saved_at=int(CRED.stat().st_mtime))


async def fetch(mid: str, dst: Path) -> None:
    from qqmusic_api.modules.song import SongFileInfo, SongFileType

    cred = _load()
    if cred is None:
        out(ok=False, why="no_login")
        return
    client = _client(cred)
    for quality, ftype in (("320k", SongFileType.MP3_320), ("128k", SongFileType.MP3_128)):
        res = await client.song.get_song_urls([SongFileInfo(mid=mid)], ftype)
        data = json.loads(res.model_dump_json()) if hasattr(res, "model_dump_json") else res
        purl = _find(data, "purl")
        if not purl:
            continue
        url = purl if purl.startswith("http") else STREAM + purl
        req = urllib.request.Request(url, headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0"})
        tmp = dst.with_suffix(dst.suffix + ".part")
        with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
        size = tmp.stat().st_size
        if size < 100_000:   # 拿到的是试听片段或错误页
            tmp.unlink(missing_ok=True)
            continue
        tmp.replace(dst)
        out(ok=True, bytes=size, quality=quality)
        return
    expired = await client.login.check_expired(cred)
    out(ok=False, why="expired" if expired else "no_url")


def _find(node, key: str) -> str:
    if isinstance(node, dict):
        if isinstance(node.get(key), str) and node[key]:
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for v in node:
            if found := _find(v, key):
                return found
    return ""


def main(argv: list[str]) -> None:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "search":
            search(args[0], int(args[1]) if len(args) > 1 else 5)
        elif cmd == "lyric":
            lyric(args[0])
        elif cmd == "login":
            asyncio.run(login(args[0], Path(args[1])))
        elif cmd == "refresh":
            asyncio.run(refresh())
        elif cmd == "status":
            asyncio.run(status())
        elif cmd == "fetch":
            asyncio.run(fetch(args[0], Path(args[1])))
        else:
            out(ok=False, error=f"不认识的命令：{cmd}")
    except Exception as e:  # noqa: BLE001 一律回 JSON，调用方看 ok
        out(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        _lock_device()


if __name__ == "__main__":
    main(sys.argv[1:])
