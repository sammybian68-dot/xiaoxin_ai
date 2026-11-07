"""翻唱：唱歌声音库（一个名字一份、对得上家里成员）、两个工具（弹录音界面、后台唱）、后台流程（挑歌 → 原曲 → SoulX →
mp3 以小新的身份发进线程）、接口（录好存下、试听、删的权限、QQ 音乐登录只有家长能动）。QQ 音乐和 SoulX 都是假的，不联网。"""

from __future__ import annotations

import json
import threading
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from server import prompts, store
from server.family import members
from server.sing import job, qq, video, voices
from server.tools import printer as printer_tools
from server.tools import registry
from server.tools import sing as sing_tools
from tests.audio import silence, wav_bytes

JIANGNAN = {"mid": "004TXEXY2G2c7C", "title": "江南", "artist": "林俊杰", "album": "第二天堂", "album_mid": "000y5gq7449K9I",
            "seconds": 267, "vip": True}


def _wav(sec: float, rate: int = 48000) -> bytes:
    return wav_bytes(silence(sec, rate), rate=rate)


@pytest.fixture
def home(client):
    """第一个孩子卞依一（她的 pad 在说话）+ 爸爸。工具上下文是她的线程。"""
    kid = members.get(client.get("/api/thread").json()["kid"])
    dad = client.post("/api/members", json={"role": "parent", "name": "爸爸"}).json()
    tok = printer_tools.ctx.set({"thread": kid["thread"], "kid": kid["id"], "kid_name": kid["name"], "by": kid["id"]})
    yield {"kid": kid, "dad": dad}
    printer_tools.ctx.reset(tok)


@pytest.fixture
def pipe(monkeypatch):
    """假的 QQ 音乐和 SoulX：搜出伴奏、现场版和原版各一首；fetch 写个假 mp3；convert 记下拿什么转的。"""
    st = {"fetch": {"ok": True, "quality": "320k"}, "converted": [], "videos": [],
          "songs": [{**JIANGNAN, "mid": "m_acc", "title": "江南 (伴奏)"}, {**JIANGNAN, "mid": "m_live", "title": "江南 (Live)"}, JIANGNAN]}
    monkeypatch.setattr(job, "service_up", lambda: True)
    monkeypatch.setattr(qq, "usable", lambda: True)
    monkeypatch.setattr(qq, "search", lambda word, n=5: list(st["songs"]))
    monkeypatch.setattr(qq, "lyric", lambda mid: "[ti:江南]\n[00:27.85]风到这里就是粘\n")
    monkeypatch.setattr(job, "_cover", lambda album_mid: album_mid)

    def fetch(mid, dst):
        if st["fetch"].get("ok"):
            dst.write_bytes(b"ID3" + b"\0" * 2000)
        return st["fetch"]

    def convert(voice_wav, song):
        st["converted"].append((voice_wav.name, song.name))
        return b"\xff\xfb" + b"\0" * 20000, {"target_sec": 267.2, "shift": -5, "total_ms": 41000}

    monkeypatch.setattr(qq, "fetch", fetch)
    monkeypatch.setattr(job, "convert", convert)
    monkeypatch.setattr(video, "ensure", lambda fid, audio, sing, *a, **k: st["videos"].append((fid, a, k)))
    return st


def _wait():
    for t in [t for t in threading.enumerate() if t.name == "sing"]:
        t.join(5)


def _last(home):
    return store.get(home["kid"]["thread"])["messages"][-1]


def _run(name, **args):
    return sing_tools.run(name, json.dumps(args, ensure_ascii=False))


# ---------------------------------------------------------------- 声音库

def test_one_voice_per_person_and_matches_family_member(home):
    assert voices.clean(" 爸爸的唱歌声音 ") == "爸爸" and voices.clean("妈妈的声音") == "妈妈"
    first = voices.save("爸爸", _wav(12))
    assert first["member"] == home["dad"]["id"] and first["seconds"] == 12.0
    second = voices.save("爸爸的唱歌声音", _wav(15))   # 重录：换掉旧的
    assert [v["id"] for v in voices.all_voices()] == [second["id"]]
    assert not voices.path(first["id"]).exists() and voices.path(second["id"]).exists()
    assert voices.find("爸爸")["id"] == second["id"]
    voices.save("卞依一", _wav(20))
    assert voices.find("依一")["name"] == "卞依一"       # 说了一半也认得
    assert voices.find("妈妈") is None


def test_voice_must_be_a_real_recording(home):
    with pytest.raises(voices.VoiceError, match="至少唱"):
        voices.save("爸爸", _wav(4))
    with pytest.raises(voices.VoiceError, match="太长"):
        voices.save("爸爸", _wav(45, rate=8000))
    with pytest.raises(voices.VoiceError):
        voices.save("爸爸", b"not a wav")
    with pytest.raises(voices.VoiceError, match="是谁"):
        voices.save(" 的声音", _wav(12))


