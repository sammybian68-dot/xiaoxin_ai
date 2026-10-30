"""闹钟 / 倒计时 / 正计时：数据层、工具、HTTP 接口、模型调工具的整条路。时间用 freeze 固定。"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from server import clock, prompts
from server.tools import clock as tools

TZ = clock.TZ


@pytest.fixture
def at(monkeypatch):
    """把「现在」钉在某个北京时间：at("2026-10-30 21:00")。time.time() 和 clock.now() 一起改。"""
    def set_(s):
        t = dt.datetime.fromisoformat(s).replace(tzinfo=TZ)
        monkeypatch.setattr(clock, "now", lambda: t)
        monkeypatch.setattr(clock.time, "time", lambda: t.timestamp())
        return t
    return set_


# ---------------------------------------------------------------- 闹钟

def test_once_alarm_goes_to_next_occurrence(at, fakes):
    at("2026-10-30 21:00")  # 周五晚上
    a = clock.add_alarm("07:00", label="起床")
    assert a["date"] == "2026-10-31" and a["repeat"] == []
    assert clock.when_label(clock.next_fire(a)) == "明天 07:00"
    b = clock.add_alarm("22:30")
    assert b["date"] == "2026-10-30"


def test_once_alarm_turns_off_after_firing(at, fakes):
    at("2026-10-30 06:00")
    a = clock.add_alarm("07:00")
    at("2026-10-30 07:01")
    snap = clock.snapshot()
    assert snap["alarms"][0]["ringing"] is not None and snap["alarms"][0]["next"] is None  # 响铃窗口内
    at("2026-10-30 07:11")
    snap = clock.snapshot()
    assert snap["alarms"][0]["enabled"] is False and snap["alarms"][0]["ringing"] is None
    assert clock.next_fire(clock._load()["alarms"][0]) is None
    assert a["id"] == snap["alarms"][0]["id"]


def test_repeat_alarm(at, fakes):
    at("2026-10-30 21:00")  # 周五
    a = clock.add_alarm("07:00", "weekdays")
    assert clock.when_label(clock.next_fire(a)) == "周一 07:00"  # 跳过周末
    assert clock.repeat_label(a["repeat"]) == "工作日"
    b = clock.add_alarm("09:00", [6])
    assert clock.when_label(clock.next_fire(b)) == "明天 09:00"
    assert clock.repeat_label([1, 3, 5]) == "每周一、三、五"


@pytest.mark.parametrize("bad", ["7点", "24:00", "7:60", "", None])
def test_bad_time(fakes, bad):
    with pytest.raises(clock.ClockError):
        clock.add_alarm(bad)


def test_past_date_rejected(at, fakes):
    at("2026-10-30 21:00")
    with pytest.raises(clock.ClockError, match="过了"):
        clock.add_alarm("07:00", date="2026-10-30")


def test_update_and_delete(at, fakes):
    at("2026-10-30 21:00")
    a = clock.add_alarm("07:00", "daily")
    b = clock.update_alarm(a["id"], time="06:45", enabled=False)
    assert (b["time"], b["enabled"]) == ("06:45", False)
    c = clock.update_alarm(a["id"], repeat="once", enabled=True)
    assert c["repeat"] == [] and c["date"] == "2026-10-31"
    clock.delete_alarms([a["id"]])
    assert clock.snapshot()["alarms"] == []
    with pytest.raises(clock.ClockError, match="没有闹钟"):
        clock.delete_alarms([a["id"]])


# ---------------------------------------------------------------- 倒计时 / 正计时

def test_timer_lifecycle(at, fakes):
    at("2026-10-30 21:00:00")
    t = clock.start_timer(600, "刷牙")
    at("2026-10-30 21:02:00")
    p = clock.control_timer(t["id"], "pause")
    assert p["state"] == "paused" and p["remaining"] == 480
    at("2026-10-30 21:30:00")  # 暂停期间不走
    r = clock.control_timer(t["id"], "resume")
    assert clock.timer_remaining(r) == 480
    r = clock.control_timer(t["id"], "add", -180)
    assert clock.timer_remaining(r) == 300
    at("2026-10-30 21:35:01")
    assert clock.snapshot()["timers"][0]["state"] == "done"
    assert clock.control_timer(t["id"], "cancel") is None
    assert clock.snapshot()["timers"] == []


def test_done_timer_cleared_later(at, fakes):
    at("2026-10-30 21:00:00")
    clock.start_timer(60)
    at("2026-10-30 21:20:00")
    assert clock.snapshot()["timers"] == []


def test_stopwatch(at, fakes):
    at("2026-10-30 21:00:00")
    clock.control_stopwatch("start")
    at("2026-10-30 21:01:30")
    sw = clock.control_stopwatch("lap")
    assert sw["laps"] == [90]
    sw = clock.control_stopwatch("pause")
    at("2026-10-30 21:10:00")
    assert clock.stopwatch_elapsed(sw) == 90
    clock.control_stopwatch("resume")
    at("2026-10-30 21:10:10")
    assert clock.snapshot()["stopwatch"]["now_elapsed"] == 100
    assert clock.control_stopwatch("reset")["state"] == "idle"


# ---------------------------------------------------------------- 工具和 prompt

def test_tools_report_result_for_model(at, fakes):
    at("2026-10-30 21:00")
    out = tools.run("alarm_set", json.dumps({"time": "07:00", "label": "起床", "say": "依依起床啦"}))
    assert out.startswith("已设闹钟 a1 07:00 一次「起床」") and "明天 07:00" in out and out.endswith("响时说「依依起床啦」")
    assert tools.run("alarm_set", '{"time": "08:00"}') == "失败：缺 say（响铃时说什么），先问用户"
    assert tools.run("alarm_set", '{"time": "7点", "say": "x"}').startswith("失败：时间要 HH:MM")
    assert tools.run("alarm_delete", '{"ids": ["a1"]}') == "已删闹钟 a1 07:00"
    assert tools.run("timer_start", '{"seconds": 300}').startswith("失败：缺 say")
    assert tools.run("timer_start", '{"seconds": 300, "say": "泡面好了"}').startswith("已开倒计时 t2 5分钟，21:05:00 响")
    assert tools.run("timer_control", '{"id": "t2", "action": "cancel"}') == "已取消倒计时 t2"
    assert tools.run("stopwatch", '{"action": "start"}') == "正计时已开始，现在 0秒"
    assert tools.run("alarm_set", "not json") == "失败：参数不是 JSON 对象"


def test_prompt_contains_clock_state(at, fakes):
    at("2026-10-30 21:00")
    assert "## 当前状态\n无" in prompts.build()
    clock.add_alarm("07:00", "weekdays", "上学")
    clock.start_timer(600, "刷牙")
    text = prompts.build()
    assert "- a1 闹钟 07:00 工作日 「上学」，响时说「卞依一，时间到了！」，下次 周一 07:00" in text
    assert "- t2 倒计时 10分钟 「刷牙」，响时说「卞依一，时间到了！」，剩 10分钟" in text
    assert "默认提示「卞依一，时间到了！」" in text
    assert "{" not in text  # 占位符都填了
    assert "语音通话" not in text and "语音通话" in prompts.build(voice=True)


def test_specs_are_valid_function_schemas():
    from server.tools import registry

    names = [s["function"]["name"] for s in registry.SPECS]
    assert len(names) == len(set(names)) and "search_web" in names and "alarm_set" in names
    for s in registry.SPECS:
        assert s["type"] == "function" and s["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------- HTTP / 端到端

def test_clock_api_crud(client, at):
    at("2026-10-30 21:00")
    snap = client.post("/api/clock/alarms", json={"time": "07:00", "repeat": "daily"}).json()
    aid = snap["alarms"][0]["id"]
    assert snap["version"] == 1
    assert client.patch(f"/api/clock/alarms/{aid}", json={"enabled": False}).json()["alarms"][0]["enabled"] is False
    r = client.post("/api/clock/alarms", json={"time": "25:00"})
    assert r.status_code == 400 and "HH:MM" in r.json()["detail"]
    snap = client.post("/api/clock/timers", json={"seconds": 90, "label": "泡面"}).json()
    tid = snap["timers"][0]["id"]
    assert client.post(f"/api/clock/timers/{tid}/pause").json()["timers"][0]["state"] == "paused"
    assert client.post("/api/clock/stopwatch/start").json()["stopwatch"]["state"] == "running"
    assert client.delete(f"/api/clock/alarms/{aid}").json()["alarms"] == []
    assert client.get("/api/clock").json()["version"] == 6


def test_clock_long_poll_returns_on_change(client, fakes):
    import threading
    import time as _t

    v = client.get("/api/clock").json()["version"]
    threading.Timer(0.3, lambda: clock.start_timer(60)).start()
    t0 = _t.monotonic()
    snap = client.get(f"/api/clock?since={v}&wait=5").json()
    assert snap["version"] == v + 1 and _t.monotonic() - t0 < 3


def test_clock_api_requires_auth(anon):
    assert anon.get("/api/clock").status_code == 401
    assert anon.post("/api/clock/alarms", json={"time": "07:00"}).status_code == 401


def test_model_sets_alarm_via_tool(client, fakes, at):
    """模型调 alarm_set → 真的建了闹钟 → 结果喂回模型 → 模型确认。"""
    at("2026-10-30 21:00")
    fakes.llm_script = [("tool", "alarm_set", {"time": "07:00", "label": "起床", "say": "依依起床啦"}),
                        ("text", "好的，明天早上 7 点叫你")]
    cid = client.post("/api/conversations").json()["id"]
    body = client.post(f"/api/conversations/{cid}/messages", json={"text": "明早七点叫我起床"}).text
    assert "明天早上 7 点" in body
    assert clock.snapshot()["alarms"][0]["label"] == "起床"
    second = fakes.llm_requests[-1]
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("已设闹钟 a1 07:00")
    assert "- a1 闹钟 07:00" in second["system"]  # 第二次请求的 prompt 已经带上新闹钟
    assert any(t["function"]["name"] == "alarm_delete" for t in second["tools"])


def test_prompts_endpoint(client):
    d = client.get("/api/prompts").json()
    assert [p["name"] for p in d["parts"]] == ["persona", "time", "search", "clock", "voice"]
    assert d["parts"][3]["text"].startswith("# 闹钟") and "{clock_state}" in d["parts"][3]["text"]
    assert "现在是 20" in d["system"] and "语音通话" in d["system_voice"]
    assert {"alarm_set", "timer_control", "stopwatch", "search_web", "query_time"} <= {t["name"] for t in d["tools"]}


def test_query_time(at, fakes):
    from server.tools import registry

    at("2026-10-30 20:25:07")
    assert registry.run_local("query_time", "{}") == "现在 2026-10-30 20:25:07 星期五（北京时间）"
    out = registry.run_local("query_time", '{"until": "07:00"}')
    assert out.endswith("到 明天 07:00 还有 10小时34分53秒")


def test_model_answers_time_via_tool(client, fakes, at):
    at("2026-10-30 20:25:07")
    fakes.llm_script = [("tool", "query_time", {}), ("text", "现在是晚上 8 点 25 分")]
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "现在几点"})
    assert fakes.llm_requests[-1]["messages"][-1]["content"].startswith("现在 2026-10-30 20:25:07")


def test_snooze_and_dismiss(at, fakes):
    at("2026-10-30 06:00")
    a = clock.add_alarm("07:00")
    at("2026-10-30 07:00:30")  # 在响
    assert clock.snapshot()["alarms"][0]["ringing"] is not None
    clock.snooze_alarm(a["id"], 5)
    snap = clock.snapshot()  # 这次停了，贪睡排在 5 分钟后
    assert snap["alarms"][0]["ringing"] is None
    nxt = dt.datetime.fromtimestamp(snap["alarms"][0]["next"], TZ)
    assert f"{nxt:%H:%M:%S}" == "07:05:30"
    at("2026-10-30 07:05:40")
    assert clock.snapshot()["alarms"][0]["ringing"] is not None  # 贪睡到点又响
    b = clock.dismiss_alarm(a["id"])
    assert "snooze_at" not in b and b["enabled"] is False and clock.next_fire(b) is None


def test_repeat_alarm_dismiss_keeps_enabled(at, fakes):
    at("2026-10-30 06:59")
    a = clock.add_alarm("07:00", "daily")
    at("2026-10-30 07:00:10")
    assert "正在响" in clock.summary()
    b = clock.dismiss_alarm(a["id"])
    assert clock.snapshot()["alarms"][0]["ringing"] is None and "正在响" not in clock.summary()
    assert b["enabled"] is True and clock.when_label(clock.next_fire(b)) == "明天 07:00"


def test_chat_records_tool_actions(client, fakes, at):
    at("2026-10-30 21:00")
    fakes.llm_script = [("tool", "timer_start", {"seconds": 600, "label": "刷牙", "say": "该刷牙啦"}), ("text", "好，10 分钟后提醒你")]
    cid = client.post("/api/conversations").json()["id"]
    body = client.post(f"/api/conversations/{cid}/messages", json={"text": "十分钟后提醒我刷牙"}).text
    assert "event: tool" in body
    rec = client.get(f"/api/conversations/{cid}").json()["messages"][-1]
    assert rec["actions"][0]["name"] == "timer_start" and rec["actions"][0]["result"].startswith("已开倒计时")


def test_alarm_ring_tool(at, fakes):
    at("2026-10-30 06:59")
    a = clock.add_alarm("07:00", "weekdays", "上学")
    at("2026-10-30 07:00:20")
    out = tools.run("alarm_ring", json.dumps({"id": a["id"], "action": "snooze", "minutes": 10}))
    assert out == "已贪睡闹钟 a1，07:10 再响"
    at("2026-10-30 07:10:05")
    out = tools.run("alarm_ring", json.dumps({"id": a["id"], "action": "dismiss"}))
    assert out == "已关掉闹钟 a1 这次响铃，下次 周一 07:00"


def test_ring_text_default_and_per_item(client, fakes):
    assert client.get("/api/clock").json()["ring_text"] == "卞依一，时间到了！"
    v = client.get("/api/clock").json()["version"]
    snap = client.put("/api/clock/ring_text", json={"text": "依依，时间到啦"}).json()
    assert snap["ring_text"] == "依依，时间到啦" and snap["version"] == v + 1
    assert client.put("/api/clock/ring_text", json={"text": ""}).status_code == 400
    snap = client.post("/api/clock/alarms", json={"time": "07:00"}).json()  # 网页手动建，没填：用默认
    assert snap["alarms"][0]["say_text"] == "依依，时间到啦"
    snap = client.post("/api/clock/timers", json={"seconds": 60, "say": "泡面好了"}).json()
    assert snap["timers"][0]["say_text"] == "泡面好了"
    assert client.get("/api/cue/ring").status_code == 200
    assert client.get("/api/cue/ring", params={"text": "泡面好了"}).status_code == 200
    said = [c[-1] for c in fakes.calls if c[0] == "doubao.tts"]
    assert "依依，时间到啦" in said and "泡面好了" in said
    assert client.get("/api/cue/ring", params={"text": "长" * 41}).status_code == 400


def test_history_replays_tool_calls(client, fakes, at):
    """上一轮调过的工具按原样放进历史：assistant(tool_calls) → tool → assistant(正文)。"""
    at("2026-10-30 21:00")
    fakes.llm_script = [("tool", "alarm_set", {"time": "07:00", "say": "起床了，卞依一"}), ("text", "好的"),
                        ("text", "还有别的吗")]
    cid = client.post("/api/conversations").json()["id"]
    client.post(f"/api/conversations/{cid}/messages", json={"text": "明早七点叫我"})
    client.post(f"/api/conversations/{cid}/messages", json={"text": "谢谢"})
    msgs = fakes.llm_requests[-1]["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant", "user"]
    call = msgs[1]["tool_calls"][0]
    assert call["function"]["name"] == "alarm_set" and "卞依一" in call["function"]["arguments"]
    assert msgs[2]["tool_call_id"] == call["id"] and msgs[2]["content"].startswith("已设闹钟")
