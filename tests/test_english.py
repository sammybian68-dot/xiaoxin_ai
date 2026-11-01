"""英文书写卡：工具检查单词；52 个字母每笔判错（node 跑 tests/js/english_judge.test.js：标准写法判对、反着判错、挪远判错）。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server.tools import english

NODE = shutil.which("node") or "/opt/homebrew/opt/node@22/bin/node"


@pytest.mark.parametrize("word,ok", [("apple", True), ("Good morning", True), ("Bb", True), ("苹果", False),
                                     ("", False), ("a" * 17, False)])
def test_tool_checks_word(word, ok):
    out = english.run("show_word_writing", json.dumps({"word": word, "meaning": "苹果"}))
    assert out.startswith("已在屏幕上演示") == ok
    if ok:
        assert "播报：" in out


def test_tool_strips_other_chars():
    assert "「its」" in english.run("show_word_writing", json.dumps({"word": "it's"}))


@pytest.mark.skipif(not Path(NODE).exists(), reason="没有 node")
def test_every_letter_stroke_judged_right():
    r = subprocess.run([NODE, "tests/js/english_judge.test.js"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 个问题" in r.stdout


# ---------------------------------------------------------------- 音标、英音美音（有道词典，落盘缓存）

class _Resp:
    def __init__(self, data=None, content=b""):
        self._data, self.content = data, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_phonetic_and_audio_cached(client, monkeypatch):
    import requests

    from server import dictionary

    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if url == dictionary.API:
            return _Resp({"ec": {"word": [{"ukphone": "ˈæp(ə)l", "usphone": "ˈæpl"}]}})
        return _Resp(content=b"ID3" + b"\0" * 1000)

    monkeypatch.setattr(requests, "get", fake_get)
    assert client.get("/api/dict/apple").json() == {"word": "apple", "uk": "ˈæp(ə)l", "us": "ˈæpl"}
    r = client.get("/api/dict/apple/us.mp3")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content.startswith(b"ID3")
    client.get("/api/dict/apple")
    client.get("/api/dict/apple/us.mp3")
    assert len(calls) == 2  # 第二次都读的缓存
    assert "type=2" in calls[1]


def test_dict_rejects_bad_input(client):
    assert client.get("/api/dict/苹果").status_code == 400
    assert client.get("/api/dict/apple/fr.mp3").status_code == 400
