"""下棋工具：play_go（开一盘 / 接着下）、go_stats（看板）、go_review（复盘某天的棋）。
工具只给文字结果，界面按工具名出卡片（static/go.js）：play_go → 「开一盘」卡，go_stats → 看板，go_review → 选一盘 / 直接打开。
说明在 server/prompts/go.md。"""

from __future__ import annotations

import datetime as dt
import json

from server.games import go, registry
from server.tools import printer as printer_tools

SPECS = [
    {"type": "function", "function": {
        "name": "play_go",
        "description": "和孩子下围棋（19 路）：在屏幕上出「开一盘」的卡片，孩子点开始就进全屏棋盘。",
        "parameters": {"type": "object", "properties": {
            "level": {"type": "string", "enum": [i for i, _ in go.LEVELS],
                      "description": "小新的棋力：world 世界冠军、china 中国冠军、9d…1d 段、1k…20k 级。孩子没说就不填（用设置里的默认）"},
            "color": {"type": "string", "enum": ["B", "W"], "description": "孩子执黑 B 还是执白 W；没说不填"},
            "handicap": {"type": "integer", "description": "让子数 2–9；没说不填"},
        }},
    }},
    {"type": "function", "function": {
        "name": "go_stats",
        "description": "孩子最近下围棋的战绩：总胜率、对各档位的胜负、最近几盘、坏棋多不多。屏幕上出看板。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "看最近几天，默认 30"},
        }},
    }},
    {"type": "function", "function": {
        "name": "go_review",
        "description": "复盘孩子和小新下过的围棋：屏幕上打开复盘（胜率曲线、哪几手下坏了、AI 建议、讲解）。某天下了好几盘就列出来让孩子选。",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "哪一天，YYYY-MM-DD；「刚才那盘」「最近一盘」不填"},
        }},
    }},
]
NAMES = {"play_go", "go_stats", "go_review"}


def _kid() -> str:
    return printer_tools.ctx.get().get("kid", "")


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    kid = _kid()
    if not kid:
        return "失败：这里不能下棋（不在孩子的聊天里）"
    if name == "play_go":
        return _play(kid, args)
    if name == "go_stats":
        return _stats(kid, int(args.get("days") or 30))
    return _review(kid, str(args.get("date") or "").strip())


def _play(kid: str, args: dict) -> str:
    cfg = registry.settings("go")
    lv = args.get("level") or cfg["level"]
    if lv not in go.LEVEL_NAME:
        return f"失败：没有这个档位 {lv}"
    color = args.get("color") or cfg["color"]
    cur = go.current(kid)
    if cur:   # 暂停着的那盘：屏幕上直接打开接着下（界面看到这个 id 就开）
        return (f"上一盘还没下完（对 {go.LEVEL_NAME[cur['level']]}，下到第 {len(cur['moves'])} 手），已在屏幕上打开接着下。"
                f"要开新的，得先在棋盘上认输。[go:{cur['id']}]\n播报：上次那盘还没下完，我们接着下。")
    return (f"已在屏幕上出了开局卡片：对手 {go.LEVEL_NAME[lv]}，你执{'黑' if color == 'B' else '白'}"
            + (f"，让 {args['handicap']} 子" if args.get("handicap") else "") + "。\n"
            "播报：好呀，点「开始」就来一盘。")


def _stats(kid: str, days: int) -> str:
    from server.games.api import stats

    s = stats(kid, max(1, min(days, 3650)))
    if not s["games"]:
        return f"最近 {days} 天还没和小新下过棋。\n播报：最近还没和我下过棋呢，要来一盘吗？"
    lv = "；".join(f"对 {b['name']} {b['won']} 胜 {b['lost']} 负" for b in s["by_level"]) or "还没有下完的"
    rate = f"{s['rate']:.0%}" if s["rate"] is not None else "-"
    return (f"最近 {days} 天下完 {s['total']} 盘：{s['won']} 胜 {s['lost']} 负，胜率 {rate}。{lv}。屏幕上出了看板。\n"
            f"播报：最近下完 {s['total']} 盘，赢了 {s['won']} 盘，详细的在屏幕上。")


def _review(kid: str, date: str) -> str:
    games = [g for g in go.all_games(kid) if any(m[0] == g["kid_color"] for m in g["moves"])]   # 孩子下过就能复盘
    if date:
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            return f"失败：日期要写成 2026-11-03 这样：{date}"
        games = [g for g in games if dt.date.fromtimestamp(g["created"]) == day]
        if not games:
            return f"{date} 没有下过棋。\n播报：那天没和我下过棋。"
    elif games:
        games = games[:1]
    else:
        return "还没有能复盘的棋。\n播报：还没有下过能复盘的棋。"
    if len(games) == 1:
        g = games[0]
        return (f"已在屏幕上打开复盘：{dt.datetime.fromtimestamp(g['created']):%m-%d %H:%M} 对 {go.LEVEL_NAME[g['level']]}，"
                f"{(g.get('result') or {}).get('text', '没下完')}。[go:{g['id']}]\n播报：复盘打开了，看看哪几手可以下得更好。")
    lines = [f"{dt.datetime.fromtimestamp(g['created']):%H:%M} 对 {go.LEVEL_NAME[g['level']]}，{(g.get('result') or {}).get('text', '没下完')}"
             for g in games]
    ids = "".join(f"[go:{g['id']}]" for g in games)
    return (f"{date} 下了 {len(games)} 盘，屏幕上列出来了，让孩子点要复盘哪一盘：" + "；".join(lines) + ids
            + f"\n播报：那天下了 {len(games)} 盘，你点一下要看哪盘。")
