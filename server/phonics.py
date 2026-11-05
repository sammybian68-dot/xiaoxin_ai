"""英文单词的自然拼读拆解：cake → c /k/ · a_e /eɪ/ · k /k/（魔法 e）；elephant → el·e·phant。
给女儿（三年级、译林版、英音）看：单词卡、书写卡上一块一块显示字母组合和它发的音。

拆法让 Opus 出（参照有道的英式音标），**代码核对**，对不上就不显示（宁缺勿错，拆错了比不拆更误导）：
  - 字母：每块的字母按顺序拼回去必须正好是这个词（魔法 e 的 a_e 放在元音的位置，e 跟在下一块后面）；
  - 发音：每块的音连起来，去掉重音、长音、括号后，必须和词典的英式音标一样（词典里括号里可读可不读的音两种都认）。
每个词拆一次存在 XIAOXIN_DATA/dict/phonics/ 下，以后不再问模型。场景对话课里的词提前批量拆好（warm）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

from server import dictionary, store

log = logging.getLogger(__name__)
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5"]
BATCH = 25
FAIL_RETRY_S = 86400          # 拆不对的词一天内不再问
WORD = re.compile(r"^[A-Za-z]{1,24}$")   # 只拆单个词（短语、带撇号的 I'm 不拆）

PROMPT = """把下面每个英文单词按自然拼读（phonics）拆开，给学英式英语的小学三年级孩子看。
每个词给：
- parts：按字母顺序拆成发音块，每块 {{"g":"字母","s":"这块发的音"}}，s 用英式 IPA（照下面给的音标），不带斜杠、重音符号。
  - 常见字母组合算一块：sh ch th ph wh ck ng qu、双写辅音（ll pp ss）、ee ea oa ai ay oo ou ow oi oy ar er ir ur or igh air ear 等。
  - 不发音的字母（lamb 的 b、know 的 k）单独一块，s 写空串 ""。
  - 魔法 e（cake、bike、home、cute）：元音那块 g 写 "a_e" / "i_e" / "o_e" / "u_e" / "e_e"，放在元音的位置；后面那个 e 不再单独列。
  - 每块的音连起来要正好等于给的音标（重音、长音符号可以不管）。
