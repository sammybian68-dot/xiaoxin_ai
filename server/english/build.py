"""英语场景对话：按 catalog 大纲批量生成课（剧本、背景、角色、配音），存到 XIAOXIN_DATA/english/。

    python -m server.english.build            # 只做还没做的课
    python -m server.english.build fruit zoo  # 只做这几课（已有的会重做）

一课一个目录 lessons/<id>/：lesson.json（剧本 + 每句时长）、bg.jpg、l<i>.mp3 / l<i>_slow.mp3。
角色（透明底 PNG）和单词读音全课共用：chars/<name>.png、words/<word>.mp3，画一次 / 读一次以后都复用，
所以同一个角色在每课里长得一样。
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server import store
from server.english.catalog import LESSONS, LOOK, VOICE_RES

log = logging.getLogger("xiaoxin.english")
STYLE = ("children's picture book illustration, soft watercolor and crayon texture, warm pastel colors, cute and friendly, "
         "clean shapes, no text, no letters, no words")
WRITER = ["claude-opus-5-5", "claude-sonnet-5-5"]
PAINTER = ["grok-imagine-image-2.0", "gpt-image-2"]
WORD_VOICE = "en_female_emily_mars_bigtts"   # 小新演示逐词读用的声音（英音女声）


def root() -> Path:
    d = store.DATA / "english"
    (d / "lessons").mkdir(parents=True, exist_ok=True)
    (d / "chars").mkdir(exist_ok=True)
    (d / "words").mkdir(exist_ok=True)
    return d


NAMES = {"bear": "Mr. Bear", "mum": "Mum", "grandpa": "Grandpa", "teacher": "Miss Green", "doctor": "Dr. Cat"}


def display(r: str) -> str:
    """角色在台词里怎么称呼（Mr. Bear、Miss Green……），其余就是名字。"""
    return NAMES.get(r, r.capitalize())


def word_key(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())


# ------------------------------------------------------------ 剧本
PROMPT = """为一个中国小学三年级女孩写一段英语场景对话，用来练口语。她学的是译林版（三起）三年级上册，学过：问候、名字、年龄、
家人、朋友、颜色、数字 1–10、简单食物、文具、动物、玩具；英式拼写（colour, favourite, Mum）。
场景：{name}（{zh}）。画面：{scene}。
两个角色：{a}（id={aid}）和 {b}（id={bid}）。台词里互相称呼时就叫「{a}」「{b}」，不要简写。
这一课要练的句型 / 词：{target}
要求：
- 一共 10 句，两人轮流说，{first} 先说。
- 每句 3–9 个单词，只用三年级学过的词和这一课的句型；新词最多 2 个，要简单常用。
- 句子自然、像真的在对话，有一点小情节和结尾（比如说再见、谢谢）。
- 用英式拼写和英式说法（Mum 不是 Mom，colour 不是 color）。不要用缩写以外的生僻标点，不要表情符号。
- 每句给中文意思（给家长看，简短）。
- 每句标出 key：这句里最该让孩子记住、默写时最该考的那一个词（原样照抄句子里的写法）。挑这一课的句型词、生词、
  三年级该会的实词（颜色、数字、家人、食物、动物、常用动词形容词）；不要挑人名、a / the / I、Mm / OK 这类，
  也不要挑词组的半截（Good morning 里的 Good）。这句实在没有值得考的词就写 ""。
- 每个角色的台词里，挑出最值得听写（整句默写）的 3–4 句，标 dict：1 最值得、2 其次……其余句子 dict 写 0。
  最值得的是：用到这一课的句型、孩子以后会反复用到、长短适中（4–8 个词）、拼写有练头的句子；
  「Thank you.」「OK.」「Goodbye.」这种太短太简单的不选。
