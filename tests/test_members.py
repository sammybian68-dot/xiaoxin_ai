"""成员和设备（阶段 2）：迁移认领、增删改、配对换 token、按线程隔离（兄妹互相看不到）、闹钟按孩子分、孩子档案进 prompt。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import clock, store
from server.family import members


@pytest.fixture
def family(client):
    """管理员建好：女儿（迁移来的第一个孩子）、儿子、爸爸，各配一台设备。返回 {名字: TestClient}。"""
    dau = client.get("/api/thread").json()["kid"]  # 第一次调：建第一个孩子，认领阶段 1 的线程
    son = client.post("/api/members", json={"role": "kid", "name": "儿子", "call": "小宝", "grade": "一年级"}).json()
    dad = client.post("/api/members", json={"role": "parent", "name": "爸爸"}).json()
    from server import main

    out = {"admin": client}
    for name, mid, kind in (("女儿", dau, "pad"), ("儿子", son["id"], "pad"), ("爸爸", dad["id"], "phone")):
        code = client.post(f"/api/members/{mid}/pair", json={"kind": kind}).json()["code"]
        token = client.post("/api/pair", json={"code": code}).json()["token"]
        c = TestClient(main.app, headers={"Authorization": f"Bearer {token}"})
        c.__enter__()
        out[name] = c
    out["ids"] = {"女儿": dau, "儿子": son["id"], "爸爸": dad["id"]}
    yield out
    for k in ("女儿", "儿子", "爸爸"):
        out[k].__exit__(None, None, None)


# ---------------------------------------------------------------- 迁移

def test_first_kid_claims_phase1_thread_and_old_clock(fakes):
    tid = store.thread_id()
    store.append(tid, {"role": "user", "text": "老的一句"})
    (store.DATA / "clock.json").write_text(json.dumps({"version": 3, "seq": 1, "alarms": [
        {"id": "a1", "time": "07:00", "repeat": [], "label": "", "say": "", "enabled": True, "created": 1}]}))
    store.update_settings(ring_text="依依起床")
    kid = members.kids()[0]
    assert kid["thread"] == tid and kid["name"] == "卞依一"
    assert not (store.DATA / "clock.json").exists()
    with clock.use(kid["id"]):
        snap = clock.snapshot()
    assert [a["id"] for a in snap["alarms"]] == ["a1"] and snap["ring_text"] == "依依起床"


# ---------------------------------------------------------------- 管理

def test_admin_manages_members(client):
    client.get("/api/thread")
    r = client.post("/api/members", json={"role": "kid", "name": "儿子", "grade": "一年级"})
    son = r.json()
    assert r.status_code == 200 and son["thread"] and store.get(son["thread"])["messages"] == []
    assert client.post("/api/members", json={"role": "kid", "name": "儿子"}).status_code == 400  # 重名
    assert client.post("/api/members", json={"role": "boss", "name": "x"}).status_code == 400
    assert client.patch(f"/api/members/{son['id']}", json={"call": "小宝", "style": "多夸夸他"}).json()["call"] == "小宝"
    assert client.patch(f"/api/members/{son['id']}", json={"role": "parent"}).status_code == 400  # 角色不能改
    g = client.get("/api/members").json()
    assert [k["name"] for k in g["kids"]] == ["卞依一", "儿子"] and g["parents"] == []
    assert client.delete(f"/api/members/{son['id']}").status_code == 200  # 默认停用
    assert members.get(son["id"])["status"] == "disabled" and store.get(son["thread"])
    assert client.delete(f"/api/members/{son['id']}?hard=1").status_code == 200
    with pytest.raises(KeyError):
        store.get(son["thread"])
    assert list(store.BACKUP.glob(f"member-{son['id']}-*/*.json"))  # 删前打包了
    audit = [json.loads(x)["action"] for x in (store.DATA / "audit.jsonl").read_text().splitlines()]
    assert audit == ["member.add", "member.edit", "member.disable", "member.remove"]


def test_pair_code_is_one_time_and_expires(client, monkeypatch):
    client.get("/api/thread")
    kid = members.kids()[0]["id"]
    code = client.post(f"/api/members/{kid}/pair", json={"kind": "pad"}).json()["code"]
    assert len(code) == 6
    r = client.post("/api/pair", json={"code": code})
    assert r.status_code == 200 and r.json()["token"].startswith("xd_") and "xiaoxin_device" in r.headers["set-cookie"]
    assert client.post("/api/pair", json={"code": code}).status_code == 400  # 用过了
    code2 = client.post(f"/api/members/{kid}/pair", json={"kind": "pad"}).json()["code"]
    real = members.time.time
    monkeypatch.setattr(members.time, "time", lambda: real() + members.PAIR_TTL_S + 1)
    assert client.post("/api/pair", json={"code": code2}).status_code == 400  # 过期
    stored = json.loads((store.DATA / "devices.json").read_text())
    assert "token" not in json.dumps(stored).replace("token_sha256", "")  # 只存哈希


def test_pair_brute_force_is_throttled(client):
    for _ in range(members.PAIR_MAX_FAILS):
        client.post("/api/pair", json={"code": "000000"})
    assert "等一分钟" in client.post("/api/pair", json={"code": "000000"}).json()["detail"]


# ---------------------------------------------------------------- 隔离

def test_me_tells_who_and_which_kids(family):
    assert family["女儿"].get("/api/me").json()["who"]["role"] == "kid"
    assert [k["name"] for k in family["女儿"].get("/api/me").json()["kids"]] == ["卞依一"]
    assert [k["name"] for k in family["爸爸"].get("/api/me").json()["kids"]] == ["卞依一", "儿子"]
    assert family["admin"].get("/api/me").json()["who"]["kind"] == "admin"


def test_siblings_cannot_see_each_other(family, fakes):
    dau_t = family["女儿"].get("/api/thread").json()["id"]
    son_t = family["儿子"].get("/api/thread").json()["id"]
    assert dau_t != son_t
    # 儿子带 ?kid= 想要女儿的：照样拿到自己的
    assert family["儿子"].get(f"/api/thread?kid={family['ids']['女儿']}").json()["id"] == son_t
    family["女儿"].post(f"/api/conversations/{dau_t}/messages", json={"text": "我的秘密"})
    s = family["儿子"]
    for path in (f"/api/conversations/{dau_t}", f"/api/conversations/{dau_t}/ctx", f"/api/debug/{dau_t}"):
        assert s.get(path).status_code in (403, 404), path
    assert s.post(f"/api/conversations/{dau_t}/messages", json={"text": "偷看"}).status_code == 404
    assert [c["id"] for c in s.get("/api/conversations").json()["conversations"]] == [son_t]
    with s.websocket_connect(f"/api/talk?conversation={dau_t}") as ws:  # 自由对话也一样：进的是自己的
        assert json.loads(ws.receive_text())["conversation"] == son_t
    # 家长两条都能进
    assert family["爸爸"].get(f"/api/conversations/{dau_t}").status_code == 200
    assert family["爸爸"].get(f"/api/thread?kid={family['ids']['儿子']}").json()["id"] == son_t


def test_kid_cannot_manage(family):
    k = family["女儿"]
    assert k.post("/api/members", json={"role": "kid", "name": "x"}).status_code == 403
    assert k.get("/api/members").status_code == 403
    assert k.get(f"/api/debug/{k.get('/api/thread').json()['id']}").status_code == 403
    assert family["爸爸"].post("/api/members", json={"role": "kid", "name": "x"}).status_code == 403  # 家长也不行


def test_files_are_scoped(family, fakes):
    up = family["女儿"].post("/api/files", content=b"hello", headers={"x-filename": "a.txt", "content-type": "text/plain"})
    fid = up.json()["id"]
    assert family["女儿"].get(f"/api/files/{fid}").status_code == 200
    assert family["爸爸"].get(f"/api/files/{fid}").status_code == 200
    assert family["儿子"].get(f"/api/files/{fid}").status_code == 404
    son_t = family["儿子"].get("/api/thread").json()["id"]
    assert family["儿子"].post(f"/api/conversations/{son_t}/messages", json={"file_ids": [fid]}).status_code == 400
    # 爸爸发进儿子线程的文件，儿子能拿
    up2 = family["爸爸"].post("/api/files", content=b"hw", headers={"x-filename": "b.txt", "content-type": "text/plain"})
    family["爸爸"].post(f"/api/conversations/{son_t}/messages", json={"file_ids": [up2.json()["id"]], "text": "作业"})
    assert family["儿子"].get(f"/api/files/{up2.json()['id']}").status_code == 200


def test_revoked_or_disabled_device_is_out(family):
    d = members.devices(family["ids"]["儿子"])[0]
    assert family["儿子"].get("/api/me").status_code == 200
    family["admin"].delete(f"/api/devices/{d['id']}")
    assert family["儿子"].get("/api/me").status_code == 401
    family["admin"].patch(f"/api/members/{family['ids']['爸爸']}", json={"active": False})
    assert family["爸爸"].get("/api/me").status_code == 401


# ---------------------------------------------------------------- 每个孩子自己的

def test_clock_is_per_kid(family):
    family["女儿"].post("/api/clock/alarms", json={"time": "07:00"})
    family["儿子"].post("/api/clock/alarms", json={"time": "06:30"})
    family["儿子"].post("/api/clock/alarms", json={"time": "21:00"})
    assert [a["time"] for a in family["女儿"].get("/api/clock").json()["alarms"]] == ["07:00"]
    assert [a["time"] for a in family["儿子"].get("/api/clock").json()["alarms"]] == ["06:30", "21:00"]
    son = family["ids"]["儿子"]
    assert len(family["爸爸"].get(f"/api/clock?kid={son}").json()["alarms"]) == 2
    assert family["儿子"].get("/api/clock").json()["ring_text"] == "小宝，时间到了！"  # 按称呼


def test_model_tools_and_prompt_follow_thread_owner(family, fakes):
    fakes.llm_script = [("tool", "alarm_set", {"time": "06:30", "say": "小宝起床"}), ("text", "好的")]
    son_t = family["儿子"].get("/api/thread").json()["id"]
    family["儿子"].post(f"/api/conversations/{son_t}/messages", json={"text": "六点半叫我"})
    assert [a["time"] for a in family["儿子"].get("/api/clock").json()["alarms"]] == ["06:30"]
    assert family["女儿"].get("/api/clock").json()["alarms"] == []
    system = fakes.llm_requests[-1]["system"]
    assert "这是儿子的小新" in system and "「小宝」" in system and "一年级" in system


def test_device_token_link_and_cookie(client, anon):
    """APK 存的是设备 token：?k=<token> 种设备 cookie 再跳回去，之后不带 header 也是这台设备；管理员 cookie 被退掉。"""
    client.get("/api/thread")
    kid = members.kids()[0]["id"]
    code = client.post(f"/api/members/{kid}/pair", json={"kind": "pad"}).json()["code"]
    token = anon.post("/api/pair", json={"code": code}).json()["token"]
    anon.cookies.clear()
    r = anon.get(f"/?k={token}", follow_redirects=False)
    assert r.status_code == 303 and "xiaoxin_device=" in r.headers["set-cookie"]
    anon.cookies.set("xiaoxin_device", token)
    me = anon.get("/api/me").json()["who"]
    assert me["role"] == "kid" and me["id"] == kid
    assert anon.get("/?k=xd_wrong", follow_redirects=False).headers.get("x-xiaoxin-link") == "bad"
