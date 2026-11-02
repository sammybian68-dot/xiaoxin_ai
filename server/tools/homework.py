"""班级作业：homework_report（看某天 / 某段时间的作业）。数据从 QQ 群抓来存在 server/homework.py，
这里只查；界面上会同时显示一张作业卡（static/homework.js）。说明在 server/prompts/homework.md。"""

from __future__ import annotations

import json
import re
import time

from server import homework

SPECS = [{"type": "function", "function": {
    "name": "homework_report",
    "description": "查班级作业（从 QQ 班级群抓好存着的）：某一天、某段时间的作业，按学科分好，带附件和群文件。"
                   "「今天的作业」「上周二的作业」「最近一周的作业」都用这个。",
    "parameters": {"type": "object", "properties": {
        "date": {"type": "string", "description": "哪一天，YYYY-MM-DD；「今天」不填"},
        "since": {"type": "string", "description": "一段时间的开始，YYYY-MM-DD（和 until 一起用，如最近一周）"},
        "until": {"type": "string", "description": "一段时间的结束，YYYY-MM-DD，不填是今天"},
    }},
}}]
NAMES = {"homework_report"}
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
    except ValueError:
        return "失败：参数不是 JSON 对象"
    since, until, date = (str(args.get(k) or "").strip() for k in ("since", "until", "date"))
    try:
        if since:
            recs = homework.range_days(since, until)
            miss = homework.missing(since, until)
            body = homework.describe(recs)
            days = [r["day"] for r in recs]
            tip = ("\n还没抓过的日子：" + "、".join(miss) + "（周末和没发作业的日子本来就没有）") if miss else ""
            return f"{since} 到 {until or '今天'}：有记录的 {len(days)} 天（{'、'.join(days) or '无'}）\n{body}{tip}"
        day = date or time.strftime("%Y-%m-%d")
        if not DAY.match(day):
            return f"失败：日期要写成 2026-11-02 这样：{day}"
        r = homework.get_day(day)
        if r is None:
            have = homework.days(8)
            return (f"{day} 没有作业记录（还没抓过，或者那天没发作业）。有记录的最近几天：" + "、".join(have)
                    + "\n播报：这一天我这儿没有作业记录。")
        return homework.describe([r])
    except homework.HomeworkError as e:
        return f"失败：{e}"
