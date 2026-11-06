"""跟着伴奏唱：把 AI 翻唱好的那首去掉人声当伴奏（调和 AI 版一样，是按这个声音变过调的），真人跟着唱、录下来，
和伴奏对齐混成「真人版」，卡片上 AI 唱 / 真人唱一键切换对比。

  伴奏：SoulX 服务的 /separate（5090 上 4 分钟的歌 5 秒），存 DATA/sing/acc/<翻唱的文件 id>.mp3
  对齐：录音是原声（不开回声消除），外放的伴奏也录进去了，拿它和干净伴奏做 GCC-PHAT 找延迟（分几段找，对得上才信）；
        戴耳机录不到伴奏、对不上就用 DEFAULT_LAT_S
  混音：和 AI 版同一条时间轴（开头没唱的部分只有伴奏），唱完再留 TAIL_S 秒伴奏淡出
"""

from __future__ import annotations

import io
import logging
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests

from server import store
from server.sing import job, video

log = logging.getLogger("xiaoxin.sing")

KEEP = 40
POLL_S = 1.5
SEP_TIMEOUT_S = 10 * 60
MIN_SING_S = 5.0
MAX_REC_BYTES = 40 * 1024 * 1024
XWIN_S = 20.0           # 找延迟：每段最长这么长，最多三段
XWIN_MIN_S = 6.0
AGREE_S = 0.015
PHAT_BETA = 0.8
LAT_RANGE = (-0.15, 0.9)
DEFAULT_LAT_S = 0.15    # 安卓外放 + 录音的常见来回延迟
ACC_GAIN = 0.8
VOICE_PEAK = 0.75
TAIL_S = 1.5

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


class KaraokeError(ValueError):
    pass


def acc_dir() -> Path:
    return store.DATA / "sing" / "acc"


def acc_path(fid: str) -> Path | None:
    try:
        store._check(fid)
    except KeyError:
        return None
    p = acc_dir() / f"{fid}.mp3"
    return p if p.exists() else None


def ensure(fid: str, song: Path) -> dict:
    """伴奏：有了 ready；没有就起线程去分离（making）；分离失败报一次 error，再要就重来。"""
    if acc_path(fid):
        return {"state": "ready"}
    with _lock:
        j = _jobs.get(fid)
        if j and j["state"] == "making":
            return dict(j)
        if j and j["state"] == "error":
            _jobs.pop(fid)
            return dict(j)
        _jobs[fid] = {"state": "making"}
    threading.Thread(target=_separate, args=(fid, song), daemon=True, name="sing-acc").start()
    return {"state": "making"}


def _separate(fid: str, song: Path) -> None:
    try:
        with song.open("rb") as f:
            r = requests.post(f"{job.SVC}/separate", files={"target": ("song.mp3", f, "audio/mpeg")}, timeout=120)
        if r.status_code != 200:
            raise KaraokeError(f"唱歌的服务没接（{r.status_code}）")
        jid = r.json()["id"]
        deadline = time.time() + SEP_TIMEOUT_S
        try:
            while (st := requests.get(f"{job.SVC}/jobs/{jid}", timeout=10).json())["state"] not in ("done", "error"):
                if time.time() > deadline:
                    raise KaraokeError("分离伴奏太久了")
                time.sleep(POLL_S)
            if st["state"] == "error":
                raise KaraokeError(st.get("error") or "伴奏没分出来")
            a = requests.get(f"{job.SVC}/jobs/{jid}/audio", params={"fmt": "mp3"}, timeout=120)
            if a.status_code != 200 or len(a.content) < 10_000:
                raise KaraokeError("伴奏没拿回来")
        finally:
            try:
                requests.delete(f"{job.SVC}/jobs/{jid}", timeout=10)
            except requests.RequestException:
                pass
        d = acc_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{fid}.part"
        tmp.write_bytes(a.content)
        tmp.replace(d / f"{fid}.mp3")
        with _lock:
            _jobs.pop(fid, None)
        _prune(d)
    except Exception as e:  # noqa: BLE001 网络、服务：记下来，前端说一声
        log.warning("sing acc %s: %s", fid, e)
        with _lock:
            _jobs[fid] = {"state": "error", "error": "伴奏没做出来"}


