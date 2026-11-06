"""翻唱发到微信的视频：截哪段（整首、一段、太短、超过 5 分钟）、同一段只做一次、真画一小段（有 ffmpeg 才跑）、
接口（做好了给地址、下载、不是翻唱的歌 / 看不到的文件 404、做坏了再要就重做）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from server import store
from server.family import members
from server.sing import video
from tests.audio import silence, wav_bytes

LRC = "[00:00.00]小星星 - 儿歌\n[00:00.50]一闪一闪亮晶晶\n[00:02.00]满天都是小星星\n[00:03.50]挂在天上放光明\n"


def test_span_whole_part_short_and_cap():
    assert video.span(None, None, 267.2) == (0.0, 267.2)
    assert video.span(30, 60, 267.2) == (30.0, 60.0)
    assert video.span(250, 999, 267.2) == (250.0, 267.2)        # 终点超了歌长：到歌尾
    assert video.span(0, None, 400) == (0.0, video.MAX_S)       # 微信最长 5 分钟
    with pytest.raises(video.VideoError, match="太短"):
        video.span(10, 11, 267.2)


def test_key_whole_song_is_file_id():
    assert video.key("abc123", 0.0, 267.2, 267.2) == "abc123"
    assert video.key("abc123", 30.0, 60.5, 267.2) == "abc123_300_605"
    assert video.key("abc123", 0.0, 267.2, 267.2, -0.5) == "abc123_o-5"   # 卡片上调过歌词快慢：另做一份
    assert video.path("abc123_300_605_o5") is None
    assert video.path("../etc/passwd") is None


@pytest.mark.skipif(not video.ffmpeg_exe(), reason="没有 ffmpeg")
def test_render_a_short_clip(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(wav_bytes(silence(6, 16000), rate=16000))
    out = tmp_path / "clip.mp4"
    seen = []
    video.render({"audio": str(audio), "out": str(out), "start": 1.0, "end": 4.0, "seconds": 6.0, "cover": "",
                  "lrc": LRC, "off": 0, "title": "小星星", "artist": "儿歌", "singer": "爸爸"}, seen.append)
    data = out.read_bytes()
    assert data[4:8] == b"ftyp" and len(data) > 5000
    assert seen[0] == 0 and seen[-1] >= 90
    assert not list(tmp_path.glob("*.part.mp4"))


def test_parallel_frames_same_as_serial(tmp_path, monkeypatch):
    """长一点的段分给几个进程画：喂给 ffmpeg 的帧、顺序和单进程一样。"""
    spec = {"audio": "", "out": str(tmp_path / "x.mp4"), "start": 0.0, "end": 6.0, "seconds": 6.0, "cover": "",
            "lrc": LRC, "off": 0, "title": "小星星", "artist": "儿歌", "singer": "爸爸"}
    fed = {}

    class Pipe:
        def __init__(self, ff, stdin=None):
            self.stdin, self.frames, self.returncode = self, [], 0
            fed.setdefault("runs", []).append(self.frames)
            Path(ff[-1]).write_bytes(b"")

        def write(self, b):
            self.frames.append(b)

        def close(self):
            pass

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(video.subprocess, "Popen", Pipe)
    monkeypatch.setattr(video, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video, "WORKERS", 1)
    video.render(spec)
    monkeypatch.setattr(video, "WORKERS", 3)
    video.render(spec)
    serial, parallel = fed["runs"]
    assert len(serial) == 6 * video.FPS and serial == parallel


@pytest.fixture
def song(client):
    """第一个孩子线程里一张翻唱卡片（mp3 是假的，画视频换成直接写个文件）。"""
    thread = members.get(client.get("/api/thread").json()["kid"])["thread"]
    fid, p = store.allocate()
    p.write_bytes(b"\xff\xfb" + b"\0" * 2000)
    meta = store.commit(fid, "爸爸唱《小星星》.mp3", "audio/mpeg", p.stat().st_size, owner="xiaoxin")
    sing = {"file": fid, "title": "小星星", "artist": "儿歌", "cover": "", "singer": "爸爸", "lrc": LRC, "seconds": 120.0}
    store.append(thread, {"role": "assistant", "model": "", "via": "sing", "files": [meta], "sing": sing, "text": "做好了"})
    return {"fid": fid, "thread": thread}


@pytest.fixture
def fake_render(monkeypatch):
    runs = []

    def run(k, spec):
        runs.append(spec)
        if spec.get("fail"):
            with video._lock:
                video._jobs[k] = {"state": "error", "error": "视频没做出来"}
            return
        Path(spec["out"]).write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 100)
        with video._lock:
            video._jobs.pop(k, None)

    monkeypatch.setattr(video, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video, "_run", run)
    return runs


def _until_ready(client, body):
    for _ in range(100):
        st = client.post("/api/sing/video", json=body).json()
        if st["state"] != "making":
            return st
        time.sleep(0.02)
    raise AssertionError("一直没做好")


def test_api_clip_ready_then_download(client, song, fake_render):
    st = _until_ready(client, {"file": song["fid"], "start": 30, "end": 45.5, "off": 0.5})
    assert st["state"] == "ready" and st["url"] == f"api/sing/video/{song['fid']}_300_455_o5.mp4"
    spec = fake_render[0]
    assert (spec["start"], spec["end"], spec["off"], spec["title"], spec["singer"]) == (30.0, 45.5, 0.5, "小星星", "爸爸")
    r = client.get("/" + st["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    _until_ready(client, {"file": song["fid"], "start": 30, "end": 45.5, "off": 0.5})
    assert len(fake_render) == 1                                   # 同一段只做一次
    whole = _until_ready(client, {"file": song["fid"]})
    assert whole["key"] == song["fid"] and len(fake_render) == 2


def test_api_rejects_unknown_and_non_sing_files(client, song, fake_render):
    assert client.post("/api/sing/video", json={"file": "nope"}).status_code == 404
    fid, p = store.allocate()
    p.write_bytes(b"\xff\xfb" + b"\0" * 100)
    store.commit(fid, "别的.mp3", "audio/mpeg", 102, owner="xiaoxin")
    assert client.post("/api/sing/video", json={"file": fid}).status_code == 404
    assert client.post("/api/sing/video", json={"file": song["fid"], "start": 10, "end": 11}).status_code == 400
    assert client.get(f"/api/sing/video/{song['fid']}.mp4").status_code == 404   # 还没做


def test_api_error_then_retry_renders_again(client, song, fake_render, monkeypatch):
    real = video._run

    def first_fails(k, spec):
        real(k, {**spec, "fail": not fake_render})
    monkeypatch.setattr(video, "_run", first_fails)
    st = _until_ready(client, {"file": song["fid"]})
    assert st["state"] == "error"
    assert _until_ready(client, {"file": song["fid"]})["state"] == "ready"
    assert len(fake_render) == 2
