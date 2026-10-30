"""一轮回答的流式朗读：模型边吐字 → 攒够一句就送 TTS → PCM 边合成边下发，音频最多领先播放进度 LEAD 秒。

思路照 carHer2 server/app/carher/no_wake/cloud_tts_hold.py：
- 切句：首句越早越好（第 4 个字以后见句号 / 问号就送，见逗号要 44 字以后），之后的句子攒长一点（88 / 120 字），
  首句等 350 ms 还没标点、够 4 个字也送出去，免得模型慢吞吞时一直不出声。
- 文字和 PCM 分两路：文字照常走 SSE 的 delta，PCM 走 SSE 的 audio 事件（base64），前端各管各的。
- 节流：按 24 kHz s16 算已下发音频的时长，wall-clock 从第一块下发起算，已下发时长 ≤ 已过时间 + LEAD 才发下一块。
  豆包合成比实时快得多，不节流的话一句长回答几百 KB 一下全涌到端侧；节流后端侧手里最多压着 LEAD 秒。
  用户打断（取消）时，没下发的直接丢，省流量也省端侧丢弃的活。
"""

from __future__ import annotations

import asyncio
import base64
import time

from src.voice.doubao_bidir import SAMPLE_RATE, BidirSession  # CosyVoice 也是 24 kHz

LEAD_S = 2.0
BYTES_PER_S = SAMPLE_RATE * 2
FRAME_BYTES = BYTES_PER_S // 10  # 下发粒度 100 ms，端侧排队播

_STRONG = set("。！？!?;；\n")
_SOFT = set("，,、：:")
_FIRST_SENTENCE_MIN, _FIRST_COMMA_MIN = 4, 44
_NEXT_SENTENCE_MIN, _NEXT_COMMA_MIN = 88, 120
_FIRST_HARD, _NEXT_HARD = 90, 180
_FIRST_TIMED_S, _FIRST_TIMED_MIN_CHARS = 0.35, 4


def flush_boundary(text: str, first: bool) -> int:
    """text 里该在哪里切一刀送 TTS，返回切点（不含）；还不该切返回 -1。"""
    if first:
        for i, ch in enumerate(text):
            if ch in _STRONG and i >= _FIRST_SENTENCE_MIN:
                return i + 1
            if ch in _SOFT and i >= _FIRST_COMMA_MIN:
                return i + 1
        return _FIRST_HARD if len(text) >= _FIRST_HARD else -1
    # 后面的句子：在已攒的里面找最靠后的够长的句末，一次送一大段，语调更连贯、请求更少
    for i in range(len(text) - 1, -1, -1):
        if text[i] in _STRONG and i >= _NEXT_SENTENCE_MIN:
            return i + 1
        if text[i] in _SOFT and i >= _NEXT_COMMA_MIN:
            return i + 1
    return len(text) if len(text) >= _NEXT_HARD else -1


