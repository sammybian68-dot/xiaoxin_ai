"""自由对话 /api/talk：一轮完整流程、插话打断、识别为空、搜索透传、鉴权、接着已有对话说。外部服务都是假的。"""

from __future__ import annotations

import json
import threading
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from server import store, voices
from tests.audio import silence, tone
from tests.conftest import TEST_KEY


def said(seconds=1.0):
    """一句话：前后静音，中间人声，够 VAD 判出 start / end。"""
    return silence(0.5) + tone(seconds, amp=2000) + silence(1.0)


class Call:
    """包一层 TestClient 的 websocket：收消息时把 JSON 和音频分开记。"""

    def __init__(self, ws):
        self.ws, self.msgs, self.audio = ws, [], 0

    def send_pcm(self, pcm, chunk=3200):
        for i in range(0, len(pcm), chunk):
            self.ws.send_bytes(pcm[i:i + chunk])

    def until(self, kind, limit=400, timeout=10.0):
        """收到 kind 为止。超过 timeout 秒没等到就失败，不让整个测试卡死。"""
        import queue
        import threading

        deadline = time.monotonic() + timeout
        for _ in range(limit):
            box: queue.Queue = queue.Queue()
            threading.Thread(target=lambda: box.put(self.ws.receive()), daemon=True).start()
            try:
                m = box.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"{timeout} 秒没等到 {kind}，收到 {[x['type'] for x in self.msgs]}") from None
            if m.get("bytes"):
                self.audio += len(m["bytes"])
                continue
            msg = json.loads(m["text"])
            self.msgs.append(msg)
            if msg["type"] == kind:
                return msg
        raise AssertionError(f"没等到 {kind}，收到 {[m['type'] for m in self.msgs]}")

    def kinds(self):
        return [m["type"] for m in self.msgs]


@pytest.fixture
def call(client):
    def open_(query=""):
        cm = client.websocket_connect("/api/talk" + query)
        ws = cm.__enter__()
        c = Call(ws)
        c._cm = cm
        c.until("ready")
        return c

    opened = []

    def factory(query=""):
        c = open_(query)
        opened.append(c)
        return c

    yield factory
    for c in opened:
        try:
            c.ws.send_text(json.dumps({"type": "stop"}))
            c._cm.__exit__(None, None, None)
        except Exception:
            pass


def test_one_turn_end_to_end(call, fakes):
    c = call()
    c.send_pcm(said())
    c.until("idle")
    k = c.kinds()
    order = ["listening", "heard", "thinking", "delta", "reply", "speaking", "idle"]
    assert all(x in k for x in order)
    assert k.index("heard") < k.index("thinking") < k.index("delta") < k.index("idle")
    assert next(m for m in c.msgs if m["type"] == "heard")["text"] == fakes.doubao_text
    assert c.audio > 0  # 二进制音频帧
    cid = c.msgs[0]["conversation"]
    roles = [(m["role"], m.get("via")) for m in store.get(cid)["messages"]]
    assert roles == [("user", "talk"), ("assistant", None)]


def test_ready_reports_engines_and_voice(call, fakes):
    voices.set_engines(asr="paraformer", tts="cosyvoice")
    c = call()
    r = c.msgs[0]
    assert (r["asr"], r["tts"]) == ("paraformer", "cosyvoice") and r["voice"] == voices.catalog()["current"]
    c.send_pcm(said())
    assert c.until("heard")["text"] == fakes.paraformer_text
    c.until("idle")
    assert "cosyvoice" in fakes.names()


def test_continue_existing_conversation(call, fakes):
    cid = store.create()["id"]
    store.append(cid, {"role": "user", "text": "早上好"})
    c = call(f"?conversation={cid}")
    assert c.msgs[0]["conversation"] == cid
    c.send_pcm(said())
    c.until("idle")
    assert [m["role"] for m in fakes.llm_requests[-1]["messages"]] == ["user", "user"]


def test_unknown_conversation(client, fakes):
    with client.websocket_connect("/api/talk?conversation=0123456789abcdef") as ws:
        assert json.loads(ws.receive_text())["type"] == "error"


def test_empty_recognition_goes_back_to_idle(call, fakes):
    fakes.doubao_text = ""
    c = call()
    c.send_pcm(said())
    c.until("idle")
    assert "thinking" not in c.kinds() and "llm" not in fakes.names()


def test_search_events_are_forwarded(call, fakes):
    fakes.llm_script = [("search", ["北京天气"]), ("text", "北京明天晴。")]
    c = call()
    c.send_pcm(said())
    c.until("idle")
    states = [m["state"] for m in c.msgs if m["type"] == "search"]
    assert states == ["start", "done"]


