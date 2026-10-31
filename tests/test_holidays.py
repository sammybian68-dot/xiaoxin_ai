"""法定节假日：2026 年用的是国务院公布的真实安排（server/assets/holidays/2026.json）。"""

from __future__ import annotations

import datetime as dt

import pytest

from server import clock, holidays

TZ = clock.TZ


@pytest.fixture
def at(monkeypatch):
    def set_(s):
        t = dt.datetime.fromisoformat(s).replace(tzinfo=TZ)
        monkeypatch.setattr(clock, "now", lambda: t)
        monkeypatch.setattr(clock.time, "time", lambda: t.timestamp())
        return t
    return set_


@pytest.mark.parametrize("day, work", [
    ("2026-10-01", False), ("2026-10-07", False),  # 国庆放假（周四、周三）
    ("2026-10-08", True),                          # 节后第一天
    ("2026-10-10", True),                          # 周六调休上班
    ("2026-10-11", False),                         # 普通周日
    ("2026-09-20", True),                          # 周日调休上班
    ("2026-09-25", False),                         # 中秋（周五）放假
])
def test_is_workday_2026(day, work):
    assert holidays.is_workday(dt.date.fromisoformat(day)) is work


def test_unknown_year_falls_back_to_mon_fri():
    assert not holidays.known(2031)
    assert holidays.is_workday(dt.date(2031, 3, 3)) and not holidays.is_workday(dt.date(2031, 3, 2))


def test_workday_alarm_skips_national_day(at, fakes):
    """「法定工作日 7 点」：9 月 30 日晚上设，跳过国庆 7 天，下次是 10 月 8 日；10 月 9 日之后是周六调休 10 日。"""
    at("2026-09-30 21:00")
    a = clock.add_alarm("07:00", "workday", say="起床了，卞依一")
    assert clock.next_fire(a).date() == dt.date(2026, 10, 8)
    assert clock.repeat_label(a["repeat"]) == "法定工作日"
    at("2026-10-09 08:00")
    assert clock.next_fire(a).date() == dt.date(2026, 10, 10)  # 周六调休，照响


def test_offday_alarm(at, fakes):
    at("2026-10-09 21:00")  # 周五晚
    a = clock.add_alarm("09:00", "offday", say="周末啦")
    assert clock.next_fire(a).date() == dt.date(2026, 10, 11)  # 10 日周六调休上班，不响；11 日周日响


def test_workday_alarm_rings_and_tool_says(at, fakes):
    from server.tools import clock as tools

    at("2026-10-08 06:59")
    a = clock.add_alarm("07:00", "workday", say="起床")
    at("2026-10-08 07:00:30")
    assert clock.snapshot()["alarms"][0]["ringing"] is not None
    out = tools.run("alarm_set", '{"time": "06:40", "repeat": "workday", "say": "上学了"}')
    assert "法定工作日" in out
