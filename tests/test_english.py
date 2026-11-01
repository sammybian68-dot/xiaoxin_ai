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
