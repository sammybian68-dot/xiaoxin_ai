"""打印：把聊天里的一份东西（图片、PDF、练习纸…）排成打印机吃的页，交给孩子的 pad 发到家里打印机。

为什么在 pad 上打：gpu2 在机房，摸不到家里的局域网；pad 和打印机在同一个 Wi‑Fi。中枢只把排好的页（每页一张
A4 JPEG）和参数推给 pad（hub 的 print 事件），pad 用 IPP 发给 HP DeskJet 4800，回报结果（POST /api/print/{job}/result）。

打印机能力（2026-10-31 mDNS 查到）：吃 image/jpeg、pwg-raster、urf、PCLm；不吃 PDF；彩色；不支持双面；最大 A4。
所以这里把 PDF 渲成每页一张 JPEG（pypdfium2，200 dpi），黑白打印时也在这里转成灰度（打印机按 JPEG 是彩色的会用彩墨）。
Word / Excel / PPT 要 LibreOffice 先转 PDF，gpu2 上还没装，先让家长转成 PDF 再发。

自动选黑白 / 彩色（server/prompts/print.md 教小新怎么选，这里给它看每份东西「彩色多少」做参考）：
color_ratio = 有明显颜色的像素占比（抽样）。照片、画、地图、彩色绘本高；作业纸、试卷、文字低。
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path

from server import store

DPI = 200
A4 = (1654, 2339)               # 200 dpi
MAX_PAGES = 30                  # 一次最多打这么多页（防误操作：小新说「打印」把 80 页的书全打了）
KID_CONFIRM_PAGES = 5           # 孩子要打超过这么多页，小新先问一句（prompt 里说）
COPIES_MAX = 5
OFFICE = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf", ".wps"}
COLOR_HINT = 0.06               # 彩色像素超过 6%：多半是彩色材料（照片、画、彩色课件）

_lock = threading.Lock()


class PrintError(ValueError):
    pass


def _jobs_dir() -> Path:
    return store.DATA / "print"


# ---------------------------------------------------------------- 看一份东西：类型、页数、彩色多少

def _color_ratio(im) -> float:
    """抽样看有多少像素是「有颜色的」（饱和度高、又不是很白很黑）。缩到 200 宽再算，快。"""
    small = im.convert("RGB")
    small.thumbnail((200, 200))
    hsv = small.convert("HSV")
    n = colored = 0
    for h, s, v in hsv.get_flattened_data():
        n += 1
        if s > 60 and 40 < v < 250:
            colored += 1
    return round(colored / max(1, n), 3)


def _pdf(path: Path):
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(str(path))
    except Exception as e:
        raise PrintError(f"PDF 打不开：{e}") from e


def inspect(fid: str) -> dict:
    """这份东西：名字、类型（image / pdf / office / other）、页数、彩色占比（前 3 页的最大值）。给小新决定参数用。"""
    info = store.file_info(fid)
    if info is None:
        raise KeyError(fid)
    blob, meta = info
    ext = Path(meta["name"]).suffix.lower()
    out = {"id": fid, "name": meta["name"], "kind": "other", "pages": 0, "color": 0.0}
    if meta["kind"] == "image":
        from PIL import Image

        src = store.variant_path(fid, "original") or store.variant_path(fid, "screen")
        if src is None:
            raise PrintError("这张图还没传完")
        with Image.open(src) as im:
            out.update(kind="image", pages=1, color=_color_ratio(im), size=list(im.size))
    elif meta["mime"] == "application/pdf" or ext == ".pdf":
        if store.variant_path(fid, "original") is None:
            raise PrintError("这个 PDF 还没传完")
        doc = _pdf(blob)
        out.update(kind="pdf", pages=len(doc))
        out["color"] = max((_color_ratio(doc[i].render(scale=0.5).to_pil()) for i in range(min(3, len(doc)))), default=0.0)
        doc.close()
    elif ext in OFFICE:
        out.update(kind="office")
    return out


# ---------------------------------------------------------------- 排页

def parse_pages(spec, total: int) -> list[int]:
    """「1-3,5」「全部」「最后一页」「前两页」→ 0 起的页号。空 = 全部。越界的去掉。"""
    s = str(spec or "").strip().replace("，", ",").replace("、", ",").replace("～", "-").replace("~", "-").replace("到", "-")
    if not s or s in ("全部", "所有", "all"):
        return list(range(total))
    if s in ("最后一页", "最后1页", "last"):
        return [total - 1] if total else []
    m = re.fullmatch(r"前\s*(\d+|[一二两三四五六七八九十]+)\s*页", s)
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if m:
        k = int(m.group(1)) if m.group(1).isdigit() else cn.get(m.group(1), 0)
        return list(range(min(k, total)))
    out = []
    for part in s.split(","):
        part = re.sub(r"[第页\s]", "", part)
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise PrintError(f"页码看不懂：{part}")
            out += range(int(a) - 1, int(b))
        elif part.isdigit():
            out.append(int(part) - 1)
        elif part in cn:
            out.append(cn[part] - 1)
        else:
            raise PrintError(f"页码看不懂：{part}")
    return [p for p in dict.fromkeys(out) if 0 <= p < total]


def _fit_a4(im, fit: str = "fit"):
    """放进 A4：横图自动转 90°；fit=fit 等比缩放留白边（默认），fill 铺满裁边。返回白底 RGB。"""
    from PIL import Image, ImageOps

    im = ImageOps.exif_transpose(im).convert("RGB")
    if im.width > im.height:
        im = im.rotate(90, expand=True)
    box = (A4[0] - 2 * 60, A4[1] - 2 * 60)  # 打印机边上印不到，留 60 px（≈7.6 mm）
    im = ImageOps.fit(im, box) if fit == "fill" else ImageOps.contain(im, box)
    page = Image.new("RGB", A4, "white")
    page.paste(im, ((A4[0] - im.width) // 2, (A4[1] - im.height) // 2))
    return page


def render_pages(fid: str, pages: list[int] | None, color: bool, fit: str = "fit") -> list[bytes]:
    """排好要打的每一页，返回 JPEG 列表。黑白就在这里转灰度（打印机拿到彩色 JPEG 会用彩墨）。"""
    from PIL import Image

    info = inspect(fid)
    if info["kind"] == "office":
        raise PrintError("Word / Excel / PPT 现在还打不了，请爸爸妈妈转成 PDF 再发一次")
    if info["kind"] == "other":
        raise PrintError("这个文件不是图片也不是 PDF，打不了")
    out = []
    if info["kind"] == "image":
        src = store.variant_path(fid, "original") or store.variant_path(fid, "screen")
        with Image.open(src) as im:
            pages_im = [_fit_a4(im, fit)]
    else:
        doc = _pdf(store.file_info(fid)[0])
        idx = pages if pages is not None else list(range(len(doc)))
        pages_im = [_fit_a4(doc[i].render(scale=DPI / 72).to_pil(), fit) for i in idx]
        doc.close()
    for page in pages_im:
        if not color:
            page = page.convert("L").convert("RGB")  # 灰度，但保持 RGB（有的打印机不吃单通道 JPEG）
        buf = io.BytesIO()
        page.save(buf, "JPEG", quality=88, dpi=(DPI, DPI))
        out.append(buf.getvalue())
    return out


# ---------------------------------------------------------------- 打印任务

def create_job(thread: str, fid: str, *, pages=None, color=None, copies: int = 1, fit: str = "fit",
               by: str = "") -> dict:
    """排好页、存成一个任务，返回任务（不含页面数据）。color=None 时按彩色占比自动选。"""
    info = inspect(fid)
    total = info["pages"] or 1
    idx = parse_pages(pages, total) if info["kind"] == "pdf" else None
    n = len(idx) if idx is not None else 1
    if n == 0:
        raise PrintError(f"没有这几页（一共 {total} 页）")
    if n > MAX_PAGES:
        raise PrintError(f"一次最多打 {MAX_PAGES} 页，这次选了 {n} 页，挑一部分")
    copies = max(1, min(int(copies or 1), COPIES_MAX))
    auto = color is None
    if auto:
        color = info["color"] >= COLOR_HINT
    datas = render_pages(fid, idx, bool(color), fit)
    jid = store._new_id()
    d = _jobs_dir() / jid
    d.mkdir(parents=True, exist_ok=True)
    for i, data in enumerate(datas):
        (d / f"p{i}.jpg").write_bytes(data)
    job = {"id": jid, "thread": thread, "file": fid, "name": info["name"], "pages": [p + 1 for p in idx] if idx else [1],
           "total": total, "sheets": len(datas) * copies, "copies": copies, "color": bool(color), "auto_color": auto,
           "fit": fit, "by": by, "state": "queued", "created": time.time(), "error": ""}
    store._write_json(d / "job.json", job)
    return job


def get_job(jid: str) -> dict:
    p = _jobs_dir() / store._check(jid) / "job.json"
    if not p.is_file():
        raise KeyError(jid)
    return json.loads(p.read_text(encoding="utf-8"))


def page_path(jid: str, i: int) -> Path | None:
    p = _jobs_dir() / store._check(jid) / f"p{int(i)}.jpg"
    return p if p.is_file() else None


def update_job(jid: str, **fields) -> dict:
    with _lock:
        job = get_job(jid)
        job.update(fields)
        store._write_json(_jobs_dir() / jid / "job.json", job)
    return job


def jobs(thread: str, limit: int = 10) -> list[dict]:
    d = _jobs_dir()
    out = []
    for p in d.glob("*/job.json") if d.is_dir() else []:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if j["thread"] == thread:
            out.append(j)
    return sorted(out, key=lambda j: j["created"], reverse=True)[:limit]


def describe(job: dict) -> str:
    pages = f"第 {job['pages'][0]} 页" if len(job["pages"]) == 1 and job["total"] > 1 else (
        f"{len(job['pages'])} 页" if job["total"] > 1 else "1 页")
    return (f"「{job['name']}」{pages}，{'彩色' if job['color'] else '黑白'}"
            + (f"，{job['copies']} 份" if job["copies"] > 1 else "") + f"，共 {job['sheets']} 张纸")


# ---------------------------------------------------------------- 打印机状态（pad 查到后上报；小新、家长都能问）

def _status_path() -> Path:
    return store.DATA / "printer_status.json"


def save_status(st: dict) -> dict:
    st = {**{k: st.get(k) for k in ("state", "reasons", "inks", "media", "name")}, "at": time.time()}
    store._write_json(_status_path(), st)
    return st


def status() -> dict | None:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def describe_status(st: dict | None) -> str:
    """给小新的一段：开没开、有没有纸、墨还剩多少、多久前查的。"""
    if not st:
        return "还不知道打印机的状态（孩子的 pad 还没连上过打印机）"
    age = time.time() - st.get("at", 0)
    when = "刚刚" if age < 120 else f"{int(age // 60)} 分钟前" if age < 3600 else f"{int(age // 3600)} 小时前"
    state = {3: "空闲，能打", 4: "正在打印", 5: "停住了"}.get(st.get("state"), "连不上（可能没开机）")
    inks = []
    for name, lvl in st.get("inks") or []:
        label = {"black ink": "黑色墨盒", "tri-color ink": "彩色墨盒"}.get(name, name)
        inks.append(f"{label} {lvl}%" + ("（快没了）" if isinstance(lvl, int) and 0 <= lvl <= 15 else ""))
    reasons = [r for r in (st.get("reasons") or "").split(",") if r and r != "none"]
    why = {"media-empty": "没纸了", "media-needed": "没纸了", "media-jam": "卡纸了", "door-open": "盖子开着",
           "cover-open": "盖子开着", "marker-supply-low": "墨快没了", "marker-supply-empty": "没墨了", "offline": "离线"}
    probs = sorted({why.get(r.rsplit("-", 1)[0] if r.endswith(("-warning", "-error", "-report")) else r, r) for r in reasons})
    media = "放的是 A4 纸" if "a4" in str(st.get("media") or "").lower() else ("纸盒里没纸" if st.get("media") == "" else "")
    return (f"打印机（{st.get('name') or 'HP DeskJet 4800'}）{state}"
            + (f"；问题：{'、'.join(probs)}" if probs else "") + (f"；{media}" if media else "")
            + (f"；{'，'.join(inks)}" if inks else "") + f"（{when}查的）")
