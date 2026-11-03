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
    assert d["on"] is True and d["model"] == "claude-opus-5-5" and d["at"][0] == "17:00"
    assert d["weekdays"] == [1, 2, 3, 4, 5] and d["max_tries"] == 4 and d["today_done"] is False
    d = client.put("/api/agent", json={"on": False, "model": "claude-sonnet-5-5", "at": ["19:00", "坏的", "16:30"],
                                       "weekdays": [1, 3, 5, 9], "max_tries": 99, "max_steps": 1}).json()
    assert d["on"] is False and d["model"] == "claude-sonnet-5-5" and d["at"] == ["16:30", "19:00"]
    assert d["weekdays"] == [1, 3, 5] and d["max_tries"] == 12 and d["max_steps"] == 10
    assert client.put("/api/agent", json={"model": "gpt-9"}).status_code == 400
    assert client.put("/api/agent", json={"at": ["明天"]}).status_code == 400
    assert client.put("/api/agent", json={"weekdays": []}).status_code == 400
    assert client.get("/api/agent").json()["on"] is False  # 存住了


def test_agent_run_now_is_idempotent(client):
    import time

    from server.agent_bus import bus

    bus.last_seen = time.time()   # pad 在线
    today = time.strftime("%Y-%m-%d")
    r = client.post("/api/agent/run", json={}).json()
    assert r["started"] is True
    assert client.get("/api/agent").json()["pending"]["day"] == today
    again = client.post("/api/agent/run", json={}).json()
    assert again["started"] is False and "排队" in again["why"]      # 重复点：不再下单

    client.put(f"/api/homework/{today}", json={"items": _items()})   # 今天已经抓到了
    done = client.post("/api/agent/run", json={}).json()
    assert done["started"] is False and "已经抓过" in done["why"] and done["items"] == 3
    assert client.get("/api/agent").json()["today_done"] is True


def test_run_log(client):
    assert client.get("/api/homework/runs").json()["runs"] == []
    client.post("/api/homework/runs", json={"since": "2026-11-02", "until": "2026-11-02", "ok": True,
                                            "saved": ["2026-11-02"], "why": "读完了", "secs": 178, "trigger": "schedule",
                                            "junk": "不该存"})
    client.post("/api/homework/runs", json={"since": "2026-11-03", "until": "2026-11-03", "ok": False, "why": "没找到"})
    runs = client.get("/api/homework/runs").json()["runs"]
    assert [r["ok"] for r in runs] == [False, True] and runs[1]["trigger"] == "schedule" and "junk" not in runs[1]


def test_notify_as_xiaoxin(client):
    from server.family import members

    kid = members.kids()[0]
    r = client.post("/api/homework/notify", json={"text": "今天的作业我没抓到，去 QQ 群看一眼。"}).json()
    assert kid["id"] in r["sent"]
    last = store.get(kid["thread"])["messages"][-1]
    assert last["role"] == "assistant" and last["via"] == "homework" and "没抓到" in last["text"]
    assert client.post("/api/homework/notify", json={"text": " "}).status_code == 400


def test_runs_tolerates_old_format(client):
    """老记录里 steps 是个数字（截图张数），不是逐步记录：别让它把接口搞崩。"""
    from server import homework

    import time

    homework.log_run({"since": "2026-10-01", "until": "2026-10-01", "ok": True, "steps": 42})
    time.sleep(1.1)  # 错开一秒：面板按时间戳取某一轮
    homework.log_run({"since": "2026-10-02", "until": "2026-10-02", "ok": True,
                      "steps": [{"i": 1, "name": "tap"}, {"i": 2, "name": "save_day"}]})
    runs = client.get("/api/homework/runs").json()["runs"]
    assert [r["n_steps"] for r in runs] == [2, 0] and "steps" not in runs[0]
    one = client.get(f"/api/homework/runs?at={runs[0]['at']}").json()["runs"][0]
    assert [x["name"] for x in one["steps"]] == ["tap", "save_day"]
    old = client.get(f"/api/homework/runs?at={runs[1]['at']}").json()["runs"][0]
    assert old["steps"] == []
