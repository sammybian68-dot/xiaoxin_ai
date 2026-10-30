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
    assert "语音通话" in system and "不要说听不到声音" in system


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


@pytest.mark.parametrize("text,rest", [
    ("小新同学明天北京会下雨吗", "明天北京会下雨吗"),          # 端侧小模型：没标点
    ("小新同学，明天北京会下雨吗？", "明天北京会下雨吗？"),    # 豆包：有标点，保留后面的
    ("嘿小新同学帮我开一下空调", "帮我开一下空调"),
    ("嘿，小新同学，帮我开空调。", "帮我开空调。"),
    ("小新同学", ""),
    ("小新同学。", ""),
    ("明天会下雨吗", "明天会下雨吗"),                          # 不是唤醒词开头：原样
])
def test_strip_wake_word(text, rest):
    from server.talk import strip_wake_word

    assert strip_wake_word(text, "小新同学") == rest


@pytest.mark.parametrize("text,rest", [
    ("你好小新现在几点", "现在几点"),
    ("你好，小新，帮我开空调", "帮我开空调"),
    ("你好小心现在几点", "现在几点"),        # 端侧把「小新」认成「小心」（实测 8 句里 2 句）
    ("小心同学帮我开空调", "帮我开空调"),
    ("小心点别摔了", "小心点别摔了"),        # 不是唤醒词：原样，不去改「小心」
    ("你好", "你好"),
])
def test_strip_wake_words_multi_and_alias(text, rest):
    from server.talk import strip_wake_word

    assert strip_wake_word(text, ["小新同学", "你好小新"]) == rest


@pytest.mark.parametrize("text,ok", [
    ("退下", True), ("退下。", True), ("退下吧！", True), ("退 下 吧", True),
    ("你先退下吧", False), ("我要退下来了", False), ("退一下", False), ("退下吧小新", False), ("", False),
])
def test_is_exit_exact(text, ok):
    from server.talk import is_exit

    assert is_exit(text, ["退下", "退下吧"]) is ok


def test_exit_word_ends_talk_without_llm(call, fakes):
    fakes.doubao_text = "小新退下。"
    c = call()
    c.send_pcm(said())
    assert c.until("exit")["text"] == "小新退下。"
    c.until("bye")
    assert "llm" not in fakes.names()
    assert "doubao.bidir" not in fakes.names()  # 「拜拜」由端侧用缓存的音色说，服务端不合成
    assert "idle" not in c.kinds()  # bye 之前不发 idle，免得 pad 那边又开始计时
    assert store.get(c.msgs[0]["conversation"])["messages"] == []


def test_exit_word_inside_sentence_is_a_normal_question(call, fakes):
    fakes.doubao_text = "小新退下吧"
    c = call()
    c.send_pcm(said())
    c.until("idle")
    assert "exit" not in c.kinds() and "llm" in fakes.names()


def test_wake_then_exit(call, fakes):
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "你好小新"}))
    c.until("idle")
    fakes.doubao_text = "小心退下"  # 「小新」认成「小心」也算
    c.send_pcm(said())
    c.until("bye")


def test_wake_with_question_skips_cloud_asr(call, fakes):
    """pad 唤醒后发来端侧认好的整句：去掉唤醒词直接问模型，不再上云识别。"""
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "小新同学明天北京会下雨吗"}))
    assert c.until("heard")["text"] == "明天北京会下雨吗"
    c.until("idle")
    assert "doubao.asr" not in fakes.names() and "paraformer.transcribe" not in fakes.names()
    first_user = fakes.llm_requests[0]["messages"][-1]
    assert first_user["content"] == "明天北京会下雨吗"


def test_wake_word_only_replies_ack_without_llm(call, fakes):
    """浏览器（没有本机缓存）：服务端合成「在」。"""
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "小新同学。"}))
    c.until("idle")
    assert "llm" not in fakes.names() and "heard" not in c.kinds()
    assert ("doubao.bidir.append", "我在的。") in fakes.calls
    assert c.audio > 0
    assert store.get(c.msgs[0]["conversation"])["messages"] == []  # 应答不进对话记录


def test_wake_acked_by_device_is_silent(call, fakes):
    """pad 已经用本机缓存说了「在」（acked）：服务端不再合成，直接等用户说。"""
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "小新同学", "acked": True}))
    c.until("idle")
    assert "doubao.bidir" not in fakes.names() and c.audio == 0


def test_wake_uses_custom_wake_word(call, fakes):
    voices.set_engines(wake_words="小新小新")
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "小新小新今天作业是什么"}))
    assert c.until("heard")["text"] == "今天作业是什么"


def test_after_wake_follow_up_goes_through_vad(call, fakes):
    """唤醒后接着说（不用再喊唤醒词）：走正常的 VAD + 识别。"""
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "小新同学"}))
    c.until("idle")
    c.send_pcm(said())
    assert c.until("heard")["text"] == fakes.doubao_text


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


