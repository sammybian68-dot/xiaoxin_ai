"""SeACo-Paraformer 中文识别：gpu2 本机 carHer2 的 ASR worker（127.0.0.1:28182，带热词和标点）。

worker 平时由 carHer2 8585 的 asr 页面按需拉起，没有看门狗。小新用之前先探一下，没起来就用同一条命令自己拉起来：
同一个 venv、同一个端口，谁先起都一样，8585 那边探到活着就直接用。
不支持流式：按住说话时 PCM 先攒着，松手整段送一次。2026-10-29 实测 3.5 秒的话约 0.13 秒出结果。
"""

from __future__ import annotations

import io
import os
import subprocess
import threading
import time
import wave
from pathlib import Path

from src.voice.doubao import Recognition

URL = os.environ.get("PARAFORMER_URL", "http://127.0.0.1:28182")
BACKEND = "seaco-paraformer"
CARHER_SERVER = Path(os.environ.get("CARHER_SERVER", "/Data/cmbian/codes/carHer2/server"))
HOTWORDS = "小新"
LOG = Path(__file__).resolve().parents[2] / "logs" / "asr-worker.log"


class ParaformerError(RuntimeError):
    pass


def status(timeout: float = 1.0) -> dict | None:
    """worker 没起来返回 None；起来了返回 seaco 那一项（有 loaded 字段），worker 里没有 seaco 返回 {}。"""
    import requests

    try:
        r = requests.get(URL + "/health", timeout=timeout)
        health = r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None
    if not health:
        return None
    return next((m for m in health.get("models") or [] if m.get("key") == BACKEND), {})


def ready() -> bool:
    return bool((status() or {}).get("loaded"))


def _spawn() -> None:
    py = CARHER_SERVER / ".venv-asr" / "bin" / "python"
    if not py.exists():
        raise ParaformerError(f"没有 ASR venv：{py}")
    env = os.environ.copy()
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("MODELSCOPE_CACHE", "/Data/cmbian/cache/modelscope")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as log:
        subprocess.Popen([str(py), "-m", "uvicorn", "app.asr.worker:app", "--host", "127.0.0.1",
                          "--port", URL.rsplit(":", 1)[-1]],
                         cwd=CARHER_SERVER, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


_start_lock = threading.Lock()


def ensure(load_timeout: float = 120) -> None:
    """worker 没起来就拉起来，seaco 没加载就让它加载。热的时候只是一次 /health；冷启动要十几秒到一分钟。"""
    import requests

    if ready():
        return
    with _start_lock:
        m = status()
        if m is None:
            _spawn()
            deadline = time.monotonic() + 60
            while (m := status()) is None:
                if time.monotonic() > deadline:
                    raise ParaformerError(f"ASR worker 60 秒没起来，见 {LOG}")
                time.sleep(0.5)
        if m == {}:
            raise ParaformerError(f"ASR worker 里没有 {BACKEND}")
        if not m.get("loaded"):
            try:
                r = requests.post(f"{URL}/models/{BACKEND}/load", timeout=load_timeout)
            except requests.RequestException as e:
                raise ParaformerError(f"加载 Paraformer 失败：{e}") from e
            if not r.ok:
                raise ParaformerError(f"加载 Paraformer 失败：{r.status_code} {r.text[:200]}")


def _wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(pcm: bytes, *, sample_rate: int = 16000, hotword: str = HOTWORDS, timeout: float = 30) -> Recognition:
    """整段识别 s16le 单声道 PCM。Recognition.final 是从发请求到拿到结果的秒数。"""
    import requests

    ensure()
    t0 = time.monotonic()
    try:
        r = requests.post(URL + "/transcribe", timeout=timeout,
                          files={"audio": ("audio.wav", _wav(pcm, sample_rate), "audio/wav")},
                          data={"backend": BACKEND, "language": "zh", "hotword": hotword})
    except requests.RequestException as e:
        raise ParaformerError(f"Paraformer 连不上：{e}") from e
    if not r.ok:
        raise ParaformerError(f"Paraformer {r.status_code}：{r.text[:200]}")
    text = str(r.json().get("text") or "").strip()
    return Recognition(text, None, time.monotonic() - t0, len(pcm) / (sample_rate * 2))


class StreamingParaformer:
    """和 doubao.StreamingAsr 一样的用法（async with / feed / partials / finish），给按住说话用。

    没有中间结果，partials 一直是空的。按下时就在后台确认 worker 起来了，冷启动的时间和说话的时间重叠。
    """

    def __init__(self, *, sample_rate: int = 16000, hotword: str = HOTWORDS):
        self.rate, self.hotword = sample_rate, hotword
        self.text, self.seconds, self.error = "", 0.0, ""
        self.partials = None
        self._pcm = bytearray()
        self._ready = None

    async def __aenter__(self) -> "StreamingParaformer":
        import asyncio

        self.partials = asyncio.Queue()
        self._ready = asyncio.create_task(asyncio.to_thread(ensure))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ready and self._ready.done() and not self._ready.cancelled():
            self._ready.exception()  # 没人等过的异常在这里取走，免得 asyncio 打警告

    async def feed(self, pcm: bytes) -> None:
        self.seconds += len(pcm) / (self.rate * 2)
        self._pcm += pcm

    async def finish(self) -> str:
        import asyncio

        await self._ready
        if self._pcm:
            rec = await asyncio.to_thread(transcribe, bytes(self._pcm), sample_rate=self.rate, hotword=self.hotword)
            self.text = rec.text
        return self.text
