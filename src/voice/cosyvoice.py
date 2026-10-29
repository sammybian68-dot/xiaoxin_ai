"""CosyVoice3 zero-shot TTS（gpu2 上的 Triton + TRT-LLM），给一段参考音频就能用那个人的声音说话。

协议照 carHer2 server/app/carher/voice/triton_tts.py：gRPC 流式，输入 reference_wav（16 kHz float32）、
reference_wav_len、reference_text、target_text，逐块返回 24 kHz float32 waveform。
Triton 只在 gpu2 本机：gRPC 127.0.0.1:18001，HTTP 健康检查 127.0.0.1:18100（18000 被到 H100.2 的 27B 隧道占了）。

服务端按 reference_text 缓存参考音频的特征，同一个人的参考文字不变，第二次起就不再重新提取。
所以 reference_text 必须和这段参考音频一一对应：换了参考音频就得换文字，不然会用到旧的缓存。
"""

from __future__ import annotations

import os
import queue
import time
import uuid
import wave
from collections.abc import Iterator
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


def load_reference(path: str | Path):
    """读 16 kHz 单声道 16 bit wav，转成 float32 [-1, 1]。"""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise CosyVoiceError(f"参考音频要 16 kHz 单声道 16 bit：{path}")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def stream(text: str, reference, reference_text: str, *, timeout: float = 60) -> Iterator[bytes]:
    """逐块 yield 24 kHz s16le PCM。reference 是 load_reference() 的结果。"""
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

    q: queue.Queue = queue.Queue()
    client = grpcclient.InferenceServerClient(url=GRPC_URL, verbose=False)
    client.start_stream(callback=lambda result, error: q.put(error or result))
    try:
        client.async_stream_infer(MODEL, inputs, request_id=uuid.uuid4().hex,
                                  outputs=[grpcclient.InferRequestedOutput("waveform")],
                                  enable_empty_final_response=True)
        while True:
            try:
                item = q.get(timeout=timeout)
            except queue.Empty:
                raise CosyVoiceError("CosyVoice 超时") from None
            if isinstance(item, InferenceServerException):
                raise CosyVoiceError(str(item))
            if item.get_response().parameters["triton_final_response"].bool_param:
                return
            audio = item.as_numpy("waveform").reshape(-1)
            if audio.size:
                yield (audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
    finally:
        client.stop_stream()
        client.close()


def tts(text: str, reference, reference_text: str) -> tuple[bytes, float, float]:
    """整段合成。返回 (PCM, 首块秒数, 总秒数)。"""
    t0 = time.monotonic()
    first = None
    parts = []
    for chunk in stream(text, reference, reference_text):
        if first is None:
            first = time.monotonic() - t0
        parts.append(chunk)
    if not parts:
        raise CosyVoiceError("CosyVoice 没出声")
    return b"".join(parts), first, time.monotonic() - t0
