"""生词默写（按单元出卷：听写没有拼音、古诗积累挖空、答案对得上资料）和练习模式（开始 → 听写 → 完成 → 交卷批改 → 群里 @家长；
第几次练、超时；当天汇总）。批改的看图模型换成假的。"""

from __future__ import annotations

import json
import random
import time

import pytest

from server import practice, store, worksheet
from server.exercises.chinese import word_dictation as wd
from server.family import members


def test_data_units_and_answers():
    d = wd.data()["units"]
    assert sorted(d, key=int) == [str(i) for i in range(1, 9)]
    assert "牧童骑{黄牛}，{歌声}振林樾。" in d["1"]["extras"][0]["lines"]
    assert all(d[u]["verified"] for u in "1234567") and not d["8"]["verified"]
    for u in d.values():
        ws = u["words"].split()
        assert len(ws) == len(set(ws)) and all(1 < len(w) <= 4 for w in ws)


@pytest.mark.parametrize("units,n", [([4], 47), ([1, 2, 3, 4], 30), ("第五单元", 37)])
def test_generate_counts(units, n):
    title, items = wd.generate(0, random.Random(1), units=units)
    listen = [it for it in items if it["part"] == "listen"]
    assert len(listen) == n and [it["q"] for it in listen][:2] == ["1.", "2."]  # 只有题号，没有拼音
    assert "单元" in title and all(it["unit"] in wd.units_of(units) for it in items)


def test_generate_bad_unit():
    from server.exercises import ExerciseError

    with pytest.raises(ExerciseError):
        wd.generate(0, random.Random(1), units=[9])


def test_worksheet_pages_and_answer_key():
    ws = worksheet.create("word_dictation", subject="chinese", units=[2])
    assert len(worksheet.pages(ws)) >= 2 and "听写 53 个词" in worksheet.summary(ws)
    files = worksheet.page_files(ws)
    assert len(files) == len(worksheet.pages(ws)) and all(store.file_info(f["id"]) for f in files)
    key = practice.answer_key(ws)
    assert key[0] == {"no": "1", "want": "庭院"} and {"no": "山行", "want": "霜叶红于二月花"} in key


def test_limit_estimate():
    ws = worksheet.create("word_dictation", subject="chinese", units=[1])
    # 38 个词 × 14 秒 + 6 个空 × 30 秒（不到 5 分钟按 5 分钟）
    assert practice.default_limit_s(ws) == -(-(38 * 14 + 300) // 60) * 60
    math = worksheet.create("zero_end_mul_div", subject="math", count=150)
    assert practice.default_limit_s(math) == 15 * 60


class _Rep:
    def __init__(self, text):
        self.text, self.model = text, "fake-vision"


def test_practice_flow_grade_and_report(client, fakes, monkeypatch):
    from src.llm import client as llm

    kid = members.kids()[0]
    if not any(m["role"] == "parent" and m["name"] == "爸爸" for m in members.all_members()):
        members.add("parent", "爸爸")
    ws = worksheet.create("word_dictation", subject="chinese", units=[1], for_kid=kid["id"])
    r = client.post("/api/practice", json={"worksheet": ws["id"], "kid": kid["id"], "minutes": 1}).json()
    assert r["state"] == "listening" and len(r["listen"]) == 38 and r["attempt"] == 1 and r["limit_s"] == 60
    assert client.get(f"/api/practice/current?kid={kid['id']}").json()["id"] == r["id"]
    assert client.post(f"/api/practice/{r['id']}/listened").json()["state"] == "writing"
    rec = practice.get(r["id"])
    rec["started"] -= 95  # 用了 1 分 35 秒：超时 35 秒
    store._write_json(practice._dir() / f"{r['id']}.json", rec)
    done = client.post(f"/api/practice/{r['id']}/finish").json()
    assert done["state"] == "done" and done["overtime_s"] >= 35
    msgs = store.get(kid["thread"])["messages"]
    assert "超时" in msgs[-1]["text"] and "拍照" in msgs[-1]["text"]

    seen = {}

    def fake_chat(models, messages, **kw):
        seen["parts"] = messages[0]["content"]
        return _Rep(json.dumps({"items": [{"no": "1", "want": "山坡", "got": "山坡", "ok": True},
                                          {"no": "2", "want": "学校", "got": "学笑", "ok": False}],
                                "right": 1, "total": 2, "comment": "字写得很工整，校字再看一看"}, ensure_ascii=False))

    monkeypatch.setattr(llm, "chat_full", fake_chat)
    fid = client.post("/api/files", content=b"\xff\xd8\xff" + b"\0" * 64,
                      headers={"Content-Type": "image/jpeg", "X-Filename": "paper.jpg"}).json()["id"]
    client.post(f"/api/practice/{r['id']}/submit", json={"file_ids": [fid]})
    for _ in range(50):
        if practice.get(r["id"])["state"] == "graded":
            break
        time.sleep(0.05)
    g = practice.get(r["id"])
    assert g["state"] == "graded" and g["grade"]["right"] == 1
    assert '"want": "山坡"' in seen["parts"][0]["text"] and seen["parts"][1]["type"] == "image_url"
    time.sleep(0.05)
    last = store.get(kid["thread"])["messages"][-1]["text"]
    assert "@爸爸" in last and "对了 1 / 2" in last and "写成了「学笑」" in last and "超时" in last

    # 再练一次同一张：第 2 次
    r2 = practice.start(ws["id"], kid)
    assert r2["attempt"] == 2
    rep = practice.report(kid["id"])
    assert "一共 2 次练习" in rep and "第 1 次练，批改了" in rep and "学校→学笑" in rep and "超时" in rep


def test_start_practice_tool(client, fakes):
    tid = client.get("/api/thread").json()["id"]
    fakes.llm_script = [("tool", "make_exercise", {"subject": "chinese", "topic": "word_dictation", "units": [4]}),
                        ("tool", "start_practice", {"worksheet": "latest"}), ("text", "好，开始！")]
    client.post(f"/api/conversations/{tid}/messages", json={"text": "出第四单元生词默写，开始练习"})
    acts = store.get(tid)["messages"][-1]["actions"]
    assert "听写 47 个词" in acts[0]["result"] and acts[1]["result"].startswith("练习开始了")
    kid = members.kid_of_thread(tid)
    assert practice.all_for(kid["id"])[-1]["title"] == "第四单元生词默写"
