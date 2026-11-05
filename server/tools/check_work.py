"""批改作业照片：check_homework。孩子 / 家长在聊天里发了卷子照片说「小新批一下」时调。

只批小新出的卷子（四角有二维码，server/worksheet.py）。流程（2026-11-06 重做，家长要快、准、话少）：
  1. 读码拉正（server/paper.py，纯 OpenCV，零点几秒）：任一对角两码读到 + 同卷同页 + 整张 A4 在照片里才合格；
     不合格直接回固定的话让孩子重拍，不调模型、不猜（家长 2026-11-05：读不到码不要大模型兜底）。
  2. 拉正的卷子上，每个答案格按出卷时记下的位置（worksheet.boxes）画一个蓝色编号框，连同标准答案（按编号）
     一起给批改模型，它**只回错的编号和孩子写的字**。位置是排版算的，不靠模型给坐标。
  3. 在拉正的卷子上把错的格子圈红、旁边写正确答案，存成小新发的图片。
  4. 话按模板拼（summary）：对了几分之几、错的哪几个（写成什么、应为什么）；看不清算错；不提对的。
后台跑：工具马上回「收到，在批」，批完发一条带红圈卷子的消息，pad 念一句（app.js 的 via=check）。
模型在「设置 → 作业」里选（AGENT_DEFAULTS 的 grade_model），说明在 server/prompts/check_work.md。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from server import store
from server.tools import printer as printer_tools

log = logging.getLogger("xiaoxin.check")

SPECS = [{"type": "function", "function": {
    "name": "check_homework",
    "description": "批改聊天里刚发的卷子照片（小新出的卷子，四角有二维码）：逐题判对错，在卷子上把错的圈出来发到聊天里。"
                   "「小新批一下」「帮我看看对不对」「检查一下作业」都用这个，不要自己看图判。后台批，批完自动发。",
    "parameters": {"type": "object", "properties": {
        "photos": {"type": "integer", "description": "批最近几张照片，默认 1（孩子一次发了好几页就填页数）"},
    }},
}}]
NAMES = {"check_homework"}
MAX_PHOTOS = 6
# 设置里能选的。2026-11-06 用女儿 11/5 的第四单元默写第 1 页评测（47 格，真错 13 个，详见 docs/check-work.md）：
#   Sonnet 5.5 13/13、15–20 秒（最好）；Opus 5.5 12/13、15–25 秒；Gemini 3.8 Flash 12/13 + 误判 1、~25 秒；
#   Kimi K3 11/13、~20 秒。DeepSeek V4.1 Flash、Grok 4.7 看这张图回空，用不了；GPT 系列 60–220 秒、漏得多，不放。
GRADE_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "gemini-3.8-flash", "kimi-k3"]
SIDE = 1000              # 只裁答题区、长边 1000 送模型：比整页 1400 快 ~5 秒，准确率不掉（1600 也没更准）
RED = (230, 30, 30)
_busy = threading.Lock()

PROMPT = """这是孩子写好的一页卷子（已经拉正），每个要孩子写答案的地方都用蓝色细框标了编号。下面是每个编号的标准答案：
{key}
逐个编号看框里孩子写的，和标准答案一个字一个字比：
- 写错字、少写、多写、没写，都算错。
- 写得认不出、涂改到看不出是什么字，也算错。
- 字迹潦草但能认出是对的字，算对。{extra}
只输出 JSON，不要别的字。只列错的，对的不要列：
{{"wrong":[{{"id":编号,"got":"孩子写的（认不出的字写 ?，没写写空串）"}}]}}"""
EXTRA = {"english": "\n- 英语：单词拼错、漏词、多词算错；大小写和标点不算错，I'm 写成 Im 也算对。",
         "math": "\n- 数学：只看最后写的得数。"}


def recent_photos(thread: str, n: int) -> list[dict]:
    """这条线程最近 n 张别人发的图片（按发的先后）。小新自己发的（卷子、红圈图）不算。"""
    try:
        msgs = store.get(thread)["messages"]
    except KeyError:
        return []
    out = []
    for m in reversed(msgs):
        for f in reversed(m.get("files") or []):
            if f.get("kind") == "image" and f.get("owner") != "xiaoxin":
                out.append(f)
                if len(out) >= n:
                    return list(reversed(out))
    return list(reversed(out))


def model_chain() -> list[str]:
    """设置里选的批改模型打头，后面按 GRADE_MODELS 兜底（一个挂了换下一个）。"""
    from server.main import agent_cfg

    first = agent_cfg().get("grade_model") or GRADE_MODELS[0]
    return [first] + [m for m in GRADE_MODELS if m != first][:2]


# ---------------------------------------------------------------- 1. 读码拉正

def _sheet(short: str, kid: str) -> dict | None:
    """二维码里的卷子 id 前 8 位 → 那份卷子（这个孩子的优先）。"""
    from server import worksheet

    d = worksheet._dir()
    best = None
    for p in d.glob(f"{short.lower()}*.json") if d.is_dir() else []:
        try:
            ws = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if best is None or (ws.get("for") == kid and best.get("for") != kid):
            best = ws
    return best


def locate(fid: str, kid: str) -> dict:
    """一张照片 → {ws, page, img(拉正的 A4，BGR)}；不合格抛 paper.PaperError（话是给孩子的）。"""
    from server import paper

    src = store.variant_path(fid, "original") or store.best_image(fid)
    if src is None:
        raise paper.PaperError("这张照片打不开，请重新拍一张。")
    img = paper.read(src)
    loc = paper.locate(img)
    ws = _sheet(loc["short"], kid)
    if ws is None:
        raise paper.PaperError("这张卷子小新找不到记录了，是不是很早以前出的？")
    return {"ws": ws, "page": loc["page"] - 1, "img": paper.deskew(img, loc["M"]), "corners": loc["corners"]}


# ---------------------------------------------------------------- 2. 编号框 + 模型只回错的

def _font(size: int, bold: bool = True):
    from server import worksheet

    return worksheet._font("bold" if bold else "sans", size)


def _numbered(img, cells: list[dict]):
    """拉正的卷子上画蓝色编号框（给模型看），返回 PIL 图。编号从 1 起，和 cells 的顺序一致。"""
    from PIL import Image, ImageDraw

    from server import worksheet

    im = Image.fromarray(img[:, :, ::-1])
    d = ImageDraw.Draw(im)
    f = _font(26)
    for n, c in enumerate(cells, 1):
        x1, y1, x2, y2 = c["box"]
        d.rectangle([x1 - 3, y1 - 3, x2 + 3, y2 + 3], outline=worksheet.BLUE, width=2)
        tag = str(n)
        tw = d.textlength(tag, font=f)
        d.rectangle([x1 - 3, y1 - 31, x1 + tw + 7, y1 - 3], fill=worksheet.BLUE)
        d.text((x1 + 2, y1 - 30), tag, font=f, fill="white")
    return im


def _data_url(im) -> str:
    im = im.copy()
    im.thumbnail((SIDE, SIDE))
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def ask(im, cells: list[dict], subject: str, models: list[str]) -> tuple[dict[int, str], str, float]:
    """问批改模型：返回 ({编号: 孩子写的}, 实际用的模型, 秒)。"""
    from src.llm import client

    key = "\n".join(f"{n}. {c['want']}" for n, c in enumerate(cells, 1))
    prompt = PROMPT.format(key=key, extra=EXTRA.get(subject, ""))
    bx = [c["box"] for c in cells]   # 只给答题区（标题、姓名、空白不给）：图小、快
    area = im.crop((max(0, min(b[0] for b in bx) - 24), max(0, min(b[1] for b in bx) - 44),
                    min(im.width, max(b[2] for b in bx) + 24), min(im.height, max(b[3] for b in bx) + 24)))
    t0 = time.time()
    rep = client.chat_full(models, [{"role": "user", "content": [
        {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": _data_url(area)}}]}],
        max_tokens=1200, timeout=90)
    m = re.search(r"\{.*\}", rep.text or "", re.S)
    if not m:
        raise ValueError(f"批改结果看不懂：{(rep.text or '')[:80]}")
    out = {}
    for w in json.loads(m.group(0)).get("wrong") or []:
        try:
            n = int(w.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(cells):
            out[n] = str(w.get("got") or "").strip()
    return out, rep.model, round(time.time() - t0, 1)


RECHECK = """下面每张小图是孩子写的一个答案，图上标了编号。每个编号的标准答案：
{key}
一个一个仔细看：和标准答案一个字一个字比，写错字、少写、多写、没写、认不出都算错；潦草但能认出是对的字算对。{extra}
只输出 JSON：{{"wrong":[{{"id":编号,"got":"孩子写的（认不出写 ?，没写写空串）"}}]}}（对的不列）"""


def _strip(im, cells: list[dict], ids: list[int]):
    """把几个格子裁下来竖着拼成一张小图（每个左上角标编号），复核用。"""
    from PIL import Image, ImageDraw

    parts = []
    for n in ids:
        x1, y1, x2, y2 = cells[n - 1]["box"]
        parts.append((n, im.crop((max(0, x1 - 16), max(0, y1 - 40), min(im.width, x2 + 16), min(im.height, y2 + 16)))))
    w = max(p.width for _, p in parts)
    out = Image.new("RGB", (w, sum(p.height + 10 for _, p in parts)), "white")
    y = 0
    for n, p in parts:
        out.paste(p, (0, y))
        y += p.height + 10
    return out


def judge(im, cells: list[dict], subject: str, models: list[str]) -> tuple[dict[int, str], str, float]:
    """一次问整页答题区（ask）。2026-11-06 比过几种（同一页 4 次，真错 13 个，docs/check-work.md）：
      单次整页 平均 20 秒、命中 12–13、偶尔误判 1–2 个（肚皮 → 胜皮）；
      两票 + 分歧复核 31 秒、命中 13，误判没少（复核小图上它还是认成胜皮）；
      切 2–3 块并行 22–28 秒、反而漏得多（块里编号断了它会乱）。
    所以就单次：快，误判也只是多圈一个、孩子看一眼就知道。分歧复核的代码留着（judge2），以后换模型再评。"""
    t0 = time.time()
    wrong, model, _ = ask(im, cells, subject, models)
    return wrong, model, round(time.time() - t0, 1)


def judge2(im, cells: list[dict], subject: str, models: list[str]) -> tuple[dict[int, str], str, float]:
    """两次一起问（并行），两次都说错的算错；只有一次说错的，把那几个格子裁出来再问一次定。现在不用（见 judge）。"""
    from src.llm import client

    t0 = time.time()
    im.load()   # 两个线程一起 crop：先读进内存（PIL 懒加载，并发读文件会读坏）
    with ThreadPoolExecutor(2) as ex:
        runs = list(ex.map(lambda _: _try(ask, im, cells, subject, models), range(2)))
    ok = [r for r in runs if r]
    if not ok:
        raise ValueError("批改模型两次都没回")
    if len(ok) == 1:
        return ok[0][0], ok[0][1], round(time.time() - t0, 1)
    (a, model, _), (b, _, _) = ok
    both = {n: a[n] or b[n] for n in a.keys() & b.keys()}
    split = sorted(a.keys() ^ b.keys())
    if split:
        key = "\n".join(f"{n}. {cells[n - 1]['want']}" for n in split)
        rep = client.chat_full(models, [{"role": "user", "content": [
            {"type": "text", "text": RECHECK.format(key=key, extra=EXTRA.get(subject, ""))},
            {"type": "image_url", "image_url": {"url": _data_url(_strip(im, cells, split))}}]}], max_tokens=600, timeout=60)
        m = re.search(r"\{.*\}", rep.text or "", re.S)
        for w in (json.loads(m.group(0)).get("wrong") or []) if m else []:
            try:
                n = int(w.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if n in split:
                both[n] = str(w.get("got") or "").strip() or a.get(n) or b.get(n, "")
        if not m:   # 复核没回好：宁可多圈（孩子再看一眼），不漏
            both.update({n: a.get(n) or b.get(n, "") for n in split})
    return both, model, round(time.time() - t0, 1)


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        log.warning("grade call failed: %s", e)
        return None


def cells_of(ws: dict, page: int) -> list[dict]:
    """这一页要批的格子：[{k, box, want, no}]，按版面从上到下（boxes 本来就是排版顺序）。"""
    from server import practice, worksheet

    key = practice.answer_key(ws)
    lay = worksheet.boxes(ws)
    out = []
    for b in lay[page] if page < len(lay) else []:
        if b["k"] < len(key):
            out.append({**b, "want": key[b["k"]]["want"], "no": key[b["k"]]["no"]})
    return out


# ---------------------------------------------------------------- 3. 红圈

def circle(img, cells: list[dict], wrong: dict[int, str]) -> bytes:
    """拉正的卷子上把错的格子圈红，旁边写正确答案。返回 JPEG。"""
    from PIL import Image, ImageDraw

    im = Image.fromarray(img[:, :, ::-1])
    d = ImageDraw.Draw(im)
    f = _font(34)
    for n in sorted(wrong):
        x1, y1, x2, y2 = cells[n - 1]["box"]
        pad = 12
        d.ellipse([x1 - pad, y1 - pad, x2 + pad, y2 + pad], outline=RED, width=5)
        want = cells[n - 1]["want"]
        tx, ty = x1, y2 + pad - 2
        if ty + 40 > im.height:
            ty = y1 - pad - 42
        d.text((tx, ty), want, font=f, fill=RED, stroke_width=3, stroke_fill="white")
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


def _save_jpeg(data: bytes, name: str) -> dict:
    from PIL import Image

    fid, path = store.allocate()
    path.write_bytes(data)
    store.commit(fid, name, "image/jpeg", len(data), owner="xiaoxin")
    store.variant_path(fid, "thumb")
    store.variant_path(fid, "screen")
    with Image.open(io.BytesIO(data)) as im:
        return store.update_file(fid, width=im.width, height=im.height)


# ---------------------------------------------------------------- 4. 话

def _got(g: str) -> str:
    if not g:
        return "没写"
    if g.strip("?？ ") == "":
        return "写得认不出"
    return f"写成「{g}」"


def _alike(a: str, b: str) -> bool:
    """孩子写的 a 像不像答案 b：一半以上的字对得上（按位置）。"""
    if not a or not b:
        return False
    return sum(x == y for x, y in zip(a, b)) * 2 >= max(len(a), len(b))


def _shifted(wrong: list[dict]) -> str:
    """听写漏了一个、后面整排往前挪了一格（写的是下一个词）：说一句，不然一串错看着吓人、其实是一处问题。
    11/5 那张：39 写了「咬字」（40 的答案）、40 写了申请（41 的）……一直到 44。"""
    nums = {int(w["no"]): w for w in wrong if str(w["no"]).isdigit()}
    want = {n: w["want"] for n, w in nums.items()}
    run = [n for n in sorted(nums) if n + 1 in want and _alike(nums[n]["got"], want[n + 1])]
    if len(run) < 2:
        return ""
    lo, hi = run[0], run[-1]
    return (f"第 {lo}–{hi} 个往前错了一格（第 {lo} 个写的是第 {lo + 1} 个的「{want[lo + 1]}」），"
            f"多半是漏听了第 {lo} 个「{want[lo]}」，后面就跟着挪了。")


def summary(title: str, total: int, wrong: list[dict], kid: str = "") -> tuple[str, str]:
    """(群里那条, pad 念的那句)。只说问题，不提对的；看不清的算错，不说「看不清」。"""
    call = f"{kid}，" if kid else ""
    right = total - len(wrong)
    if not wrong:
        return f"「{title}」批好了：全对，{right}/{total} 🎉", f"{call}{title}批好了，全对，真棒！"
    lines = [f"「{title}」批好了：对了 {right}/{total}，错 {len(wrong)} 个（卷子上红圈）："]
    for w in wrong:
        no = f"第 {w['no']} 个" if str(w["no"]).isdigit() else w["no"]
        lines.append(f"- {no}：{_got(w['got'])}，应为「{w['want']}」")
    shift = _shifted(wrong)
    if shift:
        lines.append(shift)
    zi = "、".join(w["want"] for w in wrong[:4]) + ("等" if len(wrong) > 4 else "")
    say = f"{call}{title}批好了，对了{right}个，错了{len(wrong)}个：{zi}。"
    if shift:
        say += "有一串是漏了一个词、后面跟着挪了一格。"
    say += "错的我在卷子上圈出来了，看看再改一改。"
    return "\n".join(lines), say


# ---------------------------------------------------------------- 整套

def grade(photos: list[dict], kid: str = "", models: list[str] | None = None) -> dict:
    """几张照片 → 结果。照片不合格的那张记 retake（话给孩子），其余照常批。几页并行。"""
    from server import paper

    t0 = time.time()
    models = models or model_chain()
    pages, retake, seen = [], [], set()

    def one(f):
        try:
            loc = locate(f["id"], kid)
        except paper.PaperError as e:
            return {"fid": f["id"], "retake": str(e)}
        ws, page = loc["ws"], loc["page"]
        cells = cells_of(ws, page)
        t1 = time.time()
        wrong, model, secs = judge(_numbered(loc["img"], cells), cells, ws.get("subject", ""), models) if cells else ({}, "", 0)
        return {"fid": f["id"], "ws": ws, "page": page, "cells": cells, "wrong": wrong, "model": model,
                "model_s": secs, "locate_s": round(t1 - t0, 2), "img": loc["img"]}

    with ThreadPoolExecutor(max(1, len(photos))) as ex:
        res = list(ex.map(one, photos))
    for r in res:
        if "retake" in r:
            retake.append(r["retake"])
        elif (r["ws"]["id"], r["page"]) in seen:
            continue   # 同一页拍了两张：批一次
        else:
            seen.add((r["ws"]["id"], r["page"]))
            pages.append(r)
    return {"pages": pages, "retake": retake, "seconds": round(time.time() - t0, 1)}


def report(g: dict, kid_name: str = "") -> dict:
    """grade 的结果 → 要发的消息：{text, say, files}。几页是同一份卷子的合在一起说；不同的卷子各说各的。"""
    files, texts, says = [], [], []
    by_ws: dict[str, list[dict]] = {}
    for p in sorted(g["pages"], key=lambda p: p["page"]):
        by_ws.setdefault(p["ws"]["id"], []).append(p)
    for pages in by_ws.values():
        ws = pages[0]["ws"]
        total = sum(len(p["cells"]) for p in pages)
        wrong = [{**p["cells"][n - 1], "got": got} for p in pages for n, got in sorted(p["wrong"].items())]
        for p in pages:
            data = circle(p["img"], p["cells"], p["wrong"])
            files.append(_save_jpeg(data, f"批改 {ws['title']} 第{p['page'] + 1}页.jpg"))
        from server import worksheet

        n_pages = len(worksheet.pages(ws))
        missing = n_pages - len(pages)
        t, s = summary(ws["title"], total, wrong, kid_name if not texts else "")
        if missing > 0:
            got = "、".join(f"第 {p['page'] + 1} 页" for p in pages)
            t += f"\n（这份一共 {n_pages} 页，这次只拍了{got}。）"
        texts.append(t)
        says.append(s)
    if g["retake"]:
        t = g["retake"][0] if not g["pages"] else f"还有 {len(g['retake'])} 张没认出来：{g['retake'][0]}"
        texts.append(t)
        says.append(t if not g["pages"] else "")
    return {"text": "\n\n".join(texts), "say": "".join(says), "files": files}


def _work(thread: str, kid: str, kid_name: str, photos: list[dict]) -> None:
    try:
        try:
            g = grade(photos, kid)
            out = report(g, kid_name)
        except Exception as e:  # noqa: BLE001 模型挂了、照片坏了：也要说一声
            log.warning("check_homework failed: %s", e)
            out = {"text": "批改没成功，等一下再说「小新批一下」试试。", "say": "批改没成功，等一下再试试。", "files": [],
                   "error": str(e)[:300]}
            g = {"pages": [], "seconds": 0}
        meta = {"pages": [{"ws": p["ws"]["id"], "page": p["page"] + 1, "total": len(p["cells"]),
                           "wrong": [{"no": p["cells"][n - 1]["no"], "want": p["cells"][n - 1]["want"], "got": got}
                                     for n, got in sorted(p["wrong"].items())],
                           "model": p["model"], "model_s": p["model_s"]} for p in g["pages"]],
                "seconds": g["seconds"]}
        store.append(thread, {"role": "assistant", "via": "check", "model": (g["pages"] or [{}])[0].get("model", ""),
                              "text": out["text"], "say": out["say"], "files": out["files"], "check": meta,
                              **({"error": out["error"]} if out.get("error") else {})})
        log.info("check_homework %d photos %.1fs pages=%s", len(photos), g["seconds"],
                 [(p["ws"]["id"], p["page"] + 1, len(p["wrong"]), p["model"], p["model_s"]) for p in g["pages"]])
    finally:
        _busy.release()


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    c = printer_tools.ctx.get()
    thread, kid = c.get("thread", ""), c.get("kid", "")
    if not thread:
        return "失败：这里批不了（不在聊天里）"
    n = max(1, min(int(args.get("photos") or 1), MAX_PHOTOS))
    photos = recent_photos(thread, n)
    if not photos:
        return "失败：聊天里还没有卷子照片，先拍一张发过来。播报：先把卷子拍下来发给我吧。"
    if not _busy.acquire(blocking=False):
        return "失败：上一份还在批，等它批完。播报：上一份还在批，稍等一下。"
    try:
        threading.Thread(target=_work, args=(thread, kid, c.get("kid_name", ""), photos), daemon=True,
                         name="check-work").start()
    except Exception:
        _busy.release()
        raise
    return f"已经开始批了（{len(photos)} 张照片），十几秒后结果会自动发到聊天里，不用你转述。播报：收到，我批一下。"
