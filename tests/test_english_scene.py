"""英语小剧场：每句评测（假讯飞）、星级、重读、熟练度四级、复习排期、今天推荐、看板、接口鉴权、工具。"""

from __future__ import annotations

import datetime as dt
import io
import json
import wave

import pytest

from server.english import drill
from tests.test_members import family  # noqa: F401  fixture


def _lesson(lid="icecream", order=8, unit="生活"):
    d = drill.root() / "lessons" / lid
    d.mkdir(parents=True, exist_ok=True)
    L = {"id": lid, "name": lid.title(), "zh": {"icecream": "冰淇淋店", "fruit": "水果摊", "hello": "你好"}.get(lid, lid),
         "unit": unit, "order": order,
         "roles": [{"id": "lily", "name": "Lily", "zh": "莉莉"},
                   {"id": "tom", "name": "Tom", "zh": "汤姆"} if lid == "hello" else {"id": "bear", "name": "Mr. Bear", "zh": "熊先生"}],
         "lines": [{"role": "tom" if lid == "hello" else "bear", "en": "Hello! I'm Mr. Bear.", "zh": "你好", "dur_s": 2, "slow_s": 3},
                   {"role": "lily", "en": "Hello, Mr. Bear. I'd like ice cream.", "zh": "你好", "dur_s": 2, "slow_s": 3},
                   {"role": "tom" if lid == "hello" else "bear", "en": "One scoop or two?", "zh": "几个球", "dur_s": 2, "slow_s": 3},
                   {"role": "lily", "en": "Two, please.", "zh": "两个", "dur_s": 1, "slow_s": 2}],
         "new_words": []}
    (d / "lesson.json").write_text(json.dumps(L, ensure_ascii=False))
    return L


def _wav(sec=1.0):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x10" * int(16000 * sec))
    return b.getvalue()


def fake(total, words=None):
    """假讯飞：给定总分；每个词默认 95，words 里可以改某个词的分 / 错误。"""
    def assess(text, pcm):
        ws = []
        for w in text.replace(",", "").replace(".", "").replace("!", "").replace("?", "").split():
            s, e = (words or {}).get(w.lower(), (95, ""))
            ws.append({"word": w.lower(), "score": s, "err": e})
        return {"total": total, "accuracy": total, "fluency": 90, "integrity": 100, "words": ws}
    return assess


def test_stars_and_retry_rules():
    _lesson()
    s = drill.start("k1", "icecream", "lily")
    r = drill.score_line(s["id"], "k1", 1, _wav(), assess=fake(66, {"cream": (52, "")}))
    assert r["stars"] == 1 and not r["pass"] and "cream" in r["msg"] and not r["last"]
    r = drill.score_line(s["id"], "k1", 1, _wav(), assess=fake(83, {"ice": (0, "漏读")}))
    assert not r["pass"] and "没读出来" in r["msg"]                     # 漏读：一定重读
    r = drill.score_line(s["id"], "k1", 1, _wav(), assess=fake(90))
    assert r["pass"] and r["stars"] == 3 and r["last"]                  # 第 3 次（最多 3 次）
    assert drill.audio(s["id"], "k1", 1, 2).is_file()                    # 每次的录音都存着
    with pytest.raises(ValueError, match="不是你的台词"):
        drill.score_line(s["id"], "k1", 0, _wav(), assess=fake(90))
    with pytest.raises(ValueError, match="没听清"):
        drill.score_line(s["id"], "k1", 3, _wav(0.1), assess=fake(90))


def test_third_try_moves_on_even_if_bad():
    _lesson()
    s = drill.start("k1", "icecream", "lily")
    for _ in range(2):
        drill.score_line(s["id"], "k1", 3, _wav(), assess=fake(40))
    r = drill.score_line(s["id"], "k1", 3, _wav(), assess=fake(40))
    assert r["last"] and "先往下" in r["msg"]


def _play(kid, lid, role, score, day):
    s = drill.start(kid, lid, role)
    L = drill.lesson(lid)
    for i in drill.mine(L, role):
        drill.score_line(s["id"], kid, i, _wav(), assess=fake(score))
    s = drill.get(s["id"]); s["started"] = dt.datetime.combine(day, dt.time(19)).timestamp(); drill.save(s)
    drill.finish(s["id"], kid, today=day)
    return drill.progress(kid)[lid]


def test_levels_and_spacing():
    _lesson()
    d0 = dt.date(2026, 11, 1)
    p = _play("k1", "icecream", "lily", 60, d0)
    assert p["level"] == 1 and p["interval"] == 1 and p["due"] == "2026-11-02"      # 会读，没熟：明天
    p = _play("k1", "icecream", "lily", 78, d0 + dt.timedelta(1))
    assert p["level"] == 2 and p["interval"] == 1                                     # 熟练（都 2 星）
    p = _play("k1", "icecream", "lily", 92, d0 + dt.timedelta(2))
    assert p["level"] == 2 and p["interval"] == 2                                     # 全 3 星：间隔 ×2.2
    p = _play("k1", "icecream", "lily", 93, d0 + dt.timedelta(4))
    assert p["interval"] == 4 and p["level"] == 2                                     # 熊先生还没演：不算掌握
    _play("k1", "icecream", "bear", 95, d0 + dt.timedelta(8))
    p = _play("k1", "icecream", "bear", 96, d0 + dt.timedelta(12))
    assert p["level"] == 3                                                            # 两个角色各连续两次全 3 星
    p = _play("k1", "icecream", "lily", 50, d0 + dt.timedelta(30))
    assert p["interval"] == 1                                                         # 忘了：回到 1 天