- syl：多音节词按音节用 · 隔开（el·e·phant），单音节就写原词。
单词和英式音标：
{words}
只输出 JSON，不要别的字：{{"单词":{{"parts":[...],"syl":"..."}}}}"""

_lock = threading.Lock()
_running: dict[str, threading.Event] = {}


def _dir():
    return store.DATA / "dict" / "phonics"


def _path(word: str):
    return _dir() / f"{dictionary._key(word)}.json"


def cached(word: str) -> dict | None:
    """存过的拆法（{"word","parts","syl","ipa"}）；拆不对的记了 fail；都没有返回 None。"""
    try:
        return json.loads(_path(word).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 核对

def spell(parts: list[dict]) -> str:
    """按块拼回单词：a_e 放元音、e 跟在下一块后面。"""
    out, pend = [], ""
    for p in parts:
        g = str(p.get("g") or "").lower()
        m = re.fullmatch(r"([a-z]+)_e", g)
        if m:
            out.append(m.group(1))
            pend = "e"
            continue
        out.append(g)
        if pend:
            out.append(pend)
            pend = ""
    return "".join(out) + pend


_STRIP = str.maketrans("", "", "ˈˌ.ː'ˑ /‿-")


def _norm(s: str) -> str:
    s = s.replace("ɡ", "g").replace("ɹ", "r").replace("ʴ", "")
    return s.translate(_STRIP)


def ipa_forms(ipa: str) -> set[str]:
    """词典音标去掉记号后的写法：括号里可读可不读的两种都要；(r) 是英音不读的连读 r，去掉。"""
    ipa = ipa.replace("(r)", "")
    if ";" in ipa or "," in ipa:   # 有道偶尔给两种读法「ˈelɪfənt; ˈeləfənt」：哪种都认
        return set().union(*(ipa_forms(x) for x in re.split(r"[;,]", ipa) if x.strip()))
    forms = {""}
    for tok in re.split(r"(\([^)]*\))", ipa):
        if tok.startswith("("):
            forms = {f + x for f in forms for x in ("", tok[1:-1])}
        else:
            forms = {f + tok for f in forms}
    return {_norm(f) for f in forms}


def check(word: str, d: dict, ipa: str) -> str:
    """拆法对不对：对返回空串，不对返回原因（日志、测试用）。"""
    parts = d.get("parts")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        return "parts 不对"
    if spell(parts) != word.lower():
        return f"字母拼回去是 {spell(parts)}"
    if str(d.get("syl") or "").replace("·", "").lower() != word.lower():
        return "音节拼回去不对"
    if ipa:
        got = _norm("".join(str(p.get("s") or "") for p in parts))
        if got not in ipa_forms(ipa):
            return f"音连起来是 {got}，词典是 {ipa}"
    return ""


# ---------------------------------------------------------------- 拆

def _ask(words: dict[str, str]) -> dict:
    from src.llm import client

    lines = "\n".join(f"{w}  /{re.split(r'[;,]', ipa)[0].strip()}/" if ipa else w for w, ipa in words.items())
    rep = client.chat_full(MODELS, PROMPT.format(words=lines), max_tokens=200 + 220 * len(words), timeout=120)
    t = rep.text or ""
    return json.loads(t[t.index("{"): t.rindex("}") + 1])


def _ipa(word: str) -> str:
    """英式音标；词典给了两种读法只给模型看第一种（核对时两种都认）。"""
    try:
        return dictionary.phonetic(word).get("uk") or ""
    except dictionary.DictError:
        return ""


def build(words: list[str]) -> dict[str, dict]:
    """拆一批词（已经存过的跳过），存下来。返回 {词: 结果}（拆不对的是 {"fail": 原因}）。"""
    todo = {}
    for w in dict.fromkeys(x.strip() for x in words):
        if not WORD.match(w):
            continue
        c = cached(w)
        if c and (not c.get("fail") or time.time() - c.get("at", 0) < FAIL_RETRY_S):
            continue
        todo[w] = _ipa(w)
    out = {}
    items = list(todo.items())
    for i in range(0, len(items), BATCH):
        chunk = dict(items[i:i + BATCH])
        try:
            got = _ask(chunk)
        except Exception as e:  # noqa: BLE001 模型挂了：这批先不存，下次再试
            log.warning("phonics batch failed: %s", e)
            continue
        low = {k.lower(): v for k, v in got.items() if isinstance(v, dict)}
        for w, ipa in chunk.items():
            d = low.get(w.lower()) or {}
            why = check(w, d, ipa)
            rec = ({"word": w, "parts": [{"g": str(p["g"]), "s": str(p.get("s") or "")} for p in d["parts"]],
                    "syl": d["syl"], "ipa": ipa} if not why else {"word": w, "fail": why, "at": time.time()})
            if why:
                log.info("phonics %s 不对：%s", w, why)
            _dir().mkdir(parents=True, exist_ok=True)
            store._write_json(_path(w), rec)
            out[w] = rec
    return out


def get(word: str, wait: float = 0) -> dict | None:
    """一个词的拆法。没有就后台去拆，最多等 wait 秒；等不到返回 None（界面过会儿再来要）。"""
    word = dictionary.norm(word)
    c = cached(word)
    if c and not c.get("fail"):
        return c
    if c and c.get("fail") and time.time() - c.get("at", 0) < FAIL_RETRY_S:
        return c
    if not WORD.match(word):
        return {"word": word, "fail": "只拆单个词"}
    key = word.lower()
    with _lock:
        ev = _running.get(key)
        if ev is None:
            ev = _running[key] = threading.Event()

            def work():
                try:
                    build([word])
                finally:
                    with _lock:
                        _running.pop(key, None)
                    ev.set()

            threading.Thread(target=work, daemon=True, name="phonics").start()
    if wait:
        ev.wait(wait)
    return cached(word)


def warm(words: list[str]) -> None:
    """后台把一批词先拆好（课一做好、打开看板时调），不等。"""
    todo = [w for w in dict.fromkeys(words) if WORD.match(w) and cached(w) is None]
    if todo:
        threading.Thread(target=build, args=(todo,), daemon=True, name="phonics-warm").start()
