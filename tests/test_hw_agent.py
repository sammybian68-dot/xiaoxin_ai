"""抓作业 agent 里不碰 pad 的那几块：结果校验（宁缺勿错）、坐标换算（屏幕会转）、全屏预览裁边。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from tools import hw_agent as ag


def _ok():
    return {"date": "2026-11-02", "items": [
        {"subject": "chinese", "submit": "无需提交", "text": "1.习作四草稿", "notes": ["学号"]},
        {"subject": "english", "submit": "去完成", "text": "1.完成《同步》U5"}]}


def test_check_accepts_good():
    day, items, bad = ag.check(_ok(), "2026-10-01", "2026-11-02")
    assert not bad and day == "2026-11-02" and [i["subject"] for i in items] == ["chinese", "english"]
    assert items[0]["images"] == []


@pytest.mark.parametrize("mutate,why", [
    (lambda r: r.update(date="11月2日"), "日期格式"),
    (lambda r: r.update(date="2099-01-01"), "还没到"),
    (lambda r: r["items"][0].update(subject="语文"), "学科"),
    (lambda r: r["items"][0].update(text=""), "没读到正文"),
    (lambda r: r["items"][0].update(text="（看不清）"), "没读到正文"),
    (lambda r: r["items"][1].update(subject="chinese"), "两条"),
    (lambda r: r.update(items=[]), "一条作业都没读到"),
])
def test_check_rejects_bad(mutate, why):
    """不合格一律拒收（让模型重读，或这次算失败等下次重试），不写错数据进库。"""
    r = _ok()
    mutate(r)
    day, items, bad = ag.check(r, "2026-10-01", "2026-11-02")
    assert items == [] and why in bad


def test_day_range():
    assert ag.day_range("2026-10-30", "2026-11-02") == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
    assert ag.day_range("2026-11-02", "2026-11-02") == ["2026-11-02"]


def _png(w, h, color=(255, 255, 255)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_tap_scales_by_current_shot(monkeypatch):
    """坐标按「模型看的那一张」换算：横屏 2560 宽 ×2，竖屏 1600 宽 ×1.25（实测中途会转屏）。"""
    calls = []
    pad = ag.Pad("x")
    monkeypatch.setattr(pad, "sh", lambda *a: calls.append(a) or "")
    monkeypatch.setattr(pad, "awake", lambda: None)
    monkeypatch.setattr(ag.time, "sleep", lambda s: None)
    pad.last_w = 2560
    pad.act("tap", {"x": 640, "y": 400, "why": ""})
    pad.last_w = 1600
    pad.act("tap", {"x": 640, "y": 400, "why": ""})
    taps = [c for c in calls if c[:2] == ("input", "tap")]
    assert taps == [("input", "tap", "1280", "800"), ("input", "tap", "800", "500")]


def test_crop_preview_removes_black_frame():
    """全屏预览上下是黑边：裁掉黑边，只留图片本身。"""
    im = Image.new("RGB", (1600, 2560), (0, 0, 0))
    im.paste(Image.new("RGB", (1500, 900), (230, 220, 200)), (50, 800))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    out = Image.open(io.BytesIO(ag.crop_preview(buf.getvalue())))
    assert abs(out.width - 1500) < 20 and abs(out.height - 900) < 20


def test_shot_part_fixed_width():
    part = ag.shot_part(_png(2560, 1600))
    import base64

    raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
    assert Image.open(io.BytesIO(raw)).size == (ag.VIEW_W, 800)


def _photo(color):
    """一张「全屏预览」：上下黑边，中间是图。"""
    im = Image.new("RGB", (1600, 2560), (0, 0, 0))
    im.paste(Image.new("RGB", (1500, 1000), color), (50, 700))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def fake_saver(monkeypatch):
    calls = []

    def fake_api(method, path, body=None, **kw):
        calls.append((method, path))
        if method == "GET":
            return {"days": [{"day": "2026-10-30"}]}
        if path == "api/files":
            return {"id": f"f{len([c for c in calls if c[1] == 'api/files'])}"}
        return {}

    monkeypatch.setattr(ag, "api", fake_api)
    monkeypatch.setattr(ag, "models", lambda: ["m"])
    monkeypatch.setattr(ag, "verify_image", lambda raw, chain: (True, ""))
    return calls


def test_saver_saves_day_with_its_images(fake_saver, tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "SHOT", tmp_path)
    pad = ag.Pad("x")
    pad.images = [{"date": "2026-11-02", "subject": "chinese", "what": "a", "raw": _photo((200, 180, 160))},
                  {"date": "2026-11-02", "subject": "chinese", "what": "a 又存了一次", "raw": _photo((200, 180, 160))},
                  {"date": "2026-11-02", "subject": "math", "what": "这天没数学", "raw": _photo((90, 120, 90))},
                  {"date": "2026-11-01", "subject": "chinese", "what": "别的日子的", "raw": _photo((10, 200, 10))}]
    saver = ag.Saver("2026-10-25", "2026-11-02", dry_run=False, only_missing=False)
    out = saver(_ok(), pad)
    assert out.startswith("✓ 2026-11-02") and "附图 1 张" in out and "没有这科" in out
    assert saver.done == {"2026-11-02": 1}
    assert [x["date"] for x in pad.images] == ["2026-11-01"]  # 别的日子的图留着，等交那天时用
    puts = [c for c in fake_saver if c[0] == "PUT"]
    assert puts == [("PUT", "api/homework/2026-11-02")]


def test_saver_rejects_and_skips(fake_saver):
    pad = ag.Pad("x")
    saver = ag.Saver("2026-10-25", "2026-11-02", dry_run=False, only_missing=True)
    bad = _ok()
    bad["items"][0]["text"] = ""
    assert saver(bad, pad).startswith("×") and "2026-11-02" in saver.rejected
    assert saver(_ok(), pad).startswith("✓")       # 重读对了再交：收下，拒收记录清掉
    assert "2026-11-02" not in saver.rejected
    old = _ok()
    old["date"] = "2026-10-30"
    assert "已经有记录" in saver(old, pad)          # --only-missing
    far = _ok()
    far["date"] = "2026-09-01"
    assert "不在要抓的范围" in saver(far, pad)
    assert not [c for c in fake_saver if c[0] == "PUT" and "10-30" in c[1]]


def test_image_only_subject(fake_saver, tmp_path, monkeypatch):
    """数学老师常发黑板照、卡片里没文字：text 写「见附图：…」可以交，但必须真有图，不然拒收让它补抓。"""
    monkeypatch.setattr(ag, "SHOT", tmp_path)
    day = {"date": "2026-10-29", "items": [{"subject": "chinese", "text": "1.默第13课词语"},
                                           {"subject": "math", "text": "见附图：10/29 数回 大册P43"}]}
    pad = ag.Pad("x")
    saver = ag.Saver("2026-10-25", "2026-11-02", dry_run=False, only_missing=False)
    out = saver(day, pad)
    assert out.startswith("×") and "见附图" in out and not [c for c in fake_saver if c[0] == "PUT"]
    pad.images = [{"date": "2026-10-29", "subject": "math", "what": "黑板照", "raw": _photo((60, 120, 80))}]
    out = saver(day, pad)
    assert out.startswith("✓") and "附图 1 张" in out and saver.done == {"2026-10-29": 1}


def test_trim_keeps_tool_pairs():
    """裁历史：留开头目标 + 最近 KEEP 条；切点落在普通 user 消息上，tool_calls 和 tool 结果不拆开。"""
    msgs = [{"role": "user", "content": "目标"}]
    for i in range(60):
        msgs += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "function": {"name": "swipe"}}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": "滑了"},
                 {"role": "user", "content": f"屏幕 {i}"}]
    out = ag.trim(msgs, ["2026-11-02", "2026-10-30"])
    assert len(out) <= ag.KEEP + 3 and out[0]["content"] == "目标"
    assert "已经存好的日子：2026-11-02、2026-10-30" in out[2]["content"]
    for i, m in enumerate(out):  # 每个 tool 前面紧跟着带同一个 id 的 assistant
        if m["role"] == "tool":
            assert out[i - 1]["tool_calls"][0]["id"] == m["tool_call_id"]
