"""练习模式：孩子拿到卷子说「开始练习」→ 计时；有听写的先听写（小新每个词读两遍、每遍后等 5 秒，pad 上念）→
「默写结束了，你自己完成吧，还剩 xx 分钟」→ 到点报「时间到了」，可以接着写（显示超时多少）→ 点「完成」→
拍卷子上传 → 看图模型对着答案批改 → 群里发结果 @爸爸 @妈妈。

每次练习一条记录（DATA/practice/<id>.json）：哪份卷子、开始 / 听写结束 / 完成时间、限时、超时、第几次练、批改结果。
家长问「今天练得怎么样」，小新调 practice_report，按这些记录汇总。英语听力等以后的练习也走这一套（listen 换成别的内容）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from server import store, worksheet

log = logging.getLogger(__name__)
LISTEN_GAP_S = 5          # 每遍读完等几秒
LISTEN_PER_WORD_S = 14    # 估一个词要多久（两遍 + 两次等待），算限时用
GRADE_MODELS = ["claude-sonnet-5-5", "deepseek-v4.1-flash"]
_lock = threading.Lock()


class PracticeError(ValueError):
    pass


def _dir() -> Path:
    return store.DATA / "practice"


def _save(rec: dict) -> dict:
    _dir().mkdir(parents=True, exist_ok=True)
    store._write_json(_dir() / f"{rec['id']}.json", rec)
    return rec


def get(pid: str) -> dict:
    p = _dir() / f"{store._check(pid)}.json"
    if not p.is_file():
        raise KeyError(pid)
    return json.loads(p.read_text(encoding="utf-8"))


def all_for(kid: str) -> list[dict]:
    out = []
    for p in _dir().glob("*.json") if _dir().is_dir() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("kid") == kid:
            out.append(r)
    return sorted(out, key=lambda r: r["started"])


def listen_words(ws: dict) -> list[dict]:
    return [{"no": i + 1, "word": it["a"]} for i, it in enumerate(it for it in ws["items"] if it.get("part") == "listen")]


def default_limit_s(ws: dict) -> int:
    """限时：听写按词数估；自己写的部分按题量估（填空一个空 30 秒、口算一道 6 秒、写词一个 20 秒、作文 40 分钟），
    写的部分至少 5 分钟（没有听写的卷子至少 10 分钟），取整到分钟。"""
    from server.exercises.chinese.word_dictation import BLANK

    items = ws["items"]
    listen = sum(1 for it in items if it.get("part") == "listen") * LISTEN_PER_WORD_S
    if ws["kind"] == "dictation":
        blanks = sum(len(BLANK.findall(line)) for it in items if it.get("part") == "write" for line in it["lines"])
        write = max(300, blanks * 30) if blanks else 0
    elif ws["kind"] == "math":
        write = max(600, len(items) * 6)
    elif ws["kind"] == "essay":
        write = 2400
    else:
        write = max(600, len(items) * 20)
    return -(-(listen + write) // 60) * 60


def _same_kind(a: dict, ws: dict) -> bool:
    """算「第几次练」：同一份卷子，或同一题型同一标题（同一单元再出一张也算同一种练习）。"""
    return a.get("worksheet") == ws["id"] or (a.get("topic") == ws.get("topic") and a.get("title") == ws["title"])


def start(ws_id: str, kid: dict, *, minutes: float | None = None, by: str = "") -> dict:
    try:
        ws = worksheet.get(ws_id)
    except KeyError:
        raise PracticeError("没有这份练习纸")
    with _lock:
        for r in all_for(kid["id"]):  # 同一时间只练一份：没完成的旧练习算放弃
            if r["state"] in ("listening", "writing"):
                r.update(state="abandoned", finished=time.time())
                _save(r)
        attempt = 1 + sum(1 for r in all_for(kid["id"]) if _same_kind(r, ws) and r["state"] not in ("abandoned",))
        limit = int(minutes * 60) if minutes else default_limit_s(ws)
        words = listen_words(ws)
        rec = {"id": store._new_id(), "kid": kid["id"], "thread": kid["thread"], "worksheet": ws["id"], "topic": ws.get("topic"),
               "title": ws["title"], "started": time.time(), "limit_s": max(60, min(limit, 3 * 3600)), "attempt": attempt,
               "listen": words, "state": "listening" if words else "writing", "listen_end": None if words else time.time(),
               "finished": None, "answers": [], "grade": None, "by": by}
        return _save(rec)


def listen_done(pid: str) -> dict:
    with _lock:
        r = get(pid)
        if r["state"] == "listening":
            r.update(state="writing", listen_end=time.time())
            _save(r)
        return r


def finish(pid: str) -> dict:
    with _lock:
        r = get(pid)
        if r["state"] in ("listening", "writing"):
            now = time.time()
            r.update(state="done", finished=now, listen_end=r.get("listen_end") or now)
            _save(r)
        return r


def used_s(r: dict) -> int:
    return int((r.get("finished") or time.time()) - r["started"])


def overtime_s(r: dict) -> int:
    return max(0, used_s(r) - r["limit_s"])


def mmss(s: int) -> str:
    m, s = divmod(int(s), 60)
    return f"{m} 分 {s} 秒" if m and s else f"{m} 分钟" if m else f"{s} 秒"


def finish_text(r: dict, kid_name: str) -> str:
    over = overtime_s(r)
    return (f"{kid_name}做完了「{r['title']}」，用时 {mmss(used_s(r))}（限时 {mmss(r['limit_s'])}"
            + (f"，超时 {mmss(over)}" if over else "，没超时") + f"，第 {r['attempt']} 次练）。拍照把卷子传上来，小新来批改。")


# ---------------------------------------------------------------- 批改

GRADE_PROMPT = """你是小学老师，批改孩子手写的卷子。下面是标准答案（JSON），后面是孩子卷子的照片（可能好几张）。
逐题对照：孩子写的字和答案一个字一个字比，写错字、少写、多写、没写都算错；字迹潦草但能认出是对的字算对。看不清的写「看不清」。
只输出一个 JSON，不要别的字：
{"items":[{"no":"题号或题目","want":"答案","got":"孩子写的（没写就空）","ok":true/false}],"right":对了几个,"total":一共几个,"comment":"一两句给孩子的话，先夸再说要注意的"}
听写的每个词算一题（no 用题号）；古诗、积累的每个空算一题（no 用诗题或句子开头几个字）；口算每道算一题。"""


def answer_key(ws: dict) -> list[dict]:
    from server.exercises.chinese.word_dictation import BLANK

    out = []
    for it in ws["items"]:
        if it.get("part") == "listen":
            out.append({"no": it["q"].rstrip("."), "want": it["a"]})
        elif it.get("part") == "write":
            for line in it["lines"]:
                for b in BLANK.findall(line):
                    out.append({"no": it["q"] or BLANK.sub("__", line)[:8], "want": b})
        else:
            out.append({"no": it["q"], "want": it["a"]})
    return out


def _parse_grade(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise PracticeError("批改结果看不懂")
    g = json.loads(m.group(0))
    items = [x for x in g.get("items") or [] if isinstance(x, dict)]
    right = sum(1 for x in items if x.get("ok"))
    return {"items": items, "right": right, "total": len(items) or int(g.get("total") or 0),
            "comment": str(g.get("comment") or "")[:200]}


def grade(pid: str, file_ids: list[str]) -> dict:
    """看图模型对着答案批改。同步跑（几十秒），调用方放线程里。"""
    from src.llm import client

    r = get(pid)
    ws = worksheet.get(r["worksheet"])
    parts = [{"type": "text", "text": GRADE_PROMPT + "\n\n标准答案：\n" + json.dumps(answer_key(ws), ensure_ascii=False)}]
    for fid in file_ids:
        img = store.best_image(fid)
        if img:
            parts.append({"type": "image_url", "image_url": {"url": store.data_url(img, "image/jpeg")}})
    if len(parts) == 1:
        raise PracticeError("没收到卷子照片")
    rep = client.chat_full(GRADE_MODELS, [{"role": "user", "content": parts}], max_tokens=6000, timeout=180)
    g = _parse_grade(rep.text)
    g["model"] = rep.model
    with _lock:
        r = get(pid)
        r.update(answers=file_ids, grade=g, state="graded", graded=time.time())
        _save(r)
    return r


def grade_text(r: dict, kid_name: str, parents: list[str]) -> str:
    g = r["grade"]
    wrong = [x for x in g["items"] if not x.get("ok")]
    over = overtime_s(r)
    head = " ".join(f"@{p}" for p in parents)
    lines = [f"{head} {kid_name}的「{r['title']}」批改好了：对了 {g['right']} / {g['total']}。".strip(),
             f"用时 {mmss(used_s(r))}（限时 {mmss(r['limit_s'])}，" + (f"超时 {mmss(over)}" if over else "没超时")
             + f"），第 {r['attempt']} 次练。"]
    if wrong:
        lines.append("错的：" + "；".join(f"{x.get('no')}. 应为「{x.get('want')}」" + (f"，写成了「{x['got']}」" if x.get("got") else "，没写")
                                          for x in wrong[:20]) + ("……" if len(wrong) > 20 else ""))
    else:
        lines.append("全对 🎉")
    if g.get("comment"):
        lines.append(g["comment"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 当天汇总（家长问「今天练得怎么样」）

def report(kid: str, day: str = "") -> str:
    """某天（YYYY-MM-DD，默认今天）的练习：每份卷子开始时间、用时、限时、超时、对错、第几次练。给小新组织语言。"""
    day = day or time.strftime("%Y-%m-%d")
    recs = [r for r in all_for(kid) if time.strftime("%Y-%m-%d", time.localtime(r["started"])) == day]
    if not recs:
        return f"{day} 没有练习记录"
    lines = [f"{day} 一共 {len(recs)} 次练习："]
    for r in recs:
        st = {"listening": "还在听写", "writing": "还在做", "done": "做完了、还没交卷子批改", "graded": "批改了",
              "abandoned": "没做完就放下了"}[r["state"]]
        s = f"- {time.strftime('%H:%M', time.localtime(r['started']))}「{r['title']}」第 {r['attempt']} 次练，{st}"
        if r.get("finished") and r["state"] != "abandoned":
            over = overtime_s(r)
            s += f"，用时 {mmss(used_s(r))}（限时 {mmss(r['limit_s'])}，" + (f"超时 {mmss(over)}）" if over else "没超时）")
        if r.get("grade"):
            g = r["grade"]
            wrong = [f"{x.get('want')}" + (f"→{x['got']}" if x.get("got") else "→没写") for x in g["items"] if not x.get("ok")]
            s += f"，对 {g['right']}/{g['total']}" + (f"，错：{'、'.join(wrong[:15])}" if wrong else "，全对")
        lines.append(s)
    return "\n".join(lines)
