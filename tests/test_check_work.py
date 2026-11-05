"""批改作业（server/paper.py 读码拉正 + server/tools/check_work.py）：不联网，模型换成假的。

照片用「把小新排好的卷子透视变换一下」模拟手机拍：转一点、斜一点、倒拍、糊一点、只拍一半。
"""

from __future__ import annotations

import io
import json
import time

import numpy as np
import pytest

from server import paper, store, worksheet
from tests.test_members import family  # noqa: F401  fixture


def _sheet(units=(4,), **kw):
    return worksheet.create("word_dictation", subject="chinese", units=list(units), for_kid="kid1", **kw)


def _photo(page_bgr, tilt=0.1, frac=0.92, rot=0.0, blur=0.6, out=(2048, 1536), cut=None, seed=0):
    """A4 页 → 桌上的一张手机照（BGR）。cut=(x0,y0,x1,y1) 相对比例：只留照片的这一块（拍歪了、没拍全）。"""
    import cv2

    rng = np.random.default_rng(seed)
    H, W = page_bgr.shape[:2]
    ow, oh = out
    up = 2
    bw, bh = ow * up, oh * up
    ph = bh * frac
    pw = ph * W / H
    cx, cy = bw / 2, bh / 2
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    dst = np.float32([[cx - pw / 2 * (1 - tilt), cy - ph / 2], [cx + pw / 2 * (1 - tilt), cy - ph / 2],
                      [cx + pw / 2, cy + ph / 2], [cx - pw / 2, cy + ph / 2]])
    a = np.deg2rad(rot)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    dst = ((dst - [cx, cy]) @ R.T + [cx, cy]).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    bg = np.full((bh, bw, 3), (150, 170, 190), np.uint8)
    img = cv2.warpPerspective(page_bgr, M, (bw, bh), dst=bg, flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_TRANSPARENT)
    img = (img * np.linspace(0.8, 1.0, bw)[None, :, None] * 0.92 + 12).clip(0, 255).astype(np.uint8)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur * up)
    img = cv2.resize(img, out, interpolation=cv2.INTER_AREA)
    img = (img.astype(np.int16) + rng.normal(0, 4, img.shape)).clip(0, 255).astype(np.uint8)
    if cut:
        x0, y0, x1, y1 = cut
        img = img[int(y0 * oh):int(y1 * oh), int(x0 * ow):int(x1 * ow)]
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return cv2.imdecode(enc, 1)


def _bgr(pil):
    return np.asarray(pil.convert("RGB"))[:, :, ::-1].copy()


# ---------------------------------------------------------------- 排版：一页、二维码、卷号

def test_new_sheet_fits_one_page_with_codes():
    """第四单元 47 个词 + 积累（老版排两页）：新版缩一点排进一页；四角二维码读得出，内容是这份卷子第 1 页。"""
    import cv2

    ws = _sheet()
    assert ws["v"] == 2
    g = worksheet.fit(ws)
    assert g["n"] == 1 and 0.72 <= g["s"] < 1
    im, boxes = worksheet.render_page(ws, False, "卞依一", 0)
    assert im.size == (worksheet.W, worksheet.H)
    ok, txt, _, _ = cv2.QRCodeDetectorAruco().detectAndDecodeMulti(_bgr(im))
    assert ok and sorted(txt) == [worksheet.qr_text(ws, 0, k) for k in (1, 2, 3, 4)]
    # 每个答案都有格子，格子都在正文框里、不压二维码
    from server import practice

    assert sorted(b["k"] for b in boxes) == list(range(len(practice.answer_key(ws))))
    for b in boxes:
        x1, y1, x2, y2 = b["box"]
        assert worksheet.X0 - 5 <= x1 < x2 <= worksheet.W - worksheet.X0 + 5
        assert worksheet.SAFE - 5 <= y1 < y2 <= worksheet.H - worksheet.SAFE + 5


def test_multi_page_codes_and_labels():
    """真放不下（1–8 单元 60 个词 + 所有积累）才分页：每页码里是同一份卷子、各自页号；卷号一样。"""
    import cv2

    ws = _sheet(units=range(1, 9), count=60)
    n = worksheet.fit(ws)["n"]
    assert n >= 2
    seen = set()
    for p in range(n):
        im, boxes = worksheet.render_page(ws, False, "", p)
        ok, txt, _, _ = cv2.QRCodeDetectorAruco().detectAndDecodeMulti(_bgr(im))
        assert ok and {t.split(".")[2] for t in txt} == {str(p + 1)}
        assert {t.split(".")[1] for t in txt} == {ws["id"][:8].upper()}
        seen |= {b["k"] for b in boxes}
    from server import practice

    assert seen == set(range(len(practice.answer_key(ws))))   # 每个答案在某一页上，不丢
    assert len(worksheet.code_of(ws)) == 4


