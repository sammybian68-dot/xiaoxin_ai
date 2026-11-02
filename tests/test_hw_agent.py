"""抓作业 agent 里不碰 pad 的那几块：结果校验（宁缺勿错）、坐标换算（屏幕会转）、全屏预览裁边。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from tools import hw_agent as ag


def _ok():
    return {"found": True, "date": "2026-11-02", "items": [
        {"subject": "chinese", "submit": "无需提交", "text": "1.习作四草稿", "notes": ["学号"]},
        {"subject": "english", "submit": "去完成", "text": "1.完成《同步》U5"}]}


def test_check_accepts_good():
    items, bad = ag.check(_ok(), "2026-11-02")
    assert not bad and [i["subject"] for i in items] == ["chinese", "english"]


@pytest.mark.parametrize("mutate,why", [
    (lambda r: r.update(found=False, why="没找到"), "没找到"),
    (lambda r: r.update(date="2026-11-01"), "日期"),
    (lambda r: r["items"][0].update(subject="语文"), "学科"),
    (lambda r: r["items"][0].update(text=""), "没读到正文"),
    (lambda r: r["items"][0].update(text="（看不清）"), "没读到正文"),
    (lambda r: r["items"][1].update(subject="chinese"), "两条"),
    (lambda r: r.update(items=[]), "一条作业都没读到"),
])
def test_check_rejects_bad(mutate, why):
    """不合格一律当失败（让定时任务重试），不写错数据进库。"""
    r = _ok()
    mutate(r)
    items, bad = ag.check(r, "2026-11-02")
    assert items == [] and why in bad


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
