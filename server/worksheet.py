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
# 四角二维码（批改时 OpenCV 读码 → 认卷子 + 拉正，server/tools/check_work.py）：内容「卷子id-第几页-第几个角」，
# 大写字母数字 20 个字符 → QR 版本 1（21×21 模块），一个模块 6 px，边长 126 px ≈ 1.6 cm。
# 内容「XX.卷子id前8位.第几页.第几个角」15 个字符（QR 字母数字模式，版本 1、纠错 Q，21×21 模块），
# 一个模块 9 px，边长 189 px ≈ 2.4 cm（打印再缩 7%）。2026-11-06 模拟 40 张手机照（透视、转 ±12°/倒拍、模糊、
# 只传 2048 预览图）：7 px 时对角两码都读到 34/40，9 px 38/40，11 px 39/40；9 px 不太占地方。
QR_MOD, QR_N = 9, 21
QR = QR_MOD * QR_N
QR_OFF = 36                # 离纸边（打印时整页还会缩 7% 留白，见 printing._fit_a4）
SAFE = QR_OFF + QR + 12    # 有二维码的卷子四边至少留这么宽（真实像素），正文不压到角上的码
CORNERS = ((QR_OFF, QR_OFF), (W - QR_OFF - QR, QR_OFF), (W - QR_OFF - QR, H - QR_OFF - QR), (QR_OFF, H - QR_OFF - QR))
FIT_SCALES = (1.0, 0.95, 0.9, 0.85, 0.8, 0.76, 0.72)  # 生词默写尽量一页：整页按比例缩，最小 0.72（田字格 75 px ≈ 9.5 mm）
# 排版版本：1 = 老卷子（没有二维码，2026-11-06 以前出的）；2 = 四角二维码 + 卷号 + 尽量一页。老卷子照老样子排（已经打出来了）
LAYOUT_V = 2
X0 = 100                   # 版本 2：正文框离左右纸边（真实像素）；上下留 SAFE 给二维码和卷号
BLUE = (40, 90, 200)
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
    # 英文（小剧场默写）：字母要清楚、a 和 g 是印刷体，孩子照着认
    "en": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"],
    "en_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
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
          "for": for_kid, "created": time.time(), "title": t, "items": items, "v": LAYOUT_V, **extra}
    if topic == "scene_dictation":
        ws["lesson"] = opts.get("lesson", "")
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

def _header(d, ws: dict, kid_name: str, W: int = W) -> int:
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
    return _jpeg(render_page(ws, answers, kid_name, page)[0])


def _jpeg(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, dpi=(200, 200))
    return buf.getvalue()


def render_page(ws: dict, answers: bool = False, kid_name: str = "", page: int = 0):
    """排一页，返回 (PIL 图, 这一页每个答案格在纸上的位置 [{k, box}])。k 是 practice.answer_key 里的第几题（从 0 起），
    box 是 A4 像素坐标（200 dpi）。批改时按它切格子、画红圈（server/tools/check_work.py）。

    版本 2 的卷子：正文先排在一张「虚拟页」上（为了一页放下，虚拟页可以比 A4 大，见 fit），再整体缩放贴进
    A4 中间的正文框；四角印二维码（卷子 id + 第几页 + 第几个角），上下印人能看懂的卷号和页码。"""
    from PIL import Image, ImageDraw

    boxes: list[dict] = []
    if ws.get("v", 1) < 2:
        im = Image.new("RGB", (W, H), "white")
        if ws["kind"] == "dictation":
            _render_dictation(ImageDraw.Draw(im), ws, answers, kid_name, page, W, H, boxes, footer=True)
        else:
            _render_plain(ImageDraw.Draw(im), ws, answers, kid_name, W, H, boxes, footer=True)
        return im, boxes
    g = fit(ws)
    vw, vh = g["vw"], g["vh"]
    virt = Image.new("RGB", (vw, vh), "white")
    if ws["kind"] == "dictation":
        _render_dictation(ImageDraw.Draw(virt), ws, answers, kid_name, page, vw, vh, boxes, footer=False)
    else:
        _render_plain(ImageDraw.Draw(virt), ws, answers, kid_name, vw, vh, boxes, footer=False)
    cx0, cy0, cx1, cy1 = g["crop"]
    s = g["s"]
    body = virt.crop((cx0, cy0, cx1, cy1)).resize((round((cx1 - cx0) * s), round((cy1 - cy0) * s)), Image.LANCZOS)
    im = Image.new("RGB", (W, H), "white")
    ox, oy = (W - body.width) // 2, SAFE
    im.paste(body, (ox, oy))
    for b in boxes:
        x1, y1, x2, y2 = b["box"]
        b["box"] = [round(ox + (x1 - cx0) * s), round(oy + (y1 - cy0) * s), round(ox + (x2 - cx0) * s), round(oy + (y2 - cy0) * s)]
    _codes(ImageDraw.Draw(im), im, ws, page, g["n"])
    return im, boxes


