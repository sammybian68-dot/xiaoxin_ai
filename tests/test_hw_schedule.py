"""抓作业定时任务的判断：今天已有就不抓、pad 没连上记失败、最后一次还没抓到才告诉家长且一天只说一次。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tools import hw_schedule as hs


@pytest.fixture
def env(monkeypatch, tmp_path):
    st = {"have": [], "runs": [], "told": [], "adb": f"List of devices attached\n{hs.PAD}\tdevice\n", "agent_rc": 1,
          "now": "18:00", "agent_called": 0}

    def fake_api(method, path, body=None, **kw):
        if path == "api/agent":
            return {"at": ["17:00", "18:00", "19:00", "20:30"]}
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
