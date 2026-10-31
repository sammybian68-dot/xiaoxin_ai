"""家庭线程：老的多段对话并成一条、开始新篇章、压缩和新篇章的分界、pad 记着老 id 时接到线程上。"""

from __future__ import annotations

import json

from server import compact, context, store
from tests.test_compact import at  # noqa: F401  fixture
from tests.test_talk import call, said  # noqa: F401  call 是 fixture


def _old_conv(created: float, texts: list[str], **extra) -> str:
    c = store.create()
    store.update_conv(c["id"], created=created, **extra)
    for t in texts:
        store.append(c["id"], {"role": "user", "text": t})
        store.append(c["id"], {"role": "assistant", "text": "好的：" + t})
    return c["id"]


# ---------------------------------------------------------------- 合并

def test_fresh_install_gets_an_empty_thread():
    tid = store.thread_id()
    assert store.thread_id() == tid  # 第二次不再建
    t = store.thread()
    assert t["messages"] == [] and t["epochs"][0]["kind"] == "start"


def test_migrate_merges_old_conversations_by_last_activity(fakes):
    a = _old_conv(100, ["第一段"])
    b = _old_conv(200, ["第二段甲", "第二段乙"])
    store.update_conv(b, summary="第二段的摘要", epoch=1, epoch_start=2)
    (store.TALK_AUDIO / a).mkdir(parents=True)
    (store.TALK_AUDIO / a / "aaaaaaaaaaaaaaaa.wav").write_bytes(b"RIFF")

    tid = store.thread_id()
    assert tid == b  # 沿用最近那段的 id
    t = store.get(tid)
    assert [m["text"] for m in t["messages"] if m["role"] == "user"] == ["第一段", "第二段甲", "第二段乙"]
    # 第二段从第 2 条开始；它自己压缩过一次（epoch_start=2）→ 线程里在 2+2=4 处
    assert [(e["start"], e["kind"]) for e in t["epochs"]] == [(0, "start"), (2, "legacy"), (4, "compact")]
    assert t["epoch_start"] == 4 and t["summary"] == "第二段的摘要"  # 小新的上下文和合并前一样
    # 老文件挪进 archive，有备份，原声跟着挪
    assert not (store.CONV / f"{a}.json").exists() and (store.ARCHIVE / f"{a}.json").is_file()
    assert list(store.BACKUP.glob("thread-migrate-*/conversations/*.json"))
    assert (store.TALK_AUDIO / tid / "aaaaaaaaaaaaaaaa.wav").is_file()
    assert store.archived(a) and not store.archived(tid)
    assert json.loads(store.SETTINGS.read_text())["thread"] == tid
    assert store.thread_id() == tid and len(store.thread()["messages"]) == 6  # 再跑一次不变


def test_thread_api_and_llm_context_after_migrate(client, fakes):
    _old_conv(100, ["老的"])
    tid = client.get("/api/thread").json()["id"]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "新的一句"})
    sent = [m["content"] for m in fakes.llm_requests[-1]["messages"] if m["role"] == "user"]
    assert "老的" in sent and "新的一句" in sent[-1]


# ---------------------------------------------------------------- 开始新篇章

def test_new_chapter_blank_window_keeps_messages(client, fakes):
    tid = client.get("/api/thread").json()["id"]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "我叫小明"})
    n = len(store.get(tid)["messages"])
    r = client.post("/api/thread/new_chapter", json={"keep_summary": False})
    assert r.status_code == 200 and r.json()["epoch"] == 1 and r.json()["tokens"] == 0
    t = store.get(tid)
    assert len(t["messages"]) == n and t["epoch_start"] == n and t["summary"] == ""
    assert t["epochs"][-1]["kind"] == "new" and t["epochs"][-1]["start"] == n
    client.post(f"/api/conversations/{tid}/messages", json={"text": "你好"})
    sent = fakes.llm_requests[-1]["messages"]
    assert not any("我叫小明" in str(m["content"]) for m in sent)  # 前面的不再发给模型
    # 窗口换了：第一条又是完整现状（时间 + 闹钟）
    assert sent[0]["content"].startswith("[system] 现在大约是") and "闹钟和计时" in sent[0]["content"]


def test_new_chapter_with_summary(client, fakes, monkeypatch):
    tid = client.get("/api/thread").json()["id"]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "我在学乘法"})
    seen = {}

    def fake_summarize(prev, msgs):
        seen["texts"] = [m.get("text") for m in msgs]
        return "## 学习进度\n在学乘法"

    monkeypatch.setattr(compact, "_summarize", fake_summarize)
    assert client.post("/api/thread/new_chapter", json={"keep_summary": True}).status_code == 200
    assert "我在学乘法" in seen["texts"]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "继续"})
    first = fakes.llm_requests[-1]["messages"][0]["content"]
    assert first.startswith(compact.SUMMARY_PREFIX) and "在学乘法" in first


def test_new_chapter_summary_failure_changes_nothing(client, fakes, monkeypatch):
    tid = client.get("/api/thread").json()["id"]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "你好"})

    def boom(prev, msgs):
        raise RuntimeError("主脑超时")

    monkeypatch.setattr(compact, "_summarize", boom)
    r = client.post("/api/thread/new_chapter", json={"keep_summary": True})
    assert r.status_code == 502 and "主脑超时" in r.json()["detail"]
    assert (store.get(tid).get("epoch") or 0) == 0


def test_stale_compaction_is_dropped_after_new_chapter():
    """压缩跑到一半用户翻了页：压好的那份（切点在新篇章之前）不能再换上去。"""
    tid = store.thread_id()
    for i in range(8):
        store.append(tid, {"role": "user", "text": f"q{i}"})
        store.append(tid, {"role": "assistant", "text": f"a{i}"})
    store.update_conv(tid, compact={"state": "ready", "pending": {"summary": "旧摘要", "cut": 6}})
    store.new_epoch(tid, "new")
    assert compact.maybe_swap(tid) is False
    t = store.get(tid)
    assert t["epoch_start"] == 16 and t["summary"] == "" and t["compact"]["state"] == "idle"


def test_compact_swap_records_epoch_mark():
    tid = store.thread_id()
    for i in range(8):
        store.append(tid, {"role": "user", "text": f"q{i}"})
    store.update_conv(tid, compact={"state": "ready", "pending": {"summary": "摘要", "cut": 3}})
    assert compact.maybe_swap(tid) is True
    t = store.get(tid)
    assert t["epoch"] == 1 and t["epoch_start"] == 3 and t["epochs"][-1] == {**t["epochs"][-1], "start": 3, "kind": "compact"}


def test_note_is_restated_in_new_window(at, fakes):
    at("2026-10-31 10:05")
    tid = store.thread_id()
    first = context.note_for(store.get(tid))
    store.append(tid, {"role": "note", **first})
    assert context.note_for(store.get(tid)) is None
    store.new_epoch(tid, "new")
    assert context.note_for(store.get(tid)) is not None  # 新窗口里还没说过


# ---------------------------------------------------------------- 自由对话

def test_talk_without_conversation_goes_to_thread(call, fakes):
    tid = store.thread_id()
    c = call("")
    assert c.msgs[0]["conversation"] == tid
    c.send_pcm(said())
    c.until("idle")
    assert any(m["role"] == "user" for m in store.get(tid)["messages"])


def test_talk_with_archived_id_joins_thread(call, fakes):
    old = _old_conv(100, ["老的"])
    _old_conv(200, ["新的"])
    tid = store.thread_id()
    assert old != tid and store.archived(old)
    c = call(f"?conversation={old}")
    assert c.msgs[0]["conversation"] == tid