# ---------------------------------------------------------------- 工具

def test_tools_are_registered():
    assert {"record_singing_voice", "sing_song", "sing_list"} <= set(registry.LOCAL)
    assert "sing" in prompts.ORDER


def test_record_opens_recorder_for_whoever_is_speaking(home):
    out = _run("record_singing_voice")
    assert "[sing-rec:卞依一]" in out and "播报：" in out          # 「录一下我唱歌的声音」：这台 pad 的主人
    assert "[sing-rec:爸爸]" in _run("record_singing_voice", singer="爸爸")
    voices.save("爸爸", _wav(12))
    assert "换掉旧的" in _run("record_singing_voice", singer="爸爸")


def test_tools_need_a_thread():
    assert sing_tools.run("record_singing_voice", "{}").startswith("失败：")


def test_sing_needs_a_recorded_voice(home, pipe):
    out = _run("sing_song", singer="妈妈", song="江南")
    assert out.startswith("失败：还没有「妈妈」唱歌的声音") and "还没有人录" in out and "录一下我唱歌的声音" in out
    assert _run("sing_song", singer="爸爸").startswith("失败：没说唱哪首")


def test_sing_checks_service_and_qq_login_first(home, pipe, monkeypatch):
    voices.save("爸爸", _wav(12))
    monkeypatch.setattr(job, "service_up", lambda: False)
    assert "服务没开" in _run("sing_song", singer="爸爸", song="江南")
    monkeypatch.setattr(job, "service_up", lambda: True)
    monkeypatch.setattr(qq, "usable", lambda: False)
    assert "扫码登录" in _run("sing_song", singer="爸爸", song="江南")


def test_sing_song_lands_as_music_card(home, pipe):
    v = voices.save("爸爸", _wav(12))
    out = _run("sing_song", singer="爸爸", song="《江南》")
    assert out.startswith("已开始用爸爸的声音唱《江南》（原唱 林俊杰，4 分 27 秒）") and "播报：" in out
    _wait()
    m = _last(home)
    assert m["role"] == "assistant" and m["via"] == "sing"
    assert m["text"] == "卞依一，爸爸唱的《江南》做好了，点播放听听吧。"
    f = m["files"][0]
    assert f["mime"] == "audio/mpeg" and f["owner"] == "xiaoxin" and f["name"] == "爸爸唱《江南》.mp3"
    s = m["sing"]
    assert s["file"] == f["id"] and s["mid"] == JIANGNAN["mid"] and s["singer"] == "爸爸" and s["voice"] == v["id"]
    assert s["cover"] == JIANGNAN["album_mid"] and "风到这里就是粘" in s["lrc"] and s["seconds"] == 267.2
    assert pipe["converted"] == [(f"{v['id']}.wav", f"{JIANGNAN['mid']}.mp3")]   # 伴奏、现场版都没挑
    assert (job.songs_dir() / f"{JIANGNAN['mid']}.mp3").exists()                 # 原曲缓存着，下次不用再下
    assert pipe["videos"] == [(f["id"], (), {})]                                   # 整首的分享视频先做着


def test_sing_with_my_own_voice_says_you(home, pipe):
    voices.save("卞依一", _wav(20))
    _run("sing_song", song="江南")
    _wait()
    assert _last(home)["text"].startswith("卞依一，你唱的《江南》做好了")


def test_qq_login_expired_tells_parents_to_scan(home, pipe):
    voices.save("爸爸", _wav(12))
    pipe["fetch"] = {"ok": False, "why": "expired"}
    _run("sing_song", singer="爸爸", song="江南")
    _wait()
    m = _last(home)
    assert m["via"] == "sing" and "扫码登录" in m["text"] and not m.get("files")


def test_pick_follows_named_artist(pipe):
    pipe["songs"] = [{**JIANGNAN, "mid": "cover1", "artist": "某主播"}, JIANGNAN]
    assert job.pick("江南", "林俊杰")["mid"] == JIANGNAN["mid"]
    pipe["songs"] = [{**JIANGNAN, "title": "江南 (DJ版)"}]
    with pytest.raises(job.SingError, match="没找到"):
        job.pick("江南")


# ---------------------------------------------------------------- 接口

def _device(admin, mid: str, kind: str = "pad") -> TestClient:
    from server import main

    code = admin.post(f"/api/members/{mid}/pair", json={"kind": kind}).json()["code"]
    token = admin.post("/api/pair", json={"code": code}).json()["token"]
    c = TestClient(main.app, headers={"Authorization": f"Bearer {token}"})
    c.__enter__()
    return c


