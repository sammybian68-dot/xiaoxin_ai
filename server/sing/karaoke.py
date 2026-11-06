"""跟着伴奏唱：把 AI 翻唱好的那首去掉人声当伴奏（调和 AI 版一样，是按这个声音变过调的），真人跟着唱、录下来，
和伴奏对齐混成「真人版」，卡片上 AI 唱 / 真人唱一键切换对比。

  伴奏：SoulX 服务的 /separate（5090 上 4 分钟的歌 5 秒），存 DATA/sing/acc/<翻唱的文件 id>.mp3；
        旁边 <id>.vox.mp3 是 AI 人声（翻唱减伴奏，「AI 领唱」跟着伴奏一起放、只调它的音量），<id>.mon.mp3 是跟唱时放的伴奏：
        按段放大到和 AI 版一样响（主歌那种只有一点钢琴的地方去掉人声后小了一大截，pad 外放听不清就跑调）
  对齐：录音是原声（不开回声消除），外放的伴奏也录进去了，拿它和干净伴奏做 GCC-PHAT 找延迟（分几段找，对得上才信）；
        戴耳机录不到伴奏、对不上就用 DEFAULT_LAT_S
  去回声：放的是什么（伴奏，开了「AI 领唱」的那几段再加 AI 人声）知道、延迟也对出来了，按频带估房间的传递、减掉，录音里只剩人声
  混音：和 AI 版同一条时间轴、伴奏和 AI 版里一样响（背景完全一样），人声响度对齐 AI 唱的那段；开头没唱的部分只有伴奏，
        唱完再留 TAIL_S 秒伴奏淡出
  录音原样留一份（DATA/sing/rec，最近 KEEP_REC 条），算法改了还能重新混
"""

from __future__ import annotations

import io
import logging
import subprocess
import tempfile
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
TAIL_S = 1.5
KEEP_REC = 20
AEC_NFFT, AEC_HOP, AEC_TAPS = 1024, 256, 16   # 每个频带往回看 16 帧（约 0.2 秒的房间混响）
LIMIT = 0.9             # 超过这么大的样点软压到 1 以内（不整首压小，伴奏保持 AI 版的音量）
BOOST_MAX = 4.0         # 跟唱时伴奏最多放大几倍
BOOST_WIN_S = 0.5
BOOST_SMOOTH = 5        # 前后几个窗一起平滑（约 2.5 秒），放大得不忽大忽小
PART_RATE = 32000

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


class KaraokeError(ValueError):
    pass


def acc_dir() -> Path:
    return store.DATA / "sing" / "acc"


def acc_path(fid: str, part: str = "") -> Path | None:
    """part 空是伴奏，vox 是 AI 人声，mon 是跟唱时放的（放大过的）伴奏。"""
    try:
        store._check(fid)
    except KeyError:
        return None
    p = acc_dir() / f"{fid}{'.' + part if part else ''}.mp3"
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


