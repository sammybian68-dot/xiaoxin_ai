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
    ws = worksheet.create("arithmetic", count=50, ops="+-", max=100, seed=2, for_kid="k")
    im = Image.open(io.BytesIO(worksheet.render(ws)))
    assert im.size == (worksheet.W, worksheet.H)
    py = {"森林": "sēn lín", "太阳": "tài yáng", "朋友": "péng you"}  # 测试里没有字库，拼音给全
    for kind, kw in (("pinyin_words", {"words": list(py), "pinyin": py}), ("dictation", {"words": "春天 花朵 小草"}),
                     ("essay", {"topic": "我的妈妈"})):
        w = worksheet.create(kind, for_kid="k", **kw)
        assert Image.open(io.BytesIO(worksheet.render(w))).size == (worksheet.W, worksheet.H)
        assert Image.open(io.BytesIO(worksheet.render(w, answers=True))).size == (worksheet.W, worksheet.H)


def test_worksheet_errors():
    with pytest.raises(worksheet.WorksheetError):
        worksheet.create("pinyin_words", words=["abc"])
    with pytest.raises(worksheet.WorksheetError):
        worksheet.create("essay")
    with pytest.raises(worksheet.WorksheetError):
        worksheet.create("drawing")
    w = worksheet.create("pinyin_words", words=["银行"], pinyin={"银行": "yín háng"})
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
    fakes.llm_script = [("tool", "make_exercise", {"subject": "math", "topic": "arithmetic", "count": 30, "max": 20, "carry": False}),
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


# ---------------------------------------------------------------- 打印机状态（pad 上报，小新回答「还有墨吗」）

def test_printer_status_reported_and_described(family, fakes):
    assert "还不知道" in family["女儿"].get("/api/printer/status").json()["text"]
    st = {"state": 3, "reasons": "none", "inks": [["tri-color ink", 12], ["black ink", 100]], "media": "iso_a4_210x297mm"}
    assert family["女儿"].put("/api/printer/status", json=st).status_code == 200
    assert family["爸爸"].put("/api/printer/status", json=st).status_code == 403  # 只有 pad 报
    text = family["爸爸"].get("/api/printer/status").json()["text"]
    assert "空闲" in text and "彩色墨盒 12%（快没了）" in text and "黑色墨盒 100%" in text and "A4" in text
    family["女儿"].put("/api/printer/status", json={**st, "reasons": "media-empty-error", "media": ""})
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "printer_status", {}), ("text", "没纸了")]
    family["女儿"].post(f"/api/conversations/{dau_t}/messages", json={"text": "打印机还有多少墨水"})
    res = store.get(dau_t)["messages"][-1]["actions"][0]["result"]
    assert "没纸了" in res and "彩色墨盒 12%" in res


# ---------------------------------------------------------------- 异步：任务排队、补拉、结果回到对话

def test_result_goes_back_to_conversation(family, fakes):  # 单任务也是一批
    dau = family["女儿"]
    tid = dau.get("/api/thread").json()["id"]
    fid = _upload(dau, _textpage(), "a.jpg", "image/jpeg")
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "这个", "file_ids": [fid]})
    job = printing.create_job(tid, fid)
    dau.post(f"/api/print/{job['id']}/result", json={"state": "error", "error": "打印机没纸了，请放几张 A4 纸"})
    msg = store.get(tid)["messages"][-1]
    assert msg["role"] == "assistant" and msg["text"] == "打印没成功：打印机没纸了，请放几张 A4 纸。"
    # 下一轮小新看得到（按原位置回放给模型），界面不推
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "怎么没出来"})
    assistant = [m["content"] for m in fakes.llm_requests[-1]["messages"] if m["role"] == "assistant" and m.get("content")]
    assert any("没成功" in c and "没纸" in c for c in assistant)  # 是小新自己说过的话，下一轮记得