def speakable(text: str) -> str:
    """去掉 markdown 记号和代码块，免得念出「星号星号」。"""
    import re

    text = re.sub(r"```.*?(```|$)", "（代码略）", text, flags=re.S)
    text = re.sub(r"[*#`>_~|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return text


class TurnSpeech:
    """feed(delta) 喂模型正文；take_audio() 取能下发的 PCM 块。

    speaker 是豆包音色 ID；给了 cosyvoice=(参考音频, 参考文字) 就改走 gpu2 本机的 CosyVoice。
    """

    def __init__(self, speaker: str = "", *, cosyvoice: tuple | None = None, lead_s: float = LEAD_S):
        self.lead_s = lead_s
        if cosyvoice:
            from src.voice.cosyvoice import CosyVoiceSession

            self.session = CosyVoiceSession(*cosyvoice, self._on_pcm, self._on_end)
        else:
            self.session = BidirSession(speaker, self._on_pcm, self._on_end)
        self._buf = ""
        self._buf_since = 0.0
        self._first_sent = False
        self._pcm = bytearray()
        self._sent = 0
        self._clock: float | None = None
        self._ended = asyncio.Event()
        self._error = ""
        self._kick = asyncio.Event()
        self._opening: asyncio.Task | None = None
        self._open_error = ""
        self.first_text_at: float | None = None
        self.opened_at: float | None = None     # TTS 连接建好（相对 t0）
        self.first_pcm_at: float | None = None  # TTS 吐出第一块 PCM（相对 t0），和 first_audio_at（下发）分开看
        self.first_audio_at: float | None = None
        self.t0 = time.monotonic()

    # ---- TTS 回调
    def _on_pcm(self, data: bytes) -> None:
        if self.first_pcm_at is None and data:
            self.first_pcm_at = time.monotonic() - self.t0
        self._pcm += data
        self._kick.set()

    def _on_end(self, err: str) -> None:
        self._error = err
        self._ended.set()
        self._kick.set()

    # ---- 文字进
    def start(self) -> None:
        """模型还在想的时候就把连接建好。"""
        async def _open():
            try:
                await self.session.open()
                self.opened_at = time.monotonic() - self.t0
            except Exception as e:  # DoubaoError / CosyVoiceError 或网络错误：这轮就不出声，文字照常
                self._open_error = str(e)[:200]
                self._ended.set()
                self._kick.set()
        self._opening = asyncio.create_task(_open())

    async def _submit(self, text: str) -> None:
        text = speakable(text).strip()
        if not text:
            return
        await self._opening
        if self._open_error or self.session.done:
            return
        if self.first_text_at is None:
            self.first_text_at = time.monotonic() - self.t0
        await self.session.append(text)

    async def feed(self, delta: str) -> None:
        if not self._buf:
            self._buf_since = time.monotonic()
        self._buf += delta
        await self._drain(force=False)

    async def tick(self) -> None:
        """没新字时也定期调：首句等 350 ms 还没标点就先送。"""
        if (not self._first_sent and len(self._buf.strip()) >= _FIRST_TIMED_MIN_CHARS
                and time.monotonic() - self._buf_since >= _FIRST_TIMED_S):
            await self._drain(force=True)

    async def _drain(self, *, force: bool) -> None:
        while self._buf:
            cut = flush_boundary(self._buf, first=not self._first_sent)
            if cut < 0:
                if not force:
                    return
                cut = len(self._buf)
            piece, self._buf = self._buf[:cut], self._buf[cut:]
            self._buf_since = time.monotonic()
            self._first_sent = True
            await self._submit(piece)
            force = False

    async def finish_text(self) -> None:
        await self._drain(force=True)
        await self._opening
        if not self._open_error:
            await self.session.finish()

    async def cancel(self) -> None:
        if self._opening:
            await self._opening
        await self.session.cancel()
        self._ended.set()
        self._kick.set()

    # ---- PCM 出（节流）
    def _due(self) -> int:
        """现在最多还能再发多少字节。"""
        if self._clock is None:
            return len(self._pcm) - self._sent
        played = (time.monotonic() - self._clock) * BYTES_PER_S
        allowed = int(played + self.lead_s * BYTES_PER_S) - self._sent
        return max(0, min(allowed, len(self._pcm) - self._sent))

    def take_audio(self) -> list[str]:
        """非阻塞：把现在允许发的 PCM 按 100 ms 切块，base64 返回（SSE 用）。"""
        return [base64.b64encode(b).decode() for b in self.take_audio_bytes()]

    def take_audio_bytes(self) -> list[bytes]:
        """同 take_audio，返回原始字节（WebSocket 二进制帧用）。"""
        out = []
        while True:
            n = self._due()
            if self._clock is None:
                n = min(n, int(self.lead_s * BYTES_PER_S))  # 第一批先给 LEAD 秒，起播后再按时钟放
            n = min(n, FRAME_BYTES)
            last_tail = self._ended.is_set() and self._sent + n == len(self._pcm)
            if n <= 0 or (n < FRAME_BYTES and not last_tail):
                break
            n -= n % 2
            if n <= 0:
                break
            if self._clock is None:
                self._clock = time.monotonic()
                self.first_audio_at = self._clock - self.t0
            out.append(bytes(self._pcm[self._sent:self._sent + n]))
            self._sent += n
        return out

    @property
    def audio_done(self) -> bool:
        return self._ended.is_set() and self._sent >= len(self._pcm)

    def next_wait(self) -> float:
        """距离下一块能发还要等多久（秒），给调用方 sleep 用。"""
        if self._clock is None or self._sent >= len(self._pcm):
            return 0.05
        need = (self._sent + FRAME_BYTES) - (self.lead_s * BYTES_PER_S)
        return max(0.02, min(0.2, self._clock + need / BYTES_PER_S - time.monotonic()))

    def info(self) -> dict:
        return {"sample_rate": SAMPLE_RATE, "seconds": round(self._sent / BYTES_PER_S, 2),
                "first_text_s": self.first_text_at and round(self.first_text_at, 3),
                "first_audio_s": self.first_audio_at and round(self.first_audio_at, 3),
                "opened_s": self.opened_at and round(self.opened_at, 3),
                "first_pcm_s": self.first_pcm_at and round(self.first_pcm_at, 3),
                "error": self._open_error or self._error}
