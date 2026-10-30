"""[system] 现状消息（KV cache 友好）和上下文压缩（异步压缩 + swap）。"""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from server import clock, compact, context, store

TZ = clock.TZ
SUMMARY = "\n\n".join(f"{h}\n无." for h in compact.HEADINGS).replace("## 在聊什么\n无.", "## 在聊什么\n在做数学作业。")


@pytest.fixture
def at(monkeypatch):
    def set_(s):
        t = dt.datetime.fromisoformat(s).replace(tzinfo=TZ)
        monkeypatch.setattr(clock, "now", lambda: t)
        monkeypatch.setattr(clock.time, "time", lambda: t.timestamp())
        return t
    return set_


# ---------------------------------------------------------------- [system] 现状

@pytest.mark.parametrize("t, line", [
    ("2026-10-30 13:10", "下午 1 点左右"), ("2026-10-30 13:31", "下午 1 点半左右"), ("2026-10-30 13:50", "下午 2 点左右"),
    ("2026-10-30 07:20", "早上 7 点半左右"), ("2026-10-30 23:50", "晚上 12 点左右"),
])
def test_time_line_is_coarse(t, line):
    assert context.time_line(dt.datetime.fromisoformat(t)).endswith(line)


def test_note_only_when_needed(at, fakes):
    """第一轮一定有；之后时间没跨半点、闹钟没变就不插（前缀不变）；跨了半点或闹钟变了才插。"""
    at("2026-10-30 13:05")
    first = context.note_for({"messages": []})
    assert "现在大约是" in first["text"] and "闹钟和计时：\n无" in first["text"]
    conv = {"messages": [{"role": "note", "created": time.time(), **first}]}
    at("2026-10-30 13:20")
    assert context.note_for(conv) is None
    clock.add_alarm("07:00", "daily")
    n = context.note_for(conv)
    assert "现在大约是" not in n["text"] and "a1 闹钟 07:00" in n["text"]  # 只说变了的
    at("2026-10-30 13:31")
    assert "现在大约是" in context.note_for(conv)["text"]  # 跨了半点


def test_notes_are_stored_and_replayed_in_place(client, fakes, at):
    """note 存进对话，下一轮按原位置原样重放：前面的消息每轮字节一样（KV cache 命中）。"""
    at("2026-10-30 13:05")
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "你好"})
    first = fakes.llm_requests[-1]["messages"]
    at("2026-10-30 13:12")
    client.post(f"/api/conversations/{cid}/messages", json={"text": "再见"})
    second = fakes.llm_requests[-1]["messages"]
    assert second[:len(first)] == first  # 前缀完全一样
    assert first[0]["content"].startswith("[system] 现在大约是") and len(second) == len(first) + 2  # 没新插 note
    assert fakes.llm_requests[-1]["system"] == fakes.llm_requests[0]["system"]


# ---------------------------------------------------------------- 压缩

def _conv_with_turns(n: int) -> str:
    cid = store.create()["id"]
    for i in range(n):
        store.append(cid, {"role": "user", "text": f"第{i}题怎么做"})
        store.append(cid, {"role": "assistant", "text": f"第{i}题这样做"})
    return cid


def test_cut_keeps_last_five_user_turns():
    msgs = [{"role": r} for _ in range(8) for r in ("user", "assistant")]
    assert compact.cut_index(msgs, 0) == 6  # 第 4 个 user（下标 6）起留原文：最后 5 个
    assert compact.cut_index(msgs[:10], 0) is None  # 只有 5 轮，压了省不了什么


def test_check_requires_all_headings():
    assert compact.check(SUMMARY).startswith("## 在聊什么")
    with pytest.raises(ValueError, match="缺标题"):
        compact.check("## 在聊什么\n随便")


