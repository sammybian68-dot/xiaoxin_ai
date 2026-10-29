"""对话、附件、参考音色落盘。都放在 XIAOXIN_DATA 下，不进仓库。

conversations/<id>.json   一段对话一个文件
files/<id>/blob + meta.json
voice/<id>.wav + <id>.json  录给声音复刻用的参考音频
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
import uuid
import wave
from pathlib import Path

DATA = Path(os.environ.get("XIAOXIN_DATA") or "/Data/cmbian/private_data/xiaoxin")
CONV, FILES, VOICE = DATA / "conversations", DATA / "files", DATA / "voice"
MAX_FILE = 50 * 1024 * 1024
MAX_TEXT_FILE = 200 * 1024
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".js", ".java", ".kt", ".c", ".h", ".cpp", ".go", ".rs",
            ".ts", ".html", ".css", ".xml", ".yaml", ".yml", ".log", ".sh", ".sql"}

_lock = threading.Lock()
_ID = re.compile(r"^[0-9a-f]{16}$")


class StoreError(ValueError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _check(i: str) -> str:
    if not _ID.match(i or ""):
        raise KeyError(i)
    return i


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 对话

def list_conversations() -> list[dict]:
    out = []
    for p in CONV.glob("*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": c["id"], "title": c.get("title") or "新对话", "updated": c.get("updated", 0)})
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def get(cid: str) -> dict:
    path = CONV / f"{_check(cid)}.json"
    if not path.is_file():
        raise KeyError(cid)
    return json.loads(path.read_text(encoding="utf-8"))


def create() -> dict:
    now = time.time()
    conv = {"id": _new_id(), "title": "", "created": now, "updated": now, "messages": []}
    with _lock:
        _write_json(CONV / f"{conv['id']}.json", conv)
    return conv


def delete(cid: str) -> None:
    with _lock:
        (CONV / f"{_check(cid)}.json").unlink(missing_ok=True)


def append(cid: str, msg: dict) -> dict:
    """追加一条消息，返回带 id 和时间的那条。第一条用户消息顺带当标题。"""
    with _lock:
        conv = get(cid)
        msg = {"id": _new_id(), "created": time.time(), **msg}
        conv["messages"].append(msg)
        if not conv.get("title") and msg["role"] == "user":
            title = (msg.get("text") or "").strip().splitlines()[0][:24] if (msg.get("text") or "").strip() else ""
            conv["title"] = title or "图片 / 文件"
        conv["updated"] = msg["created"]
        _write_json(CONV / f"{cid}.json", conv)
    return msg


def update_message(cid: str, mid: str, **fields) -> dict | None:
    """给已存的一条消息补字段（比如念完才知道的首响时间）。找不到返回 None。"""
    with _lock:
        conv = get(cid)
        for m in conv["messages"]:
            if m["id"] == mid:
                m.update(fields)
                _write_json(CONV / f"{cid}.json", conv)
                return m
    return None


# ---------------------------------------------------------------- 附件

def _kind(name: str, mime: str, size: int) -> str:
    if mime.startswith("image/"):
        return "image"
    if size <= MAX_TEXT_FILE and (mime.startswith("text/") or Path(name).suffix.lower() in TEXT_EXT):
        return "text"
    return "file"


def allocate() -> tuple[str, Path]:
    fid = _new_id()
    d = FILES / fid
    d.mkdir(parents=True, exist_ok=True)
    return fid, d / "blob"


def commit(fid: str, name: str, mime: str, size: int) -> dict:
    name = Path(name or "file").name[:120] or "file"
    meta = {"id": fid, "name": name, "mime": mime or "application/octet-stream", "size": size,
            "kind": _kind(name, mime or "", size), "created": time.time()}
    _write_json(FILES / fid / "meta.json", meta)
    return meta


def abort(fid: str) -> None:
    d = FILES / _check(fid)
    for p in d.glob("*"):
        p.unlink(missing_ok=True)
    d.rmdir()


def file_info(fid: str) -> tuple[Path, dict] | None:
    try:
        d = FILES / _check(fid)
    except KeyError:
        return None
    if not (d / "meta.json").is_file():
        return None
    return d / "blob", json.loads((d / "meta.json").read_text(encoding="utf-8"))


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def read_text(path: Path, limit: int = 20000) -> str:
    text = path.read_bytes()[:MAX_TEXT_FILE].decode("utf-8", "replace")
    return text if len(text) <= limit else text[:limit] + "\n……（后面截掉了）"


# ---------------------------------------------------------------- 参考音色

def wav_info(data: bytes) -> tuple[int, int, float]:
    """(采样率, 声道, 秒)。不是合法 PCM16 wav 就抛 StoreError。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2:
                raise StoreError("要 16 bit PCM wav")
            return w.getframerate(), w.getnchannels(), w.getnframes() / w.getframerate()
    except (wave.Error, EOFError) as e:
        raise StoreError(f"不是 wav：{e}") from e


