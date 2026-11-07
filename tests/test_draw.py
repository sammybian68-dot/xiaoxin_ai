"""画图工具：默认 grok、失败换 gpt；点名 gpt 先用 gpt；都失败发一条说明；同时最多画两张；
画好的图存成普通图片文件，以小新的身份发进这条线程；照图改画（ref）只认这条线程里的图、只用 gpt。"""

from __future__ import annotations

import io
import json
import threading

import pytest
from PIL import Image

from server import store
from server.tools import draw
from server.tools import printer as printer_tools


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 80, 40)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def thread():
    tid = store.create_thread("kid1")
    tok = printer_tools.ctx.set({"thread": tid, "kid_name": "卞依一"})
    yield tid
    printer_tools.ctx.reset(tok)


@pytest.fixture
def gen(monkeypatch):
    """假的出图：fail 里的模型报错，其他写一张 PNG；记下调了哪些模型。跑完等后台线程结束。"""
    st = {"calls": [], "fail": set(), "gate": None, "refs": []}

    def fake(prompt, model, out, ref=None):
        st["calls"].append(model)
        st["refs"].append(ref)
        if st["gate"]:
            st["gate"].wait(5)
        if model in st["fail"]:
            raise RuntimeError(f"{model} 502")
        out.write_bytes(_png())

    monkeypatch.setattr(draw, "_generate", fake)
    yield st


def _wait():
    for t in [t for t in threading.enumerate() if t.name == "draw"]:
        t.join(5)


def _last(tid):
    return store.get(tid)["messages"][-1]


def test_default_is_grok_and_image_lands_in_thread(thread, gen):
    out = draw.run("draw_image", json.dumps({"prompt": "一只戴帽子的小猫"}))
    assert out.startswith("已开始画（grok-imagine-image-2.0）") and "播报：" in out
    _wait()
    assert gen["calls"] == ["grok-imagine-image-2.0"]
    m = _last(thread)
    assert m["role"] == "assistant" and m["via"] == "draw" and m["model"] == "grok-imagine-image-2.0"
    assert m["text"] == "卞依一，图片画好了，请看。"  # 叫名字，不念一长串画面描述
    f = m["files"][0]
    assert f["kind"] == "image" and f["owner"] == "xiaoxin" and (f["width"], f["height"]) == (64, 48)
    assert store.variant_path(f["id"], "thumb") and store.variant_path(f["id"], "screen")


def test_grok_fails_falls_back_to_gpt(thread, gen):
    gen["fail"] = {"grok-imagine-image-2.0"}
    draw.run("draw_image", json.dumps({"prompt": "雪山"}))
    _wait()
    assert gen["calls"] == ["grok-imagine-image-2.0", "gpt-image-2"]
    m = _last(thread)
    assert m["model"] == "gpt-image-2" and "换 gpt-image-2 画的" in m["text"] and m["files"] and m["prompt"] == "雪山"


def test_named_gpt_goes_first(thread, gen):
    out = draw.run("draw_image", json.dumps({"prompt": "春联", "model": "gpt"}))
    assert "gpt-image-2" in out
    _wait()
    assert gen["calls"] == ["gpt-image-2"]


def test_both_fail_says_so_and_cleans_up(thread, gen):
    gen["fail"] = {"grok-imagine-image-2.0", "gpt-image-2"}
    before = set(p.name for p in store.FILES.glob("*")) if store.FILES.exists() else set()
    draw.run("draw_image", json.dumps({"prompt": "怪兽"}))
    _wait()
    m = _last(thread)
    assert not m.get("files") and "没画出来" in m["text"] and "502" in m["error"]
    assert set(p.name for p in store.FILES.glob("*")) == before  # 失败的空文件删掉了