def test_old_sheets_render_like_before():
    """11/6 以前出的卷子（没有 v）照老样子排：已经打出来的纸，答案页、重打都要对得上。"""
    ws = _sheet()
    ws.pop("v")
    assert worksheet.fit(ws)["n"] == 2 and len(worksheet.pages(ws)) == 2
    im, boxes = worksheet.render_page(ws, False, "", 1)
    assert boxes and im.size == (worksheet.W, worksheet.H)


def test_math_sheet_has_codes_and_boxes():
    ws = worksheet.create("zero_end_mul_div", subject="math", count=150, for_kid="kid1")
    im, boxes = worksheet.render_page(ws, False, "", 0)
    assert len(boxes) == 150
    assert paper.locate(_bgr(im))["short"] == ws["id"][:8].upper()


# ---------------------------------------------------------------- 读码拉正 / 合格检查

@pytest.mark.parametrize("kw", [
    dict(),
    dict(tilt=0.25, rot=-8, blur=1.0),
    dict(rot=180, tilt=0.15),                         # 倒着拍
    dict(rot=92, out=(2048, 1536), frac=0.72),        # 横着拍
    dict(out=(1536, 2048), frac=0.95, rot=4),         # 竖拍
])
def test_locate_and_deskew(kw):
    """各种拍法都认得出、拉正后格子对得上原卷（拉正图和原卷在格子位置上的差别很小）。"""
    import cv2

    ws = _sheet()
    page, boxes = worksheet.render_page(ws, False, "", 0)
    ref = _bgr(page)
    loc = paper.locate(_photo(ref, **kw))
    assert loc["short"] == ws["id"][:8].upper() and loc["page"] == 1
    flat = paper.deskew(_photo(ref, **kw), loc["M"])
    # 用一个田字格的红框比：拉正图里这块和原卷的相关系数要高
    x1, y1, x2, y2 = boxes[0]["box"]
    a = cv2.cvtColor(ref[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(flat[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY).astype(np.float32)
    assert np.corrcoef(a.ravel(), b.ravel())[0, 1] > 0.6


def test_one_diagonal_is_enough():
    """只有左上和右下两个码（另两个被手挡了）：照样合格。"""
    ws = _sheet()
    page = _bgr(worksheet.render_page(ws, False, "", 0)[0])
    q = worksheet.QR
    for k in (1, 3):   # 右上、左下涂掉
        x, y = worksheet.CORNERS[k]
        page[y:y + q, x:x + q] = 255
    loc = paper.locate(_photo(page))
    assert loc["corners"] == [1, 3]


def test_retake_when_codes_missing_or_cut():
    """读不到码、只有一条边上的两个码、拍得只剩半张：都让重拍，话是固定的。"""
    ws = _sheet()
    page = _bgr(worksheet.render_page(ws, False, "", 0)[0])
    blank = np.full_like(page, 255)
    with pytest.raises(paper.PaperError) as e:
        paper.locate(_photo(blank))
    assert str(e.value) == paper.RETAKE
    top = page.copy()
    q = worksheet.QR
    for k in (2, 3):   # 只留上面两个码
        x, y = worksheet.CORNERS[k]
        top[y:y + q, x:x + q] = 255
    with pytest.raises(paper.PaperError):
        paper.locate(_photo(top))
    with pytest.raises(paper.PaperError):   # 下半截没拍进来（下面两个码也就没了）
        paper.locate(_photo(page, cut=(0, 0, 1, 0.55)))


def test_retake_when_page_partly_outside():
    """四个码都在，但纸有一条边在画面外（码离纸边还有一点）：A4 角映射到照片外面 → 重拍。"""
    ws = _sheet()
    page = _bgr(worksheet.render_page(ws, False, "", 0)[0])
    with pytest.raises(paper.PaperError):
        paper.locate(_photo(page, frac=1.06, tilt=0))


# ---------------------------------------------------------------- 话

def test_summary_template():
    from server.tools import check_work as C

    t, s = C.summary("第四单元生词默写", 10, [], "卞依一")
    assert "全对" in t and s.startswith("卞依一，") and "全对" in s
    wrong = [{"no": "6", "want": "指甲", "got": "指中"}, {"no": "25", "want": "救命", "got": ""},
             {"no": "百依百（__）", "want": "顺", "got": "?"}]
    t, s = C.summary("第四单元生词默写", 62, wrong, "卞依一")
    assert "对了 59/62，错 3 个" in t
    assert "第 6 个：写成「指中」，应为「指甲」" in t and "第 25 个：没写" in t and "百依百（__）：写得认不出" in t
    assert "看不清" not in t + s and "对了59个，错了3个" in s


def test_summary_notices_shift():
    """漏听一个、后面整排挪一格（11/5 那张 39–44）：说出来是一处问题。"""
    from server.tools import check_work as C

    key = {39: "尽管", 40: "咬字", 41: "申请", 42: "介绍", 43: "主旨", 44: "占领", 45: "乏力"}
    got = {39: "咬字", 40: "生清", 41: "介绍", 42: "主旨", 43: "战令", 44: "严力", 45: ""}
    wrong = [{"no": str(n), "want": key[n], "got": got[n]} for n in key]
    t, s = C.summary("x", 47, wrong)
    assert "第 39–44 个往前错了一格" in t and "漏听了第 39 个「尽管」" in t and "挪了一格" in s
    assert "往前错了一格" not in C.summary("x", 47, wrong[:1])[0]


def test_blank_labels_name_the_right_blank():
    from server import practice

    ws = _sheet()
    key = practice.answer_key(ws)
    labels = [k["no"] for k in key if not k["no"].isdigit()]
    assert "人心__，□。" in labels and "人心□，__。" in labels
    assert "四（__）八稳" in labels and "百依百（__）" in labels


# ---------------------------------------------------------------- 整套（假模型）

def test_tool_end_to_end(family, monkeypatch):
    """孩子发照片 →「小新批一下」→ 工具马上回「在批」→ 后台批完发一条带红圈卷子的消息（text + say + 图）。"""
    from server.tools import check_work as C, printer as printer_tools
    from src.llm import client

    kid = family["ids"]["女儿"]
    tid = family["admin"].get(f"/api/thread?kid={kid}").json()["id"]
    ws = worksheet.create("word_dictation", subject="chinese", units=[4], for_kid=kid)
    page = worksheet.render_page(ws, True, "", 0)[0]   # 答案页当孩子写好的卷子（字都写在格子里）
    import cv2

    ok, enc = cv2.imencode(".jpg", _photo(_bgr(page), tilt=0.15, rot=5))
    fid, path = store.allocate()
    path.write_bytes(enc.tobytes())
    meta = store.commit(fid, "p.jpg", "image/jpeg", len(enc), owner="kid")
    store.append(tid, {"role": "user", "text": "", "files": [meta]})

    calls = []

    def fake(models, messages, **kw):
        txt = messages[0]["content"][0]["text"]
        calls.append(models[0])
        n = len([ln for ln in txt.splitlines() if ln[:1].isdigit()])
        assert n == len(C.cells_of(ws, 0))
        return client.Reply(text=json.dumps({"wrong": [{"id": 6, "got": "指中"}, {"id": 25, "got": ""}]}), model=models[0])

    monkeypatch.setattr(client, "chat_full", fake)
    tok = printer_tools.ctx.set({"thread": tid, "kid": kid, "kid_name": "卞依一"})
    try:
        out = C.run("check_homework", "{}")
        assert "开始批" in out and "播报：收到" in out
        for _ in range(100):
            msgs = [m for m in store.get(tid)["messages"] if m.get("via") == "check"]
            if msgs:
                break
            time.sleep(0.1)
    finally:
        printer_tools.ctx.reset(tok)
    m = msgs[0]
    assert calls == ["claude-sonnet-5-5"]
    assert "错 2 个" in m["text"] and "应为「指甲」" in m["text"] and m["say"].startswith("卞依一，")
    assert len(m["files"]) == 1 and m["files"][0]["owner"] == "xiaoxin"
    assert m["check"]["pages"][0]["page"] == 1 and len(m["check"]["pages"][0]["wrong"]) == 2


def test_tool_retake_message(family, monkeypatch):
    """照片里没有码：不调模型，直接发重拍的话。"""
    from server.tools import check_work as C, printer as printer_tools
    from src.llm import client
    from PIL import Image

    monkeypatch.setattr(client, "chat_full", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调模型")))
    kid = family["ids"]["女儿"]
    tid = family["admin"].get(f"/api/thread?kid={kid}").json()["id"]
    b = io.BytesIO()
    Image.new("RGB", (1200, 900), "white").save(b, "JPEG")
    fid, path = store.allocate()
    path.write_bytes(b.getvalue())
    store.append(tid, {"role": "user", "text": "", "files": [store.commit(fid, "p.jpg", "image/jpeg", len(b.getvalue()), owner="kid")]})
    tok = printer_tools.ctx.set({"thread": tid, "kid": kid, "kid_name": "卞依一"})
    try:
        C.run("check_homework", "{}")
        for _ in range(100):
            msgs = [m for m in store.get(tid)["messages"] if m.get("via") == "check"]
            if msgs:
                break
            time.sleep(0.1)
    finally:
        printer_tools.ctx.reset(tok)
    assert msgs[0]["text"] == paper.RETAKE and msgs[0]["say"] == paper.RETAKE and not msgs[0]["files"]


def test_grade_model_setting(client):
    from server.tools import check_work as C
    from server import store as st

    st.update_settings(agent={"grade_model": "claude-opus-5-5"})
    assert C.model_chain()[0] == "claude-opus-5-5" and len(C.model_chain()) == 3
