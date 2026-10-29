"""一轮回答的流式朗读：切句规则、markdown 清理、TurnSpeech 的 PCM 节流和两种引擎的接法。"""

from __future__ import annotations

import asyncio
import base64

import pytest

from server import speech
from server.speech import BYTES_PER_S, FRAME_BYTES, flush_boundary, speakable


# ---------------------------------------------------------------- 切句

@pytest.mark.parametrize("text,cut", [
    ("好的。", -1),                       # 首句太短（句号在第 2 个字），不切
    ("好的，空调开了。", 8),               # 第 4 个字以后见句号就切
    ("空调已经打开了！还有别的吗", 8),
    ("空调已经打开了", -1),                 # 没有标点，不切
])
def test_first_sentence_cut(text, cut):
    assert flush_boundary(text, first=True) == cut


def test_first_sentence_comma_needs_44_chars():
    short = "今天天气不错，"
    assert flush_boundary(short, first=True) == -1
    long = "一" * 44 + "，后面"
    assert flush_boundary(long, first=True) == 45


def test_first_sentence_hard_cut_at_90_chars():
    assert flush_boundary("一" * 90, first=True) == 90
    assert flush_boundary("一" * 89, first=True) == -1


def test_later_sentences_cut_at_last_long_enough_boundary():
    text = "一" * 90 + "。" + "二" * 5 + "。" + "三"
    # 后面的句子攒长一点：找最靠后、位置 ≥ 88 的句末
    assert flush_boundary(text, first=False) == 97
    assert flush_boundary("短句。短句。", first=False) == -1
    assert flush_boundary("一" * 180, first=False) == 180


def test_speakable_strips_markdown_and_code():
    text = "## 标题\n**加粗** 和 `代码`\n- 列表项\n```python\nprint(1)\n```\n结尾"
    out = speakable(text)
    for bad in ("#", "*", "`", "print"):
        assert bad not in out
    assert "加粗" in out and "列表项" in out and "（代码略）" in out and "结尾" in out


# ---------------------------------------------------------------- TurnSpeech

def make(monkeypatch, *, cosy=False):
    """TurnSpeech 接一个假的 session：append 收到的文字记下来，on_pcm 由测试手动推。"""
    from tests.fakes import install

    f = install(monkeypatch)
    ts = speech.TurnSpeech("spk", cosyvoice=(None, "参考") if cosy else None)
    return ts, f


def test_turnspeech_doubao_feeds_sentences(monkeypatch):
    ts, f = make(monkeypatch)

    async def main():
        ts.start()
        for d in ["好的，", "空调开了。", "还有", "别的吗？"]:
            await ts.feed(d)
        await ts.finish_text()

    asyncio.run(main())
    sent = [c[1] for c in f.calls if c[0] == "doubao.bidir.append"]
    assert "".join(sent) == "好的，空调开了。还有别的吗？"
    assert sent[0] == "好的，空调开了。"  # 首句一到句号就送出去
    assert ts.audio_done is False or ts._ended.is_set()


def test_turnspeech_cosyvoice_engine_uses_cosyvoice_session(monkeypatch):
    ts, f = make(monkeypatch, cosy=True)

    async def main():
        ts.start()
        await ts.feed("好的，空调开了。")
        await ts.finish_text()
        for _ in range(200):
            if ts._ended.is_set():
                break
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert [c[0] for c in f.calls] == ["cosyvoice"]
    assert ts._ended.is_set() and ts.info()["error"] == ""
    assert len(ts._pcm) > 0


def test_turnspeech_open_failure_keeps_text_silent(monkeypatch):
    ts, f = make(monkeypatch, cosy=True)
    f.cosyvoice_ready = False

    async def main():
        ts.start()
        await ts.feed("好的，空调开了。")
        await ts.finish_text()

    asyncio.run(main())
    assert ts.audio_done
    assert "没起来" in ts.info()["error"]


def test_take_audio_first_batch_limited_to_lead(monkeypatch):
    ts, _ = make(monkeypatch)
    ts._on_pcm(b"\1\0" * (BYTES_PER_S * 5 // 2))  # 5 秒音频一下子全到
    chunks = ts.take_audio()
    sent = sum(len(base64.b64decode(c)) for c in chunks)
    assert sent == int(ts.lead_s * BYTES_PER_S)  # 第一批只给 LEAD 秒
    assert all(len(base64.b64decode(c)) == FRAME_BYTES for c in chunks)


def test_take_audio_waits_for_full_frame_until_end(monkeypatch):
    ts, _ = make(monkeypatch)
    ts._on_pcm(b"\1\0" * 100)  # 不够 100 ms
    assert ts.take_audio() == []
    ts._on_end("")
    tail = ts.take_audio()  # 结束了，尾巴不够一帧也发
    assert len(base64.b64decode(tail[0])) == 200 and ts.audio_done


def test_take_audio_throttles_by_wall_clock(monkeypatch):
    ts, _ = make(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(speech.time, "monotonic", lambda: now[0])
    ts._on_pcm(b"\1\0" * (BYTES_PER_S * 5 // 2))
    first = sum(len(base64.b64decode(c)) for c in ts.take_audio())
    assert ts.take_audio() == []  # 时间没走，不再给
    now[0] += 1.0
    more = sum(len(base64.b64decode(c)) for c in ts.take_audio())
    assert more == BYTES_PER_S  # 过了 1 秒，再放 1 秒
    assert first + more <= (1.0 + ts.lead_s) * BYTES_PER_S