def test_not_an_image_counts_as_failure(thread, gen, monkeypatch):
    def html(prompt, model, out, ref=None):
        gen["calls"].append(model)
        out.write_bytes(b"<html>quota</html>" if model == "grok-imagine-image-2.0" else _png())
    monkeypatch.setattr(draw, "_generate", html)
    draw.run("draw_image", json.dumps({"prompt": "海报"}))
    _wait()
    assert gen["calls"] == ["grok-imagine-image-2.0", "gpt-image-2"] and _last(thread)["files"]


def test_at_most_two_at_once(thread, gen):
    gen["gate"] = threading.Event()
    assert draw.run("draw_image", json.dumps({"prompt": "一"})).startswith("已开始")
    assert draw.run("draw_image", json.dumps({"prompt": "二"})).startswith("已开始")
    assert draw.run("draw_image", json.dumps({"prompt": "三"})).startswith("失败：手上还有图在画")
    gen["gate"].set()
    _wait()
    assert draw.run("draw_image", json.dumps({"prompt": "四"})).startswith("已开始")  # 画完名额还回来了
    _wait()


def _photo(tid, role="user", name="照片.jpg"):
    fid, p = store.allocate()
    p.write_bytes(_png())
    meta = store.commit(fid, name, "image/png", p.stat().st_size, owner="kid1")
    store.append(tid, {"role": role, "text": "看", "files": [meta]})
    return fid


def test_ref_latest_uses_newest_image_and_gpt_only(thread, gen):
    _photo(thread, name="旧.jpg")
    new = _photo(thread, name="新.jpg")
    store.append(thread, {"role": "user", "text": "小新，把这张图里的人变成飞起来的样子"})
    out = draw.run("draw_image", json.dumps({"prompt": "图里的人飞在天上", "ref": "latest"}))
    assert out.startswith("已开始画（gpt-image-2）")
    _wait()
    assert gen["calls"] == ["gpt-image-2"]  # grok 改图不通，不换
    assert gen["refs"][0] == store.best_image(new)
    m = _last(thread)
    assert m["ref"] == new and m["files"]


def test_ref_can_be_a_drawn_picture(thread, gen):
    draw.run("draw_image", json.dumps({"prompt": "小猫"}))
    _wait()
    drawn = _last(thread)["files"][0]["id"]
    draw.run("draw_image", json.dumps({"prompt": "背景改成晚上", "ref": "latest"}))
    _wait()
    assert gen["refs"][-1] == store.best_image(drawn) and _last(thread)["ref"] == drawn


def test_ref_by_id_and_not_in_thread(thread, gen):
    fid = _photo(thread)
    _photo(thread)
    draw.run("draw_image", json.dumps({"prompt": "变卡通", "ref": "f_" + fid}))
    _wait()
    assert gen["refs"][-1] == store.best_image(fid)
    other = store.create_thread("kid2")
    stranger = _photo(other)
    assert draw.run("draw_image", json.dumps({"prompt": "x", "ref": "f_" + stranger})).startswith("失败：这段对话里最近没有这张图")


def test_ref_without_any_image(thread, gen):
    out = draw.run("draw_image", json.dumps({"prompt": "飞起来", "ref": "latest"}))
    assert out.startswith("失败：这段对话里还没有图片") and gen["calls"] == []


def test_shrink_ref_with_pil(tmp_path):
    from src.llm import image

    src = tmp_path / "big.jpg"
    Image.new("RGB", (3000, 1500), (10, 200, 30)).save(src, "JPEG")
    with Image.open(io.BytesIO(image._shrink(src))) as im:
        assert im.format == "PNG" and im.size == (1024, 512)


def test_bad_args():
    assert draw.run("draw_image", "{}").startswith("失败：没有画面描述")
    assert draw.run("draw_image", "[1]").startswith("失败：参数")
    assert draw.run("draw_image", json.dumps({"prompt": "猫"})).startswith("失败：这里不能画图")  # 没有线程


def test_registered_and_prompted():
    from server import prompts
    from server.tools import registry

    assert "draw_image" in registry.LOCAL and any(s["function"]["name"] == "draw_image" for s in registry.SPECS)
    assert "draw_image" in prompts.build()
