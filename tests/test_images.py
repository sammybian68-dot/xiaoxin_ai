"""图片分档（docs/architecture.md 6.5）：先登记 + thumb/screen，原图勾了才分片续传；老式整张上传服务端补档；
模型看 screen；下载按档；没原图时给 screen；只有上传者能传；原图齐了推 hub。"""

from __future__ import annotations

import io
import json

from PIL import Image

from server import store
from tests.test_members import family  # noqa: F401  fixture


def jpeg(w: int, h: int, color=(200, 80, 40), q=90) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=q)
    return buf.getvalue()


def png_rgba(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (0, 0, 255, 128)).save(buf, "PNG")
    return buf.getvalue()


def test_legacy_whole_upload_gets_variants(client):
    meta = client.post("/api/files", content=jpeg(4000, 3000),
                       headers={"x-filename": "big.jpg", "content-type": "image/jpeg"}).json()
    thumb = Image.open(io.BytesIO(client.get(f"/api/files/{meta['id']}?v=thumb").content))
    screen = Image.open(io.BytesIO(client.get(f"/api/files/{meta['id']}?v=screen").content))
    assert max(thumb.size) == 480 and max(screen.size) == 2048
    orig = client.get(f"/api/files/{meta['id']}")
    assert Image.open(io.BytesIO(orig.content)).size == (4000, 3000)  # 不带 v 是原图


def test_png_with_alpha_becomes_jpeg(client):
    meta = client.post("/api/files", content=png_rgba(600, 400),
                       headers={"x-filename": "a.png", "content-type": "image/png"}).json()
    r = client.get(f"/api/files/{meta['id']}?v=thumb")
    assert r.headers["content-type"] == "image/jpeg" and Image.open(io.BytesIO(r.content)).mode == "RGB"


def test_tiered_upload_without_original(client):
    m = client.post("/api/files/image", json={"name": "IMG_1.jpg", "size": 4_000_000, "w": 4000, "h": 3000}).json()
    fid = m["id"]
    assert client.get(f"/api/files/{fid}?v=thumb").status_code == 404  # 还没传
    assert client.put(f"/api/files/{fid}/thumb", content=jpeg(480, 360)).status_code == 200
    assert client.put(f"/api/files/{fid}/screen", content=jpeg(2048, 1536)).status_code == 200
    assert client.put(f"/api/files/{fid}/thumb", content=b"not jpeg").status_code == 400
    # 没勾原图：不带 v 拿到的是 screen（不报错），原图状态是「没有」
    r = client.get(f"/api/files/{fid}")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (2048, 1536)
    assert "original" not in store.file_info(fid)[1]


def test_original_resumable_upload(client):
    data = jpeg(3000, 2000, q=95)
    m = client.post("/api/files/image", json={"name": "o.jpg", "size": len(data), "w": 3000, "h": 2000}).json()
    fid = m["id"]
    client.put(f"/api/files/{fid}/screen", content=jpeg(2048, 1365))
    assert client.post(f"/api/files/{fid}/original").json() == {"size": len(data), "received": 0}
    half = len(data) // 2
    assert client.put(f"/api/files/{fid}/original?offset=0", content=data[:half]).json()["received"] == half
    # 断了：续传前先问进度；重发同一片（offset 不对）不收
    assert client.get(f"/api/files/{fid}/original/status").json()["received"] == half
    assert client.put(f"/api/files/{fid}/original?offset=0", content=data[:half]).json()["received"] == half
    assert client.post(f"/api/files/{fid}/original").json()["received"] == half  # 再登记一次 = 续传，不清零
    # 没传完时下载原图给的是 screen
    assert Image.open(io.BytesIO(client.get(f"/api/files/{fid}").content)).size == (2048, 1365)
    assert client.put(f"/api/files/{fid}/original?offset={half}", content=data[half:]).json()["received"] == len(data)
    assert client.get(f"/api/files/{fid}").content == data
    assert client.put(f"/api/files/{fid}/original?offset={len(data)}", content=b"x").status_code == 400  # 超了


def test_model_sees_screen_not_original(client, fakes):
    import base64

    meta = client.post("/api/files", content=jpeg(4000, 3000),
                       headers={"x-filename": "hw.jpg", "content-type": "image/jpeg"}).json()
    tid = client.get("/api/thread").json()["id"]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "看看", "file_ids": [meta["id"]],
                                                            "model": "claude-sonnet-5-5"})
    url = next(p for m in fakes.llm_requests[-1]["messages"] if isinstance(m["content"], list)
               for p in m["content"] if p["type"] == "image_url")["image_url"]["url"]
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(img.size) == 2048


def test_only_uploader_can_add_tiers(family):
    m = family["爸爸"].post("/api/files/image", json={"name": "x.jpg", "size": 100}).json()
    assert family["儿子"].put(f"/api/files/{m['id']}/thumb", content=jpeg(10, 10)).status_code == 404
    assert family["儿子"].post(f"/api/files/{m['id']}/original").status_code == 404


def test_original_complete_is_pushed(family):
    data = jpeg(800, 600)
    m = family["爸爸"].post("/api/files/image", json={"name": "p.jpg", "size": len(data)}).json()
    family["爸爸"].put(f"/api/files/{m['id']}/thumb", content=jpeg(480, 360))
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    family["爸爸"].post(f"/api/conversations/{dau_t}/messages", json={"text": "照片", "file_ids": [m["id"]]})
    family["爸爸"].post(f"/api/files/{m['id']}/original")
    with family["女儿"].websocket_connect("/api/hub") as ws:
        json.loads(ws.receive_text())  # hello
        family["爸爸"].put(f"/api/files/{m['id']}/original?offset=0", content=data)
        while True:
            ev = json.loads(ws.receive_text())
            if ev["type"] == "file":
                break
        assert ev["file"]["id"] == m["id"] and ev["file"]["original"]["received"] == len(data)
