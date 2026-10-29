"""落盘：对话、附件、录音样本、wav 校验、挑参考段、设置。数据目录由 conftest 换成临时目录。"""

from __future__ import annotations

import pytest

from server import store
from tests.audio import silence, speech_with_pauses, tone, wav_bytes


# ---------------------------------------------------------------- 对话

def test_conversation_roundtrip_and_title_from_first_user_message():
    c = store.create()
    store.append(c["id"], {"role": "user", "text": "  今天作业是什么\n第二行"})
    store.append(c["id"], {"role": "assistant", "text": "数学第 23 页"})
    store.append(c["id"], {"role": "user", "text": "别的呢"})
    got = store.get(c["id"])
    assert got["title"] == "今天作业是什么"
    assert [m["role"] for m in got["messages"]] == ["user", "assistant", "user"]
    assert store.list_conversations()[0]["id"] == c["id"]


def test_title_for_attachment_only_message():
    c = store.create()
    store.append(c["id"], {"role": "user", "text": "", "files": [{"id": "x"}]})
    assert store.get(c["id"])["title"] == "图片 / 文件"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "ABC", "", "0123456789abcdef0"])
def test_ids_are_validated_against_path_traversal(bad):
    with pytest.raises(KeyError):
        store.get(bad)
    assert store.file_info(bad) is None


def test_list_skips_corrupt_files():
    store.create()
    (store.CONV / "deadbeefdeadbeef.json").write_text("{broken")
    assert len(store.list_conversations()) == 1


# ---------------------------------------------------------------- 附件

@pytest.mark.parametrize("name,mime,size,kind", [
    ("a.png", "image/png", 10, "image"),
    ("a.py", "application/octet-stream", 10, "text"),
    ("a.txt", "text/plain", store.MAX_TEXT_FILE + 1, "file"),
    ("a.bin", "application/octet-stream", 10, "file"),
])
def test_file_kind(name, mime, size, kind):
    fid, path = store.allocate()
    path.write_bytes(b"x")
    assert store.commit(fid, name, mime, size)["kind"] == kind


def test_commit_strips_directories_from_name():
    fid, _ = store.allocate()
    assert store.commit(fid, "../../evil.txt", "text/plain", 1)["name"] == "evil.txt"


def test_abort_removes_upload():
    fid, path = store.allocate()
    path.write_bytes(b"half")
    store.abort(fid)
    assert store.file_info(fid) is None and not path.parent.exists()


def test_read_text_truncates():
    fid, path = store.allocate()
    path.write_text("字" * 100)
    assert store.read_text(path, limit=10).startswith("字" * 10)
    assert "截掉" in store.read_text(path, limit=10)


# ---------------------------------------------------------------- wav

def test_wav_pcm16k_accepts_only_16k_mono_16bit():
    pcm = tone(0.5)
    assert store.wav_pcm16k(wav_bytes(pcm)) == pcm
    with pytest.raises(store.StoreError, match="16 kHz"):
        store.wav_pcm16k(wav_bytes(tone(0.5, rate=24000), rate=24000))
    with pytest.raises(store.StoreError, match="单声道"):
        store.wav_pcm16k(wav_bytes(pcm, channels=2))
    with pytest.raises(store.StoreError, match="16 bit"):
        store.wav_info(wav_bytes(b"\0" * 100, width=1))
    with pytest.raises(store.StoreError, match="不是 wav"):
        store.wav_info(b"not a wav at all")


@pytest.mark.parametrize("secs,ok", [(2.9, False), (3.0, True), (90.0, True), (90.5, False)])
def test_save_sample_length_limits(secs, ok):
    data = wav_bytes(silence(secs))
    if ok:
        meta = store.save_sample(data, " 朗读文字 ", "爸爸")
        assert meta["name"] == "爸爸" and meta["text"] == "朗读文字"
        assert store.sample_path(meta["id"]).read_bytes() == data
    else:
        with pytest.raises(store.StoreError, match="3–90"):
            store.save_sample(data, "")


def test_delete_sample_removes_all_files():
    meta = store.save_sample(wav_bytes(silence(3)), "")
    store.write_reference(meta["id"], silence(1), 16000)
    store.delete_sample(meta["id"])
    assert list(store.VOICE.iterdir()) == []


# ---------------------------------------------------------------- 挑参考段

def test_pick_reference_lands_on_sentence_boundaries_within_6_to_10s():
    # 五句各 2.5 秒，句间 0.5 秒：最接近 8 秒的是连着三句（2.5*3 + 0.5*2 = 8.5 秒）
    pcm = speech_with_pauses([2.5] * 5)
    a, b = store.pick_reference(pcm, 16000)
    secs = (b - a) / 16000
    assert 6.0 <= secs <= 10.5
    # 起止都在停顿里（前后各留了 0.1 秒），不会切在字中间
    for edge in (a, b - 1):
        assert pcm[edge * 2:edge * 2 + 2] == b"\0\0"


def test_pick_reference_falls_back_to_first_10s_when_no_boundary_fits():
    pcm = speech_with_pauses([15.0])  # 一口气 15 秒
    a, b = store.pick_reference(pcm, 16000)
    assert (b - a) / 16000 == pytest.approx(10.2, abs=0.11)


def test_pick_reference_on_empty_audio():
    assert store.pick_reference(b"", 16000) == (0, 0)


# ---------------------------------------------------------------- 设置

def test_settings_merge_and_survive_corruption():
    assert store.settings() == {}
    store.update_settings(voice="doubao:a")
    store.update_settings(asr_engine="paraformer")
    assert store.settings() == {"voice": "doubao:a", "asr_engine": "paraformer"}
    store.SETTINGS.write_text("{oops")
    assert store.settings() == {}
