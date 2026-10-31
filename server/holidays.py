"""中国法定节假日和调休：某天是不是「法定工作日」。数据是国务院每年公布的安排（holiday-cn，MIT）。

  server/assets/holidays/<年>.json   随代码带的（现在有 2026）
  XIAOXIN_DATA/holidays/<年>.json     服务端自己拉的新年份（每天看一次 GitHub，拉到就用；拉不到不影响）
没有某年数据时按普通周一到周五算工作日，返回时 known=False，闹钟卡片上会提示「X 年安排还没公布」。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path

from server import store

log = logging.getLogger("xiaoxin.holidays")
BUNDLED = Path(__file__).parent / "assets" / "holidays"
URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
_lock = threading.Lock()
_cache: dict[int, dict | None] = {}


def _load(year: int) -> dict | None:
    """{日期: (名字, 是不是放假)}；这一年没公布返回 None。"""
    with _lock:
        if year in _cache:
            return _cache[year]
        days = None
        for d in (store.DATA / "holidays", BUNDLED):
            p = d / f"{year}.json"
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if raw.get("days"):
                days = {x["date"]: (x["name"], bool(x["isOffDay"])) for x in raw["days"]}
                break
        _cache[year] = days
        return days


def known(year: int) -> bool:
    return _load(year) is not None


def is_workday(day: dt.date) -> bool:
    """法定工作日：调休上班的周末算，放假的工作日不算；这一年没数据就按周一到周五。"""
    days = _load(day.year) or {}
    hit = days.get(day.isoformat())
    if hit is not None:
        return not hit[1]
    return day.isoweekday() <= 5


def holiday_name(day: dt.date) -> str:
    hit = (_load(day.year) or {}).get(day.isoformat())
    return hit[0] if hit else ""


def refresh(years: list[int] | None = None) -> list[int]:
    """去 GitHub 拉还没有的年份（今年和明年）。拉到的返回年份列表。网络不通就算了。"""
    import requests

    got = []
    this = dt.date.today().year
    for y in years or [this, this + 1]:
        if known(y):
            continue
        try:
            r = requests.get(URL.format(year=y), timeout=10)
            raw = r.json() if r.ok else {}
        except Exception as e:  # 网络不通、不是 JSON
            log.info("holidays %d not fetched: %s", y, e)
            continue
        if not raw.get("days"):
            continue
        d = store.DATA / "holidays"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{y}.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with _lock:
            _cache.pop(y, None)
        got.append(y)
        log.info("holidays %d fetched: %d days", y, len(raw["days"]))
    return got


def start_refresher() -> None:
    def loop():
        while True:
            try:
                refresh()
            except Exception as e:
                log.warning("holidays refresh: %s", e)
            time.sleep(24 * 3600)
    threading.Thread(target=loop, name="holidays", daemon=True).start()
