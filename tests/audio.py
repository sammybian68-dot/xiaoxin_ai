"""测试用的合成音频：不读真实录音，形状可控。"""

from __future__ import annotations

import io
import math
import wave


def tone(seconds: float, rate: int = 16000, amp: int = 6000, freq: float = 220.0) -> bytes:
    """s16le 单声道正弦波，当「在说话」用。"""
    n = int(seconds * rate)
    return b"".join(int(amp * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, "little", signed=True)
                    for i in range(n))


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\0\0" * int(seconds * rate)


def wav_bytes(pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def speech_with_pauses(sentences: list[float], pause: float = 0.5, rate: int = 16000) -> bytes:
    """几句话（各自的秒数），句间停顿 pause 秒，前后各留 0.3 秒静音。"""
    out = [silence(0.3, rate)]
    for i, s in enumerate(sentences):
        out.append(tone(s, rate))
        out.append(silence(pause if i < len(sentences) - 1 else 0.3, rate))
    return b"".join(out)
