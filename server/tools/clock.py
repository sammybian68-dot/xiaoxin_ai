"""闹钟 / 倒计时 / 正计时的工具。说明写在 server/prompts/clock.md，这里的 description 只写参数本身。"""

from __future__ import annotations

import datetime as dt
import json

from server import clock

_REPEAT = {
    "description": "once（默认，只响一次）/ daily 每天 / weekdays 周一到周五 / weekends 周六日 / "
                   "workday 法定工作日（跳过节假日，调休上班的周末照响）/ offday 法定节假日和周末；或 1–7 的数组（1=周一）",
    "anyOf": [{"type": "string", "enum": ["once", "daily", "weekdays", "weekends", "workday", "offday"]},
              {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}}],
}
_ID = {"type": "string", "description": "闹钟 id（如 a3），或序号（「第三个」就传 3，按 alarm_list 的顺序）"}
_LABEL = {"type": "string", "description": "名字，≤10 字，如「起床」「练琴」；没说就不填"}
_SAY = {"type": "string", "maxLength": 40, "description": "响铃时小新念的话，≤40 字，如「起床了，卞依一」「卞依一，该练琴了」"}


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}}


SPECS = [
    _fn("alarm_list", "列出所有闹钟（按时间排序、带序号），屏幕上弹出闹钟清单卡片，可以在卡片上直接改。", {}),
    _fn("alarm_set", "新建闹钟。", {
        "time": {"type": "string", "description": "HH:MM，24 小时制"},
        "repeat": _REPEAT,
        "date": {"type": "string", "description": "YYYY-MM-DD，只用于 once 且不是最近一次该钟点时"},
        "label": _LABEL,
        "say": _SAY,
    }, ["time", "say"]),
    _fn("alarm_update", "改闹钟，只传要改的字段。enabled=false 是关掉但保留。", {
        "id": _ID, "time": {"type": "string"}, "repeat": _REPEAT,
        "date": {"type": "string"}, "label": _LABEL, "say": _SAY, "enabled": {"type": "boolean"},
    }, ["id"]),
    _fn("alarm_delete", "删闹钟。", {
        "ids": {"type": "array", "items": _ID, "minItems": 1},
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


def ordered() -> list[dict]:
    """闹钟清单的顺序（序号就按这个）：按建的先后，不按时刻——改了时间序号不变，
    不然「第 3 个改成 7 点」之后用户眼里的「第 2 个」就换人了（测出来的坑）。"""
    return sorted(clock.snapshot()["alarms"], key=lambda a: (a.get("created", 0), a["id"]))


def resolve_id(ident) -> str:
    """「a3」原样；「3」「第三个」→ 清单里第 3 个的 id。对不上抛 ClockError。"""
    s = str(ident or "").strip()
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    t = s.removeprefix("第").removesuffix("个")
    n = int(t) if t.isdigit() else cn.get(t)
    if n is not None and not s.startswith("a"):
        items = ordered()
        if not 1 <= n <= len(items):
            raise ClockError(f"只有 {len(items)} 个闹钟，没有第 {n} 个")
        return items[n - 1]["id"]
    return s


ClockError = clock.ClockError


def _spoken_time(hhmm: str) -> str:
    """「07:00」→「早上 7 点」，「21:30」→「晚上 9 点半」：播报用 12 小时制口语。"""
    h, m = map(int, hhmm.split(":"))
    per = "凌晨" if h < 5 else "早上" if h < 9 else "上午" if h < 12 else "中午" if h < 13 else "下午" if h < 18 else "晚上"
    h12 = 0 if h == 0 else h % 12 or 12  # 半夜 0:15 说「凌晨 0 点 15 分」
    mm = "" if m == 0 else "半" if m == 30 else f" {m} 分"
    return f"{per} {h12} 点{mm}"


def _spoken_when(a: dict) -> str:
    """播报里说「什么时候响」：重复的说规律，一次性的说哪天。"""
    rep = a["repeat"]
    t = _spoken_time(a["time"])
    if rep == "workday":
        return f"法定工作日{t}"
    if rep == "offday":
        return f"节假日和周末{t}"
    if rep:
        return f"{clock.repeat_label(rep)}{t}"
    at = clock.next_fire(a)
    day = clock.when_label(at).split(" ")[0] if at else ""
    return f"{day}{t}"


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
            return ("已设闹钟 " + _alarm_line(a) + f"，响时说「{clock.ring_say(a)}」\n"
                    f"播报：好的，{_spoken_when(a)}叫你。")
        if name == "alarm_list":
            items = ordered()
            if not items:
                return "已列出闹钟：没有闹钟\n播报：现在没有闹钟。"
            lines = [f"{i + 1}. {_alarm_line(a)}，响时说「{clock.ring_say(a)}」" for i, a in enumerate(items)]
            on = sum(1 for a in items if a["next"])
            return (f"已列出闹钟（屏幕上有清单卡片，用户说「第 N 个」就按这个序号）：\n" + "\n".join(lines)
                    + f"\n播报：一共 {len(items)} 个闹钟，{on} 个开着，都列在屏幕上了。")
        if name == "alarm_update":
            fields = {k: args.get(k) for k in ("time", "repeat", "date", "label", "say", "enabled") if k in args}
            ident = resolve_id(args.get("id"))
            a = clock.update_alarm(ident, **fields)
            what = "关掉了" if fields.get("enabled") is False else f"改好了，{_spoken_when(a)}响"
            return "已改闹钟 " + _alarm_line(a) + f"，响时说「{clock.ring_say(a)}」\n播报：好的，闹钟{what}。"
        if name == "alarm_delete":
            ids = [resolve_id(i) for i in args.get("ids") or []]
            gone = clock.delete_alarms(ids)
            say = f"{_spoken_when(gone[0])}的闹钟" if len(gone) == 1 else f"{len(gone)} 个闹钟"
            return "已删闹钟 " + "；".join(f"{a['id']} {a['time']}" for a in gone) + f"\n播报：好的，删掉了{say}。"
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
