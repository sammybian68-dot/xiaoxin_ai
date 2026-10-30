"""豆包 TTS（HTTP 流式）和 ASR（WebSocket 流式）。

key 读 DOUBAO_TTS_API_KEY（新版火山语音控制台的 UUID API Key，ASR 和 TTS 共用），
没有环境变量就读仓库根 .env。协议、App Key、resource id 都照 carHer2 抄过来，那边实车在跑。
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import struct
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import requests

from src.llm.client import ROOT, _dotenv

TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
ASR_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
APP_KEY = "aGjiRDfUWi"  # carHer2 doubao_tts.py 里写死的，新版控制台 API Key 配这个
ASR_RESOURCE = "volc.seedasr.sauc.duration"
DEFAULT_SPEAKER = "zh_female_vv_uranus_bigtts"


class DoubaoError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or _dotenv(ROOT / ".env").get(name) or default).strip()


def api_key() -> str:
    key = env("DOUBAO_TTS_API_KEY") or env("DOUBAO_API_KEY")
    if not key:
        raise DoubaoError("missing DOUBAO_TTS_API_KEY (env or .env at repo root)")
    return key


# ---------------------------------------------------------------- TTS

@dataclass
class Speech:
    pcm: bytes
    sample_rate: int
    first_audio: float  # 秒
    total: float
    logid: str = ""


def tts(text: str, *, speaker: str | None = None, resource_id: str | None = None,
        sample_rate: int = 24000, speech_rate: int = 0, context: str | None = None,
        timeout: float = 60) -> Speech:
    """一段文字转 s16le 单声道 PCM。speaker 可以是官方音色，也可以是声音复刻的 S_xxx。"""
    speaker = speaker or env("DOUBAO_TTS_SPEAKER", DEFAULT_SPEAKER)
    resource_id = resource_id or env("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0")
    additions: dict = {"disable_markdown_filter": True, "enable_language_detector": True}
    if context:
        additions["context_texts"] = [context]
    body = {
        "user": {"uid": "xiaoxin"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {"format": "pcm", "sample_rate": sample_rate, "speech_rate": speech_rate},
            "additions": json.dumps(additions, ensure_ascii=False),
        },
    }
    headers = {"X-Api-Key": api_key(), "X-Api-Resource-Id": resource_id, "X-Api-App-Key": APP_KEY,
               "X-Api-Request-Id": str(uuid.uuid4()), "Content-Type": "application/json"}
    t0 = time.monotonic()
    r = requests.post(TTS_URL, headers=headers, json=body, stream=True, timeout=timeout)
    if r.status_code != 200:
        raise DoubaoError(f"TTS HTTP {r.status_code}: {r.text[:500]}")
    decoder = json.JSONDecoder()
    buf, parts, first = "", [], None
    for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
        buf += chunk
        while True:
            buf = buf.lstrip()
            if not buf:
                break
            try:
                obj, end = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                break
            buf = buf[end:]
            if obj.get("data"):
                if first is None:
                    first = time.monotonic() - t0
                parts.append(base64.b64decode(obj["data"]))
            elif obj.get("code") not in (0, None, 20000000):
                raise DoubaoError(f"TTS error: {obj}")
    if not parts:
        raise DoubaoError("TTS returned no audio")
    return Speech(b"".join(parts), sample_rate, first, time.monotonic() - t0, r.headers.get("X-Tt-Logid", ""))


def write_wav(path: str | Path, pcm: bytes, sample_rate: int) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


def read_pcm16k(path: str | Path) -> bytes:
    """读 16 kHz 单声道 s16le。.wav 校验格式；其他后缀当裸 PCM。"""
    path = Path(path)
    if path.suffix.lower() != ".wav":
        return path.read_bytes()
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise DoubaoError(f"{path}: need 16 kHz mono s16, got "
                              f"{w.getframerate()} Hz x{w.getnchannels()} {w.getsampwidth() * 8}bit")
        return w.readframes(w.getnframes())


# ---------------------------------------------------------------- ASR
# 二进制帧：4 字节头 + [4 字节序号] + 4 字节长度 + gzip 负载。
_FULL, _AUDIO, _SERVER_FULL, _SERVER_ERR = 0b0001, 0b0010, 0b1001, 0b1111
_POS, _NEG_SEQ = 0b0001, 0b0011
_JSON, _RAW, _GZIP = 0b0001, 0b0000, 0b0001


def _frame(msg_type: int, flags: int, ser: int, payload: bytes, seq: int) -> bytes:
    head = bytes([0x11, (msg_type << 4) | flags, (ser << 4) | _GZIP, 0])
    body = gzip.compress(payload)
    return head + struct.pack(">i", seq) + struct.pack(">I", len(body)) + body


def _parse(data: bytes) -> tuple[int, int, object]:
    msg_type, flags = data[1] >> 4, data[1] & 0x0F
    ser, comp = data[2] >> 4, data[2] & 0x0F
    off = (data[0] & 0x0F) * 4
    if flags & 0b0001:
        off += 4
    if msg_type == _SERVER_ERR:
        off += 4  # 错误帧先给 4 字节错误码
    size = struct.unpack(">I", data[off:off + 4])[0]
    payload = data[off + 4:off + 4 + size]
    if comp == _GZIP and payload:
        payload = gzip.decompress(payload)
    if ser == _JSON and payload:
        return msg_type, flags, json.loads(payload)
    return msg_type, flags, payload.decode("utf-8", "replace") if payload else None


@dataclass
class Recognition:
    text: str
    first_text: float | None  # 从开始发音频算，秒
    final: float              # 收到最后结果，秒
    audio_seconds: float


def _hotword_corpus(hotwords: list[str]) -> dict:
    """热词照 carHer2：放 request.corpus.context（JSON 字符串）。v3 sauc 不认顶层 hot_words。"""
    return {"context": json.dumps({"hotwords": [{"word": w} for w in hotwords]}, ensure_ascii=False)}


def asr(pcm: bytes, *, sample_rate: int = 16000, chunk_ms: int = 200, realtime: bool = False,
        hotwords: list[str] | None = None, nonstream: bool = True, timeout: float = 15) -> Recognition:
    """一段 16 kHz s16le 单声道 PCM 送流式 ASR。realtime=True 按真实时长慢慢发，测的才是边说边出字的延迟。

    nonstream=True（默认，照 carHer2）：二遍识别，发完最后一包（负序号）后豆包用非流式模型整句再认一遍，
    最终结果在带「最后一包」标记的那帧里，要等到它才算完。"""
    from websockets.sync.client import connect

    req: dict = {
        "user": {"uid": "xiaoxin"},
        "audio": {"format": "pcm", "codec": "raw", "rate": sample_rate, "bits": 16, "channel": 1},
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "enable_ddc": True,
                    "enable_nonstream": nonstream, "show_utterances": True, "end_window_size": 800},
    }
    if hotwords:
        req["request"]["corpus"] = _hotword_corpus(hotwords)
    headers = {"X-Api-Key": api_key(), "X-Api-App-Key": APP_KEY, "X-Api-Resource-Id": ASR_RESOURCE,
               "X-Api-Request-Id": str(uuid.uuid4())}
    step = sample_rate * 2 * chunk_ms // 1000
    text, first = "", None

    def take(raw: bytes) -> bool:
        nonlocal text, first
        msg_type, flags, body = _parse(raw)
        if msg_type == _SERVER_ERR:
            raise DoubaoError(f"ASR error: {body}")
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(result, dict) and result.get("text"):
            text = result["text"]
            if first is None:
                first = time.monotonic() - t0
        return flags in (0b0010, _NEG_SEQ)  # 服务端最后一帧

    with connect(ASR_URL, additional_headers=headers, max_size=None, open_timeout=timeout) as ws:
        ws.send(_frame(_FULL, _POS, _JSON, json.dumps(req).encode(), 1))
        t0 = time.monotonic()
        seq, done = 2, False
        for start in range(0, max(len(pcm), 1), step):
            last = start + step >= len(pcm)
            ws.send(_frame(_AUDIO, _NEG_SEQ if last else _POS, _RAW, pcm[start:start + step], -seq if last else seq))
            seq += 1
            while True:
                try:
                    done = take(ws.recv(timeout=0.001)) or done
                except TimeoutError:
                    break
            if realtime and not last:
                time.sleep(chunk_ms / 1000)
        deadline = time.monotonic() + timeout
        while not done and time.monotonic() < deadline:
            try:
                done = take(ws.recv(timeout=0.5))
            except TimeoutError:
                continue
    return Recognition(text, first, time.monotonic() - t0, len(pcm) / (sample_rate * 2))


class StreamingAsr:
    """边说边传。按下就连上豆包，每来一块 PCM 就 feed()，松手调 finish() 只补最后一块，等最终结果。

        async with StreamingAsr() as asr:
            await asr.feed(chunk)            # s16le 16 kHz 单声道，多长都行，建议 80–200 ms
            text = asr.partials.get_nowait() # 中间结果，边说边出字
            final = await asr.finish()

    豆包要求最后一包带负序号，所以手里始终压着最新一块，finish() 时把它当最后一包发出去。
    nonstream=True 是两遍识别：边说边出字，说完再整句精修一次，更准但松手后多等一点。
    """

    def __init__(self, *, sample_rate: int = 16000, hotwords: list[str] | None = None, nonstream: bool = True,
                 timeout: float = 10):
        self.rate, self.hotwords, self.nonstream, self.timeout = sample_rate, hotwords, nonstream, timeout
        self.text, self.seconds, self.error = "", 0.0, ""
        self.partials: "asyncio.Queue[str]" = None  # type: ignore[assignment]
        self._ws = self._reader = self._held = None
        self._seq = 2

    async def __aenter__(self) -> "StreamingAsr":
        import asyncio

        from websockets.asyncio.client import connect

        self.partials = asyncio.Queue()
        self._done = asyncio.Event()
        req: dict = {
            "user": {"uid": "xiaoxin"},
            "audio": {"format": "pcm", "codec": "raw", "rate": self.rate, "bits": 16, "channel": 1},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "enable_ddc": True,
                        "show_utterances": True, "enable_nonstream": self.nonstream, "end_window_size": 800},
        }
        if self.hotwords:
            req["request"]["corpus"] = _hotword_corpus(self.hotwords)
        headers = {"X-Api-Key": api_key(), "X-Api-App-Key": APP_KEY, "X-Api-Resource-Id": ASR_RESOURCE,
                   "X-Api-Request-Id": str(uuid.uuid4())}
        try:
            self._ws = await connect(ASR_URL, additional_headers=headers, max_size=None, open_timeout=self.timeout)
            await self._ws.send(_frame(_FULL, _POS, _JSON, json.dumps(req).encode(), 1))
        except Exception as e:
            raise DoubaoError(f"ASR 连不上：{e!r}") from e
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg_type, flags, body = _parse(raw)
                if msg_type == _SERVER_ERR:
                    self.error = f"ASR error: {body}"
                    break
                result = body.get("result") if isinstance(body, dict) else None
                text = result.get("text") if isinstance(result, dict) else None
                if text and text != self.text:
                    self.text = text
                    self.partials.put_nowait(text)
                if flags in (0b0010, _NEG_SEQ):
                    break
        except Exception as e:  # 连接断了
            self.error = self.error or f"ASR 断开：{e!r}"
        finally:
            self._done.set()

    async def _send(self, pcm: bytes, last: bool) -> None:
        seq = -self._seq if last else self._seq
        self._seq += 1
        await self._ws.send(_frame(_AUDIO, _NEG_SEQ if last else _POS, _RAW, pcm, seq))

    async def feed(self, pcm: bytes) -> None:
        if self._done.is_set():
            raise DoubaoError(self.error or "ASR 已经结束")
        self.seconds += len(pcm) / (self.rate * 2)
        if self._held is not None:
            await self._send(self._held, last=False)
        self._held = pcm

    async def finish(self) -> str:
        """发最后一包（没收到过音频就补 100 ms 静音），等豆包给最终结果。"""
        import asyncio

        if not self._done.is_set():
            await self._send(self._held or b"\0" * (self.rate // 5), last=True)
            self._held = None
            try:
                await asyncio.wait_for(self._done.wait(), self.timeout)
            except asyncio.TimeoutError:
                raise DoubaoError("ASR 等最终结果超时") from None
        if self.error and not self.text:
            raise DoubaoError(self.error)
        return self.text
