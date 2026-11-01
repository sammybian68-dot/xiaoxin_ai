"""英文单词的音标和英音 / 美音读音：从有道词典取（gpu2 能连，pad 不直连外网），取过的存在 XIAOXIN_DATA/dict 下，
同一个词以后不再联网。书写卡上显示「英 /ˈæp(ə)l/ 🔊  美 /ˈæp(ə)l/ 🔊」。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse

import requests

from server import store

log = logging.getLogger(__name__)
API = "https://dict.youdao.com/jsonapi"
VOICE = "https://dict.youdao.com/dictvoice"
ACCENTS = {"uk": 1, "us": 2}  # 有道 dictvoice 的 type：1 英音，2 美音
WORD = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,39}$")


class DictError(Exception):
    pass


def _dir():
    return store.DATA / "dict"


def _key(word: str) -> str:
    return hashlib.sha1(word.lower().encode()).hexdigest()[:16]


def norm(word: str) -> str:
    word = re.sub(r"\s+", " ", str(word or "")).strip()
    if not WORD.match(word):
        raise DictError("只查英文单词或短语")
    return word


def phonetic(word: str) -> dict:
    """{"word", "uk", "us"}：音标（不带斜杠），查不到是空字符串。"""
    word = norm(word)
    p = _dir() / f"{_key(word)}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        r = requests.get(API, params={"q": word}, timeout=8)
        r.raise_for_status()
        w = ((r.json().get("ec") or {}).get("word") or [{}])
        w = w[0] if isinstance(w, list) and w else (w if isinstance(w, dict) else {})
    except (requests.RequestException, ValueError) as e:
        log.warning("dict %s: %s", word, e)
        raise DictError("词典连不上，等会儿再试") from e
    out = {"word": word, "uk": str(w.get("ukphone") or w.get("phone") or ""),
           "us": str(w.get("usphone") or w.get("phone") or "")}
    if out["uk"] or out["us"]:  # 查不到的不存，下次再试
        _dir().mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def audio(word: str, accent: str) -> bytes:
    """这个词的英音 / 美音 mp3。"""
    word = norm(word)
    if accent not in ACCENTS:
        raise DictError("accent 只能是 uk / us")
    p = _dir() / f"{_key(word)}.{accent}.mp3"
    try:
        return p.read_bytes()
    except OSError:
        pass
    try:
        r = requests.get(f"{VOICE}?audio={urllib.parse.quote(word)}&type={ACCENTS[accent]}", timeout=8)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("dict voice %s %s: %s", word, accent, e)
        raise DictError("词典连不上，等会儿再试") from e
    if not r.content.startswith((b"ID3", b"\xff\xf3", b"\xff\xfb", b"\xff\xf2")) or len(r.content) < 500:
        raise DictError("词典没有这个词的读音")
    _dir().mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    return r.content
