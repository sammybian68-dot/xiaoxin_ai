"""英语小剧场默写（scene_dictation）：从一课对话出一张卷子，两部分——
  一、听写句子：挑孩子演的那个角色的 3–4 句短台词。练习模式里小新用英音读（每句两遍、每遍后等几秒），她写下来；
  二、补全对话：整段对话印出来，挖掉关键词。刚学的课（没到「熟练」）下面给词库；熟练以后不给词库，靠记忆和上下文。

题全由程序从剧本里出（不用模型）：挖哪个词按规则挑——这课的句型词、生词优先，其次是实词（名词、动词、颜色、数字），
不挖 a / the / I 这类虚词和人名；同一个词只挖一次。答案就是原文，不会错。
"""

from __future__ import annotations

import random
import re

SUBJECT = "english"
TOPIC = "scene_dictation"
NAME = "英语小剧场默写"
GRADE = "三年级"
LAYOUT = "dictation"
DEFAULT_COUNT = 0
MAX_COUNT = 6
HELP = ("lesson：英语小剧场的课 id（english_scene 卡片上那课）；role：孩子演的角色 id（不给就用她最近演的）；"
        "count：听写几句（默认 3）；bank：补全对话给不给词库（不给按熟练度：没到熟练给，熟练了不给）")

STOP = set("a an the i you he she it we they my your his her our its is am are was be to of in on at and or "
           "do does can let's let me us oh ok yes no not too so very here there this that what how please".split())
BLANK = "{%s}"


def _tokens(en: str) -> list[str]:
    return en.split()


def _core(w: str) -> str:
    return re.sub(r"[^A-Za-z']", "", w)


def _pick_blanks(lines: list[dict], names: set[str], prefer: set[str], rnd: random.Random, per_line: int = 1) -> list[list[int]]:
    """每句挑 per_line 个词挖空：句型词 / 生词优先，再挑长一点的实词；不挖虚词、人名、已经挖过的词。"""
    used, out = set(), []
    for l in lines:
        toks = _tokens(l["en"])
        cand = []
        for k, w in enumerate(toks):
            c = _core(w).lower()
            if len(c) < 2 or c in STOP or _core(w) in names or c in used or "'" in c:
                continue
            cand.append((0 if c in prefer else 1, -len(c), rnd.random(), k, c))
        cand.sort()
        pick = cand[:per_line]
        used.update(c for *_, c in pick)
        out.append(sorted(k for *_, k, _c in pick))
    return out


def build(L: dict, role: str, *, count: int = 3, bank: bool = True, seed=None) -> tuple[str, list[dict]]:
    rnd = random.Random(seed)
    names = {r["name"] for r in L["roles"]} | {n for r in L["roles"] for n in r["name"].split()}
    mine = [l for l in L["lines"] if l["role"] == role]
    # 听写：她的台词里挑短的（3–7 个词），按原顺序
    short = [l for l in mine if 3 <= len(_tokens(l["en"])) <= 7] or mine
    n = max(1, min(count or 3, MAX_COUNT, len(short)))
    listen = sorted(rnd.sample(short, n), key=lambda l: L["lines"].index(l))
    items = [{"q": f"{i + 1}.", "a": l["en"], "zh": l["zh"], "part": "listen", "en": True} for i, l in enumerate(listen)]
    # 补全对话：整段，每句挖一个关键词（句型里的词、生词优先）
    prefer = {w["en"].lower() for w in L.get("new_words") or []}
    prefer |= {_core(w).lower() for w in re.split(r"[\s/]+", L.get("target") or "") if _core(w)}
    holes = _pick_blanks(L["lines"], names, prefer, rnd)
    lines, answers = [], []
    for l, ks in zip(L["lines"], holes):
        toks = _tokens(l["en"])
        for k in ks:
            c = _core(toks[k])
            answers.append(c)
            toks[k] = toks[k].replace(c, BLANK % c, 1)
        speaker = next((r["name"] for r in L["roles"] if r["id"] == l["role"]), l["role"])
        lines.append(f"{speaker}: " + " ".join(toks))
    words = sorted(set(answers), key=lambda w: rnd.random()) if bank else []
    items.append({"q": "", "bank": ("词库：" + " / ".join(words)) if words else "", "lines": lines, "part": "write", "en": True,
                  "a": "；".join(answers)})
    return f"{L['name']} · 英语小剧场默写", items


def generate(count: int, rnd: random.Random, lesson: str = "", role: str = "", bank=None, kid: str = "", **_) -> tuple[str, list[dict]]:
    from server.english import drill
    from server.exercises import ExerciseError

    try:
        L = drill.lesson(lesson)
    except KeyError:
        raise ExerciseError(f"没有这一课：{lesson}")
    if not role:   # 她最近演的那个角色；没练过就用第一个
        last = next((s for s in drill.sessions(kid, L["id"])), None) if kid else None
        role = last["role"] if last else L["roles"][0]["id"]
    if role not in [r["id"] for r in L["roles"]]:
        raise ExerciseError(f"这课没有这个角色：{role}")
    if bank is None:
        lv = (drill.progress(kid).get(L["id"]) or {}).get("level", 0) if kid else 0
        bank = lv < 2
    return build(L, role, count=count or 3, bank=bool(bank), seed=rnd.random())
