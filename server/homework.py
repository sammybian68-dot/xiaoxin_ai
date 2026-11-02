"""班级作业：从 QQ 群「勤惜2024级3班」抓下来，按天按学科存着，永久留存，随时回看。

一天一个文件（DATA/homework/<YYYY-MM-DD>.json），按天幂等：同一天再抓一次就覆盖那天，不会出现两份。
一天里每个学科一条：正文、老师、要不要提交、附件（图片、群文件）、提到的文件名。

采集是 tools/qq_homework.py（pad 上的 QQ 是自绘界面，uiautomator 读不到正文，只能截图 + 视觉读，
见 docs/qq-homework.md）。这里只管存和查，小新的 homework_report 工具按这里的数据组织语言。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from server import store

SUBJECTS = {"chinese": "语文", "math": "数学", "english": "英语", "other": "其他"}
ORDER = ["chinese", "math", "english", "other"]
WEEK = "一二三四五六日"
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HomeworkError(ValueError):
    pass


def _dir() -> Path:
    return store.DATA / "homework"


def _check_day(day: str) -> str:
    day = str(day or "").strip()
    if not DAY.match(day):
        raise HomeworkError(f"日期要写成 2026-11-02 这样：{day}")
    return day


def save_day(day: str, items: list[dict], *, source: str = "qq", note: str = "") -> dict:
    """存一天的作业（覆盖这一天，按天幂等）。items 里每条：subject、teacher、text、submit、images、files、mentions。"""
    day = _check_day(day)
    out = []
    for it in items or []:
        sub = str(it.get("subject") or "other")
        if sub not in SUBJECTS:
            sub = next((k for k, v in SUBJECTS.items() if v == sub), "other")
        out.append({"subject": sub, "teacher": str(it.get("teacher") or ""), "text": str(it.get("text") or "").strip(),
                    "submit": str(it.get("submit") or ""), "title": str(it.get("title") or ""),
                    "images": [str(x) for x in it.get("images") or []],      # store 里的文件 id
                    "files": [dict(x) for x in it.get("files") or []],        # 群文件：{name, fid}
                    "mentions": [str(x) for x in it.get("mentions") or []],   # 正文里提到的文件名（去聊天记录找）
                    "notes": [str(x) for x in it.get("notes") or []],         # 批评、提醒这类附加段（老师原话）
                    "unsure": str(it.get("unsure") or "")[:200]})             # 采集时拿不准的地方（不显示，排查用）
    # 没有正文的条目丢掉：视觉模型有时看到学科色块就补一条空的（2026-11-03 实测 10/29、10/26 的「数学」
    # 其实那天没有数学作业）。它身上的附图并给当天第一条有正文的
    def _blank(it):
        t = re.sub(r"[（(]?看不清[）)]?|\s", "", it["text"])
        return not t  # 空的，或整条就是「（看不清）」：都算没抓到，不如不显示
    orphan = [f for it in out if _blank(it) for f in it["images"]]
    out = [it for it in out if not _blank(it)]
    if orphan and out:
        out[0]["images"] = list(dict.fromkeys(out[0]["images"] + orphan))
    out.sort(key=lambda x: ORDER.index(x["subject"]) if x["subject"] in ORDER else 9)
    rec = {"day": day, "week": WEEK[time.strptime(day, "%Y-%m-%d").tm_wday], "items": out,
           "source": source, "note": note, "collected": time.time()}
    _dir().mkdir(parents=True, exist_ok=True)
    store._write_json(_dir() / f"{day}.json", rec)
    return rec


def get_day(day: str) -> dict | None:
    p = _dir() / f"{_check_day(day)}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def days(limit: int = 400) -> list[str]:
    """有记录的日子，新的在前。"""
    return sorted((p.stem for p in _dir().glob("*.json") if DAY.match(p.stem)), reverse=True)[:limit]


def range_days(since: str, until: str = "") -> list[dict]:
    until = until or time.strftime("%Y-%m-%d")
    return [d for d in (get_day(x) for x in days() if since <= x <= until) if d]


def missing(since: str, until: str = "") -> list[str]:
    """这段里还没抓过的日子（采集脚本用，只抓缺的那几天）。"""
    until = until or time.strftime("%Y-%m-%d")
    t0 = time.mktime(time.strptime(_check_day(since), "%Y-%m-%d"))
    t1 = time.mktime(time.strptime(_check_day(until), "%Y-%m-%d"))
    have = set(days())
    out = []
    t = t0
    while t <= t1 + 1:
        d = time.strftime("%Y-%m-%d", time.localtime(t))
        if d not in have and d <= until:
            out.append(d)
        t += 86400
    return out


def describe(recs: list[dict], *, full: bool = True) -> str:
    """给小新看的文字：按日期、按学科列出来。full=False 只给一行概要（问「最近一周」时省 token）。"""
    if not recs:
        return "这段时间还没抓到作业记录"
    recs = sorted(recs, key=lambda r: r["day"], reverse=True)
    lines = []
    for r in recs:
        head = f"## {r['day']} 星期{r['week']}"
        if not r["items"]:
            lines.append(head + "：没有作业")
            continue
        lines.append(head)
        for it in r["items"]:
            name = SUBJECTS[it["subject"]] + (f"（{it['teacher']}）" if it["teacher"] else "")
            lines.append(f"- {name}" + (f"［{it['submit']}］" if it["submit"] else "") + "：")
            lines.append((it["text"] if full else it["text"][:60] + "…") or "（没抓到正文）")
            for n in it["notes"] if full else []:
                lines.append(f"  提醒：{n}")
            if it["images"]:
                lines.append(f"  附图 {len(it['images'])} 张（界面上能看）")
            for f in it["files"]:
                lines.append(f"  群文件：{f.get('name')}")
            for m in it["mentions"]:
                if not any(m == f.get("name") for f in it["files"]):
                    lines.append(f"  提到但没找到的文件：{m}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 采集运行记录（定时任务每跑一轮记一条）

def _runs_path() -> Path:
    return _dir() / "runs.jsonl"


def log_run(rec: dict) -> dict:
    """记一轮采集：什么时候、抓哪几天、成没成、存了哪几天、为什么结束、用了多久。只追加。"""
    rec = {"at": time.time(), **rec}
    _dir().mkdir(parents=True, exist_ok=True)
    with _runs_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def runs(limit: int = 30) -> list[dict]:
    try:
        lines = _runs_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(out))
