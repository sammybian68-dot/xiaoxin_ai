"""多页 PWG Raster（server/pwg.py）和打印任务带整份文档：解回来和原图一模一样；页头字段对；几页 / 几份才排。
2026-11-06 另外用 macOS 自带 libcups 的 cupsRasterReadHeader2 / cupsRasterReadPixels 读过生成的文件：页头、像素都对。"""

from __future__ import annotations

import io
import struct

import numpy as np
from PIL import Image, ImageDraw

from server import printing, pwg, store


def _page(seed, color=False):
    im = Image.new("RGB", pwg.A4_PX, "white")
    d = ImageDraw.Draw(im)
    rng = np.random.default_rng(seed)
    for _ in range(200):
        x, y = rng.integers(0, 2400), rng.integers(0, 3400)
        d.rectangle([x, y, x + rng.integers(5, 80), y + rng.integers(5, 40)], fill=(200, 30, 30) if color else (20, 20, 20))
    return im


def test_roundtrip_and_header():
    pages = [_page(1), _page(2, True), Image.new("RGB", (1654, 2339), "white")]   # 第三页要放大到 300 dpi
    for color in (False, True):
        doc = pwg.document(pages, color)
        assert doc[:4] == b"RaS2"
        got = pwg.decode(doc)
        assert len(got) == 3
        for k, p in enumerate(pages):
            want = np.asarray(p.convert("RGB" if color else "L").resize(pwg.A4_PX, Image.LANCZOS))
            assert np.array_equal(got[k].reshape(want.shape), want)
        h = doc[4:4 + 1796]
        assert h[:9] == b"PwgRaster" and struct.unpack_from(">II", h, 276) == (300, 300)
        assert struct.unpack_from(">I", h, 400)[0] == (19 if color else 18) and struct.unpack_from(">I", h, 452)[0] == 3
        assert h[1732:1748] == b"iso_a4_210x297mm"
    assert len(pwg.document([Image.new("RGB", (100, 100), "white")], False)) < 30_000   # 白纸压得很小


def test_job_gets_pwg_for_multi_page(client, monkeypatch):
    """一页一份：不排 PWG（照旧发 JPEG）；两份 / 多页：排一个 PWG，份数直接排进去。"""
    b = io.BytesIO()
    _page(3).save(b, "JPEG")
    fid, path = store.allocate()
    path.write_bytes(b.getvalue())
    store.commit(fid, "p.jpg", "image/jpeg", len(b.getvalue()), owner="kid")
    tid = store.thread_id()
    one = printing.create_job(tid, fid, color=False)
    assert not one["pwg"] and printing.doc_path(one["id"]) is None
    two = printing.create_job(tid, fid, color=False, copies=2)
    assert two["pwg"] and len(pwg.decode(printing.doc_path(two["id"]).read_bytes())) == 2
    monkeypatch.setattr(printing, "PWG_MAX", 10)   # 太大：不给，pad 一页一页发
    assert not printing.create_job(tid, fid, color=True, copies=3)["pwg"]


def test_stuck_printing_job_marked_error(monkeypatch):
    b = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(b, "JPEG")
    fid, path = store.allocate()
    path.write_bytes(b.getvalue())
    store.commit(fid, "p.jpg", "image/jpeg", len(b.getvalue()))
    tid = store.thread_id()
    j = printing.create_job(tid, fid, color=False)
    printing.update_job(j["id"], state="printing", printing_at=1.0)
    got = [x for x in printing.jobs(tid) if x["id"] == j["id"]][0]
    assert got["state"] == "error" and "没收到打完" in got["error"]
