"""自由对话断句：开口、说完、短噪声不算、字头不丢、底噪自适应、超长强制断。电平按 2026-10-29 pad 实测设。"""

from __future__ import annotations

import pytest

from server import vad
from server.vad import Vad
from tests.audio import silence, tone


def speech(seconds: float, amp: int = 2000) -> bytes:  # 2000 ≈ 63 dB，实测人声 55–65 dB
    return tone(seconds, amp=amp)


def hiss(seconds: float, amp: int = 2) -> bytes:  # 实测 AEC 后小新的残留约 2 dB
    return tone(seconds, amp=amp, freq=900)


def events(v: Vad, pcm: bytes, chunk: int = 3200) -> list:
    out = []
    for i in range(0, len(pcm), chunk):
        out += v.feed(pcm[i:i + chunk])
    return out


def test_one_utterance_start_then_end():
    v = Vad()
    ev = events(v, silence(1.0) + speech(1.5) + silence(1.0))
    assert [k for k, _ in ev] == ["start", "end"]
    secs = len(ev[1][1]) / 32000
    assert 1.5 <= secs <= 1.5 + vad.PRE_ROLL_MS / 1000 + 0.2  # 带了字头和一点尾巴，不带整段停顿


def test_keeps_pre_roll_so_first_syllable_is_not_lost():
    v = Vad()
    pcm = silence(1.0) + speech(1.0) + silence(1.0)
    _, (_, utt) = events(v, pcm)
    assert utt[:2] == b"\0\0" or utt.count(b"\0\0") > 0  # 句首前面有一小段静音（pre-roll）
    loud_start = len(utt) - len(utt.lstrip(b"\0"))
    assert loud_start <= vad.PRE_ROLL_MS * 32  # 字头没被吃掉：pre-roll 之内就是声音


def test_short_click_is_not_speech():
    v = Vad()
    assert events(v, silence(1.0) + speech(0.1) + silence(1.0)) == []


def test_aec_residual_is_not_speech():
    v = Vad()
    assert events(v, hiss(5.0)) == []


def test_pause_shorter_than_end_window_keeps_one_utterance():
    v = Vad()
    ev = events(v, silence(0.5) + speech(1.0) + silence(0.4) + speech(1.0) + silence(1.0))
    assert [k for k, _ in ev] == ["start", "end"]


def test_two_utterances():
    v = Vad()
    ev = events(v, silence(0.5) + speech(1.0) + silence(1.5) + speech(1.0) + silence(1.0))
    assert [k for k, _ in ev] == ["start", "end", "start", "end"]


def test_noise_present_from_the_start_is_learned_not_speech():
    v = Vad()
    noisy = tone(3.0, amp=300, freq=500)  # ≈ 47 dB 的持续底噪（空调本来就开着）
    assert events(v, noisy) == []
    assert v.threshold >= 47 + vad.SPEECH_OVER_NOISE - 3
    ev = events(v, speech(1.0, amp=8000) + noisy)  # 比底噪高很多的人声照样能判出来
    assert [k for k, _ in ev] == ["start", "end"]


def test_noise_appearing_later_costs_at_most_one_forced_cut(monkeypatch):
    monkeypatch.setattr(vad, "MAX_UTTERANCE_S", 3)
    v = Vad()
    ev = events(v, silence(1.0) + tone(10.0, amp=300, freq=500))  # 中途开了空调
    assert [k for k, _ in ev] == ["start", "end"]  # 被强制断一次，之后学会了
    ev = events(v, speech(1.0, amp=8000) + tone(2.0, amp=300, freq=500))
    assert [k for k, _ in ev] == ["start", "end"]


def test_no_start_during_warmup():
    v = Vad()
    assert events(v, speech(vad.WARMUP_MS / 1000)) == []


def test_max_utterance_forces_end(monkeypatch):
    monkeypatch.setattr(vad, "MAX_UTTERANCE_S", 2)
    v = Vad()
    ev = events(v, silence(0.5) + speech(5.0))
    assert ("end" in [k for k, _ in ev]) and len(ev[1][1]) <= 2 * 32000


def test_reset_drops_partial_utterance():
    v = Vad()
    assert [k for k, _ in events(v, silence(0.5) + speech(0.5))] == ["start"]
    v.reset()
    assert events(v, silence(1.0)) == []


@pytest.mark.parametrize("chunk", [640, 1000, 3200, 32000])
def test_chunk_size_does_not_matter(chunk):
    v = Vad()
    ev = events(v, silence(0.5) + speech(1.0) + silence(1.0), chunk=chunk)
    assert [k for k, _ in ev] == ["start", "end"]