def test_upload_voice_then_xiaoxin_says_how_to_use_it(client, home, monkeypatch):
    monkeypatch.setattr(job, "service_up", lambda: False)
    r = client.post(f"/api/sing/voices?kid={home['kid']['id']}", content=_wav(12), headers={"x-name": quote("爸爸")})
    assert r.status_code == 200 and r.json()["name"] == "爸爸"
    m = _last(home)
    assert m["via"] == "sing" and m["text"].startswith("爸爸唱歌的声音存好了（12 秒）") and "用爸爸的声音唱江南" in m["text"]
    d = client.get("/api/sing").json()
    assert [v["name"] for v in d["voices"]] == ["爸爸"] and d["service"] is False and d["qq"]["logged_in"] is False
    a = client.get(f"/api/sing/voices/{r.json()['id']}/audio")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav"
    assert client.post("/api/sing/voices", content=_wav(3), headers={"x-name": quote("爸爸")}).status_code == 400


def test_kid_can_record_but_not_delete_others_or_touch_qq_login(client, home):
    pad = _device(client, home["kid"]["id"])
    try:
        mine = pad.post("/api/sing/voices", content=_wav(12), headers={"x-name": quote("卞依一")}).json()
        dads = client.post("/api/sing/voices", content=_wav(12), headers={"x-name": quote("爸爸")}).json()
        assert pad.delete(f"/api/sing/voices/{dads['id']}").status_code == 403
        assert pad.delete(f"/api/sing/voices/{mine['id']}").status_code == 200
        assert pad.post("/api/sing/qq/login", json={"kind": "qq"}).status_code == 403
        assert pad.delete("/api/sing/qq").status_code == 403
        assert client.delete(f"/api/sing/voices/{dads['id']}").status_code == 200
    finally:
        pad.__exit__(None, None, None)


def _cover(thread, singer, title, mid, at):
    """线程里放一首唱好的歌（和 job._work 发的一样的字段），返回文件 id。"""
    fid, p = store.allocate()
    p.write_bytes(b"\xff\xfb" + b"\0" * 100)
    meta = store.commit(fid, f"{singer}唱《{title}》.mp3", "audio/mpeg", 102, owner="xiaoxin")
    store.append(thread, {"role": "assistant", "via": "sing", "files": [meta], "text": "做好了", "created": at,
                          "sing": {"file": fid, "title": title, "artist": "原唱", "album": "", "cover": "", "singer": singer,
                                   "voice": "v_" + singer, "mid": mid, "lrc": "[00:01.00]第一句", "seconds": 200.0}})
    return fid


def test_sing_list_groups_by_voice_and_keeps_newest_retake(client, home):
    t = home["kid"]["thread"]
    _cover(t, "爸爸", "江南", "m1", 1)
    retake = _cover(t, "爸爸", "江南", "m1", 3)
    _cover(t, "爸爸", "泡沫", "m2", 2)
    _cover(t, "卞依一", "江南", "m1", 4)          # 同一首，不同的声音：两首都留
    gone = _cover(t, "爸爸", "洋葱", "m3", 5)
    store.abort(gone)
    songs = job.library([t])
    assert [(s["singer"], s["title"]) for s in songs] == [("卞依一", "江南"), ("爸爸", "江南"), ("爸爸", "泡沫")]
    assert songs[1]["file"] == retake and "lrc" not in songs[0]

    out = _run("sing_list")
    assert "[sing-list:]" in out and "爸爸 2 首：江南、泡沫" in out and "播报：" in out
    assert "[sing-list:爸爸]" in _run("sing_list", singer="爸爸") and "爸爸翻唱的 2 首" in _run("sing_list", singer="爸爸")
    assert "[sing-list:卞依一]" in _run("sing_list", singer="我")   # 「我唱过哪些歌」：这台 pad 的主人
    assert "[sing-list:]" in _run("sing_list", singer="妈妈") and "还没有" in _run("sing_list", singer="妈妈")

    d = client.get("/api/sing/songs").json()
    assert d["singers"] == [{"name": "卞依一", "n": 1}, {"name": "爸爸", "n": 2}] and len(d["songs"]) == 3
    one = client.get(f"/api/sing/songs/{retake}").json()
    assert one["lrc"] == "[00:01.00]第一句" and one["title"] == "江南"


