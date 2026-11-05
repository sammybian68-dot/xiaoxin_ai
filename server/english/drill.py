"""英语场景对话的练习：一次练习（session）、每句评测、熟练度、记忆曲线排期、今天推荐。

存储（XIAOXIN_DATA/english/）：
  lessons/<id>/lesson.json         课（build.py 生成）
  sessions/<sid>.json              一次练习：哪课、演谁、每句每次的分数和录音文件
  sessions/<sid>/<i>_<n>.wav       她第 i 句第 n 次读的录音（16 kHz，看板回放）
  progress/<kid>.json              每课的熟练度、复习排期（每练完一次更新）

熟练度：0 没练过 → 1 会读（她那几句都读过）→ 2 熟练（每句最好成绩 ≥ 2 星）→ 3 掌握（两个角色都演过，且各自最近两次全 3 星）。
复习排期（简化的间隔重复，SM-2 思路）：练完按这次平均星数调间隔——全 3 星 ×2.2、≥ 2.5 星 ×1.6、≥ 2 星 不变、更低回到 1 天；
间隔从 1 天起，最长 30 天。到期日 = 这次练完的日子 + 间隔。

TODO（家长 2026-11-05 提的，先讨论清楚再做，现在不动）：
  - 跟读改成连续说（不用按住）：要先定怎么防弟弟在旁边捣乱（录进去的话会被评测、记成她的成绩）
  - 过关自动进下一句，过了一个提示音、没过另一个提示音
  - 每次练习、每句每次的记录能回看（数据已经存了：sessions/<sid>.json + 录音，缺界面）
  - 「暂停」旁边加「结束」
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
import uuid
from pathlib import Path

from server import store

# 星级阈值（讯飞英文句子总分 0–100）。童声会被压低 ~10 分（2026-11-05 实测），所以比成人宽
STAR3, STAR2, STAR1 = 85, 70, 50
WORD_WEAK, WORD_BAD = 80, 60     # 单词：< 80 标黄提醒，< 60 标红、建议重读
MAX_TRIES = 3                    # 一句最多读几次（之后夸一句往下走，不让孩子卡住）
INTERVALS = (1, 2, 4, 7, 15, 30)
_lock = threading.Lock()


def root() -> Path:
    d = store.DATA / "english"
    for sub in ("lessons", "sessions", "progress"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def lessons() -> list[dict]:
    out = []
    for p in (root() / "lessons").glob("*/lesson.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return sorted(out, key=lambda l: (l.get("order", 999), l.get("built", 0)))


def lesson(lid: str) -> dict:
    if not re.fullmatch(r"[a-z0-9_]{1,40}", lid or ""):
        raise KeyError(lid)
    p = root() / "lessons" / lid / "lesson.json"
    if not p.is_file():
        raise KeyError(lid)
    return json.loads(p.read_text(encoding="utf-8"))


def stars(score: float | None) -> int:
    if score is None:
        return 0
    return 3 if score >= STAR3 else 2 if score >= STAR2 else 1 if score >= STAR1 else 0


# ------------------------------------------------------------ 一次练习
def _spath(sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", sid or ""):
        raise KeyError(sid)
    return root() / "sessions" / f"{sid}.json"


def save(s: dict) -> None:
    p = _spath(s["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def get(sid: str) -> dict:
    p = _spath(sid)
    if not p.is_file():
        raise KeyError(sid)
    return json.loads(p.read_text(encoding="utf-8"))


def sessions(kid: str, lid: str = "") -> list[dict]:
    out = []
    for p in (root() / "sessions").glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if s.get("kid") == kid and (not lid or s.get("lesson") == lid):
            out.append(s)
    return sorted(out, key=lambda s: -s["started"])


def current(kid: str) -> dict | None:
    """暂停着（没完成）的那次，最近 12 小时内的。"""
    for s in sessions(kid):
        if not s.get("done") and time.time() - s["started"] < 12 * 3600:
            return s
    return None


def start(kid: str, lid: str, role: str) -> dict:
    L = lesson(lid)
    if role not in [r["id"] for r in L["roles"]]:
        raise ValueError(f"这课没有这个角色：{role}")
    old = current(kid)
    if old and old["lesson"] == lid and old["role"] == role:
        return old            # 同一课同一角色没练完：接着练
    s = {"id": uuid.uuid4().hex[:16], "kid": kid, "lesson": lid, "role": role, "started": time.time(),
         "at": 0, "lines": {}, "done": False, "secs": 0}
    save(s)
    return s


def mine(L: dict, role: str) -> list[int]:
    return [i for i, l in enumerate(L["lines"]) if l["role"] == role]


def score_line(sid: str, kid: str, i: int, wav: bytes, assess=None) -> dict:
    """她读第 i 句：存录音、送讯飞评测、记分。返回这一次的结果（给界面标词、给星）。"""
    from src.voice import xfyun_ise

    s = get(sid)
    if s["kid"] != kid:
        raise KeyError(sid)
    L = lesson(s["lesson"])
    if i not in mine(L, s["role"]):
        raise ValueError("这句不是你的台词")
    pcm = xfyun_ise.to_pcm16k(wav)
    if len(pcm) < 16000 * 2 * 0.4:
        raise ValueError("没听清，按住说完再松手")
    import array

    a = array.array("h", pcm)
    if max(abs(x) for x in a[::8]) < 300:   # 全是静音（麦克风没声音 / 没说话）：不送讯飞，不扣评测次数
        raise ValueError("没听到声音，靠近一点、大声说")
    r = (assess or xfyun_ise.assess)(L["lines"][i]["en"], pcm)
    tries = s["lines"].setdefault(str(i), [])
    n = len(tries) + 1
    adir = root() / "sessions" / sid
    adir.mkdir(exist_ok=True)
    (adir / f"{i}_{n}.wav").write_bytes(_wav(pcm))
    total = r.get("total")
    words = r.get("words") or []
    rec = {"n": n, "at": time.time(), "total": total, "stars": stars(total), "fluency": r.get("fluency"),
           "integrity": r.get("integrity"), "words": [{"w": w["word"], "s": w["score"], "e": w["err"]} for w in words]}
    with _lock:
        s = get(sid)
        s["lines"].setdefault(str(i), []).append(rec)
        s["at"] = max(s.get("at", 0), i)
        save(s)
    weak = [w for w in rec["words"] if w["e"] or (w["s"] is not None and w["s"] < WORD_WEAK)]
    bad = [w for w in rec["words"] if w["e"] == "漏读" or (w["s"] is not None and w["s"] < WORD_BAD)]
    rec["pass"] = rec["stars"] >= 2 and not bad
    rec["last"] = n >= MAX_TRIES
    rec["msg"] = _msg(rec, bad, weak)
    return rec


def _msg(rec: dict, bad: list, weak: list) -> str:
    if rec["pass"] and rec["stars"] == 3 and not weak:
        return "读得真好！"
    if rec["pass"]:
        return "不错！" + (f"「{weak[0]['w']}」再读清楚一点会更好。" if weak else "")
    if rec["last"]:
        return "已经很努力啦，我们先往下，下次再练这句。"
    if any(w["e"] == "漏读" for w in bad):
        return f"「{next(w['w'] for w in bad if w['e'] == '漏读')}」好像没读出来，再说一次吧。"
    if bad:
        return f"「{bad[0]['w']}」再读清楚一点。点红色的词听一听，或者点「小新演示」跟我读。"
    return "再大声、清楚一点，再说一次吧。"


def _wav(pcm: bytes, rate: int = 16000) -> bytes:
    import io
    import wave

    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return b.getvalue()


def audio(sid: str, kid: str, i: int, n: int) -> Path:
    s = get(sid)
    if kid and s["kid"] != kid:
        raise KeyError(sid)
    p = root() / "sessions" / sid / f"{int(i)}_{int(n)}.wav"
    if not p.is_file():
        raise KeyError("没有这段录音")
    return p


def finish(sid: str, kid: str, today: dt.date | None = None, early: bool = False) -> dict:
    """练完：算这次的成绩，更新熟练度和复习排期（today 给测试用）。
    early：孩子点了「结束」（没演完）。一句都没读就当没练过（不算次数、不进熟练度，也不在群里说）；
    读了几句的照常记下，summary 里 early=True，熟练度按读过的算（没读的句子是 None，min2 / all3 自然不满足）。"""
    with _lock:
        s = get(sid)
        if s["kid"] != kid:
            raise KeyError(sid)
        if s.get("done"):
            return s
        L = lesson(s["lesson"])
        s["done"] = True
        s["ended"] = time.time()
        s["secs"] = round(s["ended"] - s["started"])
        s["summary"] = summarize(s, L)
        if early:
            s["summary"]["early"] = True
            if not s["summary"]["said"]:
                s["discarded"] = True
                s.pop("summary")
        save(s)
    if not s.get("discarded"):
        update_progress(kid, s["lesson"], today=today)
    return s


def summarize(s: dict, L: dict) -> dict:
    """这次练习：每句最好成绩、平均星、重读次数、读不准的词。"""
    best, weak, retries = [], {}, 0
    for i in mine(L, s["role"]):
        tries = s["lines"].get(str(i)) or []
        if not tries:
            best.append(None)
            continue
        retries += len(tries) - 1
        b = max(tries, key=lambda t: t["total"] or 0)
        best.append(b)
        for w in b["words"]:
            if w["e"] or (w["s"] is not None and w["s"] < WORD_WEAK):
                weak[w["w"]] = min(weak.get(w["w"], 100), w["s"] or 0)
    said = [b for b in best if b]
    avg = round(sum(b["total"] or 0 for b in said) / len(said), 1) if said else None
    return {"said": len(said), "of": len(best), "avg": avg,
            "stars": round(sum(b["stars"] for b in said) / len(said), 2) if said else 0,
            "all3": bool(said) and len(said) == len(best) and all(b["stars"] == 3 for b in said),
            "min2": bool(said) and len(said) == len(best) and all(b["stars"] >= 2 for b in said),
            "retries": retries, "weak": sorted(weak.items(), key=lambda kv: kv[1])[:6]}


# ------------------------------------------------------------ 熟练度 + 排期
def _ppath(kid: str) -> Path:
    if not re.fullmatch(r"[0-9a-zA-Z_-]{1,40}", kid or ""):
        raise KeyError(kid)
    return root() / "progress" / f"{kid}.json"


def progress(kid: str) -> dict:
    p = _ppath(kid)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def level(kid: str, lid: str, L: dict | None = None) -> int:
    L = L or lesson(lid)
    done = [s for s in sessions(kid, lid) if s.get("done") and s.get("summary")]
    if not done:
        return 0
    lv = 1 if any(s["summary"]["said"] == s["summary"]["of"] for s in done) else 0
    if any(s["summary"]["min2"] for s in done):
        lv = 2
    roles = [r["id"] for r in L["roles"]]
    by_role = {r: [s for s in done if s["role"] == r] for r in roles}
    if all(len(v) >= 2 and all(s["summary"]["all3"] for s in v[:2]) for v in by_role.values()):
        lv = 3
    return lv


def update_progress(kid: str, lid: str, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    with _lock:
        P = progress(kid)
        L = lesson(lid)
        done = [s for s in sessions(kid, lid) if s.get("done") and s.get("summary")]
        last = done[0]["summary"] if done else None
        cur = P.get(lid) or {"interval": 0}
        iv = cur.get("interval", 0)
        st = last["stars"] if last else 0
        if not last or not last["min2"]:
            iv = 1
        elif last["all3"]:
            iv = max(2, round((iv or 1) * 2.2))
        elif st >= 2.5:
            iv = max(2, round((iv or 1) * 1.6))
        else:
            iv = max(1, iv or 1)
        iv = min(iv, INTERVALS[-1])
        P[lid] = {"level": level(kid, lid, L), "interval": iv, "last": today.isoformat(),
                  "due": (today + dt.timedelta(days=iv)).isoformat(), "times": len(done),
                  "avg": last["avg"] if last else None, "weak": [w for w, _ in (last["weak"] if last else [])]}
        _ppath(kid).write_text(json.dumps(P, ensure_ascii=False, indent=1), encoding="utf-8")
        return P[lid]


# ------------------------------------------------------------ 今天推荐
def recommend(kid: str, today: dt.date | None = None, unit_hint: str = "") -> dict:
    """推荐今天练哪一课（docs/english-scenes.md 的五条规则）。返回 {pick, why, also: [..]}。
    1 到期的复习（过期越久越先）→ 2 没练熟的（会读、还不熟练）→ 3 同等情况贴课本单元（unit_hint，如 U5）
    → 4 都熟了才开新课（按课程顺序）→ 5 今天练过的不推。"""
    today = today or dt.date.today()
    P = progress(kid)
    Ls = lessons()
    t = today.isoformat()
    cand = []
    for L in Ls:
        p = P.get(L["id"])
        if p and p.get("last") == t:
            continue
        unit_bonus = 0.5 if unit_hint and L.get("unit") == unit_hint else 0
        if not p:
            cand.append((3, L["order"] - unit_bonus * 10, L, "new", None))
            continue
        overdue = (today - dt.date.fromisoformat(p["due"])).days
        if overdue >= 0:
            cand.append((0, -overdue - unit_bonus, L, "due", p))
        elif p["level"] < 2:
            cand.append((1, -unit_bonus, L, "unripe", p))
        else:
            cand.append((2, (dt.date.fromisoformat(p["due"]) - today).days - unit_bonus, L, "later", p))
    cand.sort(key=lambda c: (c[0], c[1]))
    picks = [c for c in cand if c[0] != 2] or cand
    if not picks:
        return {"pick": None, "why": "今天的都练过了，真棒！", "also": []}

    def why(c) -> str:
        kind, L, p = c[3], c[2], c[4]
        if kind == "due":
            od = (today - dt.date.fromisoformat(p["due"])).days
            w = f"上次是 {p['last'][5:].replace('-', '月', 1)}日练的，" + ("今天该复习了" if od == 0 else f"已经过了 {od} 天该复习了")
            if p.get("weak"):
                w += f"，上次「{'」「'.join(p['weak'][:2])}」没读好"
            return w + "。"
        if kind == "unripe":
            return f"这课还只到「{['没练过', '会读', '熟练', '掌握'][p['level']]}」，再练一次就熟了。"
        if kind == "new":
            return "前面的都练得不错，来学一课新的。"
        return f"离复习还有 {int(c[1])} 天，想多练也可以。"
    return {"pick": {"id": picks[0][2]["id"], "kind": picks[0][3]}, "why": why(picks[0]),
            "also": [{"id": c[2]["id"], "kind": c[3], "why": why(c)} for c in picks[1:3]]}


def board(kid: str, today: dt.date | None = None) -> dict:
    """总看板数据：每课熟练度、最近两周每天练了几分钟、连续天数、读不准的词、最近练习。"""
    today = today or dt.date.today()
    P = progress(kid)
    S = [s for s in sessions(kid) if s.get("done") and s.get("summary")]   # 一句没读就结束的（discarded）不算
    mins = {}
    for s in S:
        d = dt.date.fromtimestamp(s["started"]).isoformat()
        mins[d] = mins.get(d, 0) + s.get("secs", 0) / 60
    days = [(today - dt.timedelta(days=13 - k)).isoformat() for k in range(14)]
    streak = 0
    for k in range(0, 400):
        if mins.get((today - dt.timedelta(days=k)).isoformat()):
            streak += 1
        elif k:
            break
    words: dict[str, list] = {}
    for s in sorted(S, key=lambda s: s["started"]):
        for i, tries in s["lines"].items():
            b = max(tries, key=lambda t: t["total"] or 0)
            for w in b["words"]:
                if w["s"] is not None:
                    words.setdefault(w["w"], []).append((w["s"], s["id"], int(i), b["n"]))
    weak = [{"w": w, "hist": [x[0] for x in v[-6:]], "last": v[-1][0], "sid": v[-1][1], "i": v[-1][2], "n": v[-1][3]}
            for w, v in words.items() if min(x[0] for x in v[-3:]) < WORD_WEAK]
    weak.sort(key=lambda x: (sum(x["hist"]) / len(x["hist"])))
    Ls = {L["id"]: L for L in lessons()}
    return {"lessons": [{"id": L["id"], "name": L["name"], "zh": L["zh"], "unit": L.get("unit", ""),
                         **(P.get(L["id"]) or {"level": 0, "times": 0})} for L in Ls.values()],
            "days": [{"d": d, "min": round(mins.get(d, 0), 1)} for d in days], "streak": streak,
            "total": {"sessions": len(S), "lines": sum(len(s["lines"]) for s in S),
                      "mastered": sum(1 for p in P.values() if p.get("level") == 3),
                      "avg": round(sum(s["summary"]["avg"] or 0 for s in S) / len(S), 1) if S else None},
            "weak": weak[:8],
            "recent": [{"id": s["id"], "lesson": s["lesson"], "zh": Ls.get(s["lesson"], {}).get("zh", s["lesson"]),
                        "role": s["role"], "at": s["started"], "avg": s["summary"]["avg"], "stars": s["summary"]["stars"]}
                       for s in S[:8]],
            "recommend": recommend(kid, today)}
