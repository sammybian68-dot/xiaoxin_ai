"""英语小剧场默写（scene_dictation）：从一课对话出一张卷子，两部分——
  一、听写句子：挑孩子演的那个角色的 3–4 句短台词。练习模式里小新用英音读（每句两遍、每遍后等几秒），她写下来；
  二、补全对话：整段对话印出来，挖掉关键词。刚学的课（没到「熟练」）下面给词库；熟练以后不给词库，靠记忆和上下文。

题全由程序从剧本里出（不用模型）：答案就是原文，不会错。挖哪个词：
  0. 剧本里每句有 key（Opus 写剧本时标的「这句最该考的词」）：第一次默写就挖它；
  1. 只挖「值得记」的词：这句的 key > 这课的生词 > 这课句型里的词 > 三年级核心词表（CORE：颜色、数字、家人、食物、文具、动物、
     常用动词形容词、礼貌用语）。不在这几类里的不挖（「Mm」「OK」、人名、the 这类）；
  2. 每次都会变：她以前默写里考过的词记着次数，考得少的、上次写错的先挖（同样条件随机），一课练几次能轮到每个核心词；
  3. 听写句子：Opus 写剧本时给每个角色标了最值得听写的 3–4 句（dict 排名），从这几句里出；
     也轮换——没听写过的优先，上次写错的那句再来一次。
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
           "do does can let's let me us oh ok mm yes no not too so very here there this that".split())
# 三年级（译林三上 + 生活场景）核心词：颜色、数字、家人、食物、文具、动物、身体、常用动词形容词、礼貌用语和问句词
CORE = set("""red blue yellow green white black brown pink orange purple colour
one two three four five six seven eight nine ten
mother father mum dad grandpa grandma brother sister aunt uncle family friend teacher
apple apples banana bananas orange oranges pear egg eggs milk juice water bread cake rice noodles sandwich ice cream sweets tea
pencil pen ruler rubber book bag desk chair crayon
cat dog bird fish duck panda monkey tiger lion elephant giraffe rabbit pig horse sheep cow
eye eyes ear ears nose mouth face hand hands head
like love look see help play eat drink wash open close sit stand come go run jump swim draw read sing dance have want can
big small long short tall nice good happy beautiful hungry thirsty tired cold hot sunny rainy windy
hello hi goodbye bye morning afternoon evening night please thank thanks welcome sorry
what where who how much many old name""".split())
BLANK = "{%s}"


def _tokens(en: str) -> list[str]:
    return en.split()


def _core(w: str) -> str:
    return re.sub(r"[^A-Za-z']", "", w)


# 词组的头一个词（good morning、see you、ice cream）：单挖一个字孩子不知道要填半个词组，不挖；
# 词组后半（morning、you、cream）照常可以挖
PHRASE_HEAD = {"good", "see", "ice", "how", "what", "thank"}
ALONE_NO = {"good", "see"}   # 单独出现也不挖（「Good, Lily.」里的 Good 考了没意义）


def _pick_blanks(lines: list[dict], names: set[str], prefer: set[str], seen: dict[str, int], wrong: set[str],
                 rnd: random.Random) -> list[list[int]]:
    """每句最多挖一个词。候选只要「值得记」的（这句的 key、prefer 生词 / 句型词，或者 CORE）；排序：上次写错的 > 考得少的 >
    这句的 key > 生词句型词 > 核心词，同样的随机。同一个词一张卷子只挖一次。"""
    used, out = set(), []
    for l in lines:
        best = None
        toks = _tokens(l["en"])
        kk = _core(l.get("key") or "").lower()
        for k, w in enumerate(toks):
            c = _core(w).lower()
            if c in used or _core(w) in names or len(c) < 2:
                continue
            if c != kk and (c in STOP or "'" in c):   # Opus 标的 key（如 Let's）照挖；不是 key 的虚词、缩写不挖
                continue
            nxt = _core(toks[k + 1]).lower() if k + 1 < len(toks) else ""
            if c in PHRASE_HEAD and nxt and w[-1].isalpha():   # 「Good morning」「See you」「How much」：不挖前一半
                continue
            if c in ALONE_NO and c != kk:
                continue
            is_key = c == kk
            if not is_key and c not in prefer and c not in CORE:
                continue
            # 上次写错的 > 考得少的 > 这句的核心词（Opus 标的） > 生词句型词 > 核心词表；同样随机
            key = (0 if c in wrong else 1, seen.get(c, 0), 0 if is_key else 1 if c in prefer else 2, rnd.random())
            if best is None or key < best[0]:
                best = (key, k, c)
        if best:
            used.add(best[2])
            out.append([best[1]])
        else:
            out.append([])
    return out


def build(L: dict, role: str, *, count: int = 3, bank: bool = True, seed=None, history: dict | None = None) -> tuple[str, list[dict]]:
    """history：她以前这课的默写记录（history_of 给）——words 每个词考过几次、wrong 上次写错的词、
    sentences 每句听写过几次、wrong_sentences 上次写错的句子。没有就是第一次。"""
    rnd = random.Random(seed)
    h = history or {}
    names = {r["name"] for r in L["roles"]} | {n for r in L["roles"] for n in r["name"].split()}
    mine = [l for l in L["lines"] if l["role"] == role]
    # 听写选句：Opus 标的 dict（1 最值得、2 其次……，0 不选）优先；没标的课退回挑短句（3–7 个词）。
    # 上次写错的句子先、听写得少的先，再按 dict 排名，同样随机；最后按原顺序排
    marked = [l for l in mine if l.get("dict")]
    pool = marked or [l for l in mine if 3 <= len(_tokens(l["en"])) <= 7] or mine
    n = max(1, min(count or 3, MAX_COUNT, len(pool)))
    ws_, ss_ = set(h.get("wrong_sentences") or []), h.get("sentences") or {}
    ranked = sorted(pool, key=lambda l: (0 if l["en"] in ws_ else 1, ss_.get(l["en"], 0), l.get("dict") or 9, rnd.random()))
    listen = sorted(ranked[:n], key=lambda l: L["lines"].index(l))
    items = [{"q": f"{i + 1}.", "a": l["en"], "zh": l["zh"], "part": "listen", "en": True} for i, l in enumerate(listen)]
    # 补全对话：整段，每句挖一个关键词（句型里的词、生词优先）
    prefer = {w["en"].lower() for w in L.get("new_words") or []}
    prefer |= {_core(w).lower() for w in re.split(r"[\s/]+", L.get("target") or "") if _core(w)}
    holes = _pick_blanks(L["lines"], names, prefer, h.get("words") or {}, set(h.get("wrong") or []), rnd)
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
    return build(L, role, count=count or 3, bank=bool(bank), seed=rnd.random(), history=history_of(kid, L["id"]))


def history_of(kid: str, lid: str) -> dict:
    """她以前这课默写的卷子和批改：每个词考过几次、每句听写过几次、最近一次批改写错的词和句子。"""
    from server import practice, worksheet

    out = {"words": {}, "sentences": {}, "wrong": [], "wrong_sentences": []}
    if not kid:
        return out
    papers = []
    for p in worksheet._dir().glob("*.json") if worksheet._dir().is_dir() else []:
        try:
            ws = __import__("json").loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if ws.get("topic") == TOPIC and ws.get("for") == kid and ws.get("lesson") == lid:
            papers.append(ws)
    for ws in papers:
        for it in ws["items"]:
            if it["part"] == "listen":
                out["sentences"][it["a"]] = out["sentences"].get(it["a"], 0) + 1
            else:
                for c in it["a"].split("；"):
                    out["words"][c.lower()] = out["words"].get(c.lower(), 0) + 1
    graded = [r for r in practice.all_for(kid) if r.get("grade") and r.get("worksheet") in {w["id"] for w in papers}]
    if graded:
        last = max(graded, key=lambda r: r.get("graded", 0))
        for x in last["grade"]["items"]:
            if not x.get("ok"):
                w = str(x.get("want") or "")
                (out["wrong_sentences"] if " " in w else out["wrong"]).append(w if " " in w else w.lower())
    return out