# ---------------------------------------------------------------- * 通配：匹配恰好一个字（2026-10-30 用户真人测，小新常被认成别的字）

@pytest.mark.parametrize("text,rest", [
    ("你好小婷", ""), ("你好小嗯", ""), ("小英同学", ""), ("小安同学", ""),
    ("你好小婷现在几点", "现在几点"), ("小安同学，帮我开空调", "帮我开空调"),
    ("是林同学", "是林同学"),      # 第一个字不是「小」：不算
    ("今天上学的同学", "今天上学的同学"),
    ("你好", "你好"),              # 短了：不算
])
def test_wildcard_wake(text, rest):
    from server.talk import strip_wake_word

    assert strip_wake_word(text, ["你好小*", "小*同学"]) == rest


def test_wildcard_exit_is_still_whole_sentence():
    from server.talk import is_exit

    assert is_exit("小英退下", ["小*退下"]) and is_exit("小新退下。", ["小*退下"])
    assert not is_exit("小英退下吧", ["小*退下"]) and not is_exit("小退下", ["小*退下"])


def test_utterance_audio_saved_and_replayable(call, client, fakes):
    """用户那句的原声存下来，气泡上的小喇叭能拉回放；端侧认出的字记在消息上。"""
    c = call()
    c.ws.send_text(json.dumps({"type": "local", "text": "退一下"}))
    c.send_pcm(said())
    h = c.until("heard")
    c.until("idle")
    assert h["audio"] is True and h["local"] == "退一下"
    cid = c.msgs[0]["conversation"]
    user = store.get(cid)["messages"][0]
    assert user["id"] == h["id"] and user["local"] == "退一下" and user["audio"] is True
    r = client.get(f"/api/conversations/{cid}/audio/{h['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF" and len(r.content) > 16000  # 至少半秒 16 kHz
    # 删对话连原声一起删
    client.delete(f"/api/conversations/{cid}")
    assert client.get(f"/api/conversations/{cid}/audio/{h['id']}").status_code == 404


def test_one_shot_wake_has_no_audio_but_local_text(call, fakes):
    c = call()
    c.ws.send_text(json.dumps({"type": "wake", "text": "小新同学现在几点"}))
    h = c.until("heard")
    c.until("idle")
    assert h["audio"] is False and h["local"] == "小新同学现在几点"


def test_utterance_audio_bad_ids(client):
    assert client.get("/api/conversations/../audio/x").status_code == 404
    assert client.get("/api/conversations/0123456789abcdef/audio/0123456789abcdef").status_code == 404


def test_utterance_audio_requires_auth(anon):
    assert anon.get("/api/conversations/0123456789abcdef/audio/0123456789abcdef").status_code == 401


@pytest.mark.parametrize("text, ok", [
    ("退了吧", True), ("退下吧。", True), ("推了吧", True),   # 推 = 退
    ("推一下", True),                                           # 只登记了「退一下」也认
    ("退吧", False), ("退了好吧", False), ("我推了吧", False),  # 还是整句、恰好一个字
])
def test_exit_same_sound_and_wildcard(text, ok):
    from server.talk import is_exit

    assert is_exit(text, ["退*吧", "退一下"]) is ok


def test_same_sound_keeps_original_text_when_stripping_wake():
    from server.talk import strip_wake_word

    assert strip_wake_word("小新同学，推荐一本书", ["小新同学"]) == "推荐一本书"


def test_client_vad_segments_utterances(call, fakes):
    """pad 发了端侧断句：服务端按 vad_start / vad_end 切句，能量 VAD 不管了（中间一直有声也照样切）。"""
    c = call()
    c.ws.send_text(json.dumps({"type": "vad_start"}))
    c.send_pcm(tone(1.0, amp=2000))
    c.ws.send_text(json.dumps({"type": "vad_end"}))
    c.send_pcm(tone(2.0, amp=2000))  # 句子之间的声音（电视）不算
    assert c.until("heard")["text"] == fakes.doubao_text
    c.until("idle")
    assert c.kinds().count("heard") == 1
    secs = [x[1] / 32000 for x in fakes.calls if x[0] == "doubao.asr"]
    assert secs and abs(secs[-1] - 1.0) < 0.05  # 只送了 vad_start..vad_end 那一段


def test_client_vad_barge_in(call, fakes, monkeypatch):
    c = call()
    c.ws.send_text(json.dumps({"type": "vad_start"}))
    c.send_pcm(tone(0.5, amp=2000))
    c.ws.send_text(json.dumps({"type": "vad_end"}))
    c.until("thinking")
    c.ws.send_text(json.dumps({"type": "vad_start"}))
    assert c.until("interrupt")["type"] == "interrupt"