只输出 JSON：{{"lines":[{{"role":"{aid}","en":"...","zh":"...","key":"...","dict":0}},...],"new_words":[{{"en":"...","zh":"..."}}],"title_zh":"{zh}"}}"""


def write_script(lesson: tuple) -> dict:
    from src.llm import client

    lid, name, zh, unit, roles, scene, target = lesson
    a, b = roles
    first = b if lid not in ("help", "breakfast") else a   # 一般对方先开口，孩子接话
    p = PROMPT.format(name=name, zh=zh, scene=scene, a=display(a), aid=a, b=display(b), bid=b, target=target,
                      first=display(first))
    for attempt in range(3):
        r = client.chat_full(WRITER, p, max_tokens=2500, timeout=120)
        t = r.text.strip().strip("`")
        try:
            d = json.loads(t[t.index("{"): t.rindex("}") + 1])
            lines = [l for l in d["lines"] if l.get("role") in roles and l.get("en")]
            if len(lines) >= 8 and {l["role"] for l in lines} == set(roles):
                for l in lines:   # 核心词得是这句里真有的词，不是就当没标（出题时退回规则挑）
                    k = str(l.get("key") or "").strip()
                    l["key"] = k if k and k.lower() in {word_key(w) for w in l["en"].split()} else ""
                    l["dict"] = _rank(l.get("dict"))
                d["lines"] = lines[:12]
                return d
        except (ValueError, KeyError):
            pass
        log.warning("script %s attempt %d bad", lid, attempt + 1)
    raise RuntimeError(f"{lid} 剧本写不出来")


# ------------------------------------------------------------ 画
def paint(prompt: str, out: Path, size: str) -> None:
    from src.llm import image

    last = None
    for m in PAINTER:
        try:
            image.generate(prompt, out, model=m, size=size, timeout=240)
            return
        except Exception as e:  # noqa: BLE001 换下一个模型
            last = e
    raise RuntimeError(f"画不出来：{last}")


def cutout(src: Path, dst: Path, thr: int = 232) -> None:
    """白底角色图 → 透明底 PNG：从四边往里漫水，连着边的白色都抠掉，边缘柔一下。"""
    from PIL import Image, ImageFilter

    im = Image.open(src).convert("RGBA")
    w, h = im.size
    px = im.load()
    seen = bytearray(w * h)
    q = deque([(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)])
    while q:
        x, y = q.popleft()
        if not (0 <= x < w and 0 <= y < h) or seen[y * w + x]:
            continue
        r, g, b, _ = px[x, y]
        if r <= thr or g <= thr or b <= thr:
            continue
        seen[y * w + x] = 1
        q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    alpha = Image.frombytes("L", (w, h), bytes(0 if s else 255 for s in seen)).filter(ImageFilter.GaussianBlur(1.2))
    im.putalpha(alpha)
    im = im.crop(im.getbbox())
    im.thumbnail((720, 900))
    im.save(dst, optimize=True)


def character(name: str) -> Path:
    if name not in LOOK:
        LOOK.update(_extra_chars())
    out = root() / "chars" / f"{name}.png"
    if out.is_file():
        return out
    raw = out.with_suffix(".raw.png")
    paint(f"a single {LOOK[name][0]}, standing, smiling, full body, isolated on a plain pure white background, {STYLE}", raw, "1024x1024")
    cutout(raw, out)
    raw.unlink(missing_ok=True)
    return out


def background(lid: str, scene: str) -> None:
    from PIL import Image

    d = root() / "lessons" / lid
    out = d / "bg.jpg"
    if out.is_file():
        return
    raw = d / "bg.raw.png"
    paint(f"{scene}, open empty space in the lower middle for characters to stand, no people, no animals, wide landscape "
          f"composition, {STYLE}", raw, "1536x1024")
    im = Image.open(raw).convert("RGB")
    im.thumbnail((1600, 1000))
    im.save(out, quality=84)
    raw.unlink(missing_ok=True)


# ------------------------------------------------------------ 配音
def mp3(pcm: bytes, out: Path, rate: int = 24000) -> None:
    """PCM → mp3（ffmpeg）；没有 ffmpeg 就存 wav（浏览器一样能放）。"""
    try:
        p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
                            "-b:a", "64k", str(out)], input=pcm, capture_output=True, timeout=60)
        if p.returncode == 0:
            return
    except FileNotFoundError:
        pass
    import wave

    with wave.open(str(out), "wb") as w:   # 名字还是 .mp3，内容是 wav：浏览器按内容识别
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)


# 豆包 TTS 账号有并发上限（5 课 × 6 句同时发，报 quota exceeded for types: concurrency）：全局最多 3 个在途，
# 超了等一会儿重试。小新服务里实时对话也在用 TTS，留点余量
TTS_SLOTS = threading.BoundedSemaphore(3)


def _tts(text: str, voice: str, **kw):
    from src.voice import doubao

    for attempt in range(6):
        with TTS_SLOTS:
            try:
                return doubao.tts(text, speaker=voice, resource_id=VOICE_RES.get(voice, "seed-tts-1.0"), timeout=60, **kw)
            except doubao.DoubaoError as e:
                if "concurrency" not in str(e) or attempt == 5:
                    raise
        time.sleep(1.5 * (attempt + 1))


def say(text: str, voice: str, out: Path, slow: bool = False) -> float:
    if out.is_file() and out.stat().st_size:
        import wave
        try:
            with wave.open(str(out)) as w:
                return round(w.getnframes() / w.getframerate(), 2)
        except Exception:  # noqa: BLE001 是 mp3：时长按文件大小估（64 kbps）
            return round(out.stat().st_size / 8000, 2)
    s = _tts(text, voice, speech_rate=-35 if slow else 0)
    mp3(s.pcm, out)
    return round(len(s.pcm) / 48000, 2)


def word_audio(w: str) -> None:
    out = root() / "words" / f"{w}.mp3"
    if out.is_file():
        return
    mp3(_tts(w, WORD_VOICE, speech_rate=-20).pcm, out)


# ------------------------------------------------------------ 一课
def build(lesson: tuple, *, force: bool = False) -> dict:
    lid, name, zh, unit, roles, scene, target = lesson
    d = root() / "lessons" / lid
    d.mkdir(parents=True, exist_ok=True)
    meta = d / "lesson.json"
    if meta.is_file() and not force:
        return json.loads(meta.read_text())
    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:   # 剧本、背景、两个角色并行
        fs = ex.submit(write_script, lesson)
        fb = ex.submit(background, lid, scene)
        fc = [ex.submit(character, r) for r in roles]
        script = fs.result()
        fb.result()
        for f in fc:
            f.result()
    jobs = [(i, l, slow) for i, l in enumerate(script["lines"]) for slow in (False, True)]
    words = sorted({word_key(w) for l in script["lines"] for w in l["en"].split()} - {""})
    with ThreadPoolExecutor(6) as ex:
        durs = list(ex.map(lambda j: say(j[1]["en"], LOOK[j[1]["role"]][2], d / f"l{j[0]}{'_slow' if j[2] else ''}.mp3", j[2]), jobs))
        list(ex.map(word_audio, words))
    for (i, l, slow), sec in zip(jobs, durs):
        l["slow_s" if slow else "dur_s"] = sec
    order = [x[0] for x in LESSONS].index(lid) if lid in [x[0] for x in LESSONS] else 1000
    out = {"id": lid, "name": name, "zh": zh, "unit": unit, "order": order,
           "roles": [{"id": r, "name": display(r), "zh": LOOK[r][1]} for r in roles],
           "lines": script["lines"], "new_words": script.get("new_words", [])[:3], "target": target,
           "built": time.time(), "build_s": round(time.time() - t0)}
    meta.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("lesson %s built in %ds", lid, out["build_s"])
    return out


# ------------------------------------------------------------ 孩子现场要一课（「生成一个在独库公路上开车看美景的对话」）
PLAN = """孩子想练一段英语场景对话，她说：「{ask}」。为三年级女孩（译林版三上水平）设计这一课。
已有角色（尽量从里面挑一个当孩子演的，另一个可以挑已有的或新造一个）：
{chars}
只输出 JSON：
{{"id":"英文小写下划线，<=20 字符，如 duku_road","name":"英文课名（2–5 个词）","zh":"中文课名（<=8 字）",
"roles":["孩子演的角色 id","另一个角色 id"],
"new_role":null 或 {{"id":"小写英文 id","look":"英文外形描述（像已有角色那样写，不要提背景）","zh":"中文名","gender":"female|male"}},
"scene":"英文背景画面描述（只写环境，不写人物），一句话",
"target":"这课要练的 3–5 个简单句型 / 词，用 / 隔开"}}"""


def plan(ask: str) -> tuple:
    """把孩子的一句话变成一课的大纲（和 LESSONS 里一行同样的格式）。新角色记进 LOOK（运行时）和 chars.json（持久）。"""
    from src.llm import client

    extra = _extra_chars()
    LOOK.update(extra)
    chars = "\n".join(f"- {k}：{v[1]}，{v[0]}" for k, v in LOOK.items())
    for _ in range(3):
        r = client.chat_full(WRITER, PLAN.format(ask=ask, chars=chars), max_tokens=1200, timeout=90)
        t = r.text.strip().strip("`")
        try:
            d = json.loads(t[t.index("{"): t.rindex("}") + 1])
        except ValueError:
            continue
        nr = d.get("new_role")
        if nr and re.fullmatch(r"[a-z]{2,16}", nr.get("id", "")) and nr["id"] not in LOOK:
            voice = "en_female_sarah_mars_bigtts" if nr.get("gender") == "female" else "en_male_smith_mars_bigtts"
            LOOK[nr["id"]] = (nr["look"], nr.get("zh", nr["id"]), voice)
            extra[nr["id"]] = LOOK[nr["id"]]
            (root() / "chars.json").write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")
        roles = [x for x in d.get("roles", []) if x in LOOK][:2]
        lid = re.sub(r"[^a-z0-9_]", "", (d.get("id") or "").lower())[:20]
        if len(roles) == 2 and roles[0] != roles[1] and lid:
            if (root() / "lessons" / lid).exists():
                lid = f"{lid[:14]}_{int(time.time()) % 100000}"
            return (lid, d.get("name") or lid, d.get("zh") or lid, "自选", roles, d.get("scene") or ask, d.get("target") or "")
    raise RuntimeError("这一课没设计出来")


def _extra_chars() -> dict:
    p = root() / "chars.json"
    if not p.is_file():
        return {}
    return {k: tuple(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}


def custom(ask: str) -> dict:
    """现场生成一课：设计 → 画 / 写 / 配音（同 build）。大约 1 分钟。课的 order 排在课程表后面。"""
    lesson = plan(ask)
    out = build(lesson, force=True)
    out["custom"] = True
    out["ask"] = ask
    out["order"] = 1000 + int(time.time()) % 100000
    (root() / "lessons" / out["id"] / "lesson.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


KEY_PROMPT = """下面是一段给小学三年级孩子练口语的英语对话（这一课练：{target}；生词：{new}）。每行前面是说话的角色。
默写卷子有两部分，请为每句给出两样：
1. key：补全对话时这句挖掉的那个词——这一课的句型词、生词、三年级该会的实词（颜色、数字、家人、食物、动物、常用动词形容词）优先；
   不要挑人名、a / the / I、Mm / OK，也不要挑词组的半截（Good morning 的 Good）。原样照抄句子里的写法。没有值得考的写 ""。
