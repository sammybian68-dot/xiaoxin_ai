"""外部服务的假实现：豆包（ASR / TTS / 双向 TTS）、Paraformer、CosyVoice、LLM。

只换掉「出网 / 出进程」的那一层，上面的逻辑（引擎选择、降级、节流、切句、截停）都跑真代码。
每个假实现都把调用记进 Fakes.calls，测试用它断言「走了哪个引擎、传了什么」。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import numpy as np

from tests.audio import tone

TTS_RATE = 24000


def pcm_for(text: str, per_char: float = 0.02) -> bytes:
    """假 TTS 出的音：每个字 per_char 秒的正弦波，够短，测试不用等节流。"""
    return tone(max(0.05, per_char * len(text)), rate=TTS_RATE)


@dataclass
class Fakes:
    calls: list = field(default_factory=list)
    doubao_text: str = "豆包识别结果"
    stream_partials: list | None = None  # 流式识别的中间结果，每 feed 一次吐一个
    paraformer_text: str = "Paraformer识别结果"
    llm_deltas: list = field(default_factory=lambda: ["好的，", "空调开了。"])
    llm_error: str = ""            # 非空时 LLM 抛 ProxyError
    llm_script: list = field(default_factory=list)    # 见 install 里 LLM 那段
    llm_requests: list = field(default_factory=list)  # 每次请求 LLM 时发了什么
    search_error: str = ""         # 非空时博查返回 OfflineFallback
    cosyvoice_ready: bool = True
    paraformer_ready: bool = True
    cosyvoice_error: str = ""      # 非空时 CosyVoice 合成抛错
    doubao_tts_error: str = ""

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def install(monkeypatch) -> Fakes:
    from server import speech, voices
    from src.llm import client as llm
    from src.search import web_search
    from src.voice import cosyvoice, doubao, paraformer

    f = Fakes()
    voices._probe.update(at=0.0, ok=False)  # 探活缓存每个测试重新来

    # ---------------- 豆包
    def doubao_asr(pcm, **kw):
        f.calls.append(("doubao.asr", len(pcm)))
        return doubao.Recognition(f.doubao_text, 0.1, 0.12, len(pcm) / 32000)

    def doubao_tts(text, *, speaker=None, sample_rate=TTS_RATE, **kw):
        f.calls.append(("doubao.tts", speaker, text))
        if f.doubao_tts_error:
            raise doubao.DoubaoError(f.doubao_tts_error)
        return doubao.Speech(tone(max(0.05, 0.02 * len(text)), rate=sample_rate), sample_rate, 0.05, 0.1)

    class StreamingAsr:
        def __init__(self, **kw):
            self.text, self.seconds = "", 0.0

        async def __aenter__(self):
            f.calls.append(("doubao.stream_asr",))
            self.partials = asyncio.Queue()
            return self

        async def __aexit__(self, *exc):
            pass

        async def feed(self, pcm):
            self.seconds += len(pcm) / 32000
            # stream_partials 给了就按它一个个吐（自由对话早打断测试用），没给就吐「部分N」
            if f.stream_partials is not None:
                if f.stream_partials:
                    self.partials.put_nowait(f.stream_partials.pop(0))
            else:
                self.partials.put_nowait(f"部分{int(self.seconds * 10)}")

        async def finish(self):
            self.text = f.doubao_text
            return self.text

    monkeypatch.setattr(doubao, "asr", doubao_asr)
    monkeypatch.setattr(doubao, "tts", doubao_tts)
    monkeypatch.setattr(doubao, "StreamingAsr", StreamingAsr)

    class BidirSession:
        """豆包双向流式 TTS：append 一句就回一段 PCM，finish 后 on_end("")。"""

        def __init__(self, speaker, on_pcm, on_end, **kw):
            self.speaker, self.on_pcm, self.on_end, self.done = speaker, on_pcm, on_end, False
            f.calls.append(("doubao.bidir", speaker))

        async def open(self):
            if f.doubao_tts_error:
                raise doubao.DoubaoError(f.doubao_tts_error)

        async def append(self, text):
            f.calls.append(("doubao.bidir.append", text))
            self.on_pcm(pcm_for(text))

        async def finish(self):
            if not self.done:
                self.done = True
                self.on_end("")

        async def cancel(self):
            self.done = True

    monkeypatch.setattr(speech, "BidirSession", BidirSession)

    # ---------------- Paraformer（只换 HTTP 那层，StreamingParaformer 用真代码）
    def pf_ensure(load_timeout=120):
        f.calls.append(("paraformer.ensure",))
        if not f.paraformer_ready:
            raise paraformer.ParaformerError("ASR worker 60 秒没起来")

    def pf_transcribe(pcm, *, sample_rate=16000, hotword=paraformer.HOTWORDS, timeout=30):
        f.calls.append(("paraformer.transcribe", len(pcm), hotword))
        if not f.paraformer_ready:
            raise paraformer.ParaformerError("Paraformer 连不上")
        return doubao.Recognition(f.paraformer_text, None, 0.08, len(pcm) / (sample_rate * 2))

    monkeypatch.setattr(paraformer, "ensure", pf_ensure)
    monkeypatch.setattr(paraformer, "transcribe", pf_transcribe)
    monkeypatch.setattr(paraformer, "ready", lambda: f.paraformer_ready)

    # ---------------- CosyVoice（换 Triton 那层：ready 和逐块出波形的 _triton_chunks）
    def cosy_chunks(text, reference, reference_text, *, timeout, stop):
        f.calls.append(("cosyvoice", text, reference_text))
        if f.cosyvoice_error:
            raise cosyvoice.CosyVoiceError(f.cosyvoice_error)
        audio = np.frombuffer(pcm_for(text), np.int16).astype(np.float32) / 32768
        for i in range(0, audio.size, 2400):
            if stop and stop.is_set():
                return
            yield audio[i:i + 2400]

    monkeypatch.setattr(cosyvoice, "ready", lambda timeout=1.0: f.cosyvoice_ready)
    monkeypatch.setattr(cosyvoice, "_triton_chunks", cosy_chunks)

    # ---------------- LLM
    # llm_script 非空时按顺序一次吐一步：("search", [query...]) 是调一次搜索工具，("text", "...") 是回答，
    # ("tool", name, {参数}) 是调别的工具（闹钟之类）。
    # 没给工具（到了搜索上限）还排着 search 的话，就当模型老老实实回答 llm_deltas。
    def iter_stream(model, history, *, tools=None, **kw):
        f.calls.append(("llm", model, bool(tools)))
        f.llm_requests.append({"messages": list(history), "tools": tools, "system": kw.get("system")})
        if f.llm_error:
            raise llm.ProxyError(f.llm_error)
        yield "model", model
        step = f.llm_script.pop(0) if f.llm_script else ("text", "".join(f.llm_deltas))
        if step[0] == "tool" and tools:  # ("tool", 工具名, 参数 dict)：调任意一个工具
            n = len(f.llm_requests)
            yield "tool_calls", [{"id": f"call_{n}", "name": step[1], "arguments": json.dumps(step[2], ensure_ascii=False)}]
            return
        if step[0] == "search" and tools:
            n = len([r for r in f.llm_requests if r["tools"]])
            yield "tool_calls", [{"id": f"call_{n}", "name": tools[0]["function"]["name"],
                                  "arguments": json.dumps({"queries": [{"query": q} for q in step[1]]},
                                                          ensure_ascii=False)}]
            return
        text = step[1] if step[0] == "text" else "".join(f.llm_deltas)
        if text:
            yield "content", text
        yield "usage", {"total_tokens": 10}

    monkeypatch.setattr(llm, "iter_stream", iter_stream)

    # ---------------- 联网搜索（换博查 HTTP 那层，parse / 并发 / 合并跑真代码）
    def search_one(query, freshness=None):
        f.calls.append(("search", query, freshness))
        if f.search_error:
            return web_search.Result(False, f"OfflineFallback: {f.search_error}")
        return web_search.Result(True, f"共 1 条\n\n**1. 关于{query}的结果**\n<https://example.com/{len(f.calls)}>",
                                 [query], [{"title": f"关于{query}", "url": f"https://example.com/{query}",
                                            "site": "示例网"}])

    monkeypatch.setattr(web_search, "search_one", search_one)
    return f
