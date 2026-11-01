"""练习模式的工具：start_practice（孩子说「开始练习」）、practice_report（家长问今天练得怎么样）。说明在 server/prompts/practice.md。
开始后的计时、听写朗读、完成、拍照批改都在 pad 的练习界面里走（server/practice.py、static/practice.js），不再经过小新。"""

from __future__ import annotations

import json

from server import practice, worksheet
from server.tools.printer import ctx

SPECS = [
    {"type": "function", "function": {
        "name": "start_practice",
        "description": "开始练习一份卷子：pad 进入练习模式（全屏计时；有听写的小新先一个词一个词读，每个读两遍、每遍后等 5 秒；"
                       "读完孩子自己写剩下的，到时间提醒，可以超时接着写，点「完成」后拍照交卷，小新批改发群里）。",
        "parameters": {"type": "object", "properties": {
            "worksheet": {"type": "string", "description": "练习纸 id（w_…）；不确定填 latest（最近一份）"},
            "minutes": {"type": "number", "description": "限时几分钟（孩子或家长说了才填；不填按题量估）"},
        }},
    }},
    {"type": "function", "function": {
        "name": "practice_report",
        "description": "查某天的练习记录：每份卷子什么时候做的、用时、限时、有没有超时、对错和错字、第几次练。家长问「今天练得怎么样」时用。",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD，不填是今天"},
        }},
    }},
]
NAMES = {"start_practice", "practice_report"}


def run(name: str, arguments: str) -> str:
    from server.family import members

    try:
        args = json.loads(arguments or "{}") or {}
    except ValueError:
        return "失败：参数不是 JSON 对象"
    c = ctx.get()
    kid_id = c.get("kid", "")
    if not kid_id:
        return "失败：这段对话不是哪个孩子的，开不了练习"
    try:
        kid = members.get(kid_id)
    except KeyError:
        return "失败：找不到这个孩子"
    if name == "practice_report":
        parents = [m["name"] for m in members.all_members() if m["role"] == "parent" and m["status"] == "active"]
        return (practice.report(kid_id, str(args.get("date") or ""))
                + ("\n（发给家长：开头写 " + " ".join("@" + n for n in parents) + "）" if parents else ""))
    ref = str(args.get("worksheet") or "latest").strip()
    if ref.startswith("w_"):
        wid = ref[2:]
    elif c.get("made"):
        wid = c["made"]
    else:
        ws = worksheet.latest(kid_id)
        if ws is None:
            return "失败：还没有出过卷子，先出一份"
        wid = ws["id"]
    try:
        minutes = float(args["minutes"]) if args.get("minutes") else None
        rec = practice.start(wid, kid, minutes=minutes, by=c.get("by", ""))
    except (practice.PracticeError, ValueError) as e:
        return f"失败：{e}"
    from server.family import hub

    hub.publish(kid["thread"], {"type": "practice", "practice": rec})
    n = len(rec["listen"])
    first = (f"先听写，一共 {n} 个词，每个词读两遍。准备好笔，" if n else "")
    return (f"练习开始了（p_{rec['id']}）：「{rec['title']}」，限时 {practice.mmss(rec['limit_s'])}，第 {rec['attempt']} 次练。"
            f"pad 上是练习界面，计时、听写朗读、完成、交卷都在那里。\n播报：好，开始练习！{first}限时 {practice.mmss(rec['limit_s'])}。")
