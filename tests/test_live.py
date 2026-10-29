"""打 gpu2 上真实的服务，确认假实现和真服务的契约没对不上。要在 gpu2 上跑：

    python3 -m pytest tests/test_live.py --live

服务不在就 skip，不算失败。不改任何设置、不写对话数据。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.live

CARHER = Path(os.environ.get("CARHER_ROOT", "/Data/cmbian/codes/carHer2"))
PROMPT_WAV = CARHER / "vendor/CosyVoice/asset/zero_shot_prompt.wav"
PROMPT_TEXT = "希望你以后能够做的比我还好呦。"


def prompt_16k() -> bytes:
    """CosyVoice 自带的参考音（24 kHz float32 wav）转成 16 kHz s16le。"""
    if not PROMPT_WAV.is_file():
        pytest.skip(f"没有 {PROMPT_WAV}")
    d = PROMPT_WAV.read_bytes()
    fmt = struct.unpack("<HHI", d[20:28])
    assert fmt[:3] == (3, 1, 24000), fmt
    a = np.frombuffer(d[d.find(b"data") + 8:], np.float32)
    a16 = np.interp(np.arange(0, a.size, 1.5), np.arange(a.size), a)
    return (a16 * 32767).astype(np.int16).tobytes()


def test_paraformer_recognizes_reference_audio():
    from src.voice import paraformer

    if paraformer.status() is None:
        pytest.skip("ASR worker 28182 没在跑")
    rec = paraformer.transcribe(prompt_16k())
    assert "以后" in rec.text and "比我" in rec.text
    assert rec.final < 3


def test_cosyvoice_speaks_and_stops():
    from src.voice import cosyvoice

    if not cosyvoice.ready():
        pytest.skip("Triton 18100 没起来")
    ref = np.frombuffer(prompt_16k(), np.int16).astype(np.float32) / 32768
    text = "好，空调开了。"
    pcm, first, total = cosyvoice.tts(text, ref, PROMPT_TEXT)
    secs = len(pcm) / 2 / cosyvoice.SAMPLE_RATE
    assert 0.5 < secs <= cosyvoice.max_seconds(text)
    assert first < 3


def test_bocha_search_contract():
    from src.search import web_search

    if not web_search.env("BOCHA_API_KEY"):
        pytest.skip("没配 BOCHA_API_KEY")
    res = web_search.search({"queries": [{"query": "北京天气", "freshness": "day"}]})
    assert res.ok and not res.content.startswith(("OfflineFallback", "**联网失败"))
    assert res.sources and all(s["url"].startswith("http") for s in res.sources)


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "claude-sonnet-5-5", "qwen3.8-27b"])
def test_models_call_search_tool(model):
    """代理和各家模型的工具调用格式没变：问新闻会调 search_web，参数能解析出 query。"""
    from src.llm import client
    from src.search import web_search

    try:
        out = list(client.iter_stream(model, "今天有什么新闻？", system="今天是 2026-10-29。需要实时信息就调 search_web。",
                                      max_tokens=400, timeout=60, tools=[web_search.SPEC]))
    except client.ProxyError as e:
        pytest.skip(f"{model} 不可用：{e}")
    calls = [v for k, v in out if k == "tool_calls"]
    assert calls and calls[0][0]["name"] == web_search.NAME and calls[0][0]["id"]
    assert web_search.parse_queries(calls[0][0]["arguments"])


def test_doubao_asr_contract():
    from src.voice import doubao

    try:
        doubao.api_key()
    except doubao.DoubaoError:
        pytest.skip("没配豆包 key")
    rec = doubao.asr(prompt_16k())
    assert "以后" in rec.text
