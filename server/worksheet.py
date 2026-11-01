"""练习纸：出题 + 排成 A4。给小新的出题工具（server/tools/worksheet.py）和打印用。

题怎么来：
- 数学口算：代码生成，题和答案都是算出来的，不让模型编（模型算错一道，孩子就白练了）。
- 语文（看拼音写词语、听写默写、作文）：字词和题目由小新按年级、课文挑好传进来，这里只排版；拼音用本机字库查，
  查不到的（多音字、生僻字）用小新给的。

每份练习纸存一个 JSON（DATA/worksheets/<id>.json）：kind、标题、题目、答案、给谁出的；渲染出两页图片：
练习页（打印给孩子）、答案页（家长看，默认不打）。A4 按 200 dpi 排：1654×2339。
"""

from __future__ import annotations

import io
import json
import random
import re
import time
from pathlib import Path

from server import store

W, H = 1654, 2339          # A4 @ 200 dpi
MARGIN = 140
INK = (30, 30, 30)
GRID = (210, 120, 120)     # 田字格红线
GRID_DASH = (235, 180, 180)
MUTED = (140, 140, 140)
FONTS = {
    "sans": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc"],
    "serif": ["/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
              "/System/Library/Fonts/Hiragino Sans GB.ttc"],
    "bold": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc"],
}
KINDS = ("math", "pinyin", "dictation", "essay")
MAX_ITEMS = {"math": 100, "pinyin": 30, "dictation": 40, "essay": 1}


class WorksheetError(ValueError):
    pass


def _font(kind: str, size: int):
    from PIL import ImageFont

    for path in FONTS[kind]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size, index=2 if path.endswith("CJK-Regular.ttc") or path.endswith("CJK-Bold.ttc") else 0)
    return ImageFont.load_default(size)


def _dir() -> Path:
    return store.DATA / "worksheets"


# ---------------------------------------------------------------- 出题

OPS = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "×": lambda a, b: a * b, "÷": lambda a, b: a // b}


