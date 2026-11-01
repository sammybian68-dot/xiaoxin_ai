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
KINDS = ("math", "tian", "essay", "dictation")  # 排版：口算、田字格（看拼音写词语、听写）、作文稿纸、生词默写（多页）


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


# ---------------------------------------------------------------- 出题（题型在 server/exercises/，这里只存和排版）

def math_items(count: int, ops: str = "+-", max_n: int = 100, carry: bool = True, seed: int | None = None) -> list[dict]:
    """老接口（测试用）：通用口算。"""
    from server.exercises.math import arithmetic

    return arithmetic.items(count, ops, max_n, carry, random.Random(seed))


def create(kind: str, *, subject: str = "", title: str = "", for_kid: str = "", **opts) -> dict:
    """出一套题并存下来。kind 是 server/exercises 里登记的题型；老名字（math / pinyin）也认。
    opts 里的 topic 是作文题目（不是题型）。"""
    from server import exercises

    topic = {"math": "arithmetic", "pinyin": "pinyin_words"}.get(kind, kind)
    try:
        t, layout, items, extra = exercises.create(subject, topic, title=title, **opts)
    except exercises.ExerciseError as e:
        raise WorksheetError(str(e)) from e
    ws = {"id": store._new_id(), "kind": layout, "topic": topic, "subject": exercises.TOPICS[topic].SUBJECT,
          "for": for_kid, "created": time.time(), "title": t, "items": items, **extra}
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
    if ws["kind"] == "dictation":
        listen = sum(1 for it in ws["items"] if it.get("part") == "listen")
        write = [it["q"] or "积累" for it in ws["items"] if it.get("part") == "write"]
        return (f"练习纸 {ws['id']}「{ws['title']}」，{len(pages(ws))} 页：听写 {listen} 个词"
                + (f"，古诗和积累 {len(write)} 题（{'、'.join(write)}）" if write else ""))
    what = {"math": f"{n} 道口算", "tian": f"{n} 个词语", "pinyin": f"{n} 个词语",
            "essay": "1 篇作文"}[ws["kind"]]
    return f"练习纸 {ws['id']}「{ws['title']}」，{what}"


# ---------------------------------------------------------------- 排版（A4，PIL）

