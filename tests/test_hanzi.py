"""笔顺：查字、工具结果、接口。用一份小的假数据，不依赖 gpu2 上 31 MB 的真数据。"""

from __future__ import annotations

import json

import pytest

from server import hanzi, store
from server.tools import registry

XIN = {"s": ["M 1 1", "M 2 2", "M 3 3", "M 4 4"], "m": [[[0, 0], [1, 1]]] * 4, "n": ["点", "卧钩", "点", "点"],
       "p": ["xīn"], "b": "心", "t": "独体"}


@pytest.fixture
def data(fakes, monkeypatch):
    d = store.DATA / "hanzi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hanzi.json").write_text(json.dumps({"心": XIN, "林": {**XIN, "n": [], "p": ["lín"], "b": "木", "t": "左右"}},
                                             ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(hanzi, "_data", None)


def test_summary(data):
    assert hanzi.summary("心") == "「心」xīn，部首心，独体结构，共4画：1点、2卧钩、3点、4点"
    assert "（笔画名缺）" in hanzi.summary("林")
    assert hanzi.summary("鑫") == "「鑫」没有笔顺数据"


def test_tool(data):
    out = registry.run_local("show_stroke_order", json.dumps({"chars": "心心林abc"}))
    assert out.startswith("已在屏幕上演示「心林」的笔顺。") and "共4画" in out
    assert registry.run_local("show_stroke_order", '{"chars": "xin"}').startswith("失败：没有汉字")
    assert registry.run_local("show_stroke_order", '{"chars": "鑫"}').startswith("失败：这些字都没有")


def test_api(client, data):
    r = client.get("/api/hanzi/心")
    assert r.status_code == 200 and r.json()["names"][1] == "卧钩" and r.json()["count"] == 4
    assert client.get("/api/hanzi/鑫").status_code == 404


def test_model_shows_stroke_order(client, fakes, data):
    fakes.llm_script = [("tool", "show_stroke_order", {"chars": "心"}), ("text", "心一共四画")]
    cid = client.post("/api/conversations").json()["id"]
    body = client.post(f"/api/conversations/{cid}/messages", json={"text": "心字怎么写"}).text
    assert "event: tool" in body
    rec = client.get(f"/api/conversations/{cid}").json()["messages"][-1]
    assert rec["actions"][0]["name"] == "show_stroke_order"
    assert json.loads(rec["actions"][0]["arguments"])["chars"] == "心"