def code_of(ws: dict) -> str:
    """人看的卷号：卷子 id 前 4 位大写（一个孩子手上同时就几份，4 位够分）。二维码里放前 8 位。"""
    return ws["id"][:4].upper()


def qr_text(ws: dict, page: int, corner: int) -> str:
    """二维码内容：XX.卷子id前8位.第几页.第几个角（1 左上 2 右上 3 右下 4 左下）。只用 QR 字母数字模式的字符，码最小。"""
    return f"XX.{ws['id'][:8].upper()}.{page + 1}.{corner}"


def _qr(text: str):
    """21×21 的码（版本 1、纠错 Q），放大成 QR×QR 的黑白图。"""
    import cv2
    import numpy as np
    from PIL import Image

    p = cv2.QRCodeEncoder_Params()
    p.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_Q
    q = cv2.QRCodeEncoder.create(p).encode(text)
    ys, xs = np.where(q == 0)
    q = q[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return Image.fromarray(q).resize((QR, QR), Image.NEAREST).convert("RGB")


def _codes(d, im, ws: dict, page: int, n: int) -> None:
    """四角二维码 + 上下的卷号、页码（孩子、家长一眼看出这几张是不是一份、第几页）。"""
    for k, (x, y) in enumerate(CORNERS):
        im.paste(_qr(qr_text(ws, page, k + 1)), (x, y))
    code = code_of(ws)
    tag = f"卷号 {code}" + (f"　第 {page + 1} 页 / 共 {n} 页" if n > 1 else "")
    d.text((W // 2, QR_OFF + QR // 2), tag, font=_font("bold", 34), fill=MUTED, anchor="mm")
    d.text((W // 2, H - QR_OFF - QR // 2), f"小新出题 · {tag}", font=_font("sans", 28), fill=MUTED, anchor="mm")


def fit(ws: dict) -> dict:
    """版本 2 的版面：正文排在多大的虚拟页上、缩多少贴进 A4。生词默写从 1.0 往下试，第一个能一页放下的缩放就用它；
    最小 0.72 还放不下才分页（每页同样缩放）。别的题型一页、缩放按正文框定。
    返回 {s, vw, vh, crop, n}：crop 是虚拟页上要贴的那块（去掉虚拟页自己的白边）。"""
    if ws.get("v", 1) < 2:
        return {"s": 1.0, "vw": W, "vh": H, "crop": (0, 0, W, H), "n": len(pages(ws, W, H)) if ws["kind"] == "dictation" else 1}
    box_w, box_h = W - 2 * X0, H - 2 * SAFE           # A4 上的正文框
    if ws["kind"] != "dictation":
        s = min(box_w / W, box_h / H)                  # 老排版整页（含页边）缩进去：口算 150 道用的就是窄页边
        return {"s": s, "vw": W, "vh": H, "crop": (0, 0, W, H), "n": 1}
    pad_x, pad_top, pad_bot = MARGIN - 40, MARGIN - 30, MARGIN - 40   # 虚拟页的白边只留一点
    for s in FIT_SCALES:
        vw = round(box_w / s) + 2 * pad_x
        vh = round(box_h / s) + pad_top + pad_bot
        n = len(pages(ws, vw, vh))
        if n == 1 or s == FIT_SCALES[-1]:
            return {"s": s, "vw": vw, "vh": vh, "crop": (pad_x, pad_top, vw - pad_x, vh - pad_bot), "n": n}
    raise AssertionError


def _render_plain(d, ws: dict, answers: bool, kid_name: str, W: int, H: int, boxes: list, footer: bool) -> None:
    """口算、田字格、作文：画在 W×H 的页上（版本 2 是虚拟页）。boxes 里记每题答案写在哪。"""
    y = _header(d, ws, kid_name, W)
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
            boxes.append({"k": i, "box": [x, yy - 4, line_end, yy + row_h - 4]})   # 整道题（算式 + 答案）
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
            boxes.append({"k": i, "box": [x, yy + 52, x + w, yy + 52 + s]})
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
    if footer:
        d.text((W // 2, H - 60), "小新出题", font=_font("sans", 22), fill=MUTED, anchor="mb")


# ---------------------------------------------------------------- 生词默写（多页：听写格 → 古诗积累填空）

S_DICT = 104   # 听写田字格边长（一页放下 40 来个两字词）


def pages(ws: dict, vw: int | None = None, vh: int | None = None) -> list[list[dict]]:
    """生词默写分页：每页放哪些块。块是 ("listen", 一行的词) 或 ("write", 一题)。一页高度按块高累加，放不下换页。
    vw × vh：排版用的「虚拟页」多大（版本 2 为了一页放下会把虚拟页放大、整页缩小印，见 fit）；不给就按 fit 算的。"""
    if ws["kind"] != "dictation":
        return [[{"all": True}]]
    if not vh:
        g = fit(ws)
        vw, vh = g["vw"], g["vh"]
    top, bottom = MARGIN + 230, vh - MARGIN
    out, cur, y = [], [], top
    usable = vw - 2 * MARGIN

    def put(block, h):
        nonlocal cur, y
        if cur and y + h > bottom:
            out.append(cur)
            cur, y = [], MARGIN + 60
        cur.append(block)
        y += h

    listen = [it for it in ws["items"] if it.get("part") == "listen"]
    if listen and listen[0].get("en"):   # 英语：一句一行四线三格
        put({"head": "一、听写句子（听小新读，每句读两遍）"}, 80)
        for it in listen:
            put({"en_line": it}, EN_LINE_H)
        listen = []
        ws_en = True
    else:
        ws_en = False
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
        head = {"head": ("二、" if listen or ws_en else "") + ("补全对话" if ws_en else "古诗和积累")}
        if ws_en:   # 英文对话：一句一块（整段一块太高，会整个挤到下一页，第一页下半截空着）
            it = write[0]
            put({"head": head["head"]}, 80)
            if it.get("bank"):   # 词库：单独一个框，按宽度换行
                put({"en_bank": it["bank"]}, _bank_rows(it["bank"]) * EN_ROW + 40)
            for li, line in enumerate(it["lines"]):
                put({"en_dialogue": {**it, "lines": [line]}, "src": it, "li": li},
                    _write_rows({"en": True, "lines": [line]}) * EN_ROW)
            groups = []
        for k, g in enumerate(groups):
            h = max((90 if it["q"] else 0) + _write_rows(it) * 96 for it in g) + 30
            if k == 0:
                put({"head_with": head["head"], "write": g}, h + 90)  # 大标题跟着第一题，不单独落在页底
            else:
                put({"write": g}, h)
    if cur:
        out.append(cur)
    return out


EN_LINE_H = 200   # 英文听写一行：句号 + 四线三格
EN_ROW = 72       # 英文对话一行的高度（估算和实际画都用它，分页才准）
EN_CHARS = 52     # 英文 44 号字一行大约放多少个字符


def _write_rows(it: dict) -> int:
    """填空题占几行：中文按原行；英文对话长句按宽度折行（估：每行 ~38 个字符）。"""
    if not it.get("en"):
        return len(it["lines"])
    return sum(max(1, -(-len(line) // EN_CHARS)) for line in it["lines"])


def _bank_rows(bank: str) -> int:
    return max(1, -(-len(bank) // (EN_CHARS - 4)))


def _whole_poem(it: dict) -> bool:
    from server.exercises.chinese.word_dictation import BLANK

    return not it.get("en") and bool(it["q"]) and all(BLANK.sub("", line).strip("，。；、 ") == "" for line in it["lines"])


def key_offsets(ws: dict) -> dict[int, int]:
    """每道题（ws["items"] 的下标）在 practice.answer_key 里从第几个开始：听写 1 个，填空每个空 1 个，别的 1 个。"""
    from server.exercises.chinese.word_dictation import BLANK

    out, k = {}, 0
    for i, it in enumerate(ws["items"]):
        out[i] = k
        k += sum(len(BLANK.findall(line)) for line in it["lines"]) if it.get("part") == "write" else 1
    return out


def _blanks_before(it: dict, li: int) -> int:
    from server.exercises.chinese.word_dictation import BLANK

    return sum(len(BLANK.findall(line)) for line in it["lines"][:li])


def _render_dictation(d, ws: dict, answers: bool, kid_name: str, page: int, W: int, H: int, boxes: list,
                      footer: bool) -> None:
    from server.exercises.chinese.word_dictation import BLANK

    blocks = pages(ws, W, H)[page]
    idx = {id(it): i for i, it in enumerate(ws["items"])}
    offs = key_offsets(ws)
    y = _header(d, ws, kid_name, W) if page == 0 else MARGIN + 20
    if answers:
        d.text((W - MARGIN, MARGIN + 20 if page == 0 else 50), "答案", font=_font("bold", 40), fill=(200, 60, 60), anchor="ra")
    red = (200, 60, 60)
    fno, fch, fhead = _font("sans", 26), _font("serif", 78), _font("bold", 40)
    fpoem, fby = _font("serif", 52), _font("sans", 32)
    for b in blocks:
        if "head" in b:
            d.text((MARGIN, y + 10), b["head"], font=fhead, fill=INK)
            y += 80
        elif "en_dialogue" in b:
            rec: list = []
            y = _en_dialogue(d, b["en_dialogue"], MARGIN + 10, y, W - 2 * MARGIN - 20, answers, red, rec)
            base = offs[idx[id(b["src"])]] + _blanks_before(b["src"], b["li"])
            boxes.extend({"k": base + j, "box": bx} for j, bx in enumerate(rec))
        elif "en_bank" in b:
            fbk = _font("en", 40)
            h = _bank_rows(b["en_bank"]) * EN_ROW + 16
            d.rounded_rectangle([MARGIN, y, W - MARGIN, y + h], radius=16, outline=GRID, width=2)
            x, yy = MARGIN + 24, y + 14
            label, _, words = b["en_bank"].partition("：")
            d.text((x, yy + 10), label + "：", font=_font("sans", 34), fill=MUTED)
            x += d.textlength(label + "：", font=_font("sans", 34)) + 8
            for tok in words.split(" "):
                wd = d.textlength(tok + " ", font=fbk)
                if x + wd > W - MARGIN - 20:
                    x, yy = MARGIN + 24, yy + EN_ROW
                d.text((x, yy + 8), tok, font=fbk, fill=INK)
                x += wd
            y += h + 24
        elif "en_line" in b:
            it = b["en_line"]
            d.text((MARGIN, y + 30), it["q"], font=_font("sans", 34), fill=MUTED)
            _four_lines(d, MARGIN + 60, W - MARGIN, y + 20)
            boxes.append({"k": offs[idx[id(it)]], "box": [MARGIN + 60, y, W - MARGIN, y + 20 + 96 + 24]})
            if answers:   # 写在第三条线（基线）上，和孩子写字一样
                d.text((MARGIN + 80, y + 20 + 64), it["a"], font=_font("en", 52), fill=red, anchor="ls")
            y += EN_LINE_H
        elif "row" in b:
            x = MARGIN
            for it in b["row"]:
                w = len(it["a"]) * S_DICT
                d.text((x, y + 4), it["q"], font=fno, fill=MUTED)
                for k, ch in enumerate(it["a"]):
                    _tian(d, x + k * S_DICT, y + 40, S_DICT)
                    if answers:
                        d.text((x + k * S_DICT + S_DICT // 2, y + 40 + S_DICT // 2), ch, font=fch, fill=red, anchor="mm")
                boxes.append({"k": offs[idx[id(it)]], "box": [x, y + 40, x + w, y + 40 + S_DICT]})
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
                kb = offs[idx[id(it)]]
                if it.get("en"):
                    rec = []
                    yy = _en_dialogue(d, it, x0 + 20, yy, col_w - 40, answers, red, rec)
                    boxes.extend({"k": kb + j, "box": bx} for j, bx in enumerate(rec))
                    bottom = max(bottom, yy)
                    continue
                for line in it["lines"]:
                    x = x0 + 30
                    for k, part in enumerate(BLANK.split(line)):
                        if k % 2 == 0:  # 原文
                            d.text((x, yy + 20), part, font=fpoem, fill=INK)
                            x += d.textlength(part, font=fpoem)
                        else:           # 空：按字数留宽，答案页填红字
                            wd = max(2, len(part)) * 56 + 20
                            d.line([(x + 6, yy + 82), (x + wd - 6, yy + 82)], fill=INK, width=2)
                            boxes.append({"k": kb, "box": [x, yy + 4, x + wd, yy + 92]})
                            kb += 1
                            if answers:
                                d.text((x + wd // 2, yy + 20), part, font=fpoem, fill=red, anchor="ma")
                            x += wd
                    yy += 96
                bottom = max(bottom, yy)
            y = bottom + 30
    if footer:
        n = len(pages(ws, W, H))
        d.text((W // 2, H - 60), f"小新出题 · 第 {page + 1} 页，共 {n} 页" if n > 1 else "小新出题", font=_font("sans", 22),
               fill=MUTED, anchor="mb")


def _four_lines(d, x0: int, x1: int, y: int) -> None:
    """英语本的四线三格：上下两条细线、中间两条（第三条是基线，红色），格高 96。"""
    for k, (col, wd) in enumerate([(GRID, 2), (GRID_DASH, 2), ((214, 120, 120), 3), (GRID, 2)]):
        d.line([(x0, y + k * 32), (x1, y + k * 32)], fill=col, width=wd)


def _en_dialogue(d, it: dict, x0: int, y: int, width: int, answers: bool, red, rec: list | None = None) -> int:
    """英文对话填空：一个词一个词排，放不下换行；空格留横线（按答案长度），答案页填红字。返回画完的 y。
    rec 给了就按顺序记每个空的位置（批改画圈用）。"""
    from server.exercises.chinese.word_dictation import BLANK

    f, fb = _font("en", 44), _font("en_bold", 44)
    space = d.textlength(" ", font=f)
    for line in it["lines"]:
        who, _, rest = line.partition(": ")
        x, yy = x0, y
        d.text((x, yy + 20), who + ":", font=fb, fill=INK)
        x += d.textlength(who + ": ", font=fb)
        indent = x
        prev_blank = False
        for tok in re.split(r"(\{[^}]*\}|\s+)", rest):
            if not tok:
                continue
            if tok.isspace():
                prev_blank = False
                continue
            m = BLANK.fullmatch(tok) if tok.startswith("{") else None
            wd = (max(4, len(m.group(1))) * 26 + 30) if m else d.textlength(tok, font=f)
            glue = prev_blank and not m and re.fullmatch(r"[.,!?;:']+\S*", tok)   # 「____ .」→「____.」标点贴着空
            if glue:
                x -= space
            if x + wd > x0 + width and not glue:
                x, yy = indent, yy + EN_ROW
            if m:
                d.line([(x + 4, yy + 62), (x + wd - 4, yy + 62)], fill=INK, width=2)
                if rec is not None:
                    rec.append([round(x), yy, round(x + wd), yy + EN_ROW])
                if answers:
                    d.text((x + wd / 2, yy + 14), m.group(1), font=f, fill=red, anchor="ma")
            else:
                d.text((x, yy + 14), tok, font=f, fill=INK)
            x += wd + space
            prev_blank = bool(m)
        y = yy + EN_ROW
    return y


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
    lay = []
    for i in range(n):
        im, bx = render_page(ws, answers, kid_name, i)
        lay.append(bx)
        data = _jpeg(im)
        fid, path = store.allocate()
        path.write_bytes(data)
        suffix = ("（答案）" if answers else "") + (f"第{i + 1}页" if n > 1 else "")
        meta = store.commit(fid, f"{ws['title']}{suffix}.jpg", "image/jpeg", len(data), owner="xiaoxin")
        store.variant_path(fid, "thumb")
        store.variant_path(fid, "screen")
        metas.append(meta)
    ws[key] = [m["id"] for m in metas]
    ws["page_file" if not answers else "answer_file"] = metas[0]["id"]
    ws["boxes"] = lay
    store._write_json(_dir() / f"{ws['id']}.json", ws)
    return metas


def boxes(ws: dict) -> list[list[dict]]:
    """每页每个答案格在 A4 上的位置（render_page 记的）。出卷时存下了就用存的，没有现排一遍（不存图）。"""
    if ws.get("boxes"):
        return ws["boxes"]
    return [render_page(ws, False, "", i)[1] for i in range(len(pages(ws)))]


def page_file(ws: dict, answers: bool = False, kid_name: str = "") -> dict:
    """排好的那页存成一个普通文件（store.files，图片三档都有），聊天里当图片发、打印也用它。缓存：同一份只排一次。"""
    if ws["kind"] == "dictation":
        return page_files(ws, answers, kid_name)[0]
    key = "answer_file" if answers else "page_file"
    if ws.get(key) and store.file_info(ws[key]):
        return store.file_info(ws[key])[1]
    im, bx = render_page(ws, answers, kid_name)
    data = _jpeg(im)
    fid, path = store.allocate()
    path.write_bytes(data)
    meta = store.commit(fid, f"{ws['title']}{'（答案）' if answers else ''}.jpg", "image/jpeg", len(data), owner="xiaoxin")
    store.variant_path(fid, "thumb")
    store.variant_path(fid, "screen")
    ws[key] = fid
    ws["boxes"] = [bx]
    store._write_json(_dir() / f"{ws['id']}.json", ws)
    return meta
