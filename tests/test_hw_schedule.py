"""抓作业定时器（server/hw_schedule.py，跑在小新服务里）的判断：到点才抓、一个时刻只触发一次、
今天有了不抓、开关和抓取日、次数上限、「立即抓取」、最后一次没抓到才告诉家长且一天只说一次。
真正去抓（hw_agent.grab）换成假的。"""

from __future__ import annotations

import time

import pytest

from server import homework, hw_schedule as hs, store


@pytest.fixture
def env(client, monkeypatch):
    st = {"now": "17:00", "wd": time.localtime().tm_wday + 1, "grab_ok": False, "grabs": []}

    def fake_grab(day, *, trigger="manual", **kw):
        st["grabs"].append((day, trigger))
        homework.log_run({"since": day, "until": day, "ok": st["grab_ok"], "why": "没找到" if not st["grab_ok"] else "",
                          "trigger": trigger})
        if st["grab_ok"]:
            homework.save_day(day, [{"subject": "chinese", "text": "抄词"}])
        return {"ok": st["grab_ok"], "saved": [day] if st["grab_ok"] else [], "rejected": {}, "why": "没找到" if not st["grab_ok"] else "", "secs": 1}

    from server import hw_agent

    monkeypatch.setattr(hw_agent, "grab", fake_grab)
    monkeypatch.setattr(hs, "now_hm", lambda: st["now"])
    store.update_settings(agent={"on": True, "at": ["17:00", "18:00", "20:30"], "weekdays": list(range(1, 8)),
                                 "max_tries": 3, "notify": True, "max_steps": 40})
    return st


def told(kid_thread):
    return [m for m in store.get(kid_thread)["messages"] if m.get("via") == "homework"]


def kid():
    from server.family import members

    return members.kids()[0]


def test_not_due_yet(env):
    env["now"] = "16:59"
    assert hs.tick(set()) == "还没到点" and not env["grabs"]


def test_due_fires_once_per_slot(env):
    fired = set()
    assert hs.tick(fired).startswith("17:00") and env["grabs"] == [(hs.today(), "schedule")]
    assert hs.tick(fired) == "还没到点" and len(env["grabs"]) == 1   # 同一个时刻不再触发
    env["now"] = "18:00"
    assert hs.tick(fired).startswith("18:00") and len(env["grabs"]) == 2


def test_missed_slot_too_late(env):
    env["now"] = "17:45"   # 服务在 17:00 前后挂了：错过超过 30 分钟就不补这个点
    assert hs.tick(set()) == "还没到点"


def test_have_today_skips(env):
    homework.save_day(hs.today(), [{"subject": "math", "text": "x"}])
    assert "已经有了" in hs.tick(set()) and not env["grabs"]


def test_off_and_weekday(env):
    store.update_settings(agent={**store.settings()["agent"], "on": False})
    assert hs.tick(set()) == "自动抓关着"
    store.update_settings(agent={**store.settings()["agent"], "on": True,
                                 "weekdays": [d for d in range(1, 8) if d != env["wd"]]})
    assert hs.tick(set()) == "今天不是抓取日"


def test_max_tries(env):
    for _ in range(3):
        homework.log_run({"since": hs.today(), "until": hs.today(), "ok": False, "trigger": "schedule"})
    assert "次数用完" in hs.tick(set()) and not env["grabs"]


def test_tell_parents_only_at_last_and_once(env):
    t = kid()["thread"]
    fired = set()
    hs.tick(fired)                       # 17:00 没抓到：还有机会，不说
    assert not told(t)
    env["now"] = "20:30"
    hs.tick(fired)                       # 最后一个时刻也没抓到：说一次
    assert len(told(t)) == 1 and "没抓到" in told(t)[0]["text"]
    env["now"] = "20:40"
    hs.maybe_tell(hs.cfg(), "又没抓到")   # 一天只说一次
    assert len(told(t)) == 1


def test_notify_off(env):
    store.update_settings(agent={**store.settings()["agent"], "notify": False})
    env["now"] = "20:30"
    hs.tick(set())
    assert not told(kid()["thread"])


def test_run_now(env, client):
    env["grab_ok"] = True
    from server.agent_bus import bus

    bus.last_seen = time.time()          # pad 在线
    env["now"] = "10:00"                 # 不在时刻表里也能立即抓
    assert client.post("/api/agent/run", json={}).json()["started"] is True
    assert hs.tick(set()).startswith("立即抓取") and env["grabs"] == [(hs.today(), "now")]
    assert hs.tick(set()) == "还没到点"   # 单子领过就没了
    again = client.post("/api/agent/run", json={}).json()
    assert again["started"] is False and "已经抓过" in again["why"]


def test_run_now_pad_offline(env, client):
    from server.agent_bus import bus

    bus.last_seen = 0
    r = client.post("/api/agent/run", json={}).json()
    assert r["started"] is False and "不在线" in r["why"]