def test_sing_list_kid_sees_only_own_thread(client, home):
    son = client.post("/api/members", json={"role": "kid", "name": "儿子"}).json()
    mine = _cover(home["kid"]["thread"], "爸爸", "江南", "m1", 1)
    his = _cover(members.get(son["id"])["thread"], "爸爸", "孤勇者", "m9", 2)
    assert len(client.get("/api/sing/songs").json()["songs"]) == 2          # 家长看全家
    pad = _device(client, home["kid"]["id"])
    try:
        assert [s["file"] for s in pad.get("/api/sing/songs").json()["songs"]] == [mine]
        assert pad.get(f"/api/sing/songs/{his}").status_code == 404
    finally:
        pad.__exit__(None, None, None)
    assert "孤勇者" not in _run("sing_list")                                 # 孩子说的：只数自己线程里的
    tok = printer_tools.ctx.set({**printer_tools.ctx.get(), "by": home["dad"]["id"]})
    try:
        assert "孤勇者" in _run("sing_list")                                 # 爸爸说的：全家
    finally:
        printer_tools.ctx.reset(tok)


def test_sing_delete_removes_every_trace(client, home, monkeypatch):
    from server.family import hub

    events = []
    monkeypatch.setattr(hub, "publish", lambda tid, ev: events.append((tid, ev)))
    t = home["kid"]["thread"]
    old = _cover(t, "爸爸", "江南", "m1", 1)
    new = _cover(t, "爸爸", "江南", "m1", 3)                 # 重唱过：两版一起删，不然删完老的又冒出来
    hers = _cover(t, "卞依一", "江南", "m1", 4)              # 同一首别的声音：留着，原曲缓存也留着
    pao = _cover(t, "爸爸", "泡沫", "m2", 2)
    msgs = store.get(t)["messages"]
    store.update_message(t, next(m["id"] for m in msgs if (m.get("sing") or {}).get("file") == pao), sing={
        **next(m["sing"] for m in msgs if (m.get("sing") or {}).get("file") == pao), "cover": "alb2alb2X"})
    fwd = store.append(t, {"role": "user", "text": "听这个", "files": [store.file_info(new)[1], store.file_info(hers)[1]]})
    songs, vids = job.songs_dir(), video.videos_dir()
    songs.mkdir(parents=True, exist_ok=True)
    vids.mkdir(parents=True, exist_ok=True)
    for name in ("m1.mp3", "m1.lrc", "m2.mp3", "m2.lrc"):
        (songs / name).write_bytes(b"x")
    job.cover_path("alb2alb2X").parent.mkdir(parents=True, exist_ok=True)
    job.cover_path("alb2alb2X").write_bytes(b"\xff\xd8")
    for name in (f"{old}.mp4", f"{new}.mp4", f"{new}_10_200.mp4", f"{new}_o5.mp4", f"{hers}.mp4"):
        (vids / name).write_bytes(b"v")

    r = client.delete(f"/api/sing/songs/{new}").json()
    assert r["ok"] and sorted(r["files"]) == sorted([old, new]) and r["messages"] == 2
    assert store.file_info(old) is None and store.file_info(new) is None and store.file_info(hers) is not None
    assert sorted(p.name for p in vids.iterdir()) == [f"{hers}.mp4"]
    assert (songs / "m1.mp3").exists()
    left = store.get(t)["messages"]
    assert not [m for m in left if (m.get("sing") or {}).get("file") in (old, new)]
    assert [f["id"] for f in next(m for m in left if m["id"] == fwd["id"])["files"]] == [hers]
    assert [(s["singer"], s["title"]) for s in client.get("/api/sing/songs").json()["songs"]] == [("卞依一", "江南"), ("爸爸", "泡沫")]
    kinds = [ev["type"] for _, ev in events]
    assert "msg.delete" in kinds and ("sing.delete" in kinds) and all(tid == t for tid, _ in events)
    assert client.get(f"/api/files/{new}").status_code == 404
    assert '"sing.delete"' in (store.DATA / "audit.jsonl").read_text(encoding="utf-8")

    client.delete(f"/api/sing/songs/{pao}")
    assert not (songs / "m2.mp3").exists() and not (songs / "m2.lrc").exists() and not job.cover_path("alb2alb2X").exists()
    assert client.delete(f"/api/sing/songs/{pao}").status_code == 404


def test_sing_delete_kid_only_own_thread(client, home):
    son = client.post("/api/members", json={"role": "kid", "name": "儿子"}).json()
    mine = _cover(home["kid"]["thread"], "爸爸", "江南", "m1", 1)
    his = _cover(members.get(son["id"])["thread"], "爸爸", "孤勇者", "m9", 2)
    pad = _device(client, home["kid"]["id"])
    try:
        assert pad.delete(f"/api/sing/songs/{his}").status_code == 404
        assert store.file_info(his) is not None
        assert pad.delete(f"/api/sing/songs/{mine}").json()["ok"]
    finally:
        pad.__exit__(None, None, None)
    assert store.file_info(mine) is None


def test_cover_rejects_odd_names(client):
    assert client.get("/api/sing/cover/..%2F..%2Fmembers").status_code == 404
    assert client.get("/api/sing/cover/000y5gq7449K9I").status_code == 404   # 没下过
