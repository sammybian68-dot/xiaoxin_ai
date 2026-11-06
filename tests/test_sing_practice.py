"""录唱歌声音时跟着唱的歌：歌单、从 LRC 截一段（去掉作词作曲行、找副歌、遇到间奏截断）、
排第一的版本没歌词就往下找、接口（段落、原唱要登录）。QQ 音乐是假的，不联网。"""

from __future__ import annotations

import pytest

from server.sing import job, practice, qq

DAOXIANG = """[ti:稻香]
[ar:周杰伦]
[00:00.00]稻香 - 周杰伦
[00:01.20]词：周杰伦
[00:02.40]曲：周杰伦
[00:20.00]对这个世界如果你有太多的抱怨
[00:24.00]跌倒了就不敢继续往前走
[00:50.00]还记得你说家是唯一的城堡
[00:54.00]随着稻香河流继续奔跑
[00:58.00]微微笑 小时候的梦我知道
[01:30.00]说唱一大段
[02:40.00]还记得你说家是唯一的城堡
[02:44.00]随着稻香河流继续奔跑
"""


def _lines(*pairs):
    return [(float(t), w) for t, w in pairs]


def test_parse_drops_credits_and_title_line():
    lines = practice.parse_lrc(DAOXIANG)
    assert lines[0] == (20.0, "对这个世界如果你有太多的抱怨")
    assert all("：" not in w and " - " not in w for _, w in lines)


def test_parse_handles_offset_and_multi_tags():
    lines = practice.parse_lrc("[offset:500]\n[00:10.00][00:40.00]一闪一闪亮晶晶\n")
    assert lines == [(9.5, "一闪一闪亮晶晶"), (39.5, "一闪一闪亮晶晶")]


def test_chorus_is_first_repeated_pair():
    lines = practice.parse_lrc(DAOXIANG)
    i = practice.start_index(lines, "chorus")
    assert lines[i][1] == "还记得你说家是唯一的城堡" and lines[i][0] == 50.0


def test_start_from_phrase_and_top():
    lines = practice.parse_lrc(DAOXIANG)
    assert lines[practice.start_index(lines, "微微笑")][1].startswith("微微笑")
    assert practice.start_index(lines, "top") == 0
    assert practice.start_index(lines, "没有这句") == 0


def test_segment_stops_at_interlude_and_is_relative():
    seg = practice.segment(practice.parse_lrc(DAOXIANG), "chorus")
    assert [x["text"] for x in seg["lines"]] == ["还记得你说家是唯一的城堡", "随着稻香河流继续奔跑", "微微笑 小时候的梦我知道"]
    assert [x["t"] for x in seg["lines"]] == [0, 4, 8]
    assert seg["start"] == pytest.approx(50.0)
    assert seg["end"] == pytest.approx(63.0)   # 下一句隔了 32 秒（间奏）：最后一句后面留 5 秒


def test_segment_caps_length():
    lines = _lines(*[(i * 3, f"第{i}句") for i in range(40)])
    seg = practice.segment(lines, "top")
    assert seg["lines"][-1]["t"] < practice.SEG_S
    assert seg["end"] <= practice.SEG_S + 6


@pytest.fixture
def fake_qq(monkeypatch):
    """搜《数鸭子》出三个版本：第一个没有带时间的歌词，第二个是伴奏，第三个能用。"""
    st = {"lyric_calls": [], "fetch": {"ok": True}}
    base = {"title": "数鸭子", "artist": "儿歌", "album": "儿歌大全", "album_mid": "a1", "seconds": 110, "vip": False}
    songs = [{**base, "mid": "m_plain"}, {**base, "mid": "m_acc", "title": "数鸭子 (伴奏)"}, {**base, "mid": "m_good"}]
    lrc = {"m_plain": "门前大桥下\n游过一群鸭\n",
           "m_good": "".join(f"[00:{2 + 2 * i:02d}.00]{w}\n" for i, w in enumerate(["门前大桥下", "游过一群鸭", "快来快来数一数", "二四六七八"]))}
    monkeypatch.setattr(qq, "search", lambda word, n=5: list(songs))

    def lyric(mid):
        st["lyric_calls"].append(mid)
        return lrc.get(mid, "")

    def fetch(mid, dst):
        if st["fetch"].get("ok"):
            dst.write_bytes(b"ID3" + b"\0" * 2000)
        return st["fetch"]

    monkeypatch.setattr(qq, "lyric", lyric)
    monkeypatch.setattr(qq, "fetch", fetch)
    monkeypatch.setattr(qq, "usable", lambda: True)
    return st


def test_get_skips_versions_without_timed_lyrics_and_caches(fake_qq):
    d = practice.get("shuyazi")
    assert d["mid"] == "m_good" and d["lines"][0] == {"t": 0, "text": "门前大桥下"}
    assert fake_qq["lyric_calls"] == ["m_plain", "m_good"]   # 伴奏那个根本不试
    practice.get("shuyazi")
    assert fake_qq["lyric_calls"] == ["m_plain", "m_good"]   # 第二次读缓存


def test_unknown_song_and_no_lyrics(fake_qq, monkeypatch):
    with pytest.raises(practice.PracticeError):
        practice.get("nope")
    monkeypatch.setattr(qq, "lyric", lambda mid: "")
    with pytest.raises(practice.PracticeError, match="没有带时间的歌词"):
        practice.get("xiaoxingxing")


def test_every_song_has_a_known_group():
    groups = {g for g, _ in practice.GROUPS}
    assert all(s["group"] in groups for s in practice.SONGS)
    assert len({s["id"] for s in practice.SONGS}) == len(practice.SONGS)
    assert all(s["id"].isalpha() and s["id"].islower() for s in practice.SONGS)   # 前端拿 id 拼地址、存本机


def test_api_list_segment_and_original(client, fake_qq):
    d = client.get("/api/sing/practice").json()
    assert d["listen"] is True and {g["id"] for g in d["groups"]} == {"kids", "pop", "classic"}
    assert any(s["title"] == "暖暖" and s["artist"] == "梁静茹" for s in d["songs"])
    seg = client.get("/api/sing/practice/shuyazi").json()
    assert seg["title"] == "数鸭子" and seg["lines"][1]["text"] == "游过一群鸭" and "info" not in seg
    a = client.get("/api/sing/practice/shuyazi/audio", headers={"Range": "bytes=0-9"})
    assert a.status_code == 206 and len(a.content) == 10
    assert client.get("/api/sing/practice/nope").status_code == 404


def test_api_original_needs_qq_login(client, fake_qq):
    fake_qq["fetch"] = {"ok": False, "why": "expired"}
    r = client.get("/api/sing/practice/shuyazi/audio")
    assert r.status_code == 404 and "登录过期" in r.json()["detail"]
