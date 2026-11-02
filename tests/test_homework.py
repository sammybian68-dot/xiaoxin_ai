"""班级作业：按天幂等存取、缺哪几天、按学科排序、给小新看的文字，homework_report 工具（某天 / 某段时间 / 没记录）。"""

from __future__ import annotations

import time

import pytest

from server import homework, store


def _items():
    return [{"subject": "english", "text": "完成同步 U5", "submit": "去完成", "mentions": ["U5听力.mp3"]},
            {"subject": "语文", "teacher": "张老师", "text": "1.抄词第12课；\n2.习字册", "submit": "无需提交",
             "notes": ["28、26 号没带作业本"], "images": ["f1"]},
            {"subject": "math", "text": "正反打印作业纸并完成", "submit": "无需提交"}]


def test_save_is_idempotent_and_sorted():
    r = homework.save_day("2026-10-23", _items())
    assert r["week"] == "五" and [it["subject"] for it in r["items"]] == ["chinese", "math", "english"]
    assert homework.save_day("2026-10-23", _items())["items"] == r["items"]  # 再存一遍：覆盖，不是追加
    assert homework.days() == ["2026-10-23"]
    assert homework.get_day("2026-10-23")["items"][0]["teacher"] == "张老师"
    assert homework.get_day("2026-10-24") is None


def test_bad_day():
    with pytest.raises(homework.HomeworkError):
        homework.save_day("10-23", [])
    with pytest.raises(homework.HomeworkError):
        homework.get_day("../etc/passwd")


def test_range_and_missing():
    for d in ("2026-10-20", "2026-10-23"):
        homework.save_day(d, _items())
    assert [r["day"] for r in homework.range_days("2026-10-19", "2026-10-23")] == ["2026-10-23", "2026-10-20"]  # 新的在前
    assert homework.missing("2026-10-19", "2026-10-23") == ["2026-10-19", "2026-10-21", "2026-10-22"]


def test_describe():
    homework.save_day("2026-10-23", _items())
    t = homework.describe([homework.get_day("2026-10-23")])
    assert "## 2026-10-23 星期五" in t and "语文（张老师）［无需提交］" in t
    assert "抄词第12课" in t and "提醒：28、26 号没带作业本" in t and "附图 1 张" in t
    assert "提到但没找到的文件：U5听力.mp3" in t


def test_api_and_tool(client, fakes):
    day = time.strftime("%Y-%m-%d")
    assert client.put(f"/api/homework/{day}", json={"items": _items()}).json()["items"][0]["subject"] == "chinese"
    assert len(client.get("/api/homework").json()["days"]) == 1
    assert client.get(f"/api/homework?date={day}").json()["days"][0]["day"] == day

    from server.tools import registry

    out = registry.run_local("homework_report", "{}")
    assert "抄词第12课" in out and "英语" in out
    old = registry.run_local("homework_report", '{"date": "2026-01-02"}')
    assert "没有作业记录" in old and day in old
    rng = registry.run_local("homework_report", '{"since": "%s"}' % day)
    assert "有记录的 1 天" in rng and "抄词第12课" in rng
    assert "失败" in registry.run_local("homework_report", '{"date": "10月23日"}')


def test_tool_card_in_chat(client, fakes):
    day = time.strftime("%Y-%m-%d")
    client.put(f"/api/homework/{day}", json={"items": _items()})
    tid = client.get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "homework_report", {}), ("text", "今天语文抄词，英语做同步")]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "看下今天的作业"})
    acts = store.get(tid)["messages"][-1]["actions"]
    assert acts[0]["name"] == "homework_report" and "抄词第12课" in acts[0]["result"]


# ---------------------------------------------------------------- 采集：分批读出来的结果怎么合并（tools/qq_homework.py）

def test_merge_keeps_longest_text(monkeypatch, tmp_path):
    """同一条作业在相邻几批里都读到：正文被屏幕切断的那份短，要留最全的；notes / mentions 取并集、附图标记取或。"""
    from tools import qq_homework as q

    batches = [
        [{"date": "2026-10-23", "items": [{"subject": "chinese", "text": "1.抄词第12课；\n2.习字", "notes": ["甲"]}]}],
        [{"date": "2026-10-23", "items": [{"subject": "chinese", "text": "1.抄词第12课；\n2.习字册；\n3.小册", "notes": ["乙"],
                                           "mentions": ["练习卷.pdf"], "has_image": True}]}],
        [{"date": "2026-10-23", "items": [{"subject": "math", "text": "正反打印作业纸"}]}],
    ]
    monkeypatch.setattr(q, "CHUNK", 1)
    monkeypatch.setattr(q, "OVERLAP", 0)
    monkeypatch.setattr(q, "read_chunk", lambda shots, today: batches[int(shots[0].stem)])
    shots = []
    for i in range(3):
        p = tmp_path / f"{i}.png"
        p.write_bytes(b"x")
        shots.append(p)

    days, where = q.read_shots(shots, "2026-10-25")
    assert len(days) == 1 and days[0]["date"] == "2026-10-23"
    items = {it["subject"]: it for it in days[0]["items"]}
    assert items["chinese"]["text"].endswith("3.小册") and items["chinese"]["notes"] == ["甲", "乙"]
    # 「（看不清）」那份不能盖掉完整的（屏幕切断时模型会这么读）
    from tools.qq_homework import _score

    assert _score({"text": "1.完成《同步》期中自我测评\n2.订正2号本"}) > _score({"text": "1.完成《同步》\n（看不清）\n还有一行"})
    assert _score({"text": "（看不清）"}) > _score({"text": ""})
    assert items["chinese"]["mentions"] == ["练习卷.pdf"] and items["chinese"]["has_image"] is True
    assert items["math"]["text"] == "正反打印作业纸"
    assert where[("2026-10-23", "chinese")] == ["0", "1"]  # 这条出现在第 0、1 屏：附图按屏归属认领


def test_empty_items_dropped():
    """视觉模型有时看到学科色块就补一条空的（实测 10/29 其实没有数学作业）：没有正文的条目不存，附图并给第一条。"""
    r = homework.save_day("2026-10-29", [{"subject": "chinese", "text": "1.默第13课词语", "images": ["a"]},
                                         {"subject": "math", "text": "", "images": ["b"]},
                                         {"subject": "english", "text": "   "},
                                         {"subject": "other", "text": "（看不清）", "images": ["c"]}])
    assert [it["subject"] for it in r["items"]] == ["chinese"]
    assert r["items"][0]["images"] == ["a", "b", "c"]
    # 正文里只是夹着「（看不清）」的那几行要留着
    keep = homework.save_day("2026-10-28", [{"subject": "math", "text": "1.大册P28\n（看不清）"}])
    assert keep["items"][0]["text"].startswith("1.大册P28")


def test_agent_settings(client):
    d = client.get("/api/agent").json()
    assert d["model"] == "claude-opus-5-5" and d["at"][0] == "17:00" and "claude-opus-5-5" in d["models"]
    d = client.put("/api/agent", json={"model": "claude-sonnet-5-5", "at": ["17:30", "坏的", "19:00"], "max_steps": 99}).json()
    assert d["model"] == "claude-sonnet-5-5" and d["at"] == ["17:30", "19:00"] and d["max_steps"] == 60
    assert client.put("/api/agent", json={"model": "gpt-9"}).status_code == 400
    assert client.put("/api/agent", json={"at": ["明天"]}).status_code == 400
    assert client.get("/api/agent").json()["model"] == "claude-sonnet-5-5"  # 存住了