def _prune(d: Path) -> None:
    accs = sorted(d.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in accs[KEEP:]:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 真人版

def _decode(path: Path, rate: int) -> np.ndarray:
    exe = video.ffmpeg_exe()
    if not exe:
        raise KaraokeError("服务器上没有 ffmpeg")
    r = subprocess.run([exe, "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise KaraokeError("伴奏读不了")
    return np.frombuffer(r.stdout, dtype=np.float32)


def _encode_mp3(x: np.ndarray, rate: int) -> bytes:
    r = subprocess.run([video.ffmpeg_exe(), "-nostdin", "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "-",
                        "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "-"],
                       input=x.astype(np.float32).tobytes(), capture_output=True, timeout=180)
    if r.returncode != 0 or len(r.stdout) < 1000:
        raise KaraokeError("真人版没合成出来")
    return r.stdout


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise KaraokeError("录音格式不对")
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    except (wave.Error, EOFError) as e:
        raise KaraokeError("录音读不了") from e
    return pcm.astype(np.float32) / 32768, rate


def _lag(rec: np.ndarray, acc: np.ndarray, rate: int, s0: float, w: int) -> float | None:
    """GCC-PHAT（β 加权，比纯 PHAT 经得起人声盖过伴奏）找录音开头 w 个点里伴奏的延迟（秒）。"""
    lo, hi = int(LAT_RANGE[0] * rate), int(LAT_RANGE[1] * rate)
    a0 = int(s0 * rate)
    if a0 - lo + w > len(acc) or len(rec) < w + hi - lo:
        return None
    ref = acc[a0 - lo: a0 - lo + w]           # 录音第 n + k 个点对着它第 n 个点时，延迟 = k + lo
    sig = rec[: w + hi - lo]
    n = 1 << int(np.ceil(np.log2(len(sig) + len(ref))))
    g = np.fft.rfft(sig, n) * np.conj(np.fft.rfft(ref, n))
    cc = np.fft.irfft(g / (np.abs(g) ** PHAT_BETA + 1e-9), n)
    return (int(np.argmax(np.abs(cc[: hi - lo + 1]))) + lo) / rate


def latency(rec: np.ndarray, acc: np.ndarray, rate: int, s0: float) -> tuple[float, int]:
    """录音里外放的伴奏比「歌里 s0 秒对着录音第 0 个点」晚了多少秒，和有几段对得上。录音第 i 个点 ≈ 歌里 s0 + i/rate - 延迟。
    录音切成几段各找一次，至少两段差不到 AGREE_S 才信：戴耳机录不到伴奏时每段找到的都是乱的，碰不到一起。"""
    usable = len(rec) / rate - (LAT_RANGE[1] - LAT_RANGE[0])
    parts = min(3, int(usable // XWIN_MIN_S))
    if parts < 2:
        return DEFAULT_LAT_S, 0
    w = min(XWIN_S, usable / parts)
    lags = [x for i in range(parts) if (x := _lag(rec[int(i * w * rate):], acc, rate, s0 + i * w, int(w * rate))) is not None]
    if len(lags) < 2:
        return DEFAULT_LAT_S, 0
    mid = float(np.median(lags))
    near = [x for x in lags if abs(x - mid) <= AGREE_S]
    if len(near) < 2:
        return DEFAULT_LAT_S, 0
    return float(np.mean(near)), len(near)


def mix(rec: np.ndarray, rate: int, acc: np.ndarray, s0: float, lat: float) -> np.ndarray:
    """和 AI 版同一条时间轴：伴奏垫底，人声从歌里 s0 - lat 秒放进去，唱完再留 TAIL_S 秒、淡出。"""
    at = int(round((s0 - lat) * rate))
    end = min(len(acc), at + len(rec) + int(TAIL_S * rate))
    out = acc[:end] * ACC_GAIN
    peak = float(np.percentile(np.abs(rec), 99.9)) if len(rec) else 0.0
    voice = rec * (VOICE_PEAK / peak if peak > 1e-3 else 1.0)
    src0, dst0 = max(0, -at), max(0, at)
    n = min(len(voice) - src0, end - dst0)
    if n > 0:
        ramp = min(n, int(0.03 * rate))
        v = voice[src0: src0 + n].copy()
        v[:ramp] *= np.linspace(0, 1, ramp, dtype=np.float32)
        out[dst0: dst0 + n] += v
    fade = min(len(out), int(TAIL_S * rate))
    out[len(out) - fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    top = float(np.max(np.abs(out))) if len(out) else 1.0
    return out / top * 0.98 if top > 0.98 else out


def make_real(data: bytes, acc: Path, s0: float) -> tuple[bytes, dict]:
    """录音（wav）+ 伴奏 → 真人版 mp3 和 {start, end（唱的那段，歌里的秒）, seconds（真人版多长）, lat, agree}。"""
    rec, rate = read_wav(data)
    if len(rec) < rate * MIN_SING_S:
        raise KaraokeError(f"唱得太短了，至少唱 {MIN_SING_S:.0f} 秒")
    a = _decode(acc, rate)
    if not 0 <= s0 < len(a) / rate - 1:
        raise KaraokeError("从哪儿开始唱的不对")
    lat, conf = latency(rec, a, rate, s0)
    out = mix(rec, rate, a, s0, lat)
    start = max(0.0, s0 - lat)
    return _encode_mp3(out, rate), {"start": round(start, 2), "end": round(min(len(a) / rate, start + len(rec) / rate), 2),
                                    "seconds": round(len(out) / rate, 1), "lat": round(lat, 3), "agree": conf}
