"""查找聊天记录：关键字（多词都要有）、按说话人、只看图片 / 文件、新的在前；跳到某条给前后一段；孩子查不到别人的群。"""

from __future__ import annotations

from server import store
from tests.test_members import family  # noqa: F401  fixture


def _seed(family):
    dau, dad = family["ids"]["女儿"], family["ids"]["爸爸"]
    tid = family["admin"].get(f"/api/thread?kid={dau}").json()["id"]
    msgs = [{"role": "user", "text": "今天的作业是什么", "from": dau},
            {"role": "assistant", "text": "语文作业：读课文三遍"},
            {"role": "user", "text": "记得带围棋", "from": dad},
            {"role": "user", "text": "", "from": dad, "files": [{"id": "f1", "name": "数学作业.pdf", "kind": "file"}]},
            {"role": "user", "text": "看这张", "from": dad, "files": [{"id": "f2", "name": "a.jpg", "kind": "image"}]},
            {"role": "note", "text": "[system] 作业 现状"}]
    for i in range(60):
        msgs.append({"role": "user", "text": f"闲聊 {i}", "from": dau})
    ids = [store.append(tid, m)["id"] for m in msgs]
    return tid, ids, dau, dad


def test_keyword_who_kind(family):
    tid, ids, dau, dad = _seed(family)
    c = family["admin"]
    r = c.get(f"/api/thread/search?kid={dau}&q=作业").json()["results"]
    assert [m["id"] for m in r] == [ids[3], ids[1], ids[0]]       # 新的在前；文件名也算；note 不算
    assert r[0]["from"] == dad and r[1]["from"] == "xiaoxin" and r[2]["from"] == dau
    assert [m["id"] for m in c.get(f"/api/thread/search?kid={dau}&q=语文 课文").json()["results"]] == [ids[1]]  # 两个词都要有
    assert [m["id"] for m in c.get(f"/api/thread/search?kid={dau}&q=作业&who=xiaoxin").json()["results"]] == [ids[1]]
    assert [m["id"] for m in c.get(f"/api/thread/search?kid={dau}&who={dad}").json()["results"]] == [ids[4], ids[3], ids[2]]
    assert [m["id"] for m in c.get(f"/api/thread/search?kid={dau}&kind=image").json()["results"]] == [ids[4]]
    assert [m["id"] for m in c.get(f"/api/thread/search?kid={dau}&kind=file").json()["results"]] == [ids[3]]
    assert c.get(f"/api/thread/search?kid={dau}&q=WEIQI").json()["results"] == []


def test_around_gives_window_and_has_newer(family):
    tid, ids, dau, _ = _seed(family)
    d = family["admin"].get(f"/api/thread/around?kid={dau}&mid={ids[1]}&limit=20").json()
    got = [m["id"] for m in d["messages"]]
    assert ids[1] in got and got[0] == ids[0] and len(got) == 12 and d["has_newer"] and not d["has_more"]
    last = family["admin"].get(f"/api/thread/around?kid={dau}&mid={ids[-1]}&limit=20").json()
    assert not last["has_newer"] and last["has_more"]
    assert family["admin"].get(f"/api/thread/around?kid={dau}&mid=nope").status_code == 404


def test_kid_only_searches_own_thread(family):
    _seed(family)
    son = family["儿子"]
    assert son.get(f"/api/thread/search?kid={family['ids']['女儿']}&q=作业").json()["results"] == []  # 孩子拿到的永远是自己的群


def test_filter_by_skill(family):
    dau = family["ids"]["女儿"]
    c = family["admin"]
    tid = c.get(f"/api/thread?kid={dau}").json()["id"]
    a = store.append(tid, {"role": "assistant", "text": "这个字的写法参考笔顺图。",
                           "actions": [{"name": "show_stroke_order", "arguments": "{}", "result": "ok"}]})["id"]
    b = store.append(tid, {"role": "assistant", "text": "卞依一，图片画好了，请看。", "via": "draw"})["id"]
    d = store.append(tid, {"role": "assistant", "text": "好呀，点开始就来一盘。",
                           "actions": [{"name": "play_go", "arguments": "{}", "result": "ok"}]})["id"]
    e = store.append(tid, {"role": "assistant", "text": "搜到了", "searches": [{"queries": ["x"], "ok": True, "sources": []}]})["id"]
    store.append(tid, {"role": "user", "text": "画一只猫", "from": dau})   # 孩子说的话不算「画画」的结果
    ids = lambda sk: [m["id"] for m in c.get(f"/api/thread/search?kid={dau}&skill={sk}").json()["results"]]
    assert ids("stroke") == [a] and ids("draw") == [b] and ids("go") == [d] and ids("search") == [e]
    r = c.get(f"/api/thread/search?kid={dau}&skill=go").json()["results"][0]
    assert r["skills"] == ["go"]
    sk = {s["id"]: s["count"] for s in c.get(f"/api/thread/skills?kid={dau}").json()["skills"]}
    assert sk == {"stroke": 1, "draw": 1, "go": 1, "search": 1}   # 没用过的不列
