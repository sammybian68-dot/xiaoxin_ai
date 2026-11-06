"""跟着伴奏唱：找录音里外放伴奏的延迟（外放找得准、戴耳机退回默认）、混音放对位置、真合成一次（有 ffmpeg 才跑）、
接口（伴奏做好给地址、真人版挂到卡片上、再唱换掉旧的、孩子能听、真人版也能发视频、伴奏没好不收）。SoulX 是假的。"""

from __future__ import annotations

import io
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from server import store
from server.family import members
from server.sing import karaoke, video

R = 24000


def _music(secs: float, seed: int = 1) -> np.ndarray:
    """像伴奏的宽带信号：噪声 × 每 0.5 秒一拍的包络。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(secs * R)) / R
    beat = np.exp(-(t % 0.5) / 0.08)
    return (0.3 * rng.standard_normal(len(t)) * (0.3 + beat)).astype(np.float32)


def _recording(acc: np.ndarray, s0: float, lat: float, secs: float, acc_gain: float, seed: int = 2) -> np.ndarray:
    """录音第 i 个点 = 歌里 s0 + i/R - lat 秒的伴奏（外放进麦克风）+ 不相干的「人声」。"""
    i0 = int(round((s0 - lat) * R))
    n = int(secs * R)
    voice = 0.3 * np.random.default_rng(seed).standard_normal(n)
    return (acc_gain * acc[i0: i0 + n] + voice).astype(np.float32)


def _wav(x: np.ndarray, rate: int = R) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_latency_found_from_speaker_bleed():
    acc = _music(120)
    for lat, s0 in ((0.21, 30.0), (0.05, 2.0), (-0.08, 60.0)):
        got, agree = karaoke.latency(_recording(acc, s0, lat, 40, 0.3), acc, R, s0)
        assert abs(got - lat) < 0.003 and agree >= 2, (lat, got, agree)


def test_latency_falls_back_with_headphones_or_too_short():
    acc = _music(120)
    voice_only = (0.3 * np.random.default_rng(5).standard_normal(40 * R)).astype(np.float32)
    assert karaoke.latency(voice_only, acc, R, 30.0) == (karaoke.DEFAULT_LAT_S, 0)
    assert karaoke.latency(_recording(acc, 30.0, 0.2, 8, 0.3), acc, R, 30.0) == (karaoke.DEFAULT_LAT_S, 0)


def test_mix_puts_voice_on_the_song_timeline():
    acc = np.zeros(60 * R, dtype=np.float32)
    rec = np.zeros(10 * R, dtype=np.float32)
    rec[R // 2] = 1.0                                   # 录音第 0.5 秒拍一下
    out = karaoke.mix(rec, R, acc, s0=20.0, lat=0.2)
    assert abs(int(np.argmax(np.abs(out))) / R - 20.3) < 0.001   # 歌里 20 + 0.5 - 0.2
    assert len(out) == int(19.8 * R) + 10 * R + int(karaoke.TAIL_S * R)


@pytest.mark.skipif(not video.ffmpeg_exe(), reason="没有 ffmpeg")
def test_make_real_end_to_end(tmp_path):
    acc = _music(90)
    accf = tmp_path / "acc.mp3"
    accf.write_bytes(_wav(acc))                        # ffmpeg 看内容不看后缀
    mp3, info = karaoke.make_real(_wav(_recording(acc, 12.0, 0.18, 30, 0.4)), accf, 12.0)
    assert mp3[:3] == b"ID3" or mp3[:2] == b"\xff\xfb"
    assert abs(info["lat"] - 0.18) < 0.005 and info["agree"] >= 2
    assert info["start"] == pytest.approx(11.82, abs=0.01) and info["end"] == pytest.approx(41.82, abs=0.01)
    assert info["seconds"] == pytest.approx(41.82 + karaoke.TAIL_S, abs=0.1)
    with pytest.raises(karaoke.KaraokeError, match="太短"):
        karaoke.make_real(_wav(np.zeros(2 * R, dtype=np.float32)), accf, 12.0)


# ---------------------------------------------------------------- 接口

@pytest.fixture
def song(client):
    thread = members.get(client.get("/api/thread").json()["kid"])["thread"]
    fid, p = store.allocate()
    p.write_bytes(b"\xff\xfb" + b"\0" * 2000)
    meta = store.commit(fid, "爸爸唱《小星星》.mp3", "audio/mpeg", p.stat().st_size, owner="xiaoxin")
    sing = {"file": fid, "title": "小星星", "artist": "儿歌", "cover": "", "singer": "爸爸", "lrc": "", "seconds": 90.0}
    m = store.append(thread, {"role": "assistant", "model": "", "via": "sing", "files": [meta], "sing": sing, "text": "做好了"})
    return {"fid": fid, "thread": thread, "mid": m["id"], "acc": _music(90)}


@pytest.fixture
def fake_sep(monkeypatch, song):
    calls = []

    def separate(fid, src):
        calls.append(fid)
        d = karaoke.acc_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{fid}.mp3").write_bytes(_wav(song["acc"]))
        with karaoke._lock:
            karaoke._jobs.pop(fid, None)

    monkeypatch.setattr(karaoke, "_separate", separate)
    return calls


def _acc_ready(client, fid):
    for _ in range(100):
        st = client.post("/api/sing/karaoke", json={"file": fid}).json()
        if st["state"] != "making":
            return st
        time.sleep(0.02)
    raise AssertionError("伴奏一直没好")


def test_api_accompaniment(client, song, fake_sep):
    assert client.post("/api/sing/real", params={"file": song["fid"], "s0": 0}, content=_wav(song["acc"][: R * 10])).status_code == 409
    st = _acc_ready(client, song["fid"])
    assert st == {"state": "ready", "url": f"api/sing/karaoke/{song['fid']}.mp3"}
    assert client.get("/" + st["url"]).status_code == 200
    _acc_ready(client, song["fid"])
    assert fake_sep == [song["fid"]]                                 # 只分离一次
    assert client.post("/api/sing/karaoke", json={"file": "nope"}).status_code == 404


@pytest.mark.skipif(not video.ffmpeg_exe(), reason="没有 ffmpeg")
def test_api_real_version_on_the_card(client, song, fake_sep, monkeypatch):
    _acc_ready(client, song["fid"])
    rec = _wav(_recording(song["acc"], 20.0, 0.2, 25, 0.4))
    r = client.post("/api/sing/real", params={"file": song["fid"], "s0": 20.0, "singer": "爸爸"}, content=rec)
    assert r.status_code == 200, r.text
    real = r.json()["sing"]["real"]
    assert real["singer"] == "爸爸" and abs(real["lat"] - 0.2) < 0.005 and real["start"] == pytest.approx(19.8, abs=0.01)
    m = next(x for x in store.get(song["thread"])["messages"] if x["id"] == song["mid"])
    assert m["sing"]["real"]["file"] == real["file"] and {f["id"] for f in m["files"]} == {song["fid"], real["file"]}
    assert client.get(f"/api/files/{real['file']}").status_code == 200
    assert client.post("/api/sing/karaoke", json={"file": real["file"]}).status_code == 404   # 真人版不再拿去分离

    again = client.post("/api/sing/real", params={"file": song["fid"], "s0": 30.0, "singer": "卞依一"}, content=rec).json()
    real = again["sing"]["real"]
    m = next(x for x in store.get(song["thread"])["messages"] if x["id"] == song["mid"])
    assert real["singer"] == "卞依一"
    assert {f["id"] for f in m["files"]} == {song["fid"], real["file"]}   # 旧的真人版换掉了

    specs = []
    monkeypatch.setattr(video, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(video, "_run", lambda k, spec: specs.append(spec))
    client.post("/api/sing/video", json={"file": real["file"]})
    assert specs[0]["person"] is True and specs[0]["singer"] == "卞依一" and specs[0]["seconds"] == real["seconds"]


def test_api_real_rejects_bad_recordings(client, song, fake_sep):
    _acc_ready(client, song["fid"])
    assert client.post("/api/sing/real", params={"file": song["fid"], "s0": 0}, content=b"not a wav").status_code == 400
    big = b"\0" * (karaoke.MAX_REC_BYTES + 1)
    assert client.post("/api/sing/real", params={"file": song["fid"], "s0": 0}, content=big).status_code == 413
