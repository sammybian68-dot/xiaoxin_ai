"""接口层：FastAPI TestClient 打 server.main，外部服务都是 tests/fakes.py 的假实现。

覆盖鉴权、引擎接口、两种 ASR 的 HTTP / WebSocket、两种 TTS 的单句和对话流式朗读、降级提示。
"""

from __future__ import annotations

import base64
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from server import main, store, voices
from tests.audio import tone, wav_bytes
from tests.conftest import TEST_KEY
from tests.test_voices import add_clone


def sse(resp) -> list[tuple[str, object]]:
    out = []
    for block in resp.text.strip().split("\n\n"):
        ev = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((ev, data))
    return out


# ---------------------------------------------------------------- 鉴权

def test_api_requires_auth(anon):
    assert anon.get("/api/health").status_code == 200
    for method, path in [("GET", "/api/engines"), ("PUT", "/api/engines"), ("POST", "/api/asr"),
                         ("GET", "/api/voices"), ("POST", "/api/tts")]:
        assert anon.request(method, path).status_code == 401, path


def test_wrong_bearer_rejected(anon):
    assert anon.get("/api/engines", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_magic_link_sets_cookie_and_redirects(anon):
    r = anon.get(f"/?k={TEST_KEY}", follow_redirects=False)
    assert r.status_code == 303 and "xiaoxin_auth" in r.headers["set-cookie"]
    assert TEST_KEY not in r.headers["set-cookie"]  # cookie 放派生值，不放 key
    assert anon.get("/api/engines").status_code == 200


def test_ws_rejects_foreign_origin_and_missing_auth(anon):
    with pytest.raises(WebSocketDisconnect) as e:
        with anon.websocket_connect("/api/asr/stream") as ws:
            ws.receive_text()
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/api/asr/stream", headers={"Authorization": f"Bearer {TEST_KEY}",
                                                                "Origin": "https://evil.example"}) as ws:
            ws.receive_text()


# ---------------------------------------------------------------- 引擎接口

def test_engines_get_and_put(client, fakes):
    r = client.get("/api/engines").json()
    assert (r["asr"], r["tts"]) == ("doubao", "doubao")
    assert r["cosyvoice_ready"] is True and r["paraformer_ready"] is True
    r = client.put("/api/engines", json={"asr": "paraformer"}).json()
    assert (r["asr"], r["tts"]) == ("paraformer", "doubao")
    assert store.settings()["asr_engine"] == "paraformer"


def test_engines_put_rejects_unknown(client):
    r = client.put("/api/engines", json={"tts": "edge"})
    assert r.status_code == 400 and "edge" in r.json()["detail"]


def test_engines_report_services_down(client, fakes):
    fakes.cosyvoice_ready = fakes.paraformer_ready = False
    r = client.get("/api/engines").json()
    assert r["cosyvoice_ready"] is False and r["paraformer_ready"] is False


# ---------------------------------------------------------------- HTTP ASR

