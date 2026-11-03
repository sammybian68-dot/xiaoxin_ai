"""服务端 ↔ pad 的动作总线：下发、领走、回结果；pad 不在线要超时报错，不能把 agent 卡死。"""

from __future__ import annotations

import threading

import pytest

from server.agent_bus import Bus, PadOffline


def test_call_roundtrip():
    bus = Bus()
    got = {}

    def pad():
        job = bus.next(wait=5)
        got.update(job)
        bus.done(job["id"], {"ok": True, "out": "点了", "w": 2560, "h": 1600})

    t = threading.Thread(target=pad)
    t.start()
    r = bus.call("tap", {"x": 10, "y": 20}, claim_timeout=5, run_timeout=5)
    t.join()
    assert got["name"] == "tap" and got["args"] == {"x": 10, "y": 20}
    assert r["out"] == "点了" and bus.online()


def test_pad_offline_times_out():
    bus = Bus()
    with pytest.raises(PadOffline, match="没来领"):
        bus.call("tap", {}, claim_timeout=0.3)
    assert bus.next(wait=0.1) == {}   # 超时的动作不会留在队列里被 pad 回头领走


def test_claimed_but_no_result():
    bus = Bus()
    threading.Thread(target=lambda: bus.next(wait=5)).start()
    with pytest.raises(PadOffline, match="没回结果"):
        bus.call("tap", {}, claim_timeout=5, run_timeout=0.3)


def test_next_returns_empty_when_idle():
    bus = Bus()
    assert bus.next(wait=0.1) == {} and bus.online()
