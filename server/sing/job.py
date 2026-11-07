"""后台翻唱：sing_song 工具先把能马上查的查完（声音、服务、QQ 音乐登录、搜歌），再起线程：
原曲（QQ 音乐下整首，缓存在 DATA/sing/songs/）→ 歌词 → 封面 → SoulX-Singer-SVC（gpu2 127.0.0.1:18024，
分离人声 → 换成录的声音 → 自动变调 → 混回伴奏）→ mp3 存成普通文件 → 以小新的身份发进线程（via=sing，带 sing 卡片字段）。

一首 4 分钟的歌在 A100 上转 30–60 秒，原曲下载 1–2 秒。SoulX 一次只转一首，后面的排队；这里同时最多接两首。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import requests

from server import store
from server.sing import qq, video, voices

log = logging.getLogger("xiaoxin.sing")

SVC = os.environ.get("SOULX_SVC_URL") or "http://127.0.0.1:18024"
MAX_BUSY = 2
JOB_TIMEOUT_S = 20 * 60
POLL_S = 3
KEEP_SONGS = 30           # 原曲缓存最多留几首（一首 320k 约 10 MB）
OPTIONS = {"prompt_sep": "false", "target_sep": "true", "auto_shift": "true", "mix_acc": "true",
           "n_steps": "32", "cfg": "1.0"}
COVER = "https://y.gtimg.cn/music/photo_new/T002R300x300M000{}.jpg"
_SKIP = re.compile(r"伴奏|纯音乐|instrumental|karaoke|dj|remix|铃声|片段", re.I)
_LIVE = re.compile(r"现场|live|演唱会", re.I)
_ALBUM = re.compile(r"^[0-9A-Za-z]{8,20}$")

_busy = threading.BoundedSemaphore(MAX_BUSY)


class SingError(RuntimeError):
    """说给孩子听的原因。"""


def songs_dir() -> Path:
    return store.DATA / "sing" / "songs"


def cover_path(album_mid: str) -> Path | None:
    if not _ALBUM.match(album_mid or ""):
        return None
    return store.DATA / "sing" / "covers" / f"{album_mid}.jpg"


LIST_FIELDS = ("file", "title", "artist", "album", "cover", "singer", "voice", "mid", "seconds")


def library(threads: list[str]) -> list[dict]:
    """这几条线程里唱好的歌，新的在前。同一个声音唱同一首（重唱过）只留最新的；文件没了的不列。"""
    out, seen = [], set()
    for tid in threads:
        try:
            msgs = store.get(tid)["messages"]
        except KeyError:
            continue
        for m in msgs:
            s = m.get("sing") or {}
            if s.get("file") and store.file_info(s["file"]) is not None:
                out.append({**{k: s.get(k) for k in LIST_FIELDS}, "created": m.get("created", 0)})
    out.sort(key=lambda x: -x["created"])
    keep = []
    for s in out:
        k = (s["voice"] or s["singer"], s["mid"] or s["title"])
        if k not in seen:
            seen.add(k)
            keep.append(s)
    return keep


def service_up() -> bool:
    try:
        return requests.get(f"{SVC}/health", timeout=3).json().get("ok") is True
    except (requests.RequestException, ValueError):
        return False


def _norm(s: str) -> str:
    return re.sub(r"[\s·・\-—_（）()《》【】\[\]]+", "", s or "").lower()


def candidates(song: str, artist: str = "") -> list[dict]:
    """搜歌、按合适程度排好：歌名对得上的在前，伴奏 / DJ / 片段不要（没人声，唱不了），没点名「现场版」就不要现场版。"""
    found = qq.search(f"{song} {artist}".strip(), 10)
    want = _norm(song)
    live_ok = bool(_LIVE.search(song))
    ok = [s for s in found if not _SKIP.search(s["title"]) and s["seconds"] >= 30 and (live_ok or not _LIVE.search(s["title"]))]
    if artist:
        named = [s for s in ok if _norm(artist) in _norm(s["artist"])]
        ok = named or ok
    return [s for s in ok if _norm(s["title"]) == want] + [s for s in ok if _norm(s["title"]) != want]


def pick(song: str, artist: str = "") -> dict:
    found = candidates(song, artist)
    if not found:
        raise SingError(f"QQ 音乐上没找到《{song}》")
    return found[0]


def _original(info: dict) -> Path:
    d = songs_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{info['mid']}.mp3"
    if p.exists():
        p.touch()
        return p
    r = qq.fetch(info["mid"], p)
    if r.get("ok"):
        _prune(d)
        return p
    if r.get("why") in ("no_login", "expired"):
        raise SingError("QQ 音乐的登录过期了，请爸爸妈妈在「设置 → 唱歌」里扫码登录一下，再让我唱")
    raise SingError(f"《{info['title']}》在 QQ 音乐上拿不到整首（可能没有版权），换一首吧")


def _prune(d: Path) -> None:
    songs = sorted(d.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in songs[KEEP_SONGS:]:
        p.unlink(missing_ok=True)
        p.with_suffix(".lrc").unlink(missing_ok=True)


def _lyrics(mid: str) -> str:
    p = songs_dir() / f"{mid}.lrc"
    if p.exists():
        return p.read_text(encoding="utf-8")
    try:
        text = qq.lyric(mid)
    except qq.QQError as e:
        log.warning("lyric %s: %s", mid, e)
        return ""
    if text:
        p.write_text(text, encoding="utf-8")
    return text


def _cover(album_mid: str) -> str:
    p = cover_path(album_mid)
    if p is None:
        return ""
    if not p.exists():
        try:
            r = requests.get(COVER.format(album_mid), timeout=10)
            if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):
                return ""
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
        except requests.RequestException:
            return ""
    return album_mid


def convert(voice_wav: Path, song: Path) -> tuple[bytes, dict]:
    """交给 SoulX 转，等它转完拿 mp3。返回 (mp3, 任务状态)。"""
    with voice_wav.open("rb") as v, song.open("rb") as s:
        r = requests.post(f"{SVC}/jobs", files={"prompt": ("voice.wav", v, "audio/wav"), "target": ("song.mp3", s, "audio/mpeg")},
                          data=OPTIONS, timeout=120)
    if r.status_code != 200:
        raise SingError(f"唱歌的服务没接这首（{r.status_code}）")
    jid = r.json()["id"]
    deadline = time.time() + JOB_TIMEOUT_S
    try:
        while True:
            st = requests.get(f"{SVC}/jobs/{jid}", timeout=10).json()
            if st["state"] == "done":
                break
            if st["state"] == "error":
                log.warning("soulx %s error: %s", jid, st.get("error"))
                raise SingError("这首没唱出来")
            if time.time() > deadline:
                raise SingError("唱了太久还没好")
            time.sleep(POLL_S)
        a = requests.get(f"{SVC}/jobs/{jid}/audio", params={"kind": "result", "fmt": "mp3"}, timeout=180)
        if a.status_code != 200 or len(a.content) < 10_000:
            raise SingError("唱好的歌没拿回来")
        return a.content, st
    finally:
        try:
            requests.delete(f"{SVC}/jobs/{jid}", timeout=10)
        except requests.RequestException:
            pass


def _call(kid_name: str) -> str:
    return f"{kid_name}，" if kid_name else ""


def _work(thread: str, kid: dict, voice: dict, info: dict) -> None:
    started = time.time()
    who = "你" if kid.get("id") and voice.get("member") == kid.get("id") else voice["name"]
    name = kid.get("call") or kid.get("name") or ""
    try:
        song = _original(info)
        lrc = _lyrics(info["mid"])
        cover = _cover(info.get("album_mid", ""))
        mp3, st = convert(voices.path(voice["id"]), song)
        fid, p = store.allocate()
        try:
            p.write_bytes(mp3)
            meta = store.commit(fid, f"{voice['name']}唱《{info['title']}》.mp3", "audio/mpeg", len(mp3), owner="xiaoxin")
        except Exception:
            store.abort(fid)
            raise
        sing = {"file": fid, "title": info["title"], "artist": info["artist"], "album": info.get("album", ""),
                "cover": cover, "singer": voice["name"], "member": voice.get("member", ""), "voice": voice["id"],
                "mid": info["mid"], "lrc": lrc, "seconds": round(float(st.get("target_sec") or info["seconds"]), 1),
                "shift": st.get("shift", 0)}
        log.info("sing %s by %s ok in %.0fs (svc %.0fs)", info["mid"], voice["name"], time.time() - started,
                 (st.get("total_ms") or 0) / 1000)
        store.append(thread, {"role": "assistant", "model": "", "via": "sing", "files": [meta], "sing": sing,
                              "text": f"{_call(name)}{who}唱的《{info['title']}》做好了，点播放听听吧。"})
        try:
            video.ensure(fid, p, sing)   # 整首的分享视频先做着，点「发到微信」就不用等
        except video.VideoError as e:
            log.warning("sing video %s: %s", fid, e)
    except SingError as e:
        store.append(thread, {"role": "assistant", "model": "", "via": "sing", "text": f"{_call(name)}{e}。"})
    except Exception as e:  # noqa: BLE001 网络、磁盘：说一句没唱成，原因记下来
        log.exception("sing failed")
        store.append(thread, {"role": "assistant", "model": "", "via": "sing", "error": str(e)[:300],
                              "text": f"{_call(name)}《{info['title']}》这次没唱成，等会儿再试试吧。"})
    finally:
        _busy.release()


def start(thread: str, kid: dict, voice: dict, info: dict) -> bool:
    """起后台线程。手上已经有两首在唱就返回 False。"""
    if not _busy.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_work, args=(thread, kid, voice, info), daemon=True, name="sing").start()
    except Exception:
        _busy.release()
        raise
    return True
