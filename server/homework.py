"""班级作业：从 QQ 群「勤惜2024级3班」抓下来，按天按学科存着，永久留存，随时回看。

一天一个文件（DATA/homework/<YYYY-MM-DD>.json），按天幂等：同一天再抓一次就覆盖那天，不会出现两份。
一天里每个学科一条：正文、老师、要不要提交、附件（图片、群文件）、提到的文件名。

采集是 server/hw_agent.py（小新服务自己跑，模型看屏、pad 上的无障碍服务执行：QQ 是自绘界面，
uiautomator 读不到正文，只能截图 + 视觉读，见 docs/qq-homework.md）。这里只管存和查，
小新的 homework_report 工具按这里的数据组织语言。
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
                    "revised": bool(it.get("revised")),                       # 这一科是老师发的【修改】版
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


def _norm(t: str) -> str:
    """比较用：去掉空白、标点、换行，全角半角统一。重读同一段作业时，这些地方最容易读得不一样。"""
    import unicodedata

    t = unicodedata.normalize("NFKC", str(t or ""))
    return re.sub(r"[\s\W_]+", "", t)


def changed_enough(old: str, new: str, *, ratio: float = 0.12, chars: int = 6) -> bool:
    """两份正文是不是「真的改了」，而不是重读的误差。代码比，不让模型比（模型比不稳）。

    去掉空白和标点后，差的字数既超过 chars 个、又超过较长那份的 ratio，才算改了。
    重读一般只差个标点或一两个字（实测「4☆P52」读成「4⊕P52」），老师改作业是整句增删。"""
    import difflib

    a, b = _norm(old), _norm(new)
    if a == b:
        return False
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    diff = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    return diff > chars and diff > ratio * max(len(a), len(b))


def merge_day(day: str, items: list[dict], *, source: str = "qq", note: str = "",
              verify: bool = False) -> tuple[dict, list[str]]:
    """按学科增量合并：已经存好的学科**不动**，只补新出现的学科。返回 (合并后的这一天, 这次新增 / 更新了哪几科)。

    老师改了作业的两种情况：
      - 群里重新分享了一张带【修改】的卡：那一科 revised=True，直接替换。
      - verify=True（截止前最后一轮，把每科正文都重读了一遍）：和库里比，**代码判断**改得够多才替换
        （changed_enough）；只差个标点、一两个字，当成重读的误差，留着原来那份。
    替换的那一科标 revised，作业卡上显示「已修改」。

    为什么不整天覆盖：老师下午陆续发（实测语文常常最后到，10/23 是 17:32），截止前要看好几次；
    整天覆盖等于每次把读对的再读一遍，重读还可能读错盖掉对的。"""
    old = get_day(day)
    have = {it["subject"]: it for it in (old or {}).get("items") or []}
    changed = []
    for it in items:
        sub = str(it.get("subject") or "other")
        prev = have.get(sub)
        if prev is not None:
            # 这科已经有了：代码比对，改得够多才替换。增量模式下模型标了 revised（看到【修改】卡）也得过这一关——
            # 实测它会把没改过的科目也标 revised、或者读了一半就交（「1.小册第10课」），不能光信它的标记。
            # 核对模式（verify）一样走比对。差得少的当成重读的误差，留着原来那份
            if (it.get("revised") or verify) and changed_enough(prev.get("text", ""), it.get("text", "")):
                it = {**it, "revised": True}
            elif not it.get("revised") and not verify and changed_enough(prev.get("text", ""), it.get("text", "")) \
                    and len(_norm(it.get("text", ""))) > len(_norm(prev.get("text", ""))):
                # 增量模式没标 revised，但重读出来比库里多出一大截（库里那份当时被截断了 / 老师补了内容没重新分享）：
                # 只在「变长」时才收，变短不收（变短多半是这次没读全）
                it = {**it, "revised": True}
            else:
                continue                               # 没改：不动（附图也不动）
            if not it.get("images"):
                it = {**it, "images": prev.get("images") or []}   # 这次没重抓图：留着原来的图
        have[sub] = it
        changed.append(sub)
    if old and not changed:
        return old, []
    rec = save_day(day, list(have.values()), source=source, note=note)
    return rec, changed


def subjects(day: str) -> list[str]:
    r = get_day(day)
    return [it["subject"] for it in (r or {}).get("items") or []]


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
            name = SUBJECTS[it["subject"]] + (f"（{it['teacher']}）" if it["teacher"] else "") + ("［老师改过］" if it.get("revised") else "")
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


MAX_RUNS = 60     # runs.jsonl 里留最近几轮（每轮带几十张缩略图，别无限长）


def log_run(rec: dict) -> dict:
    """记一轮采集：什么时候、抓哪几天、成没成、存了哪几天、为什么结束、用了多久，以及每一步做了什么
    （steps：动作、参数、结果，和那一步的小截图——「作业 agent」面板按这个一步步回看）。只追加，超了裁掉最早的。"""
    rec = {"at": time.time(), **rec}
    _dir().mkdir(parents=True, exist_ok=True)
    with _runs_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:  # 裁掉最早的几轮（带缩略图，文件会很大）
        lines = _runs_path().read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_RUNS:
            _runs_path().write_text("\n".join(lines[-MAX_RUNS:]) + "\n", encoding="utf-8")
    except OSError:
        pass
    return rec


def runs(limit: int = 30, *, with_steps: bool = False) -> list[dict]:
    """最近几轮，新的在前。默认不带 steps（列表只要概要，一轮几十张缩略图太大），要看某一轮再单独取。"""
    try:
        lines = _runs_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        st = r.get("steps")
        if not isinstance(st, list):
            st = []          # 老记录里 steps 是个数字（截图张数），不是逐步记录
        if not with_steps:
            r = {**{k: v for k, v in r.items() if k != "steps"}, "n_steps": len(st)}
        else:
            r = {**r, "steps": st}
        out.append(r)
    return list(reversed(out))


def run_at(at: float) -> dict | None:
    """某一轮的完整记录（含 steps）。at 是它的时间戳，同一秒有多轮时取最晚的那条
    （面板是按 at 请求的，秒级精度足够；同秒只会出现在测试里）。"""
    best = None
    for r in runs(MAX_RUNS, with_steps=True):
        if abs(float(r.get("at") or 0) - at) < 1.0 and (best is None or r.get("at", 0) >= best.get("at", 0)):
            best = r
    return best