def test_async_compact_then_swap(client, fakes, monkeypatch):
    """超阈值 → 后台压缩（期间照常答）→ 下一轮开始时 swap：epoch+1、发给模型的是摘要 + 最后 5 轮。"""
    seen = {}

    def summarize(prev, msgs):
        seen["prev"], seen["n"] = prev, len(msgs)
        return SUMMARY

    monkeypatch.setattr(compact, "_summarize", summarize)
    monkeypatch.setattr(compact, "limit", lambda: 100)
    cid = _conv_with_turns(7)
    store.update_conv(cid, ctx_tokens=500)
    assert compact.maybe_kick(cid)
    for _ in range(50):
        if store.get(cid).get("compact", {}).get("state") == "ready":
            break
        time.sleep(0.02)
    assert store.get(cid)["compact"]["state"] == "ready" and seen["prev"] == "" and seen["n"] == 4  # 7 轮压前 2 轮
    assert store.list_conversations()[0]["compacting"] is True
    client.post(f"/api/conversations/{cid}/messages", json={"text": "下一题"})
    conv = store.get(cid)
    assert conv["epoch"] == 1 and conv["summary"] == SUMMARY and conv["compact"]["state"] == "idle"
    sent = fakes.llm_requests[-1]["messages"]
    assert sent[0]["content"].startswith(compact.SUMMARY_PREFIX)
    users = [m["content"] for m in sent if m["role"] == "user" and not m["content"].startswith(("[system]", "前面的对话"))]
    assert users == ["第2题怎么做", "第3题怎么做", "第4题怎么做", "第5题怎么做", "第6题怎么做", "下一题"]
    assert len(conv["messages"]) > 14  # 原文一条没删


def test_failed_compact_does_not_swap(fakes, monkeypatch):
    monkeypatch.setattr(compact, "_summarize", lambda prev, msgs: compact.check("坏的"))
    monkeypatch.setattr(compact, "limit", lambda: 100)
    cid = _conv_with_turns(7)
    store.update_conv(cid, ctx_tokens=500)
    compact.maybe_kick(cid)
    for _ in range(50):
        if store.get(cid).get("compact", {}).get("state") == "idle":
            break
        time.sleep(0.02)
    conv = store.get(cid)
    assert conv["compact"]["state"] == "idle" and "缺标题" in conv["compact"]["error"]
    assert not compact.maybe_swap(cid) and (conv.get("epoch") or 0) == 0


def test_compact_now_button_and_prompt_view(client, fakes, monkeypatch):
    monkeypatch.setattr(compact, "_summarize", lambda prev, msgs: SUMMARY)
    cid = _conv_with_turns(3)
    r = client.post(f"/api/conversations/{cid}/compact").json()
    assert r["started"] is True  # 立即压缩：不看阈值，轮次少也压（留最后一轮）
    for _ in range(50):
        if client.get(f"/api/conversations/{cid}/ctx").json()["state"] == "ready":
            break
        time.sleep(0.02)
    client.post(f"/api/conversations/{cid}/messages", json={"text": "继续"})
    v = client.get(f"/api/prompts?conversation={cid}").json()
    assert v["summary"]["text"] == SUMMARY and v["summary"]["epoch"] == 1 and "## 学习进度" in v["compact"]
    assert client.get(f"/api/conversations/{cid}/ctx").json()["epoch"] == 1


def test_ctx_tokens_recorded_from_usage(client, fakes):
    cid = client.post("/api/conversations").json()["id"]
    body = client.post(f"/api/conversations/{cid}/messages", json={"text": "你好"}).text
    done = json.loads(body.split("event: done\ndata: ")[1].split("\n")[0])
    assert done["ctx"]["limit"] == compact.limit() and done["ctx"]["epoch"] == 0


def test_ctx_limit_setting(client):
    assert client.put("/api/ctx_limit", json={"limit": 50000}).json()["limit"] == 50000
    assert compact.limit() == 50000
    assert client.put("/api/ctx_limit", json={"limit": 100}).status_code == 400