def wav_pcm16k(data: bytes) -> bytes:
    rate, channels, _ = wav_info(data)
    if (rate, channels) != (16000, 1):
        raise StoreError(f"ASR 要 16 kHz 单声道，收到 {rate} Hz × {channels}")
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.readframes(w.getnframes())


def save_sample(data: bytes, text: str, name: str = "") -> dict:
    rate, channels, seconds = wav_info(data)
    if not 3 <= seconds <= 90:
        raise StoreError(f"录了 {seconds:.1f} 秒，要 3–90 秒")
    sid = _new_id()
    VOICE.mkdir(parents=True, exist_ok=True)
    (VOICE / f"{sid}.wav").write_bytes(data)
    meta = {"id": sid, "name": name.strip()[:20] or f"我的声音 {len(list_samples()) + 1}", "text": text.strip(),
            "seconds": round(seconds, 2), "rate": rate, "channels": channels, "created": time.time()}
    _write_json(VOICE / f"{sid}.json", meta)
    return meta


def sample_meta(sid: str) -> dict:
    path = VOICE / f"{_check(sid)}.json"
    if not path.is_file():
        raise KeyError(sid)
    return json.loads(path.read_text(encoding="utf-8"))


def update_sample(sid: str, **fields) -> dict:
    with _lock:
        meta = sample_meta(sid)
        meta.update(fields)
        _write_json(VOICE / f"{sid}.json", meta)
    return meta


def pick_reference(pcm: bytes, rate: int, lo: float = 6.0, hi: float = 10.0) -> tuple[int, int]:
    """从整段录音里挑一段 lo–hi 秒、按停顿切齐的参考，返回 (起, 止) 采样点。

    按 50 ms 一帧算能量，低于最响段 6% 的帧算停顿，连续停顿 ≥ 250 ms 就是句间。
    在句子边界上找总长落在 lo–hi 秒的连续几句，选离 8 秒最近、削顶最少的。都不合适就取开头 hi 秒。
    """
    import array
    import math

    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    hop = rate // 20
    rms = []
    for i in range(0, len(samples) - hop + 1, hop):
        frame = samples[i:i + hop]
        rms.append(math.sqrt(sum(s * s for s in frame) / hop))
    if not rms:
        return 0, len(samples)
    loud = sorted(rms)[int(len(rms) * 0.95)] or 1
    voiced = [r > loud * 0.06 for r in rms]
    segs, start, quiet = [], None, 0
    for i, v in enumerate(voiced + [False] * 6):
        if v:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= 5:
                segs.append((start, i - quiet + 1))
                start, quiet = None, 0
    best, best_score = None, None
    for a in range(len(segs)):
        for b in range(a, len(segs)):
            secs = (segs[b][1] - segs[a][0]) / 20
            if secs > hi:
                break
            if secs < lo:
                continue
            s0, s1 = segs[a][0] * hop, segs[b][1] * hop
            clipped = sum(1 for s in samples[s0:s1] if abs(s) >= 32000)
            score = abs(secs - 8.0) + clipped / 50
            if best_score is None or score < best_score:
                best, best_score = (s0, s1), score
    if best is None:
        first = segs[0][0] * hop if segs else 0
        best = (first, min(len(samples), first + int(hi * rate)))
    pad = rate // 10  # 前后各留 100 ms，免得切掉字头字尾
    return max(0, best[0] - pad), min(len(samples), best[1] + pad)


def write_reference(sid: str, pcm: bytes, rate: int) -> Path:
    path = VOICE / f"{_check(sid)}.ref.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


# ---------------------------------------------------------------- 全家共用的设置（当前音色、自己加的豆包音色）

SETTINGS = DATA / "settings.json"


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def update_settings(**fields) -> dict:
    with _lock:
        s = settings()
        s.update(fields)
        _write_json(SETTINGS, s)
    return s


def list_samples() -> list[dict]:
    out = []
    for p in VOICE.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda s: s["created"], reverse=True)


def sample_path(sid: str) -> Path:
    path = VOICE / f"{_check(sid)}.wav"
    if not path.is_file():
        raise KeyError(sid)
    return path


def delete_sample(sid: str) -> None:
    for suffix in (".wav", ".ref.wav", ".json"):
        (VOICE / f"{_check(sid)}{suffix}").unlink(missing_ok=True)