def math_items(count: int, ops: str = "+-", max_n: int = 100, carry: bool = True, seed: int | None = None) -> list[dict]:
    """口算题。ops 里的运算随机混；减法不出负数；除法都能除尽；max_n 是结果（加减）或乘数（乘除）的上限。
    carry=False：加法不进位、减法不退位（一年级上册）。同一份里不重复。"""
    ops = "".join(o for o in ops if o in OPS) or "+"
    rnd = random.Random(seed)
    out, seen, tries = [], set(), 0
    while len(out) < count and tries < count * 200:
        tries += 1
        op = rnd.choice(ops)
        if op in "+-":
            a, b = rnd.randint(1, max_n), rnd.randint(1, max_n)
            if op == "+":
                if a + b > max_n:
                    continue
                if not carry and (a % 10 + b % 10 >= 10):
                    continue
            else:
                a, b = max(a, b), min(a, b)
                if a == b or (not carry and a % 10 < b % 10):
                    continue
        elif op == "×":
            top = min(max_n, 9) if max_n <= 81 else min(max_n, 99)
            a, b = rnd.randint(2, top), rnd.randint(2, 9)
        else:
            b = rnd.randint(2, 9)
            q = rnd.randint(1, 9) if max_n <= 81 else rnd.randint(2, 99 // b)  # 表内除法：商 1–9
            a = b * q
        key = (a, op, b)
        if key in seen:
            continue
        seen.add(key)
        out.append({"q": f"{a} {op} {b} =", "a": str(OPS[op](a, b))})
    return out


def _pinyin_of(word: str) -> str:
    from server import hanzi

    parts = []
    for ch in word:
        e = hanzi.lookup(ch)
        py = (e or {}).get("pinyin") or []
        if not py:
            return ""
        parts.append(py[0] if isinstance(py, list) else str(py).split(",")[0])
    return " ".join(parts)


def _clean_words(words) -> list[str]:
    if isinstance(words, str):
        words = re.split(r"[\s,，、;；]+", words)
    out = []
    for w in words or []:
        w = re.sub(r"[^一-鿿]", "", str(w))
        if 1 <= len(w) <= 8 and w not in out:
            out.append(w)
    return out


# ---------------------------------------------------------------- 存取

def create(kind: str, *, title: str = "", for_kid: str = "", **opts) -> dict:
    """出一份练习纸并存下来。返回 {id, kind, title, items, …}。参数不对抛 WorksheetError。"""
    if kind not in KINDS:
        raise WorksheetError(f"不认识的练习：{kind}")
    ws = {"id": store._new_id(), "kind": kind, "for": for_kid, "created": time.time()}
    opts.pop("answers", None)
    if kind == "math":
        count = max(10, min(int(opts.get("count") or 50), MAX_ITEMS["math"]))
        max_n = max(10, min(int(opts.get("max") or 100), 1000))
        items = math_items(count, str(opts.get("ops") or "+-"), max_n, bool(opts.get("carry", True)), opts.get("seed"))
        if not items:
            raise WorksheetError("按这个条件出不来题，换个范围")
        ops_name = "".join({"+": "加", "-": "减", "×": "乘", "÷": "除"}[o] for o in dict.fromkeys(str(opts.get("ops") or "+-")) if o in OPS)
        ws.update(title=title or f"{max_n} 以内{ops_name}法口算", items=items)
    elif kind in ("pinyin", "dictation"):
        words = _clean_words(opts.get("words"))[:MAX_ITEMS[kind]]
        if not words:
            raise WorksheetError("要给几个词语（汉字）")
        given = opts.get("pinyin") or {}
        items = []
        for w in words:
            py = (given.get(w) if isinstance(given, dict) else "") or _pinyin_of(w)
            items.append({"q": py, "a": w})
        if kind == "pinyin" and any(not it["q"] for it in items):
            missing = "、".join(it["a"] for it in items if not it["q"])
            raise WorksheetError(f"这些词查不到拼音，要在 pinyin 里给：{missing}")
        ws.update(title=title or ("看拼音，写词语" if kind == "pinyin" else "听写"), items=items)
    else:
        topic = str(opts.get("topic") or "").strip()[:40]
        if not topic:
            raise WorksheetError("作文要有题目")
        words = max(100, min(int(opts.get("words_count") or 300), 800))
        ws.update(title=title or "作文", items=[{"q": topic, "a": str(opts.get("hint") or "")[:200]}],
                  topic=topic, words_count=words)
    ws["title"] = ws["title"][:30]
    _dir().mkdir(parents=True, exist_ok=True)
    store._write_json(_dir() / f"{ws['id']}.json", ws)
    return ws


def get(wid: str) -> dict:
    path = _dir() / f"{store._check(wid)}.json"
    if not path.is_file():
        raise KeyError(wid)
    return json.loads(path.read_text(encoding="utf-8"))


def latest(for_kid: str) -> dict | None:
    """这个孩子最近一份（「把这套题打印出来」说的就是它）。"""
    best = None
    for p in _dir().glob("*.json") if _dir().is_dir() else []:
        try:
            ws = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if ws.get("for") == for_kid and (best is None or ws["created"] > best["created"]):
            best = ws
    return best


def summary(ws: dict) -> str:
    """给模型看的一行：出了什么、多少题（不给答案，免得它直接念答案）。"""
    n = len(ws["items"])
    what = {"math": f"{n} 道口算", "pinyin": f"{n} 个词语", "dictation": f"{n} 个词语", "essay": "1 篇作文"}[ws["kind"]]
    return f"练习纸 {ws['id']}「{ws['title']}」，{what}"


# ---------------------------------------------------------------- 排版（A4，PIL）

def _header(d, ws: dict, kid_name: str) -> int:
    d.text((W // 2, MARGIN), ws["title"], font=_font("bold", 64), fill=INK, anchor="mt")
    y = MARGIN + 110
    f = _font("sans", 32)
    d.text((MARGIN, y), f"姓名：{kid_name or '________'}", font=f, fill=INK)
    d.text((W // 2 - 120, y), "日期：____月____日", font=f, fill=INK)
    d.text((W - MARGIN, y), "用时：______  对了：____ 题", font=f, fill=INK, anchor="ra")
    y += 70
    d.line([(MARGIN, y), (W - MARGIN, y)], fill=MUTED, width=2)
    return y + 50


def _tian(d, x: int, y: int, s: int, ch: str = "", font=None) -> None:
    """田字格：外框实线、中间十字虚线；ch 给了就写进去（答案页）。"""
    d.rectangle([x, y, x + s, y + s], outline=GRID, width=3)
    for i in range(0, s, 14):
        d.line([(x + i, y + s // 2), (x + min(i + 7, s), y + s // 2)], fill=GRID_DASH, width=2)
        d.line([(x + s // 2, y + i), (x + s // 2, y + min(i + 7, s))], fill=GRID_DASH, width=2)
    if ch:
        d.text((x + s // 2, y + s // 2), ch, font=font, fill=INK, anchor="mm")


def render(ws: dict, answers: bool = False, kid_name: str = "") -> bytes:
    """排一页 A4，返回 JPEG。answers=True 是答案页（家长看）。"""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    y = _header(d, ws, kid_name)
    if answers:
        d.text((W - MARGIN, MARGIN + 20), "答案", font=_font("bold", 40), fill=(200, 60, 60), anchor="ra")
    items = ws["items"]
    if ws["kind"] == "math":
        cols = 4 if len(items) > 50 else 3
        rows = -(-len(items) // cols)
        row_h = min(90, (H - y - MARGIN) // rows)
        f = _font("sans", min(46, int(row_h * 0.52)))
        col_w = (W - 2 * MARGIN) // cols
        for i, it in enumerate(items):
            r, c = divmod(i, cols)
            x, yy = MARGIN + c * col_w, y + r * row_h
            text = it["q"] + (f" {it['a']}" if answers else "")
            d.text((x, yy), text, font=f, fill=(200, 60, 60) if answers else INK)
    elif ws["kind"] in ("pinyin", "dictation"):
        s = 120                                   # 田字格边长
        gap_x, gap_y = 44, 70
        per_row = max(1, (W - 2 * MARGIN + gap_x) // (max(len(it["a"]) for it in items) * s + gap_x))
        fpy = _font("sans", 34)
        fch = _font("serif", 92)
        fno = _font("sans", 28)
        x, yy = MARGIN, y + 10
        for i, it in enumerate(items):
            w = len(it["a"]) * s
            if x + w > W - MARGIN:
                x, yy = MARGIN, yy + s + 60 + gap_y
            if yy + s + 60 > H - MARGIN:
                break  # 一页放不下就截掉（工具那边已限制数量）
            label = it["q"] if ws["kind"] == "pinyin" else f"{i + 1}."
            d.text((x + w // 2, yy), label, font=fpy if ws["kind"] == "pinyin" else fno, fill=INK, anchor="mt")
            for k, ch in enumerate(it["a"]):
                _tian(d, x + k * s, yy + 52, s, ch if answers else "", fch)
            x += w + gap_x
        if ws["kind"] == "dictation" and not answers:
            d.text((MARGIN, H - MARGIN + 30), "家长念词语，孩子写在格子里。", font=_font("sans", 28), fill=MUTED)
    else:  # 作文：题目 + 方格稿纸
        d.text((MARGIN, y), f"题目：{ws['topic']}", font=_font("bold", 44), fill=INK)
        y += 90
        if answers and ws["items"][0]["a"]:
            d.text((MARGIN, y), "提示：" + ws["items"][0]["a"], font=_font("sans", 30), fill=(200, 60, 60))
            y += 60
        cell = 66
        cols = (W - 2 * MARGIN) // cell
        x0 = (W - cols * cell) // 2
        rows = (H - MARGIN - y) // (cell + 14)
        for r in range(rows):
            yy = y + r * (cell + 14)
            for c in range(cols):
                d.rectangle([x0 + c * cell, yy, x0 + (c + 1) * cell, yy + cell], outline=(170, 200, 170), width=2)
            if (r + 1) * cols % 100 < cols:  # 每满 100 字标一下
                d.text((W - MARGIN + 6, yy + cell), str((r + 1) * cols // 100 * 100), font=_font("sans", 20), fill=MUTED, anchor="ls")
    d.text((W // 2, H - 60), "小新出题", font=_font("sans", 22), fill=MUTED, anchor="mb")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, dpi=(200, 200))
    return buf.getvalue()


def page_file(ws: dict, answers: bool = False, kid_name: str = "") -> dict:
    """排好的那页存成一个普通文件（store.files，图片三档都有），聊天里当图片发、打印也用它。缓存：同一份只排一次。"""
    key = "answer_file" if answers else "page_file"
    if ws.get(key) and store.file_info(ws[key]):
        return store.file_info(ws[key])[1]
    data = render(ws, answers, kid_name)
    fid, path = store.allocate()
    path.write_bytes(data)
    meta = store.commit(fid, f"{ws['title']}{'（答案）' if answers else ''}.jpg", "image/jpeg", len(data), owner="xiaoxin")
    store.variant_path(fid, "thumb")
    store.variant_path(fid, "screen")
    ws[key] = fid
    store._write_json(_dir() / f"{ws['id']}.json", ws)
    return meta
