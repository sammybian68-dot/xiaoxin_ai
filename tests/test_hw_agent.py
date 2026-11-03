"""抓作业 agent（server/hw_agent.py）里不碰 pad 的那几块：结果校验（宁缺勿错）、坐标换算（屏幕会转）、
全屏预览裁边、读一天存一天、附图归属；pad 那一头用假的总线。"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from server import hw_agent as ag


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
    """坐标按「模型看的那一张」换算：横屏 2560 宽 ×2，竖屏 1600 宽 ×1.25（QQ 点开图片会自己转屏）。"""
    sent = []
    monkeypatch.setattr(ag.bus, "call", lambda name, args=None, **kw: sent.append((name, args)) or {"out": "点了"})
    pad = ag.Pad()
    pad.last = {"w": 2560, "h": 1600}
    pad.act("tap", {"x": 640, "y": 400, "why": ""})
    pad.last = {"w": 1600, "h": 2560}
    pad.act("tap", {"x": 640, "y": 400, "why": ""})
    pad.act("swipe", {"dir": "down"})
    assert sent[0] == ("tap", {"x": 1280, "y": 800}) and sent[1] == ("tap", {"x": 800, "y": 500})
    sw = sent[2][1]
    assert sw["x1"] == 800 and sw["y1"] > sw["y2"] and sw["y1"] < 2560  # 竖屏按竖屏的高算


def test_widgets_scaled():
    pad = ag.Pad()
    pad.last = {"w": 2560, "h": 1600, "widgets": [{"t": "作业", "x": 1840, "y": 1341}, {"t": "作业", "x": 1, "y": 1}]}
    assert pad.widgets() == "「作业」(920,670)"


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
def fake_saver(monkeypatch, client):
    """Saver 直接写 store / homework（测试环境是临时数据目录）；模型和核验换成假的。"""
    from server import homework

    monkeypatch.setattr(ag, "models", lambda: ["m"])
    monkeypatch.setattr(ag, "verify_image", lambda raw, chain: (True, ""))
    homework.save_day("2026-10-30", [{"subject": "chinese", "text": "已有"}])
    return homework


def test_saver_saves_day_with_its_images(fake_saver):
    pad = ag.Pad()
    pad.images = [{"date": "2026-11-02", "subject": "chinese", "what": "a", "raw": _photo((200, 180, 160))},
                  {"date": "2026-11-02", "subject": "chinese", "what": "a 又存了一次", "raw": _photo((200, 180, 160))},
                  {"date": "2026-11-02", "subject": "math", "what": "这天没数学", "raw": _photo((90, 120, 90))},
                  {"date": "2026-11-01", "subject": "chinese", "what": "别的日子的", "raw": _photo((10, 200, 10))}]
    saver = ag.Saver("2026-10-25", "2026-11-02", dry_run=False, only_missing=False)
    out = saver(_ok(), pad)
    assert out.startswith("✓ 2026-11-02") and "附图 1 张" in out and "没有这科" in out
    assert saver.done == {"2026-11-02": 1}
    assert [x["date"] for x in pad.images] == ["2026-11-01"]  # 别的日子的图留着，等交那天时用
    r = fake_saver.get_day("2026-11-02")
    assert [x["subject"] for x in r["items"]] == ["chinese", "english"] and len(r["items"][0]["images"]) == 1


def test_saver_rejects_and_skips(fake_saver):
    pad = ag.Pad()
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
    assert "调 finish" in saver(far, pad)   # 范围里的都存好了、读到更早的：提示它收尾，别再往下翻
    assert fake_saver.get_day("2026-10-30")["items"][0]["text"] == "已有"   # 没被覆盖


def test_image_only_subject(fake_saver):
    """数学老师常发黑板照、卡片里没文字：text 写「见附图：…」可以交，但必须真有图，不然拒收让它补抓。"""
    day = {"date": "2026-10-29", "items": [{"subject": "chinese", "text": "1.默第13课词语"},
                                           {"subject": "math", "text": "见附图：10/29 数回 大册P43"}]}
    pad = ag.Pad()
    saver = ag.Saver("2026-10-25", "2026-11-02", dry_run=False, only_missing=False)
    out = saver(day, pad)
    assert out.startswith("×") and "见附图" in out and fake_saver.get_day("2026-10-29") is None
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


def test_fix_year():
    """模型把 2026-11-03 写成 2024-11-03：月日在范围里就按范围纠正。"""
    assert ag.fix_year("2024-11-03", "2026-11-03", "2026-11-03") == "2026-11-03"
    assert ag.fix_year("2026-11-03", "2026-11-03", "2026-11-03") == "2026-11-03"
    assert ag.fix_year("2024-12-30", "2025-12-28", "2026-01-03") == "2025-12-30"   # 跨年
    assert ag.fix_year("2024-01-02", "2025-12-28", "2026-01-03") == "2026-01-02"
    assert ag.fix_year("2024-06-01", "2026-11-01", "2026-11-03") == "2024-06-01"   # 月日不在范围：不乱改
    day, items, bad = ag.check({"date": "2024-11-02", "items": [{"subject": "chinese", "text": "x"}]},
                               "2026-10-30", "2026-11-03")
    assert day == "2026-11-02" and not bad


def _jpeg(w, h):
    import base64

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 240)).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def test_grab_end_to_end_with_fake_pad(client, monkeypatch):
    """整条：grab() → 总线下发 → 假 pad 执行、回截图 → 假模型按剧本 tap/swipe/save_day/finish → 写库 + 运行记录。"""
    import threading
    import time as _t

    from server import homework
    from server.agent_bus import bus

    today = _t.strftime("%Y-%m-%d")
    script = iter([
        ("tap", {"x": 190, "y": 402, "why": "进群"}),
        ("swipe", {"dir": "down"}),
        ("save_day", {"date": today, "items": [{"subject": "chinese", "text": "1.习字册"},
                                                {"subject": "math", "text": "1.大册P42"}]}),
        ("finish", {"why": "读完了"}),
    ])

    def fake_ask(msgs, chain):
        name, args = next(script)
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{len(msgs)}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}

    monkeypatch.setattr(ag, "ask", fake_ask)
    monkeypatch.setattr(ag, "models", lambda: ["fake"])
    did, stop = [], threading.Event()

    def pad():
        while not stop.is_set():
            job = bus.next(wait=0.2)
            if job:
                did.append(job["name"])
                bus.done(job["id"], {"ok": True, "out": "好了", "w": 2560, "h": 1600, "shot": _jpeg(2560, 1600),
                                     "widgets": [{"t": "作业", "x": 1840, "y": 1341}]})

    t = threading.Thread(target=pad, daemon=True)
    t.start()
    try:
        r = ag.grab(today, trigger="test")
    finally:
        stop.set()
    assert r["ok"] and r["saved"] == [today], r["why"]
    assert did[:3] == ["wake", "open_qq", "tap"] and "swipe" in did
    day = homework.get_day(today)
    assert [x["subject"] for x in day["items"]] == ["chinese", "math"] and day["source"] == "qq-agent"
    run = homework.runs(1, with_steps=True)[0]
    assert run["trigger"] == "test" and run["ok"] and [s["name"] for s in run["steps"]] == ["tap", "swipe", "save_day", "finish"]
    assert run["steps"][0]["shot"].startswith("data:image/jpeg")


def test_grab_pad_offline(client, monkeypatch):
    """pad 不在线：这一轮算失败、原因写清楚，不能卡死。"""
    from server import agent_bus, homework

    real = agent_bus.bus.call
    monkeypatch.setattr(agent_bus.bus, "call", lambda name, args=None, **kw: real(name, args, claim_timeout=0.2))
    r = ag.grab("2026-11-03", trigger="test")
    assert not r["ok"] and "没来领" in r["why"]
    assert homework.runs(1)[0]["why"] == r["why"]


def _run_scripted(monkeypatch, day, script, mode=""):
    """假模型按 script 走、假 pad 在总线上回截图，跑一轮 grab()。返回 (结果, 模型看到的第一句目标)。"""
    import threading

    from server.agent_bus import bus

    it = iter(script)
    seen = {}

    def fake_ask(msgs, chain):
        if "goal" not in seen:
            seen["goal"] = msgs[0]["content"][0]["text"]
        name, args = next(it)
        return {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(msgs)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}

    monkeypatch.setattr(ag, "ask", fake_ask)
    monkeypatch.setattr(ag, "models", lambda: ["fake"])
    stop = threading.Event()

    def pad():
        while not stop.is_set():
            job = bus.next(wait=0.2)
            if job:
                bus.done(job["id"], {"ok": True, "out": "好了", "w": 2560, "h": 1600, "shot": _jpeg(2560, 1600)})

    threading.Thread(target=pad, daemon=True).start()
    try:
        return ag.grab(day, trigger="test", mode=mode), seen["goal"]
    finally:
        stop.set()


def test_incremental_adds_only_new_subject(client, monkeypatch):
    """17:20：已经有英语、数学。模型被告知这一点；它交上来三科（英语读得略不一样），只有语文进库，英语原样不动。"""
    from server import homework

    d = "2026-10-23"
    homework.save_day(d, [{"subject": "english", "text": "1.完成《同步》期中自我测评"}, {"subject": "math", "text": "1.大册P42"}])
    r, goal = _run_scripted(monkeypatch, d, [
        ("save_day", {"date": d, "items": [{"subject": "english", "text": "1.完成《同步》期中自我测评。"},
                                           {"subject": "math", "text": "1.大册P42"},
                                           {"subject": "chinese", "text": "1.抄词第12课；2.习字册"}]}),
        ("finish", {"why": "补上了语文"})])
    assert "已经存好了：数学、英语" in goal and "【修改】" in goal
    assert r["ok"] and r["mode"] == "incremental" and r["changed"] == ["chinese"]
    got = {x["subject"]: x["text"] for x in homework.get_day(d)["items"]}
    assert got["english"] == "1.完成《同步》期中自我测评" and got["chinese"].startswith("1.抄词")


def test_incremental_nothing_new_is_ok(client, monkeypatch):
    from server import homework

    d = "2026-10-23"
    homework.save_day(d, [{"subject": "chinese", "text": "抄词"}])
    r, _ = _run_scripted(monkeypatch, d, [("finish", {"why": "没有新的"})])
    assert r["ok"] and r["changed"] == [] and r["saved"] == []


def test_revised_card_replaces_subject(client, monkeypatch):
    """群里有【修改】卡：模型重读那一科、标 revised，库里那一科被替换、标「已修改」，别的科不动。"""
    from server import homework

    d = "2026-10-15"
    homework.save_day(d, [{"subject": "chinese", "text": "1.小册第10课"}, {"subject": "math", "text": "1.大册第32-33页"}])
    r, _ = _run_scripted(monkeypatch, d, [
        ("save_day", {"date": d, "items": [{"subject": "chinese", "text": "1.小册第10课；2.订正课堂默写", "revised": True}]}),
        ("finish", {"why": "语文有修改，已更新"})])
    assert r["changed"] == ["chinese"]
    zh = next(x for x in homework.get_day(d)["items"] if x["subject"] == "chinese")
    assert zh["revised"] and "订正课堂默写" in zh["text"]
    assert next(x for x in homework.get_day(d)["items"] if x["subject"] == "math")["text"] == "1.大册第32-33页"


def test_verify_mode_code_decides(client, monkeypatch):
    """最后一轮核对：模型照常读每一科，不用它判断；代码比对，语文加了一整条 → 替换，数学只差标点 → 不动。"""
    from server import homework

    d = "2026-10-15"
    homework.save_day(d, [{"subject": "chinese", "text": "1.小册第10课;\n2.订正课堂默写"},
                          {"subject": "math", "text": "1、大册第32-33页，画一个计分格在33页上。"}])
    r, goal = _run_scripted(monkeypatch, d, [
        ("save_day", {"date": d, "items": [
            {"subject": "chinese", "text": "1.小册第10课;\n2.订正课堂默写\n3.默第9课词语，给字加拼音，积累AABC的叠词3个"},
            {"subject": "math", "text": "1.大册第32-33页 画一个计分格在33页上"}]}),
        ("finish", {"why": "核对完了"})], mode="verify")
    assert "每一科都重新读一遍" in goal and "不用你判断" in goal
    assert r["changed"] == ["chinese"]
    items = {x["subject"]: x for x in homework.get_day(d)["items"]}
    assert items["chinese"]["revised"] and "默第9课" in items["chinese"]["text"]
    assert items["math"]["text"] == "1、大册第32-33页，画一个计分格在33页上。" and not items["math"]["revised"]


def test_big_swipe_is_split(monkeypatch):
    """找旧日子大步滑：2.5 屏拆成 3 下（1 + 1 + 0.5），每下都在屏幕里。"""
    sent = []
    monkeypatch.setattr(ag.bus, "call", lambda name, args=None, **kw: sent.append(args) or {"out": "滑了"})
    pad = ag.Pad()
    pad.last = {"w": 2560, "h": 1600}
    pad.act("swipe", {"dir": "down", "amount": 2.5})
    assert len(sent) == 3 and all(0 < a["y2"] < a["y1"] <= 1600 for a in sent)
    assert sent[0]["y1"] - sent[0]["y2"] > 2 * (sent[2]["y1"] - sent[2]["y2"]) - 5


def test_capture_other_day_refused(monkeypatch):
    """找 10/15 时去抓 11/3 的图：不存，告诉它继续找。"""
    monkeypatch.setattr(ag.bus, "call", lambda name, args=None, **kw: {"out": "好"})
    pad = ag.Pad("2026-10-15", "2026-10-15")
    pad.raw = b"x"
    out = pad.act("capture_image", {"date": "2025-11-03", "subject": "math", "what": "黑板照"})
    assert "没存" in out and "2026-10-15" in out and pad.images == []
    pad.act("capture_image", {"date": "2024-10-15", "subject": "chinese", "what": "订正范本"})   # 年份写错也认
    assert len(pad.images) == 1


def test_wrong_day_hint_direction(fake_saver):
    """找 10/15 时交了 11/3（年份还写错成 2025）：提示「继续往下滑」，不能说成「滑过头了」。"""
    saver = ag.Saver("2026-10-15", "2026-10-15", dry_run=False, only_missing=False)
    out = saver({"date": "2025-11-03", "items": [{"subject": "math", "text": "x"}]}, ag.Pad())
    assert "继续往下滑" in out and "10月15日" in out
    out = saver({"date": "2026-10-09", "items": [{"subject": "math", "text": "x"}]}, ag.Pad())
    assert "滑过头" in out


def test_single_day_stops_after_save(client, monkeypatch):
    """只抓一天：存上以后还一直翻（实测把 40 步耗完）——再给 AFTER_SAVE 步就自动收尾，算正常结束。"""
    import time as _t

    d = _t.strftime("%Y-%m-%d")
    script = [("save_day", {"date": d, "items": [{"subject": "chinese", "text": "1.抄词"}]})] + [("swipe", {"dir": "down"})] * 30
    r, _ = _run_scripted(monkeypatch, d, script)
    assert r["ok"] and r["saved"] == [d] and "收尾" in r["why"]
    from server import homework

    steps = homework.runs(1, with_steps=True)[0]["steps"]
    assert len(steps) <= 1 + ag.AFTER_SAVE + 1
