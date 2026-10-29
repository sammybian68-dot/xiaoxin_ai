"""CosyVoice3 zero-shot TTS（gpu2 上的 Triton + TRT-LLM），给一段参考音频就能用那个人的声音说话。

协议照 carHer2 server/app/carher/voice/triton_tts.py：gRPC 流式，输入 reference_wav（16 kHz float32）、
reference_wav_len、reference_text、target_text，逐块返回 24 kHz float32 waveform。
Triton 只在 gpu2 本机：gRPC 127.0.0.1:18001，HTTP 健康检查 127.0.0.1:18100（18000 被到 H100.2 的 27B 隧道占了）。

服务端按 reference_text 缓存参考音频的特征，同一个人的参考文字不变，第二次起就不再重新提取。
所以 reference_text 必须和这段参考音频一一对应：换了参考音频就得换文字，不然会用到旧的缓存。
两段录音认出来的参考文字一模一样时，后一段会用到前一段的声音，这是 Triton 那边的缓存键决定的。

2026-10-29 在 gpu2 实测（参考段 8 秒）：热的时候首块 0.4 秒，34 个字合成 11.6 秒音频用 1.9 秒。

首响慢不是算得慢（每块 0.15–0.2 秒就出来了），是两种静音（2026-10-30 实测）：
  - 前导静音：先吐 0.5–2 秒静音才开口。原样放出去用户就在静音里等着。现在开头只留 0.15 秒。
  - 整段无声：参考是豆包念的那段时 25–40% 一个音都不出（录的真人声音和官方参考 0/12），
    原来要等到 30 秒上限。现在 1.5 秒没声就取消重来一次，再不行跳过这句。
  根子和下面的跑飞一样，在 Triton 版的采样。

偶尔停不下来：大约每 10–16 次有一次，话念完以后一直出静音，直到 30 秒。原因在 Triton 版的 model.py：
LLM 固定 max_tokens=750（25 token/s 正好 30 秒），用普通 top-k/top-p 采样。官方 cosyvoice/llm/llm.py
是按字数算 max_len（字数 × 20），用 RAS 采样跳出重复，Triton 版这两样都没有，EOS 偶尔出不来。
那个服务是 carHer2 的，这里不改它，只在客户端兜底：说过话以后连着静音 1 秒就停、取消请求，
静音不往外发；另外按字数设时长上限。
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import uuid
import wave
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

GRPC_URL = os.environ.get("COSYVOICE_GRPC_URL", "127.0.0.1:18001")
HTTP_URL = os.environ.get("COSYVOICE_HTTP_URL", "http://127.0.0.1:18100")
MODEL = "cosyvoice3"
SAMPLE_RATE = 24000


class CosyVoiceError(RuntimeError):
    pass


def ready(timeout: float = 1.0) -> bool:
    from urllib.request import urlopen

    try:
        with urlopen(f"{HTTP_URL}/v2/health/ready", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def resample_s16(raw: bytes, src_rate: int, dst_rate: int) -> bytes:
    """s16le 单声道线性插值重采样。官方素材是 22.05 / 24 kHz，参考音要 16 kHz。"""
    import numpy as np

    x = np.frombuffer(raw[:len(raw) // 2 * 2], np.int16).astype(np.float32)
    if src_rate == dst_rate or x.size == 0:
        return x.astype(np.int16).tobytes()
    pos = np.arange(0, x.size, src_rate / dst_rate)
    return np.interp(pos, np.arange(x.size), x).round().clip(-32768, 32767).astype(np.int16).tobytes()


def load_reference(path: str | Path):
    """读 16 kHz 单声道 16 bit wav，转成 float32 [-1, 1]。"""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise CosyVoiceError(f"参考音频要 16 kHz 单声道 16 bit：{path}")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


SILENCE_LEVEL = 300   # s16 绝对值低于这个算静音（正常说话段的逐秒 RMS 在 1500–3000）
SILENCE_STOP_S = 1.0  # 说过话以后连着静音这么久就停
LEAD_KEEP_S = 0.15    # 开头的静音只留这么多，多的直接丢
# 一直没出声就放弃：实测 Triton 版偶尔先吐 1–2 秒静音才开口，或者整段都是静音。
# 2026-10-30 测：出声的那些第一个有声样本在 0.3–0.8 秒到，所以 1.5 秒还没声就不等了，调用方换一次重来。
SILENT_GIVEUP_S = 1.5


def max_seconds(text: str) -> float:
    """一段话最多念多久。正常大约每字 0.35 秒，给到 0.6 秒再加 3 秒余量。"""
    return 3 + 0.6 * len(text)


def trim_runaway(chunks: Iterable, max_samples: int) -> Iterator[bytes]:
    """把 Triton 吐出的 float32 波形块变成 s16le PCM，顺带处理跑飞：

    - 开头的静音只留 LEAD_KEEP_S，多的丢掉：Triton 版常先吐 0.5–2 秒静音才开口，原样放出去用户就干等着
    - 中间的静音先压着，后面又出声了再一起放出去
    - 说过话以后连着静音 SILENCE_STOP_S 秒就当念完了，后面的不要
    - 收到 SILENT_GIVEUP_S 秒还一个有声样本都没有，就抛 Silent（墙钟时间，调用方重来一次通常就好）
    - 总长到 max_samples 就截断
    调用方停止迭代时要关掉 chunks，让上游取消请求。
    """
    import numpy as np

    sent = 0
    held, spoken = bytearray(), False
    keep = int(LEAD_KEEP_S * SAMPLE_RATE) * 2
    t0 = time.monotonic()
    for audio in chunks:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)[:max(0, max_samples - sent)]
        sent += audio.size
        pcm = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        loud = np.abs(pcm) > SILENCE_LEVEL
        if loud.any():
            last = int(np.nonzero(loud)[0][-1]) + 1
            if not spoken:  # 第一次出声：前面攒的静音只留最后一点，免得切掉字头
                first = int(np.nonzero(loud)[0][0])
                head = (bytes(held) + pcm[:first].tobytes())[-keep:] if keep else b""
                out = head + pcm[first:last].tobytes()
            else:
                out = bytes(held) + pcm[:last].tobytes()
            held = bytearray(pcm[last:].tobytes())
            spoken = True
            yield out
        elif pcm.size:
            held += pcm.tobytes()
            if spoken and len(held) >= SILENCE_STOP_S * SAMPLE_RATE * 2:
                return
            if not spoken:
                held = held[-keep:] if keep else bytearray()  # 只是省内存，裁剪由上面第一次出声时做
                if time.monotonic() - t0 >= SILENT_GIVEUP_S:
                    raise Silent()
        if sent >= max_samples:
            return


class Silent(CosyVoiceError):
    """等了 SILENT_GIVEUP_S 还没出声。"""


def stream(text: str, reference, reference_text: str, *, timeout: float = 60,
           stop: threading.Event | None = None, retries: int = 1) -> Iterator[bytes]:
    """逐块 yield 24 kHz s16le PCM。reference 是 load_reference() 的结果。

    stop 置位就不再往外给；超过 max_seconds(text) 或念完后一直静音就截断。这几种情况都会让 Triton 取消这个请求。
    1.5 秒还没出声就取消、重来，最多 retries 次（采样是随机的，重来一次多半就正常）；还不行抛 Silent。
    """
    for attempt in range(retries + 1):
        chunks = _triton_chunks(text, reference, reference_text, timeout=timeout, stop=stop)
        got = False
        try:
            for pcm in trim_runaway(chunks, int(max_seconds(text) * SAMPLE_RATE)):
                got = True
                yield pcm
            if got or (stop and stop.is_set()):
                return
            # 很快就结束了、可一个有声样本都没有：和等不到声音一样，重来
        except Silent:
            pass
        finally:
            chunks.close()
        if attempt == retries:
            raise Silent()


def _triton_chunks(text: str, reference, reference_text: str, *, timeout: float,
                   stop: threading.Event | None) -> Iterator:
    """gRPC 流式请求，逐块 yield float32 波形。没收到 final 就被关掉时取消请求。"""
    import numpy as np
    import tritonclient.grpc as grpcclient
    from tritonclient.utils import InferenceServerException, np_to_triton_dtype

    samples = reference.astype(np.float32).reshape(1, -1)
    lengths = np.array([[samples.shape[1]]], dtype=np.int32)
    inputs = [
        grpcclient.InferInput("reference_wav", samples.shape, np_to_triton_dtype(samples.dtype)),
        grpcclient.InferInput("reference_wav_len", lengths.shape, np_to_triton_dtype(lengths.dtype)),
        grpcclient.InferInput("reference_text", [1, 1], "BYTES"),
        grpcclient.InferInput("target_text", [1, 1], "BYTES"),
    ]
    inputs[0].set_data_from_numpy(samples)
    inputs[1].set_data_from_numpy(lengths)
    inputs[2].set_data_from_numpy(np.array([reference_text], dtype=object).reshape(1, 1))
    inputs[3].set_data_from_numpy(np.array([text], dtype=object).reshape(1, 1))

    finished = False
    q: queue.Queue = queue.Queue()
    client = grpcclient.InferenceServerClient(url=GRPC_URL, verbose=False)
    client.start_stream(callback=lambda result, error: q.put(error or result))
    try:
        client.async_stream_infer(MODEL, inputs, request_id=uuid.uuid4().hex,
                                  outputs=[grpcclient.InferRequestedOutput("waveform")],
                                  enable_empty_final_response=True)
        while not (stop and stop.is_set()):
            try:
                item = q.get(timeout=timeout)
            except queue.Empty:
                raise CosyVoiceError("CosyVoice 超时") from None
            if isinstance(item, InferenceServerException):
                raise CosyVoiceError(str(item))
            if item.get_response().parameters["triton_final_response"].bool_param:
                finished = True
                return
            yield item.as_numpy("waveform")
    finally:
        client.stop_stream(cancel_requests=not finished)
        client.close()


def tts(text: str, reference, reference_text: str, *, retry: bool = True) -> tuple[bytes, float, float]:
    """整段合成。返回 (PCM, 首块秒数, 总秒数)。"""
    t0 = time.monotonic()
    first = None
    parts = []
    try:
        for chunk in stream(text, reference, reference_text, retries=1 if retry else 0):
            if first is None:
                first = time.monotonic() - t0
            parts.append(chunk)
    except Silent:
        raise CosyVoiceError("CosyVoice 没出声") from None
    if not parts:
        raise CosyVoiceError("CosyVoice 没出声")
    return b"".join(parts), first, time.monotonic() - t0


class CosyVoiceSession:
    """和 doubao_bidir.BidirSession 一样的用法，给一轮回答的流式朗读用：
    await open() → append(text)… → finish()；PCM 每到一块就调 on_pcm(bytes)，念完调 on_end(错误或 "")。

    CosyVoice 没有一条连接念多句的接口，这里按句排队，一句一个 gRPC 流，前一句念完再合成下一句。
    合成比实时快 5 倍左右，前一句还在播的时候下一句就出来了。
    """

    def __init__(self, reference, reference_text: str, on_pcm: Callable[[bytes], None],
                 on_end: Callable[[str], None]):
        self.reference, self.reference_text = reference, reference_text
        self.on_pcm, self.on_end = on_pcm, on_end
        self.done = False
        self._stop = threading.Event()
        self._q: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def open(self) -> None:
        if not await asyncio.to_thread(ready):
            raise CosyVoiceError("CosyVoice 没起来（gpu2 Triton 18100）")
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    async def append(self, text: str) -> None:
        if not self.done and text.strip():
            self._q.put_nowait(text)

    async def finish(self) -> None:
        if not self.done:
            self._q.put_nowait(None)

    async def cancel(self) -> None:
        if self.done:
            return
        self.done = True  # 先置位，_run 收尾时就不再回调 on_end，和 BidirSession 一致
        self._stop.set()
        if self._task:
            self._task.cancel()

    def _synth(self, text: str) -> None:
        try:
            for chunk in stream(text, self.reference, self.reference_text, stop=self._stop):
                self._loop.call_soon_threadsafe(self.on_pcm, chunk)
        except Silent:
            pass  # 重来过一次还是没声：这句跳过，接着念下一句，不让整轮朗读失败

    async def _run(self) -> None:
        err = ""
        try:
            while not self._stop.is_set():
                text = await self._q.get()
                if text is None:
                    break
                await asyncio.to_thread(self._synth, text)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # CosyVoiceError 或 gRPC 断开
            err = f"CosyVoice 失败：{e}"[:200]
        finally:
            if not self.done:
                self.done = True
                self.on_end(err)
