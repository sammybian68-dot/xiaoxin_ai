"""抓作业定时任务的判断：今天已有就不抓、pad 没连上记失败、最后一次还没抓到才告诉家长且一天只说一次。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tools import hw_schedule as hs


@pytest.fixture
def env(monkeypatch, tmp_path):
    st = {"have": [], "runs": [], "told": [], "adb": f"List of devices attached\n{hs.PAD}\tdevice\n", "agent_rc": 1,
          "now": "18:00", "agent_called": 0, "on": True, "weekdays": list(range(1, 8)), "max_tries": 4,
          "notify": True, "job": None}

    def fake_api(method, path, body=None, **kw):
        if path == "api/agent":
            return {"on": st["on"], "at": ["17:00", "18:00", "19:00", "20:30"], "weekdays": st["weekdays"],
                    "max_tries": st["max_tries"], "max_steps": 40, "notify": st["notify"]}
        if path == "api/agent/claim":
            return {"job": st["job"]}
        if path.startswith("api/homework?date="):
            return {"days": st["have"]}
        if path.startswith("api/homework/runs?"):
            return {"runs": list(reversed(st["runs"]))}
        if path == "api/homework/runs":
            st["runs"].append({**body, "at": __import__("time").time()})
            return {}
        if path == "api/homework/notify":
            st["told"].append(body["text"])
            return {"sent": ["k"]}
        raise AssertionError(path)

    def fake_run(cmd, **kw):
        if cmd[:2] == ["adb", "devices"]:
            return subprocess.CompletedProcess(cmd, 0, st["adb"], "")
        st["agent_called"] += 1
        return subprocess.CompletedProcess(cmd, st["agent_rc"], "× 这次没成：没找到这一天的作业\n", "")

    monkeypatch.setattr(hs, "api", fake_api)
    monkeypatch.setattr(hs.subprocess, "run", fake_run)
    monkeypatch.setattr(hs, "LOG", tmp_path)
    monkeypatch.setattr(hs, "now_hm", lambda: st["now"])
    monkeypatch.setattr(sys, "argv", ["hw_schedule"])
    return st


def test_already_have_today(env):
    env["have"] = [{"items": [1, 2]}]
    assert hs.main() == 0 and env["agent_called"] == 0


def test_success(env):
    env["agent_rc"] = 0
    assert hs.main() == 0 and env["agent_called"] == 1 and not env["told"]


def test_fail_early_no_alarm(env):
    """18:00 没抓到：老师可能还没发，不打扰人。"""
    assert hs.main() == 1 and not env["told"]


def test_fail_last_tells_once(env):
    env["now"] = "20:31"
    assert hs.main() == 1 and len(env["told"]) == 1 and "没抓到" in env["told"][0]
    assert hs.main() == 1 and len(env["told"]) == 1  # 一天只说一次


def test_pad_offline(env):
    env["adb"] = "List of devices attached\n"
    env["now"] = "20:31"
    assert hs.main() == 1 and env["agent_called"] == 0
    assert any("没连上" in r.get("why", "") for r in env["runs"]) and "没连上" in env["told"][0]


def test_off_switch(env):
    env["on"] = False
    assert hs.main() == 0 and env["agent_called"] == 0


def test_not_a_weekday(env):
    env["weekdays"] = [9]  # 今天肯定不在里面
    assert hs.main() == 0 and env["agent_called"] == 0


def test_max_tries(env):
    import time

    env["max_tries"] = 2
    env["runs"] = [{"trigger": "schedule", "at": time.time()}, {"trigger": "schedule", "at": time.time()}]
    assert hs.main() == 1 and env["agent_called"] == 0


def test_notify_can_be_off(env):
    env["now"] = "20:31"
    env["notify"] = False
    assert hs.main() == 1 and not env["told"]


def test_claim_runs_once(env, monkeypatch):
    """--watch：领到任务单就抓一次（这里只跑一轮循环体，不真的常驻）。"""
    env["job"] = {"day": "2026-11-03", "force": True}
    env["agent_rc"] = 0
    assert hs.once(env and {"max_steps": 40}, "now", "2026-11-03", True) == 0
    assert env["agent_called"] == 1
