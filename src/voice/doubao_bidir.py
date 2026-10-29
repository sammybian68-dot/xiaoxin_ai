"""豆包双向流式 TTS：一轮回答一个 session，文字分段往里塞，PCM 连续流出来，整段语调连贯。

协议照 carHer2 server/app/carher/voice/doubao_tts_bidir.py：
  StartConnection → StartSession → TaskRequest(文字)… → FinishSession → SessionFinished
  帧：4 字节头 + 4 字节事件号 + [4 字节长度 + session id] + 4 字节长度 + 负载。
这里每轮自己开一条连接（不像 carHer2 全进程共用一条），简单，不会串台；连接在等模型首字的时候就建好，不占首响。
"""

from __future__ import annotations

import asyncio
import json
import struct
import uuid
from collections.abc import Callable

from src.voice.doubao import APP_KEY, DoubaoError, api_key, env

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
SAMPLE_RATE = 24000

EV_START_CONNECTION, EV_FINISH_CONNECTION = 1, 2
EV_START_SESSION, EV_CANCEL_SESSION, EV_FINISH_SESSION, EV_TASK_REQUEST = 100, 101, 102, 200
EV_CONNECTION_STARTED, EV_CONNECTION_FAILED = 50, 51
EV_SESSION_STARTED, EV_SESSION_CANCELED, EV_SESSION_FINISHED, EV_SESSION_FAILED = 150, 151, 152, 153
EV_TTS_RESPONSE = 352
MSG_FULL_CLIENT, MSG_AUDIO_ONLY, MSG_ERROR = 0b0001, 0b1011, 0b1111


def _pack(event: int, session_id: str | None, meta: dict) -> bytes:
    out = bytearray([0x11, (MSG_FULL_CLIENT << 4) | 0b0100, 0x10, 0])
    out += struct.pack(">I", event)
    if session_id is not None:
        sid = session_id.encode()
        out += struct.pack(">I", len(sid)) + sid
    payload = json.dumps(meta, ensure_ascii=False).encode()
    return bytes(out + struct.pack(">I", len(payload)) + payload)


def _parse(data: bytes) -> tuple[int, int, object]:
    msg_type = data[1] >> 4
    if msg_type == MSG_ERROR:
        code = struct.unpack(">I", data[4:8])[0]
        return -1, msg_type, f"code={code} {data[12:].decode('utf-8', 'replace')[:300]}"
    event = struct.unpack(">I", data[4:8])[0]
    off = 8
    if off + 4 <= len(data):
        sid_len = struct.unpack(">I", data[off:off + 4])[0]
        off += 4 + sid_len
    payload: object = b""
    if off + 4 <= len(data):
        size = struct.unpack(">I", data[off:off + 4])[0]
        raw = data[off + 4:off + 4 + size]
        if msg_type == MSG_AUDIO_ONLY or event == EV_TTS_RESPONSE:
            payload = raw
        else:
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = raw
    return event, msg_type, payload


class BidirSession:
    """await open() → append(text)… → finish()；PCM 每到一块就调 on_pcm(bytes)，结束调 on_end(错误或 "")。"""

    def __init__(self, speaker: str, on_pcm: Callable[[bytes], None], on_end: Callable[[str], None], *,
                 resource_id: str | None = None, speech_rate: int = 0, context: str = ""):
        self.speaker = speaker
        self.resource_id = resource_id or env("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0")
        self.speech_rate = speech_rate
        self.context = context
        self.on_pcm, self.on_end = on_pcm, on_end
        self.session_id = uuid.uuid4().hex
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._started = asyncio.Event()
        self._error = ""
        self.done = False

    def _audio_params(self) -> dict:
        return {"format": "pcm", "sample_rate": SAMPLE_RATE, "speech_rate": self.speech_rate}

    async def open(self, timeout: float = 8) -> None:
        from websockets.asyncio.client import connect

        headers = {"X-Api-Key": api_key(), "X-Api-App-Key": APP_KEY, "X-Api-Resource-Id": self.resource_id,
                   "X-Api-Request-Id": str(uuid.uuid4())}
        try:
            self._ws = await asyncio.wait_for(
                connect(URL, additional_headers=headers, max_size=8 * 1024 * 1024, open_timeout=timeout), timeout)
            await self._ws.send(_pack(EV_START_CONNECTION, None, {}))
            while True:
                event, _, payload = _parse(await asyncio.wait_for(self._ws.recv(), timeout))
                if event == EV_CONNECTION_STARTED:
                    break
                if event in (EV_CONNECTION_FAILED, -1):
                    raise DoubaoError(f"TTS 连接失败：{payload}")
            additions = {"disable_markdown_filter": True, "enable_language_detector": True}
            if self.context:
                additions["context_texts"] = [self.context]
            await self._ws.send(_pack(EV_START_SESSION, self.session_id, {
                "event": EV_START_SESSION, "namespace": "BidirectionalTTS",
                "req_params": {"speaker": self.speaker, "audio_params": self._audio_params(),
                               "additions": json.dumps(additions, ensure_ascii=False)}}))
            self._reader = asyncio.create_task(self._read())
            await asyncio.wait_for(self._started.wait(), timeout)
        except DoubaoError:
            await self.close()
            raise
        except Exception as e:
            await self.close()
            raise DoubaoError(f"TTS 连不上：{e!r}") from e
        if self._error:
            await self.close()
            raise DoubaoError(self._error)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                event, msg_type, payload = _parse(bytes(raw))
                if event == EV_SESSION_STARTED:
                    self._started.set()
                elif event in (EV_SESSION_FAILED, -1):
                    self._error = f"TTS 失败：{payload}"
                    break
                elif event in (EV_SESSION_FINISHED, EV_SESSION_CANCELED):
                    break
                elif (event == EV_TTS_RESPONSE or msg_type == MSG_AUDIO_ONLY) and isinstance(payload, bytes) and payload:
                    self.on_pcm(payload)
        except Exception as e:  # 连接断了
            if not self.done:
                self._error = self._error or f"TTS 断开：{e!r}"
        finally:
            self._started.set()
            if not self.done:
                self.done = True
                self.on_end(self._error)

    async def append(self, text: str) -> None:
        if self.done or not text.strip():
            return
        await self._ws.send(_pack(EV_TASK_REQUEST, self.session_id, {
            "event": EV_TASK_REQUEST, "namespace": "BidirectionalTTS",
            "req_params": {"text": text, "speaker": self.speaker, "audio_params": self._audio_params()}}))

    async def finish(self) -> None:
        """文字发完了。豆包把剩下的念完会回 SessionFinished，那时 on_end("")。"""
        if not self.done:
            await self._ws.send(_pack(EV_FINISH_SESSION, self.session_id, {}))

    async def cancel(self) -> None:
        if self.done:
            return
        self.done = True
        try:
            await self._ws.send(_pack(EV_CANCEL_SESSION, self.session_id, {}))
        except Exception:
            pass
        await self.close()

    async def close(self) -> None:
        if self._reader and not self._reader.done():
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(_pack(EV_FINISH_CONNECTION, None, {}))
            except Exception:
                pass
            await self._ws.close()