def _header(d, ws: dict, kid_name: str) -> int:
    d.text((W // 2, MARGIN), ws["title"], font=_font("bold", 64), fill=INK, anchor="mt")
    y = MARGIN + 110
    f = _font("sans", 32)
    d.text((MARGIN, y), f"姓名：{kid_name or '________'}", font=f, fill=INK)
    d.text((MARGIN + 420, y), "日期：____月____日", font=f, fill=INK)
    d.text((W - MARGIN, y), "用时：______  得分：______", font=f, fill=INK, anchor="ra")
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


def render(ws: dict, answers: bool = False, kid_name: str = "", page: int = 0) -> bytes:
    """排一页 A4，返回 JPEG。answers=True 是答案页（家长看）。生词默写可能好几页，page 是第几页。"""
    from PIL import Image, ImageDraw

    if ws["kind"] == "dictation":
        return _render_dictation(ws, answers, kid_name, page)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    y = _header(d, ws, kid_name)
    if answers:
        d.text((W - MARGIN, MARGIN + 20), "答案", font=_font("bold", 40), fill=(200, 60, 60), anchor="ra")
    items = ws["items"]
    if ws["kind"] == "math":
        n = len(items)
        cols = 6 if n > 80 else 5 if n > 60 else 4 if n > 40 else 3
        left = 60 if cols >= 5 else MARGIN  # 题多：边距收一点，一页放下
        rows = -(-n // cols)
        bottom = 90 if n > 100 else MARGIN  # 150 道（6×25）：底边也收一点
        row_h = min(100, (H - y - bottom) // rows)
        col_w = (W - 2 * left) // cols
        widest = max(len(it["q"]) for it in items)
        f = _font("sans", min(44, int(row_h * 0.5), int(col_w / (widest * 0.62 + 3))))
        for i, it in enumerate(items):
            c, r = divmod(i, rows)  # 竖着排：一列做完再下一列，和纸质卷子一样
            x, yy = left + c * col_w, y + r * row_h
            d.text((x, yy), it["q"], font=f, fill=INK)
            qw = d.textlength(it["q"] + " ", font=f)
            line_end = x + col_w - 18
            if answers:
                d.text((x + qw, yy), it["a"], font=f, fill=(200, 60, 60))
            elif line_end - (x + qw) > 30:
                base = yy + int(f.size * 1.05)
                d.line([(x + qw, base), (line_end, base)], fill=INK, width=2)
    elif ws["kind"] in ("tian", "pinyin", "dictation"):
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
            label = it["q"]
            d.text((x + w // 2, yy), label, font=fno if label.endswith(".") else fpy, fill=INK, anchor="mt")
            for k, ch in enumerate(it["a"]):
                _tian(d, x + k * s, yy + 52, s, ch if answers else "", fch)
            x += w + gap_x
        if ws.get("topic") == "dictation" and not answers:
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


# ---------------------------------------------------------------- 生词默写（多页：听写格 → 古诗积累填空）

S_DICT = 104   # 听写田字格边长（一页放下 40 来个两字词）


def pages(ws: dict) -> list[list[dict]]:
    """生词默写分页：每页放哪些块。块是 ("listen", 一行的词) 或 ("write", 一题)。一页高度按块高累加，放不下换页。"""
    if ws["kind"] != "dictation":
        return [[{"all": True}]]
    top, bottom = MARGIN + 230, H - MARGIN
    out, cur, y = [], [], top
    usable = W - 2 * MARGIN

    def put(block, h):
        nonlocal cur, y
        if cur and y + h > bottom:
            out.append(cur)
            cur, y = [], MARGIN + 60
        cur.append(block)
        y += h

    listen = [it for it in ws["items"] if it.get("part") == "listen"]
    if listen:
        put({"head": "一、听写词语（听小新读，每个词读两遍）"}, 80)
        row, w = [], 0
        for it in listen:
            need = len(it["a"]) * S_DICT + 40
            if row and w + need > usable:
                put({"row": row}, S_DICT + 64)
                row, w = [], 0
            row.append(it)
            w += need
        if row:
            put({"row": row}, S_DICT + 64)
    write = [it for it in ws["items"] if it.get("part") == "write"]
    if write:
        # 整首默写的古诗（每行都是空）窄：两首并排一块；别的（句子里挖空、积累）一题一块，占整行
        groups, i = [], 0
        while i < len(write):
            if _whole_poem(write[i]) and i + 1 < len(write) and _whole_poem(write[i + 1]):
                groups.append([write[i], write[i + 1]])
                i += 2
            else:
                groups.append([write[i]])
                i += 1
        head = {"head": ("二、" if listen else "") + "古诗和积累"}
        for k, g in enumerate(groups):
            h = max((90 if it["q"] else 0) + len(it["lines"]) * 96 for it in g) + 30
            if k == 0:
                put({"head_with": head["head"], "write": g}, h + 90)  # 大标题跟着第一题，不单独落在页底
            else:
                put({"write": g}, h)
    if cur:
        out.append(cur)
    return out


def _whole_poem(it: dict) -> bool:
    from server.exercises.chinese.word_dictation import BLANK

    return bool(it["q"]) and all(BLANK.sub("", line).strip("，。；、 ") == "" for line in it["lines"])


def _render_dictation(ws: dict, answers: bool, kid_name: str, page: int) -> bytes:
    from PIL import Image, ImageDraw

    from server.exercises.chinese.word_dictation import BLANK

    blocks = pages(ws)[page]
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    y = _header(d, ws, kid_name) if page == 0 else MARGIN + 20
    if answers:
        d.text((W - MARGIN, MARGIN + 20 if page == 0 else 50), "答案", font=_font("bold", 40), fill=(200, 60, 60), anchor="ra")
    red = (200, 60, 60)
    fno, fch, fhead = _font("sans", 26), _font("serif", 78), _font("bold", 40)
    fpoem, fby = _font("serif", 52), _font("sans", 32)
    for b in blocks:
        if "head" in b:
            d.text((MARGIN, y + 10), b["head"], font=fhead, fill=INK)
            y += 80
        elif "row" in b:
            x = MARGIN
            for it in b["row"]:
                w = len(it["a"]) * S_DICT
                d.text((x, y + 4), it["q"], font=fno, fill=MUTED)
                for k, ch in enumerate(it["a"]):
                    _tian(d, x + k * S_DICT, y + 40, S_DICT)
                    if answers:
                        d.text((x + k * S_DICT + S_DICT // 2, y + 40 + S_DICT // 2), ch, font=fch, fill=red, anchor="mm")
                x += w + 40
            y += S_DICT + 64
        else:
            if b.get("head_with"):
                d.text((MARGIN, y + 10), b["head_with"], font=fhead, fill=INK)
                y += 90
            col_w = (W - 2 * MARGIN) // len(b["write"])
            bottom = y
            for c, it in enumerate(b["write"]):
                x0, yy = MARGIN + c * col_w, y
                if it["q"]:
                    d.text((x0, yy + 10), it["q"], font=_font("bold", 46), fill=INK)
                    if it.get("by"):
                        d.text((x0 + d.textlength(it["q"], font=_font("bold", 46)) + 24, yy + 22), it["by"], font=fby, fill=MUTED)
                    yy += 90
                for line in it["lines"]:
                    x = x0 + 30
                    for k, part in enumerate(BLANK.split(line)):
                        if k % 2 == 0:  # 原文
                            d.text((x, yy + 20), part, font=fpoem, fill=INK)
                            x += d.textlength(part, font=fpoem)
                        else:           # 空：按字数留宽，答案页填红字
                            wd = max(2, len(part)) * 56 + 20
                            d.line([(x + 6, yy + 82), (x + wd - 6, yy + 82)], fill=INK, width=2)
                            if answers:
                                d.text((x + wd // 2, yy + 20), part, font=fpoem, fill=red, anchor="ma")
                            x += wd
                    yy += 96
                bottom = max(bottom, yy)
            y = bottom + 30
    n = len(pages(ws))
    d.text((W // 2, H - 60), f"小新出题 · 第 {page + 1} 页，共 {n} 页" if n > 1 else "小新出题", font=_font("sans", 22),
           fill=MUTED, anchor="mb")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, dpi=(200, 200))
    return buf.getvalue()


def page_files(ws: dict, answers: bool = False, kid_name: str = "") -> list[dict]:
    """每一页一个文件（生词默写可能好几页；别的题型就一页）。"""
    if ws["kind"] != "dictation":
        return [page_file(ws, answers, kid_name)]
    key = "answer_files" if answers else "page_files"
    have = ws.get(key) or []
    if have and all(store.file_info(f) for f in have):
        return [store.file_info(f)[1] for f in have]
    metas = []
    n = len(pages(ws))
    for i in range(n):
        data = render(ws, answers, kid_name, i)
        fid, path = store.allocate()
        path.write_bytes(data)
        suffix = ("（答案）" if answers else "") + (f"第{i + 1}页" if n > 1 else "")
        meta = store.commit(fid, f"{ws['title']}{suffix}.jpg", "image/jpeg", len(data), owner="xiaoxin")
        store.variant_path(fid, "thumb")
        store.variant_path(fid, "screen")
        metas.append(meta)
    ws[key] = [m["id"] for m in metas]
    ws["page_file" if not answers else "answer_file"] = metas[0]["id"]
    store._write_json(_dir() / f"{ws['id']}.json", ws)
    return metas


def page_file(ws: dict, answers: bool = False, kid_name: str = "") -> dict:
    """排好的那页存成一个普通文件（store.files，图片三档都有），聊天里当图片发、打印也用它。缓存：同一份只排一次。"""
    if ws["kind"] == "dictation":
        return page_files(ws, answers, kid_name)[0]
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
