"""闹钟、倒计时、正计时。数据在服务端（XIAOXIN_DATA/clock.json），网页、pad、模型的工具调用改的是同一份；
pad 原生服务长轮询 /api/clock?since=<version> 拿变化，本机存一份副本排程、到点响铃（断网也响）。

时间一律北京时间。闹钟按「几点几分 + 重复哪几天」存，一次性的闹钟带日期，响过（时间过了）自动关掉。
倒计时存结束时刻（epoch 秒），暂停时存剩余秒数；到点后状态是 done，等人关掉（或 10 分钟后自动清掉）。
正计时只有一个：running 时 elapsed = 已累计 + (现在 - 开始)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time

from server import store

TZ = dt.timezone(dt.timedelta(hours=8))
MAX_ALARMS = 20
MAX_TIMERS = 10
MAX_TIMER_S = 24 * 3600
DONE_KEEP_S = 600          # 响完没人关的倒计时，这么久后自动清掉
RING_WINDOW_S = 600        # 闹钟到点后这么久内没人关，都算「正在响」（pad 最多响 5 分钟）
WEEK = "一二三四五六日"     # repeat 里 1..7 = 周一..周日

_lock = threading.Lock()
_changed = threading.Condition(_lock)


class ClockError(ValueError):
    pass


# 响铃时小新用当前音色念的话。每个闹钟 / 倒计时自己的存在 say 里（语音设的由模型填，必填）；
# 没有（网页上手动建的、留空的）就用默认提示，默认提示在设置里改。
RING_TEXT = "卞依一，时间到了！"
MAX_RING_TEXT = 40


def ring_text() -> str:
    return store.settings().get("ring_text") or RING_TEXT


def _check_say(text) -> str:
    text = str(text or "").strip()
    if len(text) > MAX_RING_TEXT:
        raise ClockError(f"响铃时说的话最多 {MAX_RING_TEXT} 个字")
    return text


def set_ring_text(text) -> str:
    text = _check_say(text)
    if not text:
        raise ClockError("默认响铃提示不能空")
    with _lock:
        store.update_settings(ring_text=text)
        _save(_load())  # 版本号加一，pad 马上拉到新文字、重新下载语音
    return text


def ring_say(item: dict | None = None) -> str:
    """某个闹钟 / 倒计时响时实际念的话。"""
    return (item or {}).get("say") or ring_text()


def _path():
    return store.DATA / "clock.json"


def _empty() -> dict:
    return {"version": 0, "seq": 0, "alarms": [], "timers": [],
            "stopwatch": {"state": "idle", "started_at": None, "elapsed": 0.0, "laps": []}}


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    base = _empty()
    base.update(data)
    return base


def _save(data: dict) -> None:
    data["version"] += 1
    store._write_json(_path(), data)
    _changed.notify_all()


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


# ---------------------------------------------------------------- 闹钟

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_time(value) -> str:
    m = _TIME.match(str(value or "").strip())
    if not m:
        raise ClockError(f"时间要 HH:MM（24 小时制），收到 {value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_repeat(value) -> list[int]:
    if value in (None, "", "once"):
        return []
    presets = {"daily": [1, 2, 3, 4, 5, 6, 7], "weekdays": [1, 2, 3, 4, 5], "weekends": [6, 7]}
    if isinstance(value, str):
        if value in presets:
            return presets[value]
        raise ClockError(f"repeat 不认识：{value!r}")
    try:
        days = sorted({int(d) for d in value})
    except (TypeError, ValueError):
        raise ClockError(f"repeat 要 1–7 的数组，收到 {value!r}") from None
    if any(not 1 <= d <= 7 for d in days):
        raise ClockError("repeat 的数字是 1–7（周一到周日）")
    return days


def _parse_date(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ClockError(f"date 要 YYYY-MM-DD，收到 {value!r}") from None


def next_fire(alarm: dict, after: dt.datetime | None = None) -> dt.datetime | None:
    """下次响的时刻；关着的、一次性且已经过了的返回 None。正在「再睡一会」的，先响贪睡那一次。"""
    after = after or now()
    snooze = alarm.get("snooze_at")
    if snooze and snooze > after.timestamp():
        return dt.datetime.fromtimestamp(snooze, TZ)
    if not alarm.get("enabled", True):
        return None
    h, m = map(int, alarm["time"].split(":"))
    if not alarm["repeat"]:
        if alarm.get("date"):
            d = dt.date.fromisoformat(alarm["date"])
            at = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
            return at if at > after else None
        at = after.replace(hour=h, minute=m, second=0, microsecond=0)
        return at if at > after else at + dt.timedelta(days=1)
    for k in range(8):
        day = (after + dt.timedelta(days=k)).replace(hour=h, minute=m, second=0, microsecond=0)
        if day.isoweekday() in alarm["repeat"] and day > after:
            return day
    return None


def ringing_at(alarm: dict, at: dt.datetime | None = None) -> float | None:
    """正在响的那一次的时刻（epoch 秒）：到点不到 RING_WINDOW_S、还没被关掉或贪睡。不在响返回 None。"""
    at = at or now()
    t, done = at.timestamp(), alarm.get("dismissed") or 0
    snooze = alarm.get("snooze_at")
    if snooze and snooze <= t < snooze + RING_WINDOW_S and snooze > done:
        return snooze
    if not alarm.get("enabled", True):
        return None
    h, m = map(int, alarm["time"].split(":"))
    for back in (0, 1):
        day = (at - dt.timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        if alarm["repeat"]:
            ok = day.isoweekday() in alarm["repeat"]
        else:
            ok = alarm.get("date") == day.date().isoformat()
        ts = day.timestamp()
        if ok and ts <= t < ts + RING_WINDOW_S and ts > done:
            return ts
    return None


def repeat_label(days: list[int]) -> str:
    if not days:
        return "一次"
    if days == [1, 2, 3, 4, 5, 6, 7]:
        return "每天"
    if days == [1, 2, 3, 4, 5]:
        return "工作日"
    if days == [6, 7]:
        return "周末"
    return "每周" + "、".join(WEEK[d - 1] for d in days)


def when_label(at: dt.datetime | None, ref: dt.datetime | None = None) -> str:
    """「今天 07:00」「明天 07:00」「周三 07:00」「11-05 07:00」。"""
    if at is None:
        return "不会响"
    ref = ref or now()
    days = (at.date() - ref.date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(days)
    if day is None:
        day = f"周{WEEK[at.isoweekday() - 1]}" if days < 7 else f"{at:%m-%d}"
    return f"{day} {at:%H:%M}"


def _expire_alarms(data: dict) -> bool:
    """一次性闹钟响完（过了响铃窗口）就关掉；过期的贪睡清掉。返回有没有改动。"""
    changed = False
    t = now()
    for a in data["alarms"]:
        if a.get("snooze_at") and a["snooze_at"] + RING_WINDOW_S <= t.timestamp():
            a.pop("snooze_at")
            changed = True
        if a["enabled"] and not a["repeat"] and a.get("date"):
            h, m = map(int, a["time"].split(":"))
            d = dt.date.fromisoformat(a["date"])
            if dt.datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp() + RING_WINDOW_S <= t.timestamp():
                a["enabled"] = False
                changed = True
    return changed


def _new_id(data: dict, prefix: str) -> str:
    data["seq"] += 1
    return f"{prefix}{data['seq']}"


def add_alarm(time_: str, repeat=None, label: str = "", date=None, say: str = "") -> dict:
    hhmm, days, day, say = _parse_time(time_), _parse_repeat(repeat), _parse_date(date), _check_say(say)
    with _lock:
        data = _load()
        if len(data["alarms"]) >= MAX_ALARMS:
            raise ClockError(f"闹钟最多 {MAX_ALARMS} 个，先删掉几个")
        a = {"id": _new_id(data, "a"), "time": hhmm, "repeat": days, "label": str(label or "").strip()[:20],
             "say": say, "enabled": True, "created": time.time()}
        if not days:
            # 一次性：没给日期就定在下一个这个钟点，存成具体日期，响过就自动关
            at = next_fire({**a, "date": day}) if day else next_fire(a)
            if at is None:
                raise ClockError(f"{day} {hhmm} 已经过了")
            a["date"] = at.date().isoformat()
        data["alarms"].append(a)
        _save(data)
        return a


def _find(items: list[dict], ident: str, what: str) -> dict:
    for it in items:
        if it["id"] == ident:
            return it
    raise ClockError(f"没有{what} {ident}")


def update_alarm(ident: str, **fields) -> dict:
    with _lock:
        data = _load()
        a = _find(data["alarms"], ident, "闹钟")
        if fields.get("time") is not None:
            a["time"] = _parse_time(fields["time"])
        if fields.get("label") is not None:
            a["label"] = str(fields["label"]).strip()[:20]
        if fields.get("say") is not None:
            a["say"] = _check_say(fields["say"])
        if "repeat" in fields and fields["repeat"] is not None:
            a["repeat"] = _parse_repeat(fields["repeat"])
        if fields.get("enabled") is not None:
            a["enabled"] = bool(fields["enabled"])
        if a["repeat"]:
            a.pop("date", None)
        elif a["enabled"]:
            day = _parse_date(fields.get("date"))
            a.pop("date", None)
            at = next_fire({**a, "date": day}) if day else next_fire(a)
            if at is None:
                raise ClockError(f"{day} {a['time']} 已经过了")
            a["date"] = at.date().isoformat()
        _save(data)
        return a


SNOOZE_MIN = 5


def snooze_alarm(ident: str, minutes=SNOOZE_MIN) -> dict:
    """响铃时「再睡一会」：这次先停，几分钟后再响一次，不改闹钟本身。"""
    try:
        mins = int(minutes or SNOOZE_MIN)
    except (TypeError, ValueError):
        raise ClockError("minutes 要数字") from None
    if not 1 <= mins <= 60:
        raise ClockError("再睡 1–60 分钟")
    with _lock:
        data = _load()
        a = _find(data["alarms"], ident, "闹钟")
        a["dismissed"] = ringing_at(a) or time.time()
        a["snooze_at"] = time.time() + mins * 60
        _save(data)
        return a


def dismiss_alarm(ident: str) -> dict:
    """响铃时「关掉」：取消贪睡；一次性的闹钟顺便关掉。"""
    with _lock:
        data = _load()
        a = _find(data["alarms"], ident, "闹钟")
        a["dismissed"] = ringing_at(a) or time.time()
        a.pop("snooze_at", None)
        if not a["repeat"] and next_fire(a) is None:
            a["enabled"] = False
        _save(data)
        return a


def delete_alarms(ids: list[str]) -> list[dict]:
    with _lock:
        data = _load()
        gone = [a for a in data["alarms"] if a["id"] in ids]
        missing = set(ids) - {a["id"] for a in gone}
        if missing:
            raise ClockError("没有闹钟 " + "、".join(sorted(missing)))
        data["alarms"] = [a for a in data["alarms"] if a["id"] not in ids]
        _save(data)
        return gone


# ---------------------------------------------------------------- 倒计时

def timer_remaining(t: dict, at: float | None = None) -> float:
    if t["state"] == "paused":
        return t["remaining"]
    return max(0.0, t["ends_at"] - (at or time.time()))


def _settle_timers(data: dict) -> bool:
    """到点的标成 done；done 太久的清掉。"""
    changed, t0 = False, time.time()
    keep = []
    for t in data["timers"]:
        if t["state"] == "running" and t["ends_at"] <= t0:
            t["state"] = "done"
            changed = True
        if t["state"] == "done" and t0 - t["ends_at"] > DONE_KEEP_S:
            changed = True
            continue
        keep.append(t)
    data["timers"] = keep
    return changed


def start_timer(seconds, label: str = "", say: str = "") -> dict:
    say = _check_say(say)
    try:
        secs = int(round(float(seconds)))
    except (TypeError, ValueError):
        raise ClockError(f"seconds 要数字，收到 {seconds!r}") from None
    if not 1 <= secs <= MAX_TIMER_S:
        raise ClockError("倒计时 1 秒到 24 小时")
    with _lock:
        data = _load()
        _settle_timers(data)
        if len([t for t in data["timers"] if t["state"] != "done"]) >= MAX_TIMERS:
            raise ClockError(f"倒计时最多同时 {MAX_TIMERS} 个")
        t = {"id": _new_id(data, "t"), "label": str(label or "").strip()[:20], "say": say, "duration": secs,
             "state": "running", "ends_at": time.time() + secs, "remaining": float(secs), "created": time.time()}
        data["timers"].append(t)
        _save(data)
        return t


def control_timer(ident: str, action: str, seconds=None) -> dict | None:
    """pause / resume / cancel / add（加减秒数，负数是减）/ dismiss（关掉响完的）。cancel、dismiss 返回 None。"""
    with _lock:
        data = _load()
        _settle_timers(data)
        t = _find(data["timers"], ident, "倒计时")
        t0 = time.time()
        if action in ("cancel", "dismiss"):
            data["timers"].remove(t)
            _save(data)
            return None
        if t["state"] == "done":
            raise ClockError(f"倒计时 {ident} 已经响完了，只能关掉（cancel）")
        if action == "pause":
            if t["state"] == "running":
                t["remaining"], t["state"] = timer_remaining(t, t0), "paused"
        elif action == "resume":
            if t["state"] == "paused":
                t["ends_at"], t["state"] = t0 + t["remaining"], "running"
        elif action == "add":
            try:
                delta = int(round(float(seconds)))
            except (TypeError, ValueError):
                raise ClockError("add 要给 seconds") from None
            left = timer_remaining(t, t0) + delta
            if not 1 <= left <= MAX_TIMER_S:
                raise ClockError("加减之后要在 1 秒到 24 小时之间")
            t["duration"] = max(1, t["duration"] + delta)
            if t["state"] == "paused":
                t["remaining"] = left
            else:
                t["ends_at"] = t0 + left
        else:
            raise ClockError(f"倒计时不支持 {action}")
        _save(data)
        return t


# ---------------------------------------------------------------- 正计时

def stopwatch_elapsed(sw: dict, at: float | None = None) -> float:
    if sw["state"] == "running":
        return sw["elapsed"] + (at or time.time()) - sw["started_at"]
    return sw["elapsed"]


def control_stopwatch(action: str) -> dict:
    """start（从 0 开始）/ pause / resume / reset / lap。"""
    with _lock:
        data = _load()
        sw, t0 = data["stopwatch"], time.time()
        if action == "start":
            sw.update(state="running", started_at=t0, elapsed=0.0, laps=[])
        elif action == "pause":
            if sw["state"] == "running":
                sw.update(state="paused", elapsed=stopwatch_elapsed(sw, t0), started_at=None)
        elif action == "resume":
            if sw["state"] == "paused":
                sw.update(state="running", started_at=t0)
            elif sw["state"] == "idle":
                sw.update(state="running", started_at=t0, elapsed=0.0, laps=[])
        elif action == "reset":
            sw.update(state="idle", started_at=None, elapsed=0.0, laps=[])
        elif action == "lap":
            if sw["state"] == "idle":
                raise ClockError("正计时没在走")
            sw["laps"] = (sw["laps"] + [round(stopwatch_elapsed(sw, t0), 2)])[-99:]
        else:
            raise ClockError(f"正计时不支持 {action}")
        _save(data)
        return sw


# ---------------------------------------------------------------- 读

def snapshot() -> dict:
    """完整状态，给网页和 pad。每个闹钟带 next（epoch 秒，不响是 null）。"""
    with _lock:
        data = _load()
        if _expire_alarms(data) | _settle_timers(data):
            _save(data)
        t0 = time.time()
        alarms = []
        for a in sorted(data["alarms"], key=lambda a: a["time"]):
            at = next_fire(a)
            alarms.append({**a, "next": at.timestamp() if at else None, "ringing": ringing_at(a),
                           "repeat_label": repeat_label(a["repeat"]), "say_text": ring_say(a)})
        timers = [{**t, "left": round(timer_remaining(t, t0), 1), "say_text": ring_say(t)} for t in data["timers"]]
        sw = {**data["stopwatch"], "now_elapsed": round(stopwatch_elapsed(data["stopwatch"], t0), 2)}
        return {"version": data["version"], "now": t0, "alarms": alarms, "timers": timers, "stopwatch": sw,
                "ring_text": ring_text()}


def wait_change(since: int, timeout: float) -> dict:
    """长轮询：版本号比 since 新就马上返回，否则最多等 timeout 秒。"""
    deadline = time.monotonic() + timeout
    with _lock:
        while _load()["version"] <= since:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            _changed.wait(left)
    return snapshot()


def fmt_duration(secs: float) -> str:
    s = int(round(secs))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分" + (f"{s}秒" if s else "")
    if m:
        return f"{m}分" + (f"{s}秒" if s else "钟")
    return f"{s}秒"


def summary() -> str:
    """给模型看的当前状态，一行一个，带 id。没有就写「无」。"""
    snap = snapshot()
    lines = []
    for a in snap["alarms"]:
        at = dt.datetime.fromtimestamp(a["next"], TZ) if a["next"] else None
        state = f"下次 {when_label(at)}" if at else "关"
        if a["ringing"]:
            state = "正在响"
        lines.append(f"- {a['id']} 闹钟 {a['time']} {a['repeat_label']}"
                     + (f" 「{a['label']}」" if a["label"] else "") + f"，响时说「{a['say_text']}」，{state}")
    for t in snap["timers"]:
        state = {"running": f"剩 {fmt_duration(t['left'])}", "paused": f"暂停，剩 {fmt_duration(t['left'])}",
                 "done": "已响，待关"}[t["state"]]
        lines.append(f"- {t['id']} 倒计时 {fmt_duration(t['duration'])}"
                     + (f" 「{t['label']}」" if t["label"] else "") + f"，响时说「{t['say_text']}」，{state}")
    sw = snap["stopwatch"]
    if sw["state"] != "idle":
        lines.append(f"- 正计时 {fmt_duration(sw['now_elapsed'])}，{'在走' if sw['state'] == 'running' else '暂停'}")
    return "\n".join(lines) or "无"