def test_recommend_order():
    _lesson("hello", 0, "U1"); _lesson("fruit", 9); _lesson("icecream", 8)
    d = dt.date(2026, 11, 5)
    r = drill.recommend("k1", today=d)
    assert r["pick"]["id"] == "hello" and r["pick"]["kind"] == "new"                 # 都没练：按课程顺序开新课
    _play("k1", "fruit", "lily", 60, d - dt.timedelta(3))                            # 会读，到期（11/3 到期）
    _play("k1", "icecream", "lily", 92, d - dt.timedelta(1))                         # 熟练，没到期
    r = drill.recommend("k1", today=d)
    assert r["pick"]["id"] == "fruit" and r["pick"]["kind"] == "due" and "过了 2 天" in r["why"]
    assert [a["id"] for a in r["also"]][:1] == ["hello"]
    _play("k1", "fruit", "lily", 92, d)                                              # 今天练过：不再推
    assert drill.recommend("k1", today=d)["pick"]["id"] != "fruit"


def test_board_numbers():
    _lesson()
    d = dt.date.today()
    _play("k1", "icecream", "lily", 66, d)
    b = drill.board("k1", today=d)
    ice = next(L for L in b["lessons"] if L["id"] == "icecream")
    assert ice["level"] == 1 and ice["times"] == 1 and b["streak"] == 1 and b["total"]["sessions"] == 1
    assert b["days"][-1]["d"] == d.isoformat()


def test_api_flow_and_kid_isolation(family, monkeypatch):
    from src.voice import xfyun_ise

    _lesson()
    monkeypatch.setattr(xfyun_ise, "assess", fake(91))
    dau, son, dad = family["女儿"], family["儿子"], family["爸爸"]
    s = dau.post("/api/english/session", json={"lesson": "icecream", "role": "lily"}).json()
    r = dau.post(f"/api/english/session/{s['id']}/line/1", content=_wav(), headers={"Content-Type": "audio/wav"}).json()
    assert r["stars"] == 3 and r["pass"]
    assert dau.get(f"/api/english/session/{s['id']}/audio/1/1.wav").status_code == 200
    assert son.get(f"/api/english/session/{s['id']}").status_code == 404          # 别的孩子看不到
    assert dau.get("/api/english").json()["current"]["id"] == s["id"]              # 暂停着：首页能接着练
    f = dau.post(f"/api/english/session/{s['id']}/finish", json={}).json()
    assert f["done"] and f["summary"]["said"] == 1
    kid = family["ids"]["女儿"]
    assert dad.get(f"/api/english?kid={kid}").json()["lessons"][0]["times"] == 1   # 家长看得到女儿的
    msgs = family["admin"].get(f"/api/thread?kid={kid}").json()["messages"]
    assert "练完了英语小剧场「冰淇淋店」" in msgs[-1]["text"]                      # 群里发了小结
    assert dau.get("/api/english/asset/icecream/../x").status_code == 404


def test_tools(family):
    from server.tools import english_scene as T
    from server.tools import printer as printer_tools

    _lesson("hello", 0, "U1"); _lesson()
    tok = printer_tools.ctx.set({"thread": "t", "kid": family["ids"]["女儿"]})
    try:
        out = T.run("english_scene", "{}")
        assert "[en:hello]" in out and "播报：" in out                           # 推荐
        assert "[en:icecream]" in T.run("english_scene", json.dumps({"lesson": "冰淇淋店"}))
        assert "[en:icecream]" in T.run("english_scene", json.dumps({"lesson": "小熊那个"}))      # 角色名里有「熊」
        assert "[en:icecream]" in T.run("english_scene", json.dumps({"lesson": "再练一下冰淇淋的对话"}))
        assert T.run("english_scene", json.dumps({"lesson": "火星"})).startswith("失败：没找到")
        assert "看板" in T.run("english_scene_board", "{}")
    finally:
        printer_tools.ctx.reset(tok)
    assert T.run("english_scene", "{}").startswith("失败：这里不能")


def test_generate_is_async(family, monkeypatch):
    import threading

    from server.english import api as eapi, build
    from server.tools import english_scene as T
    from server.tools import printer as printer_tools

    gate = threading.Event()

    def fake_custom(ask):
        gate.wait(5)
        return _lesson("duku", 1000)
    monkeypatch.setattr(build, "custom", fake_custom)
    kid = family["ids"]["女儿"]
    tok = printer_tools.ctx.set({"thread": "t", "kid": kid})
    try:
        out = T.run("english_scene_new", json.dumps({"ask": "在独库公路上开车看美景"}))
    finally:
        printer_tools.ctx.reset(tok)
    assert "正在为你写对话场景" in out and eapi.GENERATING[kid]                    # 马上回，后台在做
    gate.set()
    for t in [t for t in threading.enumerate() if t.name == "english-gen"]:
        t.join(5)
    msgs = family["admin"].get(f"/api/thread?kid={kid}").json()["messages"]
    assert "对话场景做好了，我们一起练习吧" in msgs[-1]["text"] and msgs[-1]["english"]["lesson"] == "duku"
    assert not eapi.GENERATING[kid]


def test_silence_not_sent_to_xfyun():
    _lesson()
    s = drill.start("k1", "icecream", "lily")
    quiet = io.BytesIO()
    with wave.open(quiet, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00" * 16000)
    called = []
    with pytest.raises(ValueError, match="没听到声音"):
        drill.score_line(s["id"], "k1", 1, quiet.getvalue(), assess=lambda t, p: called.append(1))
    assert not called
