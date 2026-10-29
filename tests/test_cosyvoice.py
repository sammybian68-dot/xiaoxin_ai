"""CosyVoice：跑飞截停（trim_runaway）、stream 关上游、tts 重试、CosyVoiceSession 的生命周期。

跑飞是 2026-10-29 在 gpu2 上实测到的：话念完后一直出静音到 30 秒（Triton 版固定 max_tokens=750）。
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from src.voice import cosyvoice
from src.voice.cosyvoice import SAMPLE_RATE, trim_runaway


def speech(seconds: float, amp: float = 0.2) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    return (amp * np.sin(2 * np.pi * 220 * np.arange(n) / SAMPLE_RATE)).astype(np.float32)


def quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), np.float32)


def blocks(*parts: np.ndarray, size: float = 0.2):
    """把波形按 size 秒切成块，模仿 Triton 一块一块吐。"""
    audio = np.concatenate(parts)
    step = int(size * SAMPLE_RATE)
    return [audio[i:i + step] for i in range(0, audio.size, step)]


def seconds(out: list[bytes]) -> float:
    return sum(len(b) for b in out) / 2 / SAMPLE_RATE


BIG = 10 ** 9


# ---------------------------------------------------------------- trim_runaway

def test_normal_speech_passes_through_without_trailing_silence():
    out = list(trim_runaway(blocks(speech(2.0), quiet(0.5)), BIG))
    # 话全在，尾巴上的静音不发（没有后续声音，就一直压着）
    assert seconds(out) == pytest.approx(2.0, abs=0.01)


def test_runaway_silence_after_speech_is_cut_after_one_second():
    consumed = []

    def source():
        for b in blocks(speech(3.0), quiet(27.0)):  # 跑飞：3 秒话 + 27 秒静音
            consumed.append(b.size)
            yield b

    out = list(trim_runaway(source(), BIG))
    assert seconds(out) == pytest.approx(3.0, abs=0.01)
    # 只多读了约 1 秒静音就停，没把 27 秒都拉完
    assert sum(consumed) / SAMPLE_RATE < 3.0 + cosyvoice.SILENCE_STOP_S + 0.5


def test_short_pause_inside_speech_is_kept():
    out = list(trim_runaway(blocks(speech(1.0), quiet(0.6), speech(1.0)), BIG))
    assert seconds(out) == pytest.approx(2.6, abs=0.01)


def test_leading_silence_is_held_and_sent_with_first_speech():
    out = list(trim_runaway(blocks(quiet(0.4), speech(1.0)), BIG))
    assert seconds(out) == pytest.approx(1.4, abs=0.01)
    assert out[0][:2] == b"\0\0"  # 开头静音没丢，跟着第一块出去


def test_long_leading_silence_does_not_stop_before_speech():
    # 还没说过话时不触发「静音 1 秒就停」
    out = list(trim_runaway(blocks(quiet(2.0), speech(0.5)), BIG))
    assert seconds(out) == pytest.approx(2.5, abs=0.01)


def test_hard_cap_truncates_continuous_speech():
    out = list(trim_runaway(blocks(speech(10.0)), int(4.0 * SAMPLE_RATE)))
    assert seconds(out) <= 4.0


def test_all_silence_yields_nothing():
    assert list(trim_runaway(blocks(quiet(3.0)), BIG)) == []


def test_output_is_s16le_with_clipping():
    out = b"".join(trim_runaway([np.array([2.0, -2.0, 0.5], np.float32)], BIG))
    assert np.frombuffer(out, np.int16).tolist() == [32767, -32768, 16383]


def test_max_seconds_scales_with_text():
    assert cosyvoice.max_seconds("") == 3
    assert cosyvoice.max_seconds("一" * 10) > cosyvoice.max_seconds("一" * 5)
    # 34 个字正常念 10–13 秒，上限不能卡掉正常的
    assert cosyvoice.max_seconds("一" * 34) > 18


# ---------------------------------------------------------------- stream：停下时要关掉上游（让 Triton 取消请求）

def test_stream_closes_upstream_when_runaway_cut(monkeypatch):
    closed = []

    alive = []  # 留着上游生成器的引用，这样只有显式 close 才会触发 finally，GC 帮不了忙

    def chunks(*a, **kw):
        def gen():
            try:
                yield from blocks(speech(1.0), quiet(30.0))
            finally:
                closed.append(True)
        alive.append(gen())
        return alive[-1]

    monkeypatch.setattr(cosyvoice, "_triton_chunks", chunks)
    out = list(cosyvoice.stream("你好", None, "参考"))
    assert seconds(out) == pytest.approx(1.0, abs=0.01)
    assert closed == [True]  # 截停那一刻就关上游，Triton 才会取消请求


def test_stream_closes_upstream_when_consumer_stops_early(monkeypatch):
    closed = []

    def chunks(*a, **kw):
        try:
            yield from blocks(speech(5.0))
        finally:
            closed.append(True)

    monkeypatch.setattr(cosyvoice, "_triton_chunks", chunks)
    gen = cosyvoice.stream("你好你好你好", None, "参考")
    next(gen)
    gen.close()
    assert closed == [True]


# ---------------------------------------------------------------- tts：只出静音重试一次

def test_tts_retries_once_when_only_silence(monkeypatch):
    calls = []

    def chunks(*a, **kw):
        calls.append(1)
        yield from blocks(quiet(1.0) if len(calls) == 1 else speech(1.0))

    monkeypatch.setattr(cosyvoice, "_triton_chunks", chunks)
    pcm, first, total = cosyvoice.tts("你好", None, "参考")
    assert len(calls) == 2
    assert len(pcm) / 2 / SAMPLE_RATE == pytest.approx(1.0, abs=0.01)


def test_tts_raises_after_second_silent_attempt(monkeypatch):
    calls = []

    def chunks(*a, **kw):
        calls.append(1)
        yield from blocks(quiet(1.0))

    monkeypatch.setattr(cosyvoice, "_triton_chunks", chunks)
    with pytest.raises(cosyvoice.CosyVoiceError, match="没出声"):
        cosyvoice.tts("你好", None, "参考")
    assert len(calls) == 2


def test_load_reference_rejects_wrong_format(tmp_path):
    from tests.audio import tone, wav_bytes

    p = tmp_path / "ref.wav"
    p.write_bytes(wav_bytes(tone(1.0, rate=24000), rate=24000))
    with pytest.raises(cosyvoice.CosyVoiceError, match="16 kHz"):
        cosyvoice.load_reference(p)
    p.write_bytes(wav_bytes(tone(1.0)))
    ref = cosyvoice.load_reference(p)
    assert ref.dtype == np.float32 and ref.size == 16000 and np.abs(ref).max() <= 1


# ---------------------------------------------------------------- CosyVoiceSession

def run_session(fake_chunks, monkeypatch, texts, *, cancel_after_first=False, ready=True):
    monkeypatch.setattr(cosyvoice, "ready", lambda timeout=1.0: ready)
    monkeypatch.setattr(cosyvoice, "_triton_chunks", fake_chunks)
    got, ended = [], []

    async def main():
        s = cosyvoice.CosyVoiceSession(None, "参考", got.append, ended.append)
        await s.open()
        for t in texts:
            await s.append(t)
        if cancel_after_first:
            while not got:
                await asyncio.sleep(0.01)
            await s.cancel()
            await asyncio.sleep(0.05)
            return s
        await s.finish()
        for _ in range(200):
            if ended:
                break
            await asyncio.sleep(0.01)
        return s

    s = asyncio.run(main())
    return got, ended, s


def test_session_synthesizes_sentences_in_order(monkeypatch):
    order = []

    def chunks(text, *a, **kw):
        order.append(text)
        yield from blocks(speech(0.3))

    got, ended, s = run_session(chunks, monkeypatch, ["第一句。", "第二句。"])
    assert order == ["第一句。", "第二句。"]
    assert ended == [""] and s.done
    assert seconds(got) == pytest.approx(0.6, abs=0.01)


def test_session_reports_error_through_on_end(monkeypatch):
    def chunks(*a, **kw):
        raise cosyvoice.CosyVoiceError("gRPC 断了")
        yield  # noqa: unreachable，让它是生成器

    got, ended, s = run_session(chunks, monkeypatch, ["一句。"])
    assert got == [] and len(ended) == 1 and "gRPC 断了" in ended[0]


def test_session_cancel_stops_without_on_end(monkeypatch):
    seen_stop = []

    def chunks(text, reference, reference_text, *, timeout, stop):
        for b in blocks(speech(5.0), size=0.1):
            if stop.is_set():
                seen_stop.append(True)
                return
            time.sleep(0.01)  # 真 Triton 是一块一块慢慢出的
            yield b

    got, ended, s = run_session(chunks, monkeypatch, ["很长的一句。"], cancel_after_first=True)
    time.sleep(0.05)  # 让合成线程看到 stop
    assert s.done and ended == []  # 和豆包 BidirSession 一样：取消不回调 on_end
    assert seen_stop == [True]  # 合成线程收到了停止，不会把整句念完
    assert seconds(got) < 5.0


def test_session_open_fails_when_triton_down(monkeypatch):
    monkeypatch.setattr(cosyvoice, "ready", lambda timeout=1.0: False)

    async def main():
        await cosyvoice.CosyVoiceSession(None, "参考", print, print).open()

    with pytest.raises(cosyvoice.CosyVoiceError, match="没起来"):
        asyncio.run(main())