2. dict：听写（整句默写）选句。每个角色各挑最值得听写的 3–4 句，标 1（最值得）、2、3、4，其余写 0。
   最值得的是：用到这一课的句型、以后会反复用到、长短适中（4–8 个词）、拼写有练头的；「Thank you.」「Goodbye.」这种太短太简单的不选。
{lines}
只输出 JSON 数组，和句子一一对应：[{{"key":"...","dict":0}},...]"""


def _rank(v) -> int:
    try:
        return max(0, min(int(v or 0), 9))
    except (TypeError, ValueError):
        return 0


def mark_keys(lid: str) -> list[str]:
    """给已经做好的课补上每句的核心词（剧本不重写，只标注）。核心词必须是这句里真有的词，不是就当没标。"""
    from src.llm import client

    p = root() / "lessons" / lid / "lesson.json"
    L = json.loads(p.read_text(encoding="utf-8"))
    body = "\n".join(f"{i + 1}. [{display(l['role'])}] {l['en']}" for i, l in enumerate(L["lines"]))
    q = KEY_PROMPT.format(target=L.get("target") or "", new="、".join(w["en"] for w in L.get("new_words") or []) or "无", lines=body)
    for _ in range(3):
        t = client.chat_full(WRITER, q, max_tokens=800, timeout=90).text.strip().strip("`")
        try:
            keys = json.loads(t[t.index("["): t.rindex("]") + 1])
        except ValueError:
            continue
        if len(keys) == len(L["lines"]):
            break
    else:
        raise RuntimeError(f"{lid} 核心词标不出来")
    for l, x in zip(L["lines"], keys):
        x = x if isinstance(x, dict) else {"key": x}
        k = str(x.get("key") or "").strip()
        l["key"] = k if k and k.lower() in {word_key(w) for w in l["en"].split()} else ""
        l["dict"] = _rank(x.get("dict"))
    p.write_text(json.dumps(L, ensure_ascii=False, indent=1), encoding="utf-8")
    return [(l["key"], l["dict"]) for l in L["lines"]]


def main(ids: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    todo = [l for l in LESSONS if not ids or l[0] in ids]
    # 先把所有角色画好（同一个角色多课共用，避免并行时重复画）
    chars = sorted({r for l in todo for r in l[4]})
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(character, chars))
    with ThreadPoolExecutor(3) as ex:
        for l, r in zip(todo, ex.map(lambda l: _safe(l, bool(ids)), todo)):
            print(l[0], "OK" if isinstance(r, dict) else f"失败：{r}", flush=True)


def _safe(l, force):
    try:
        return build(l, force=force)
    except Exception as e:  # noqa: BLE001 一课失败不影响别的
        log.exception("lesson %s", l[0])
        return str(e)[:200]


if __name__ == "__main__":
    main(sys.argv[1:])