@pytest.mark.parametrize("engine,expect,call", [
    ("doubao", "豆包识别结果", "doubao.asr"),
    ("paraformer", "Paraformer识别结果", "paraformer.transcribe"),
])
def test_asr_follows_engine_setting(client, fakes, engine, expect, call):
    voices.set_engines(asr=engine)
    r = client.post("/api/asr", content=wav_bytes(tone(1.0)), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200
    assert r.json()["text"] == expect and r.json()["engine"] == engine
    assert call in fakes.names()


def test_asr_query_overrides_setting(client, fakes):
    r = client.post("/api/asr?engine=paraformer", content=wav_bytes(tone(1.0)),
                    headers={"Content-Type": "audio/wav"})
    assert r.json()["engine"] == "paraformer" and voices.engines()["asr"] == "doubao"


def test_asr_accepts_raw_pcm_from_device(client, fakes):
    pcm = tone(1.0)
    r = client.post("/api/asr", content=pcm + b"\x01", headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json()["seconds"] == pytest.approx(1.0)
    assert ("doubao.asr", len(pcm)) in fakes.calls  # 奇数字节被截掉，不会错位


@pytest.mark.parametrize("body,ctype,status,msg", [
    (wav_bytes(tone(0.2)), "audio/wav", 400, "太短"),
    (wav_bytes(tone(1.0, rate=24000), rate=24000), "audio/wav", 400, "16 kHz"),
    (b"garbage", "audio/wav", 400, "wav"),
])
def test_asr_bad_input(client, body, ctype, status, msg):
    r = client.post("/api/asr", content=body, headers={"Content-Type": ctype})
    assert r.status_code == status and msg in r.json()["detail"]


def test_asr_unknown_engine(client):
    r = client.post("/api/asr?engine=whisper", content=wav_bytes(tone(1.0)), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 400


def test_asr_service_failure_is_502(client, fakes):
    fakes.paraformer_ready = False
    r = client.post("/api/asr?engine=paraformer", content=wav_bytes(tone(1.0)),
                    headers={"Content-Type": "audio/wav"})
    assert r.status_code == 502 and "ASR 失败" in r.json()["detail"]


# ---------------------------------------------------------------- WebSocket ASR（按住说话）

def ws_talk(client, engine=None, *, chunks=5, cmd="end"):
    url = "/api/asr/stream" + (f"?engine={engine}" if engine else "")
    msgs = []
    with client.websocket_connect(url) as ws:
        msgs.append(json.loads(ws.receive_text()))
        for _ in range(chunks):
            ws.send_bytes(tone(0.1))
        ws.send_text(json.dumps({"type": cmd}))
        try:
            while True:
                msgs.append(json.loads(ws.receive_text()))
                if msgs[-1]["type"] in ("final", "error"):
                    break
        except WebSocketDisconnect:
            pass
    return msgs


def test_ws_doubao_has_partials_and_final(client, fakes):
    msgs = ws_talk(client)
    assert msgs[0] == {"type": "ready", "engine": "doubao", "partials": True}
    assert any(m["type"] == "partial" for m in msgs)
    assert msgs[-1]["type"] == "final" and msgs[-1]["text"] == "豆包识别结果"


def test_ws_paraformer_no_partials_one_transcribe(client, fakes):
    voices.set_engines(asr="paraformer")
    msgs = ws_talk(client)
    assert msgs[0] == {"type": "ready", "engine": "paraformer", "partials": False}
    assert not any(m["type"] == "partial" for m in msgs)
    assert msgs[-1]["text"] == "Paraformer识别结果" and msgs[-1]["engine"] == "paraformer"
    (_, nbytes, _), = [c for c in fakes.calls if c[0] == "paraformer.transcribe"]
    assert nbytes == len(tone(0.1)) * 5  # 按下到松手的 PCM 整段送了一次


def test_ws_cancel_produces_no_result(client, fakes):
    voices.set_engines(asr="paraformer")
    msgs = ws_talk(client, cmd="cancel")
    assert [m["type"] for m in msgs] == ["ready"]
    assert "paraformer.transcribe" not in fakes.names()


def test_ws_paraformer_down_reports_error(client, fakes):
    fakes.paraformer_ready = False
    msgs = ws_talk(client, "paraformer")
    assert msgs[-1]["type"] == "error"


def test_ws_unknown_engine_reports_error(client):
    with client.websocket_connect("/api/asr/stream?engine=whisper") as ws:
        m = json.loads(ws.receive_text())
    assert m["type"] == "error"


def test_ws_enforces_max_talk_length(client, fakes, monkeypatch):
    monkeypatch.setattr(main, "MAX_TALK_SECONDS", 0.3)
    msgs = ws_talk(client, chunks=5)
    assert msgs[-1]["type"] == "error" and "最多" in msgs[-1]["detail"]


# ---------------------------------------------------------------- 单句 TTS

def test_tts_doubao(client, fakes):
    r = client.post("/api/tts", json={"text": "你好"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.headers["x-voice"] == voices.DEFAULT
    assert fakes.calls[-1][0] == "doubao.tts"


def test_tts_cosyvoice_when_clone_selected(client, fakes):
    clone = add_clone()
    voices.select(clone)
    r = client.post("/api/tts", json={"text": "你好"})
    assert r.headers["x-voice"] == clone and fakes.calls[-1][0] == "cosyvoice"


def test_tts_falls_back_with_note_when_triton_down(client, fakes):
    from urllib.parse import unquote

    voices.select(add_clone())
    fakes.cosyvoice_ready = False
    voices._probe.update(at=0.0)
    r = client.post("/api/tts", json={"text": "你好"})
    assert r.status_code == 200 and r.headers["x-voice"].startswith("doubao:")
    assert "没起来" in unquote(r.headers["x-voice-note"])


def test_tts_empty_text(client):
    assert client.post("/api/tts", json={"text": "  "}).status_code == 400


# ---------------------------------------------------------------- 对话 + 流式朗读

def chat(client, **body):
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"text": "开空调", **body})
    assert r.status_code == 200
    return cid, sse(r)


def audio_seconds(events) -> float:
    return sum(len(base64.b64decode(d)) for e, d in events if e == "audio") / 48000


def test_chat_without_speak_has_no_audio(client, fakes):
    cid, ev = chat(client)
    kinds = [e for e, _ in ev]
    assert kinds[0] == "user" and "done" in kinds and "audio" not in kinds
    assert store.get(cid)["messages"][-1]["text"] == "好的，空调开了。"


def test_chat_speak_doubao_streams_audio(client, fakes):
    cid, ev = chat(client, speak=True)
    kinds = [e for e, _ in ev]
    assert kinds.index("done") < kinds.index("audio_end")  # 文字先完，音频接着下来
    assert audio_seconds(ev) > 0
    assert ("doubao.bidir", voices.DEFAULT.split(":")[1]) in fakes.calls
    assert "cosyvoice" not in fakes.names()


def test_chat_speak_cosyvoice_when_engine_selected(client, fakes):
    voices.select(add_clone(ref_text="参考段文字"))
    cid, ev = chat(client, speak=True)
    assert audio_seconds(ev) > 0
    cosy = [c for c in fakes.calls if c[0] == "cosyvoice"]
    assert cosy and all(c[2] == "参考段文字" for c in cosy)
    assert "doubao.bidir" not in fakes.names()
    end = dict(ev)["audio_end"]
    assert end["error"] == "" and end["sample_rate"] == 24000


def test_chat_speak_cosyvoice_builtin_male(client, fakes):
    """切到 CosyVoice 默认就是内置男声：参考是内置素材，不拿豆包念参考、不走豆包流式。"""
    assert client.put("/api/engines", json={"tts": "cosyvoice"}).json()["tts"] == "cosyvoice"
    assert client.get("/api/voices").json()["current"] == voices.BUILTIN
    cid, ev = chat(client, speak=True)
    assert audio_seconds(ev) > 0
    cosy = [c for c in fakes.calls if c[0] == "cosyvoice"]
    assert cosy and all(c[2] == voices.BUILTIN_MALE_TEXT for c in cosy)
    assert "doubao.bidir" not in fakes.names() and "doubao.tts" not in fakes.names()


def test_selecting_doubao_voice_switches_back_to_doubao(client, fakes):
    client.put("/api/engines", json={"tts": "cosyvoice"})
    cat = client.put("/api/voices/current", json={"voice": "doubao:" + voices.PRESETS[1][0]}).json()
    assert cat["engines"]["tts"] == "doubao"
    r = client.post("/api/voices/preview", json={"voice": "doubao:" + voices.PRESETS[1][0]})
    assert r.status_code == 200 and fakes.calls[-1][0] == "doubao.tts"


def test_chat_speak_cosyvoice_down_falls_back_with_note(client, fakes):
    voices.select(add_clone())
    fakes.cosyvoice_ready = False
    voices._probe.update(at=0.0)
    cid, ev = chat(client, speak=True)
    notes = [d for e, d in ev if e == "note"]
    assert notes and "没起来" in notes[0]
    assert "doubao.bidir" in fakes.names() and audio_seconds(ev) > 0


def test_chat_speak_cosyvoice_bad_reference_falls_back(client, fakes):
    clone = add_clone()
    voices.select(clone)
    store.sample_path(clone.split(":")[1]).with_suffix(".ref.wav").unlink()
    cid, ev = chat(client, speak=True)
    assert any(e == "note" and "参考段" in d for e, d in ev)
    assert "doubao.bidir" in fakes.names()


def test_chat_cosyvoice_synth_error_keeps_text(client, fakes):
    voices.select(add_clone())
    fakes.cosyvoice_error = "Triton 挂了"
    cid, ev = chat(client, speak=True)
    assert dict(ev)["done"]["text"] == "好的，空调开了。"
    assert "audio" not in [e for e, _ in ev]


def test_chat_llm_failure_falls_back_then_errors(client, fakes):
    fakes.llm_error = "HTTP 500"
    cid, ev = chat(client, speak=True)
    assert [c[1] for c in fakes.calls if c[0] == "llm"] == [main.MODELS[0]["id"], main.MODELS[0]["id"], main.FALLBACK]
    assert ev[-1][0] == "error" or any(e == "error" for e, _ in ev)


def test_chat_image_switches_to_vision_model(client, fakes):
    fid = client.post("/api/files", content=b"\x89PNG", headers={"Content-Type": "image/png",
                                                                  "X-Filename": "a.png"}).json()["id"]
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "看图", "file_ids": [fid],
                                                            "model": "deepseek-v4.1-flash"})
    assert main.VISION in [c[1] for c in fakes.calls if c[0] == "llm"]


# ---------------------------------------------------------------- 联网搜索的工具循环

def test_chat_offers_search_tool_and_tells_date(client, fakes):
    chat(client)
    req = fakes.llm_requests[0]
    assert req["tools"][0]["function"]["name"] == "search_web"
    assert "search_web" in req["system"] and "现在大约是" not in req["system"]  # 时间不进 system（KV cache）
    assert req["messages"][0]["content"].startswith("[system] 现在大约是 20")  # 作为 [system] 消息插在前面


def test_chat_search_then_answer(client, fakes):
    fakes.llm_script = [("search", ["今日新闻", "国际新闻"]), ("text", "今天的新闻是……")]
    cid, ev = chat(client)
    searches = [d for e, d in ev if e == "search"]
    assert [s["state"] for s in searches] == ["start", "done"]
    assert searches[0]["queries"] == ["今日新闻", "国际新闻"]
    assert len(searches[1]["sources"]) == 2
    assert {c[1] for c in fakes.calls if c[0] == "search"} == {"今日新闻", "国际新闻"}  # 两个 query 都搜了
    # 第二次请求里接上了 assistant(tool_calls) 和 tool 结果，assistant 那条不带 reasoning_content
    second = fakes.llm_requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call_1"
    assert "reasoning_content" not in second[-2]
    assert second[-1] == {"role": "tool", "tool_call_id": "call_1", "content": second[-1]["content"]}
    assert "关于今日新闻" in second[-1]["content"]
    # 存下来的回答带着出处，刷新后还能列来源
    saved = store.get(cid)["messages"][-1]
    assert saved["text"] == "今天的新闻是……"
    assert saved["searches"][0]["queries"] == ["今日新闻", "国际新闻"]
    assert dict(ev)["done"]["searches"] == saved["searches"]


def test_chat_search_limit_keeps_tools_but_refuses(client, fakes):
    """到上限后工具照样给（拿掉的话主脑 27B 会回空），第三次调用不真搜，回「到上限」让它用已有结果答。"""
    fakes.llm_script = [("search", ["a"]), ("search", ["b"]), ("search", ["c"]), ("text", "就这些")]
    cid, ev = chat(client)
    assert [c[1] for c in fakes.calls if c[0] == "search"] == ["a", "b"]
    assert all(r["tools"] for r in fakes.llm_requests) and len(fakes.llm_requests) == 4
    assert fakes.llm_requests[3]["messages"][-1]["content"] == main.SEARCH_LIMIT_RESULT
    assert [d["state"] for e, d in ev if e == "search"] == ["start", "done", "start", "done"]  # 第三次没发 search 事件
    assert dict(ev)["done"]["text"] == "就这些"


def test_chat_model_that_never_stops_calling_tools_ends_with_error(client, fakes):
    fakes.llm_script = [("search", [str(i)]) for i in range(10)]
    cid, ev = chat(client)
    # 真搜 2 次 + 回「到上限」1 次 = 3 轮工具；第 4 次请求还要调就放弃
    assert len(fakes.llm_requests) == main.MAX_TOOL_ROUNDS + 1
    errors = [d for e, d in ev if e == "error"]
    assert errors and "上限" in errors[0]


def test_chat_search_failure_still_answers(client, fakes):
    fakes.search_error = "ConnectTimeout"
    fakes.llm_script = [("search", ["今日新闻"]), ("text", "现在没法联网，")]
    cid, ev = chat(client)
    done = [d for e, d in ev if e == "search" and d["state"] == "done"][0]
    assert done["ok"] is False
    assert "OfflineFallback" in fakes.llm_requests[1]["messages"][-1]["content"]
    assert dict(ev)["done"]["text"] == "现在没法联网，"


def test_chat_error_after_search_does_not_switch_model(client, fakes, monkeypatch):
    """已经搜过了再出错，不换兜底模型重来（免得重复搜、前后两截）。"""
    fakes.llm_script = [("search", ["今日新闻"])]
    from src.llm import client as llm

    real = llm.iter_stream

    def flaky(model, history, **kw):
        if len(fakes.llm_requests) >= 1:
            fakes.llm_requests.append({"messages": history, "tools": kw.get("tools")})
            raise llm.ProxyError("HTTP 500")
        yield from real(model, history, **kw)

    monkeypatch.setattr(llm, "iter_stream", flaky)
    cid, ev = chat(client)
    assert len(fakes.llm_requests) == 2
    assert any(e == "error" for e, _ in ev)


def test_chat_speak_does_not_read_search_status(client, fakes):
    fakes.llm_script = [("search", ["北京天气"]), ("text", "北京明天晴。")]
    cid, ev = chat(client, speak=True)
    spoken = "".join(c[1] for c in fakes.calls if c[0] == "doubao.bidir.append")
    assert spoken == "北京明天晴。"


def test_saved_search_history_is_not_resent_as_tool_messages(client, fakes):
    fakes.llm_script = [("search", ["今日新闻"]), ("text", "新闻是……")]
    cid, _ = chat(client)
    client.post(f"/api/conversations/{cid}/messages", json={"text": "再说说"})
    hist = fakes.llm_requests[-1]["messages"]
    assert [m["role"] for m in hist if not str(m.get("content")).startswith("[system]")] == ["user", "assistant", "user"]  # 历史里只留正文


# ---------------------------------------------------------------- 唤醒「在」/ 主动退出「拜拜」

def test_cue_synthesized_in_current_voice_and_cached(client, fakes):
    r = client.get("/api/cue/zai")
    assert r.status_code == 200 and r.headers["x-sample-rate"] == "24000" and len(r.content) > 0
    assert r.headers["x-voice"] == voices.DEFAULT
    assert ("doubao.tts", voices.PRESETS[0][0], "我在的。") in fakes.calls
    n = len([c for c in fakes.calls if c[0] == "doubao.tts"])
    client.get("/api/cue/zai")
    assert len([c for c in fakes.calls if c[0] == "doubao.tts"]) == n  # 第二次读缓存


def test_cue_follows_voice(client, fakes):
    r = client.get("/api/cue/bye", params={"voice": voices.BUILTIN})
    assert r.status_code == 200 and r.headers["x-voice"] == voices.BUILTIN
    assert fakes.calls[-1] == ("cosyvoice", "拜拜。", voices.BUILTIN_MALE_TEXT)


def test_cue_trims_silence():
    from server.main import _trim_pcm
    from tests.audio import silence, tone

    pcm = silence(0.5) + tone(0.3, rate=24000, amp=3000) + silence(0.5)
    out = _trim_pcm(pcm, 24000)
    assert 0.3 <= len(out) / 48000 <= 0.5  # 头留 30 ms、尾留 150 ms
    import array
    assert array.array("h", out)[-1] == 0  # 尾巴淡出到 0，不是一刀切


def test_cue_unknown(client):
    assert client.get("/api/cue/hello").status_code == 404


def test_cue_retries_when_cut_short(client, fakes, monkeypatch):
    """CosyVoice 念到逗号就停：太短的重来一次。"""
    from server import main
    from tests.audio import silence, tone

    takes = [tone(0.4, rate=24000, amp=3000) + silence(0.3, rate=24000), tone(2.0, rate=24000, amp=3000)]
    calls = []

    def synth(text, voice_id=None):
        calls.append(text)
        from src.voice import doubao
        return doubao.Speech(takes[min(len(calls), 2) - 1], 24000, 0.1, 0.5), voice_id, ""

    monkeypatch.setattr(main.voices, "synth", synth)
    r = client.get("/api/cue/ring", params={"text": "卞依一，5秒倒计时到啦！"})
    assert r.status_code == 200 and len(calls) == 2 and len(r.content) / 48000 > 1.5


def test_cue_say_short_phrases(client, fakes):
    r = client.get("/api/cue/say", params={"text": "卧钩"})
    assert r.status_code == 200 and len(r.content) > 0
    assert client.get("/api/cue/say", params={"text": ""}).status_code == 400
    assert client.get("/api/cue/say", params={"text": "长" * 31}).status_code == 400
    n = len(fakes.calls)
    client.get("/api/cue/say", params={"text": "卧钩"})  # 第二次读缓存，不再合成
    assert len(fakes.calls) == n


@pytest.mark.parametrize("chunks, out", [
    (["[assistant]", " 好的"], "好的"), (["[assi", "stant]\n\n今天", "几件"], "今天几件"), (["[", "我觉得"], "[我觉得"),
    (["好的", "[assistant]"], "好的[assistant]"), (["小新：", "你好"], "你好"), (["小", "朋友好"], "小朋友好"),
])
def test_role_tag_filter(chunks, out):
    from server.main import RoleTagFilter

    f = RoleTagFilter()
    assert "".join(f.feed(c) for c in chunks) + f.buf == out


def test_leaked_role_tag_stripped_in_reply(client, fakes):
    fakes.llm_deltas = ["[assistant]", "\n\n今天有两件事。"]
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "今天有什么新闻"})
    assert store.get(cid)["messages"][-1]["text"] == "今天有两件事。"


def test_debug_trace_records_each_request(client, fakes):
    """调试面板：每轮记下每次发给主脑的请求（system、messages、tools、回答、用量），按轮次新的在前。"""
    fakes.llm_script = [("tool", "query_time", {}), ("text", "现在是上午 8 点")]
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "现在几点"})
    d = client.get(f"/api/debug/{cid}").json()
    t = d["turns"][0]
    assert t["voice"] is False and len(t["calls"]) == 2 and t["done"] is True
    first, second = t["calls"]
    assert first["system"] and "query_time" in first["tools"] and first["tool_calls"][0]["name"] == "query_time"
    assert first["messages"][0]["content"].startswith("[system]")  # 现状消息在 messages 里，不在 system
    assert second["reply"] == "现在是上午 8 点" and second["messages"][-1]["role"] == "tool"
    assert t["actions"][0]["name"] == "query_time"
    assert d["ctx"]["epoch"] == 0 and d["summary"] == ""


def test_debug_requires_auth(anon):
    assert anon.get("/api/debug/0123456789abcdef").status_code == 401