def _prune(d: Path, pattern: str = "*.mp3", keep: int = KEEP) -> None:
    items = sorted((p for p in d.glob(pattern) if "." not in p.stem), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in items[keep:]:
        p.unlink(missing_ok=True)
        for part in ("vox", "mon"):
            (d / f"{p.stem}.{part}.mp3").unlink(missing_ok=True)


def monitor(acc: np.ndarray, song: np.ndarray, rate: int) -> np.ndarray:
    """跟唱时放的伴奏：每 BOOST_WIN_S 秒放大到和 AI 版差不多响（1 到 BOOST_MAX 倍，平滑过）。"""
    f = int(BOOST_WIN_S * rate)
    n = -(-len(acc) // f)

    def lv(x):
        x = np.concatenate([x, np.zeros(n * f - len(x), np.float32)])
        return np.sqrt(np.mean(x.reshape(n, f) ** 2, axis=1))

    la, lc = lv(acc), lv(song)
    g = np.where(lc > 1e-3, np.clip(lc / np.maximum(la, 1e-4), 1.0, BOOST_MAX), 1.0)
    k = np.ones(BOOST_SMOOTH) / BOOST_SMOOTH
    lg = np.convolve(np.pad(np.log(g), BOOST_SMOOTH // 2, mode="edge"), k, mode="valid")
    gs = np.interp(np.arange(len(acc)), (np.arange(n) + 0.5) * f, np.exp(lg)).astype(np.float32)
    return _limit(acc * gs)


def extras(fid: str, song: Path) -> None:
    """跟唱要的另外两样，第一次要的时候做（几秒）：AI 人声 vox（「AI 领唱」）和跟唱时放的伴奏 mon。"""
    acc = acc_path(fid)
    if acc is None or (acc_path(fid, "vox") and acc_path(fid, "mon")):
        return
    a, c = _decode(acc, PART_RATE), _decode(song, PART_RATE)
    n = min(len(a), len(c))
    a, c = a[:n], c[:n]
    for part, x in (("vox", _limit(c - a)), ("mon", monitor(a, c, PART_RATE))):
        tmp = acc_dir() / f"{fid}.{part}.part"
        tmp.write_bytes(_encode_mp3(x, PART_RATE))
        tmp.replace(acc_dir() / f"{fid}.{part}.mp3")


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
    """写到文件再读回来：写管道 ffmpeg 补不了 LAME 头里的编码延迟，播放时整首会晚 46 毫秒（和 AI 版错开）。"""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "real.mp3"
        r = subprocess.run([video.ffmpeg_exe(), "-nostdin", "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "-",
                            "-codec:a", "libmp3lame", "-b:a", "128k", str(out)],
                           input=x.astype(np.float32).tobytes(), capture_output=True, timeout=180)
        data = out.read_bytes() if out.exists() else b""
    if r.returncode != 0 or len(data) < 1000:
        raise KaraokeError("真人版没合成出来")
    return data


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


def _level(x: np.ndarray, rate: int) -> float:
    """响度：50 毫秒一帧的 RMS 取 90 分位（只看唱着的地方，空着的、换气的不算）。"""
    f = int(0.05 * rate)
    n = len(x) // f
    if n < 1:
        return 0.0
    return float(np.percentile(np.sqrt(np.mean(x[: n * f].reshape(n, f) ** 2, axis=1)), 90))


def _frames(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    w = np.hanning(AEC_NFFT).astype(np.float32)
    pad = np.concatenate([np.zeros(AEC_NFFT, np.float32), x.astype(np.float32), np.zeros(AEC_NFFT, np.float32)])
    n = 1 + (len(pad) - AEC_NFFT) // AEC_HOP
    idx = np.arange(AEC_NFFT)[None, :] + AEC_HOP * np.arange(n)[:, None]
    return np.fft.rfft(pad[idx] * w, axis=1), w


def dereverb_echo(rec: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """录音里减掉外放进来的 ref（已经对齐）：每个频带用前 AEC_TAPS 帧的 ref 做最小二乘，估出房间的传递再减。
    人声和放的东西不相关，整段一起估就准；戴耳机时估出来约等于 0，录音不变。"""
    Y, w = _frames(rec)
    X, _ = _frames(ref)
    Y, X = Y.astype(np.complex64), X.astype(np.complex64)
    T, L = len(Y), AEC_TAPS
    # 正规方程按频带一起解：X^H X 是 Toeplitz（c[d] = Σ conj(X[t]) X[t+d]），X^H Y 是 Σ conj(X[t-l]) Y[t]
    c = np.stack([np.sum(np.conj(X[: T - d]) * X[d:], axis=0) for d in range(L)])
    d = np.arange(L)[:, None] - np.arange(L)[None, :]
    AhA = np.where((d >= 0)[..., None], c[np.abs(d)], np.conj(c[np.abs(d)])).transpose(2, 0, 1)
    AhY = np.stack([np.sum(np.conj(X[: T - l]) * Y[l:], axis=0) for l in range(L)], axis=1)
    reg = 1e-3 * np.abs(c[0]) + 1e-9
    h = np.linalg.solve(AhA + reg[:, None, None] * np.eye(L, dtype=np.complex64), AhY[..., None])[..., 0]
    E = Y.copy()
    for l in range(L):
        E[l:] -= h[:, l][None, :] * X[: T - l]
    x = np.fft.irfft(E, AEC_NFFT, axis=1).astype(np.float32) * w
    out = np.zeros(AEC_NFFT + AEC_HOP * (T - 1), np.float32)
    nrm = np.zeros_like(out)
    for i in range(T):
        out[i * AEC_HOP: i * AEC_HOP + AEC_NFFT] += x[i]
        nrm[i * AEC_HOP: i * AEC_HOP + AEC_NFFT] += w ** 2
    return (out / np.maximum(nrm, 1e-6))[AEC_NFFT: AEC_NFFT + len(rec)]


def _limit(x: np.ndarray) -> np.ndarray:
    a = np.abs(x)
    over = a > LIMIT
    if over.any():
        x = x.copy()
        x[over] = np.sign(x[over]) * (LIMIT + (0.99 - LIMIT) * np.tanh((a[over] - LIMIT) / (0.99 - LIMIT)))
    return x


def mix(voice: np.ndarray, rate: int, acc: np.ndarray, s0: float, lat: float) -> np.ndarray:
    """和 AI 版同一条时间轴：伴奏原样垫底（和 AI 版一样响），人声（响度已经对好）从歌里 s0 - lat 秒放进去，
    唱完再留 TAIL_S 秒、淡出。"""
    at = int(round((s0 - lat) * rate))
    end = min(len(acc), at + len(voice) + int(TAIL_S * rate))
    out = acc[:end].copy()
    src0, dst0 = max(0, -at), max(0, at)
    n = min(len(voice) - src0, end - dst0)
    if n > 0:
        ramp = min(n, int(0.03 * rate))
        v = voice[src0: src0 + n].copy()
        v[:ramp] *= np.linspace(0, 1, ramp, dtype=np.float32)
        out[dst0: dst0 + n] += v
    fade = min(len(out), int(TAIL_S * rate))
    out[len(out) - fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    return _limit(out)


def _span(x: np.ndarray, at: int, n: int) -> np.ndarray:
    """x 里从 at 开始 n 个点，越界的地方补 0。"""
    out = np.zeros(n, np.float32)
    a, b = max(0, at), min(len(x), at + n)
    if b > a:
        out[a - at: b - at] = x[a:b]
    return out


def make_real(data: bytes, acc: Path, s0: float, song: Path, guide: list[tuple[float, float]] = (),
              mon: Path | None = None) -> tuple[bytes, dict]:
    """录音（wav）+ 伴奏 + AI 翻唱 → 真人版 mp3 和 {start, end（唱的那段，歌里的秒）, seconds（真人版多长）, lat, agree, gain}。
    mon：跟唱时放的其实是这个（放大过的伴奏），对延迟、去回声拿它；混音用原样的伴奏（和 AI 版一样）。
    guide：开着「AI 领唱」的那几段（歌里的秒），外放的还有 AI 人声，去回声时一起减。"""
    rec, rate = read_wav(data)
    if len(rec) < rate * MIN_SING_S:
        raise KaraokeError(f"唱得太短了，至少唱 {MIN_SING_S:.0f} 秒")
    a = _decode(acc, rate)
    if not 0 <= s0 < len(a) / rate - 1:
        raise KaraokeError("从哪儿开始唱的不对")
    c = _decode(song, rate)
    n = min(len(a), len(c))
    a, ai = a[:n], c[:n] - a[:n]
    heard = a
    if mon is not None:
        m = _decode(mon, rate)[:n]
        heard = np.concatenate([m, np.zeros(n - len(m), np.float32)])
    lat, conf = latency(rec, heard, rate, s0)
    at = int(round((s0 - lat) * rate))
    t = (at + np.arange(len(rec))) / rate
    on = np.zeros(len(rec), np.float32)
    for g0, g1 in guide:
        on[(t >= g0) & (t < g1)] = 1
    played = _span(heard, at, len(rec)) + _span(ai, at, len(rec)) * on
    voice = dereverb_echo(rec, played)
    lv = _level(voice, rate)
    gain = float(np.clip(_level(_span(ai, at, len(rec)), rate) / lv, 0.05, 30)) if lv > 1e-5 else 1.0
    out = mix(voice * gain, rate, a, s0, lat)
    start = max(0.0, s0 - lat)
    return _encode_mp3(out, rate), {"start": round(start, 2), "end": round(min(n / rate, start + len(rec) / rate), 2),
                                    "seconds": round(len(out) / rate, 1), "lat": round(lat, 3), "agree": conf,
                                    "gain": round(gain, 2)}


def keep_recording(real_fid: str, data: bytes) -> None:
    d = store.DATA / "sing" / "rec"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{real_fid}.wav").write_bytes(data)
    _prune(d, "*.wav", KEEP_REC)
