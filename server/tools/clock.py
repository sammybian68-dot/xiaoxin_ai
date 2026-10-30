"""闹钟 / 倒计时 / 正计时的工具。说明写在 server/prompts/clock.md，这里的 description 只写参数本身。"""

from __future__ import annotations

import datetime as dt
import json

from server import clock

_REPEAT = {
    "description": "once（默认，只响一次）/ daily / weekdays / weekends，或 1–7 的数组（1=周一）",
    "anyOf": [{"type": "string", "enum": ["once", "daily", "weekdays", "weekends"]},
              {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}}],
}
_LABEL = {"type": "string", "description": "名字，≤10 字，如「起床」「练琴」；没说就不填"}
_SAY = {"type": "string", "maxLength": 40, "description": "响铃时小新念的话，≤40 字，如「起床了，卞依一」「卞依一，该练琴了」"}


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


SPECS = [
    _fn("alarm_set", "新建闹钟。", {
        "time": {"type": "string", "description": "HH:MM，24 小时制"},
        "repeat": _REPEAT,
        "date": {"type": "string", "description": "YYYY-MM-DD，只用于 once 且不是最近一次该钟点时"},
        "label": _LABEL,
        "say": _SAY,
    }, ["time", "say"]),
    _fn("alarm_update", "改闹钟，只传要改的字段。enabled=false 是关掉但保留。", {
        "id": {"type": "string"}, "time": {"type": "string"}, "repeat": _REPEAT,
        "date": {"type": "string"}, "label": _LABEL, "say": _SAY, "enabled": {"type": "boolean"},
    }, ["id"]),
    _fn("alarm_delete", "删闹钟。", {
        "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    }, ["ids"]),
    _fn("alarm_ring", "处理正在响的闹钟：dismiss 关掉这次；snooze 过几分钟再响。", {
        "id": {"type": "string"},
        "action": {"type": "string", "enum": ["dismiss", "snooze"]},
        "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "description": "snooze 用，默认 5"},
    }, ["id", "action"]),
    _fn("timer_start", "新建倒计时。", {
        "seconds": {"type": "integer", "minimum": 1, "maximum": 86400}, "label": _LABEL, "say": _SAY,
    }, ["seconds", "say"]),
    _fn("timer_control", "操作倒计时。add 用 seconds 加减时间（负数是减）；cancel 也用来关掉已响的。", {
        "id": {"type": "string"},
        "action": {"type": "string", "enum": ["pause", "resume", "cancel", "add"]},
        "seconds": {"type": "integer"},
    }, ["id", "action"]),
    _fn("stopwatch", "正计时（只有一个）。start 从 0 开始；lap 记一圈。", {
        "action": {"type": "string", "enum": ["start", "pause", "resume", "reset", "lap"]},
    }, ["action"]),
]
NAMES = {s["function"]["name"] for s in SPECS}


def _alarm_line(a: dict) -> str:
    at = clock.next_fire(a)
    label = f"「{a['label']}」" if a.get("label") else ""
    return f"{a['id']} {a['time']} {clock.repeat_label(a['repeat'])}{label}，" + (
        f"下次 {clock.when_label(at)}" if at else "已关")


def run(name: str, arguments: str) -> str:
    """执行一个工具，返回给模型读的结果：成功「已…」+ 当前状态；失败「失败：原因」。"""
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    try:
        if name == "alarm_set":
            if not str(args.get("say") or "").strip():
                return "失败：缺 say（响铃时说什么），先问用户"
            a = clock.add_alarm(args.get("time"), args.get("repeat"), args.get("label") or "", args.get("date"),
                                args.get("say"))
            return "已设闹钟 " + _alarm_line(a) + f"，响时说「{clock.ring_say(a)}」"
        if name == "alarm_update":
            fields = {k: args.get(k) for k in ("time", "repeat", "date", "label", "say", "enabled") if k in args}
            return "已改闹钟 " + _alarm_line(clock.update_alarm(str(args.get("id")), **fields))
        if name == "alarm_delete":
            ids = [str(i) for i in args.get("ids") or []]
            gone = clock.delete_alarms(ids)
            return "已删闹钟 " + "；".join(f"{a['id']} {a['time']}" for a in gone)
        if name == "alarm_ring":
            ident = str(args.get("id"))
            if args.get("action") == "snooze":
                a = clock.snooze_alarm(ident, args.get("minutes") or clock.SNOOZE_MIN)
                at = dt.datetime.fromtimestamp(a["snooze_at"], clock.TZ)
                return f"已贪睡闹钟 {a['id']}，{at:%H:%M} 再响"
            a = clock.dismiss_alarm(ident)
            return f"已关掉闹钟 {a['id']} 这次响铃，" + _alarm_line(a).split("，", 1)[1]
        if name == "timer_start":
            if not str(args.get("say") or "").strip():
                return "失败：缺 say（响铃时说什么），先问用户"
            t = clock.start_timer(args.get("seconds"), args.get("label") or "", args.get("say"))
            end = dt.datetime.fromtimestamp(t["ends_at"], clock.TZ)
            return f"已开倒计时 {t['id']} {clock.fmt_duration(t['duration'])}，{end:%H:%M:%S} 响"
        if name == "timer_control":
            action = str(args.get("action"))
            t = clock.control_timer(str(args.get("id")), action, args.get("seconds"))
            if t is None:
                return f"已取消倒计时 {args.get('id')}"
            return f"已{ {'pause': '暂停', 'resume': '继续', 'add': '调整'}[action] }倒计时 {t['id']}，剩 " \
                   f"{clock.fmt_duration(clock.timer_remaining(t))}"
        if name == "stopwatch":
            action = str(args.get("action"))
            sw = clock.control_stopwatch(action)
            verb = {"start": "开始", "pause": "暂停", "resume": "继续", "reset": "清零", "lap": "记圈"}[action]
            extra = f"，第 {len(sw['laps'])} 圈 {clock.fmt_duration(sw['laps'][-1])}" if action == "lap" else ""
            return f"正计时已{verb}，现在 {clock.fmt_duration(clock.stopwatch_elapsed(sw))}{extra}"
    except clock.ClockError as e:
        return f"失败：{e}"
    return f"失败：没有工具 {name}"