def test_pending_catch_up_in_order_and_stale_dropped(family, monkeypatch):
    dau = family["女儿"]
    tid = dau.get("/api/thread").json()["id"]
    fid = _upload(dau, _textpage(), "a.jpg", "image/jpeg")
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "这个", "file_ids": [fid]})
    old = printing.create_job(tid, fid)
    printing.update_job(old["id"], created=old["created"] - printing.STALE_S - 1)  # pad 关了一晚上
    a = printing.create_job(tid, fid)
    b = printing.create_job(tid, fid)
    done = printing.create_job(tid, fid)
    printing.update_job(done["id"], state="done")
    got = dau.get("/api/print/jobs?pending=1").json()["jobs"]
    assert [j["id"] for j in got] == [a["id"], b["id"]]  # 按提交顺序，打完的不给
    assert printing.get_job(old["id"])["state"] == "error" and "作废" in printing.get_job(old["id"])["error"]
    assert family["儿子"].get("/api/print/jobs?pending=1").json()["jobs"] == []  # 别人的不给


def test_batch_summary_once_when_all_finished(family, fakes):
    """一轮里交了两个任务（练习纸 + 答案页）：第一个打完不说，两个都结束才一条总结，按张数算成功失败。"""
    dau = family["女儿"]
    tid = dau.get("/api/thread").json()["id"]
    fid = _upload(dau, _pdf(3), "卷.pdf", "application/pdf")
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "这个", "file_ids": [fid]})
    a = printing.create_job(tid, fid, batch="b1")             # 3 页
    b = printing.create_job(tid, fid, pages="1-2", batch="b1")  # 2 页
    n = len(store.get(tid)["messages"])
    with dau.websocket_connect("/api/hub") as hub:
        json.loads(hub.receive_text())
        dau.post(f"/api/print/{a['id']}/result", json={"state": "done", "done_pages": 3})
        assert len(store.get(tid)["messages"]) == n  # 还有一个没打完：不说
        dau.post(f"/api/print/{b['id']}/result", json={"state": "error", "error": "打印机没纸了", "done_pages": 1})
        while True:
            ev = json.loads(hub.receive_text())
            if ev["type"] == "print.summary":
                break
    assert ev["text"] == "打印完成，4 张成功，1 张失败：打印机没纸了。" and ev["ok"] is False
    msg = store.get(tid)["messages"][-1]
    assert msg["role"] == "assistant" and msg["text"] == ev["text"] and msg["id"] == ev["id"]


def test_batch_all_ok_summary():
    tid = store.thread_id()
    from tests.test_print import _textpage  # noqa
    fid_meta = store.allocate()
    fid_meta[1].write_bytes(_textpage())
    store.commit(fid_meta[0], "通知.jpg", "image/jpeg", 1)
    store.append(tid, {"role": "user", "text": "x", "files": [store.file_info(fid_meta[0])[1]]})
    j = printing.create_job(tid, fid_meta[0], copies=2, batch="b2")
    printing.update_job(j["id"], state="done")
    assert printing.batch_summary(tid, "b2") == "打印完成，2 张都出来了。"


def test_same_worksheet_twice_in_one_turn_prints_once(family, fakes):
    dau = family["女儿"]
    tid = dau.get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "make_exercise", {"subject": "math", "topic": "arithmetic", "count": 20, "max": 10}), ("text", "好")]
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "出题"})
    wid = "w_" + __import__("re").search(r"w_([0-9a-f]{16})", store.get(tid)["messages"][-1]["actions"][0]["result"]).group(1)
    n = len(printing.jobs(tid, 50))
    fakes.llm_script = [("tool", "print_file", {"file": wid}), ("tool", "print_file", {"file": wid, "answers": True}),
                        ("tool", "print_file", {"file": wid}), ("text", "好")]
    dau.post(f"/api/conversations/{tid}/messages", json={"text": "打印，连答案页"})
    js = printing.jobs(tid, 50)[: len(printing.jobs(tid, 50)) - n]
    names = sorted(j["name"] for j in js)
    assert len(names) == 2 and names[1] == names[0].replace(".jpg", "（答案）.jpg")  # 练习纸 1 张 + 答案页 1 张
    res = [a["result"] for a in store.get(tid)["messages"][-1]["actions"]]
    assert "补上答案页" in res[1] and "不重复打" in res[2]
