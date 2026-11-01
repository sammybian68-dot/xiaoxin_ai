"""出题和打印：口算题答案一定对、进位规则、练习纸排版；打印的页码、黑白彩色自动判断、PDF 分页、份数上限；
工具：make_worksheet → print_file(latest) 推 print 事件，pad 取页、回报；兄妹拿不到对方的打印任务。"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw

from server import printing, store, worksheet
from tests.test_members import family  # noqa: F401  fixture


# ---------------------------------------------------------------- 出题

@pytest.mark.parametrize("ops,max_n,carry", [("+-", 20, False), ("+-", 100, True), ("×", 9, True), ("÷", 9, True), ("+-×÷", 100, True)])
def test_math_answers_are_right(ops, max_n, carry):
    items = worksheet.math_items(80, ops, max_n, carry, seed=1)
    assert len(items) >= 40 and len({it["q"] for it in items}) == len(items)  # 不重复
    for it in items:
        a, op, b, _ = it["q"].split()
        a, b = int(a), int(b)
        want = {"+": a + b, "-": a - b, "×": a * b, "÷": a // b}[op]
        assert int(it["a"]) == want and want >= 0
        if op == "÷":
            assert a % b == 0
        if op in "+-" and not carry:
            assert (a % 10 + b % 10 < 10) if op == "+" else (a % 10 >= b % 10)
        if op == "+":
            assert want <= max_n


def test_worksheet_pages_render_a4():
    ws = worksheet.create("math", count=50, ops="+-", max=100, seed=2, for_kid="k")
    im = Image.open(io.BytesIO(worksheet.render(ws)))
    assert im.size == (worksheet.W, worksheet.H)
    py = {"森林": "sēn lín", "太阳": "tài yáng", "朋友": "péng you"}  # 测试里没有字库，拼音给全
    for kind, kw in (("pinyin", {"words": list(py), "pinyin": py}), ("dictation", {"words": "春天 花朵 小草"}),
                     ("essay", {"topic": "我的妈妈"})):
        w = worksheet.create(kind, for_kid="k", **kw)
        assert Image.open(io.BytesIO(worksheet.render(w))).size == (worksheet.W, worksheet.H)
        assert Image.open(io.BytesIO(worksheet.render(w, answers=True))).size == (worksheet.W, worksheet.H)


def test_worksheet_errors():
    with pytest.raises(worksheet.WorksheetError):
        worksheet.create("pinyin", words=["abc"])
    with pytest.raises(worksheet.WorksheetError):
        worksheet.create("essay")
    with pytest.raises(worksheet.WorksheetError):
        worksheet.create("drawing")
    w = worksheet.create("pinyin", words=["银行"], pinyin={"银行": "yín háng"})
    assert w["items"][0]["q"] == "yín háng"


# ---------------------------------------------------------------- 打印参数

@pytest.mark.parametrize("spec,want", [("", [0, 1, 2, 3, 4]), ("全部", [0, 1, 2, 3, 4]), ("1-3", [0, 1, 2]), ("2,4", [1, 3]),
                                       ("第 2 页", [1]), ("前两页", [0, 1]), ("最后一页", [4]), ("1到2，5", [0, 1, 4]), ("3-9", [2, 3, 4])])
def test_parse_pages(spec, want):
    assert printing.parse_pages(spec, 5) == want


def test_parse_pages_rejects_garbage():
    with pytest.raises(printing.PrintError):
        printing.parse_pages("一些", 5)


def _upload(client, data: bytes, name: str, mime: str) -> str:
    from urllib.parse import quote

    return client.post("/api/files", content=data, headers={"x-filename": quote(name), "content-type": mime}).json()["id"]


def _photo() -> bytes:
    im = Image.new("RGB", (1200, 900))
    d = ImageDraw.Draw(im)
    for i in range(0, 1200, 40):
        d.rectangle([i, 0, i + 40, 900], fill=((i * 7) % 255, 120, 255 - (i * 5) % 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def _textpage() -> bytes:
    im = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(im)
    for y in range(80, 1150, 40):
        d.rectangle([80, y, 800, y + 12], fill=(20, 20, 20))
    d.rectangle([80, 30, 300, 60], fill=(220, 30, 30))  # 一点红色标题
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def _pdf(pages: int) -> bytes:
    ims = [Image.open(io.BytesIO(_textpage())) for _ in range(pages)]
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:], resolution=100)
    return buf.getvalue()


def test_auto_color_photo_vs_text(client):
    photo = _upload(client, _photo(), "画.jpg", "image/jpeg")
    text = _upload(client, _textpage(), "通知.jpg", "image/jpeg")
    assert printing.inspect(photo)["color"] > 0.3 and printing.inspect(text)["color"] < 0.05
    tid = client.get("/api/thread").json()["id"]
    assert printing.create_job(tid, photo)["color"] is True
    assert printing.create_job(tid, text)["color"] is False
    assert printing.create_job(tid, text, color=True)["color"] is True  # 说了彩色就彩色


def test_bw_pages_are_gray_and_a4(client):
    fid = _upload(client, _photo(), "画.jpg", "image/jpeg")
    page = printing.render_pages(fid, None, color=False)[0]
    im = Image.open(io.BytesIO(page))
    assert im.size == printing.A4 and im.mode == "RGB"
    assert all(r == g == b for r, g, b in list(im.get_flattened_data())[::5000])  # 灰度
    color = Image.open(io.BytesIO(printing.render_pages(fid, None, color=True)[0]))
    assert any(len({*px}) > 1 for px in list(color.get_flattened_data())[::5000])


def test_pdf_pages_and_limits(client):
    fid = _upload(client, _pdf(6), "试卷.pdf", "application/pdf")
    info = printing.inspect(fid)
    assert info["kind"] == "pdf" and info["pages"] == 6 and info["color"] < 0.05
    tid = client.get("/api/thread").json()["id"]
    job = printing.create_job(tid, fid, pages="2-3", copies=2)
    assert job["pages"] == [2, 3] and job["sheets"] == 4 and not job["color"]
    assert printing.page_path(job["id"], 1) and not printing.page_path(job["id"], 2)
    with pytest.raises(printing.PrintError):
        printing.create_job(tid, fid, pages="9")
    big = _upload(client, _pdf(31), "书.pdf", "application/pdf")
    with pytest.raises(printing.PrintError, match="最多"):
        printing.create_job(tid, big)


def test_office_files_explain(client):
    fid = _upload(client, b"PK\x03\x04fake", "作业.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    tid = client.get("/api/thread").json()["id"]
    with pytest.raises(printing.PrintError, match="PDF"):
        printing.create_job(tid, fid)


# ---------------------------------------------------------------- 工具 → pad

def test_worksheet_then_print_latest(family, fakes):
    dau = family["女儿"]
    tid = dau.get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "make_worksheet", {"kind": "math", "count": 30, "max": 20, "carry": False}),
                        ("text", "出好了")]
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "出 30 道 20 以内的口算"})
    res = store.get(tid)["messages"][-1]["actions"][0]["result"]
    assert "w_" in res and "20 以内加减法口算" in res and "播报：" in res
    with dau.websocket_connect("/api/hub") as hub:
        json.loads(hub.receive_text())
        fakes.llm_script = [("tool", "print_file", {"file": "latest"}), ("text", "好的")]
        dau.post(f"/api/conversations/{tid}/messages", json={"text": "帮我打印出来"})
        while True:
            ev = json.loads(hub.receive_text())
            if ev["type"] == "print":
                break
    job = ev["job"]
    assert job["color"] is False and job["sheets"] == 1 and job["state"] == "queued"
    page = dau.get(f"/api/print/{job['id']}/page/0")
    assert page.status_code == 200 and Image.open(io.BytesIO(page.content)).size == printing.A4
    r = dau.post(f"/api/print/{job['id']}/result", json={"state": "done", "printer": "HP DeskJet 4800"})
    assert r.json()["state"] == "done"
    # 儿子拿不到女儿的打印任务
    assert family["儿子"].get(f"/api/print/{job['id']}/page/0").status_code == 404
    assert family["儿子"].post(f"/api/print/{job['id']}/result", json={"state": "done"}).status_code == 404


def test_print_file_must_be_in_thread(family, fakes):
    son_t = family["儿子"].get("/api/thread").json()["id"]
    fid = _upload(family["爸爸"], _photo(), "p.jpg", "image/jpeg")
    family["爸爸"].post(f"/api/conversations/{son_t}/messages", json={"text": "给儿子的", "file_ids": [fid]})
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "print_file", {"file": "f_" + fid}), ("text", "好")]
    family["女儿"].post(f"/api/conversations/{dau_t}/messages", json={"text": "打印"})
    assert "不在这段对话里" in store.get(dau_t)["messages"][-1]["actions"][0]["result"]


def test_list_printables_shows_color_and_sender(family, fakes):
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    fid = _upload(family["爸爸"], _pdf(3), "期中试卷.pdf", "application/pdf")
    family["爸爸"].post(f"/api/conversations/{dau_t}/messages", json={"text": "试卷", "file_ids": [fid]})
    fakes.llm_script = [("tool", "list_printables", {}), ("text", "好")]
    family["女儿"].post(f"/api/conversations/{dau_t}/messages", json={"text": "爸爸发的打印一下"})
    res = store.get(dau_t)["messages"][-1]["actions"][0]["result"]
    assert "期中试卷.pdf" in res and "3 页" in res and "爸爸发的" in res and "彩色占比" in res