def test_barge_in_cancels_turn_and_tells_device(call, fakes, monkeypatch):
    """小新在想（LLM 慢）的时候用户又开口：发 interrupt，旧这一轮不再出字、不出声，新这句照常回答。"""
    from src.llm import client as llm

    gate = threading.Event()
    real = llm.iter_stream

    def slow(model, history, **kw):
        if len(fakes.llm_requests) == 0:  # 第一轮卡住，等插话
            fakes.llm_requests.append({"messages": history, "tools": kw.get("tools")})
            gate.wait(5)
            yield "content", "第一轮的回答。"
            return
        yield from real(model, history, **kw)

    monkeypatch.setattr(llm, "iter_stream", slow)
    c = call()
    c.send_pcm(said())
    c.until("thinking")
    c.send_pcm(silence(0.2) + tone(1.0, amp=2000))   # 插话
    c.until("interrupt")
    c.send_pcm(silence(1.0))
    gate.set()
    c.until("idle")
    k = c.kinds()
    assert k.count("heard") == 2
    after = k[k.index("interrupt"):]
    deltas = [m["text"] for m in c.msgs[k.index("interrupt"):] if m["type"] == "delta"]
    assert "第一轮的回答。" not in deltas and "reply" in after
    # 被打断那一轮，模型说完的话也存了，标 interrupted，历史不会只有问题没有回答
    time.sleep(0.3)
    msgs = store.get(c.msgs[0]["conversation"])["messages"]
    assert [m["role"] for m in msgs].count("user") == 2
    assert any(m.get("interrupted") for m in msgs if m["role"] == "assistant")


def test_barge_in_while_speaking_stops_audio(call, fakes, monkeypatch):
    from server import speech

    monkeypatch.setattr(speech, "LEAD_S", 0.3)  # 让音频慢慢放，方便在念的时候插话
    fakes.llm_deltas = ["今天天气不错，" * 20 + "。"]
    c = call()
    c.send_pcm(said())
    c.until("speaking")
    c.send_pcm(silence(0.2) + tone(1.0, amp=2000))
    c.until("interrupt")
    before = c.audio
    c.send_pcm(silence(1.0))
    c.until("heard")
    # 插话之后旧这一轮不再下发音频（新一轮的音频在 heard 之后才有）
    assert c.audio == before


def test_model_from_query_is_used(call, fakes):
    c = call("?model=qwen3.8-27b")
    c.send_pcm(said())
    c.until("idle")
    assert [x[1] for x in fakes.calls if x[0] == "llm"] == ["qwen3.8-27b"]


def test_switch_model_mid_call(call, fakes):
    """通话中在顶栏换模型：下一句起用新的（2026-10-29 用户切了 27B，回答还是 deepseek）。"""
    c = call()
    c.send_pcm(said())
    c.until("idle")
    c.ws.send_text(json.dumps({"type": "model", "model": "qwen3.8-27b"}))
    assert c.until("model")["model"] == "qwen3.8-27b"
    c.send_pcm(said())
    c.until("idle")
    assert [x[1] for x in fakes.calls if x[0] == "llm"] == ["deepseek-v4.1-flash", "qwen3.8-27b"]


def test_switch_to_unknown_model_ignored(call, fakes):
    c = call()
    c.ws.send_text(json.dumps({"type": "model", "model": "gpt-99"}))
    c.send_pcm(said())
    c.until("idle")
    assert "model" not in c.kinds()
    assert [x[1] for x in fakes.calls if x[0] == "llm"] == ["deepseek-v4.1-flash"]


def test_voice_hint_in_system_prompt(call, fakes):
    """语音通话要告诉模型：不然它说「我听不到声音，请打字」。"""
    c = call()
    c.send_pcm(said())
    c.until("idle")
    system = fakes.llm_requests[-1]["system"]
    assert "语音通话" in system and "别说你听不到声音" in system


def test_timing_reported_and_saved(call, fakes):
    fakes.llm_script = [("search", ["北京天气"]), ("text", "北京明天晴。")]
    c = call()
    c.send_pcm(said())
    t = c.until("idle")["timing"]
    assert set(t) == {"asr", "search", "first_text", "first_audio", "model"}
    assert 0 <= t["asr"] <= t["first_text"] <= t["first_audio"]
    assert t["search"] >= 0 and t["model"] == "deepseek-v4.1-flash"
    assert next(m for m in c.msgs if m["type"] == "heard")["t_asr"] == t["asr"]
    saved = store.get(c.msgs[0]["conversation"])["messages"][-1]
    assert saved["timing"] == t


def test_vad_end_ms_setting_applies_to_new_call(call, fakes):
    """停 0.6 秒：默认 800 ms 下这还算一句话；设成 400 ms 就断成两句（看服务端判了几次「说完」）。"""
    from server import talk as talk_mod

    gap = silence(0.5) + tone(1.0, amp=2000) + silence(0.6) + tone(1.0, amp=2000) + silence(1.2)
    c = call()
    assert c.msgs[0]  # ready
    c.send_pcm(gap)
    c.until("idle")
    asr_calls = [x for x in fakes.calls if x[0] == "doubao.asr"]
    assert len(asr_calls) == 1
    voices.set_engines(vad_end_ms=400)
    c2 = call()
    vad = talk_mod.Vad(end_ms=voices.engines()["vad_end_ms"])
    assert [k for k, _ in vad.feed(gap)] == ["start", "end", "start", "end"]


def test_stop_closes(call, fakes):
    c = call()
    c.ws.send_text(json.dumps({"type": "stop"}))
    assert c.ws.receive()["type"] == "websocket.close"


def test_requires_auth(anon):
    with pytest.raises(WebSocketDisconnect) as e:
        with anon.websocket_connect("/api/talk") as ws:
            ws.receive()
    assert e.value.code == 4401


def test_bearer_auth_without_origin(anon, fakes):
    """APK 用 Bearer、不带 Origin。"""
    with anon.websocket_connect("/api/talk", headers={"Authorization": f"Bearer {TEST_KEY}"}) as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
