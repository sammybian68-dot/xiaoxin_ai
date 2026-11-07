"""讯飞语音评测（流式版）：拿一句英文参考文本 + 孩子的录音，回整句分和每个词的分（英语口语场景对话用）。

接口：wss://ise-api.xfyun.cn/v2/open-ise，HMAC-SHA256 签名鉴权（APIKey / APISecret），APPID 放请求体。
音频要求 16 kHz、16 bit、单声道 PCM（raw），一次会话不超过 5 分钟。英文句子题型 category=read_sentence，ent=en_vip。
文档：https://www.xfyun.cn/doc/Ise/IseAPI.html 。key 在仓库根 .env：XFYUN_APPID / XFYUN_API_KEY / XFYUN_API_SECRET。

    python -m src.voice.xfyun_ise "Hello, Mr. Bear." 录音.wav
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

HOST, PATH = "ise-api.xfyun.cn", "/v2/open-ise"
ROOT = Path(__file__).resolve().parents[2]
FRAME = 1280          # 每帧 40 ms（16 kHz × 2 字节 × 0.04 s）
# 讯飞英文每个词的漏读 / 多读 / 回读 / 替换（dp_message）
DP = {"0": "", "16": "漏读", "32": "多读", "64": "回读", "128": "读错"}


class IseError(RuntimeError):
    pass


def _env(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v
    f = ROOT / ".env"
    if f.is_file():
        for line in f.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    raise IseError(f"缺少 {name}（.env）")


def _url() -> str:
    """鉴权 URL：用 APISecret 对 host / date / request-line 签名（讯飞 WebAPI 通用做法）。"""
    date = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    origin = f"host: {HOST}\ndate: {date}\nGET {PATH} HTTP/1.1"
    sig = base64.b64encode(hmac.new(_env("XFYUN_API_SECRET").encode(), origin.encode(), hashlib.sha256).digest()).decode()
    auth = (f'api_key="{_env("XFYUN_API_KEY")}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{sig}"')
    q = urlencode({"authorization": base64.b64encode(auth.encode()).decode(), "date": date, "host": HOST})
    return f"wss://{HOST}{PATH}?{q}"


def to_pcm16k(audio: bytes) -> bytes:
    """录音转 16 kHz 单声道 s16le。界面的 Recorder 本来就录成 16 kHz 单声道 wav：直接去掉文件头，不用 ffmpeg
    （gpu2 没装）；别的格式（webm / mp3）才走 ffmpeg。"""
    import io
    import subprocess
    import wave

    if audio[:4] == b"RIFF":
        with wave.open(io.BytesIO(audio)) as w:
            if w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2:
                return w.readframes(w.getnframes())

    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"],
                       input=audio, capture_output=True, timeout=60)
    if p.returncode or not p.stdout:
        raise IseError("录音转不了：" + p.stderr.decode(errors="ignore")[-200:])
    return p.stdout


def assess(text: str, pcm16k: bytes, *, timeout: float = 30, category: str = "read_sentence") -> dict:
    """评一句英文（category=read_word 评单个词）。返回 {"total", "accuracy", "fluency", "integrity", "words": [...], "raw"}。"""
    from websockets.sync.client import connect

    text = re.sub(r"\s+", " ", text).strip()
    business = {"category": "read_sentence", "sub": "ise", "ent": "en_vip", "aue": "raw", "auf": "audio/L16;rate=16000",
                "text": "﻿[content]\n" + text, "tte": "utf-8", "rstcd": "utf8", "cmd": "ssb",
                "ise_unite": "1", "rst": "entirety", "extra_ability": "multi_dimension"}
    t0 = time.monotonic()
    with connect(_url(), open_timeout=10, close_timeout=3) as ws:
        ws.send(json.dumps({"common": {"app_id": _env("XFYUN_APPID")}, "business": business, "data": {"status": 0}}))
        chunks = [pcm16k[i:i + FRAME] for i in range(0, len(pcm16k), FRAME)] or [b""]
        for k, ch in enumerate(chunks):
            aus = 1 if k == 0 else 4 if k == len(chunks) - 1 else 2
            ws.send(json.dumps({"business": {"cmd": "auw", "aus": aus},
                                "data": {"status": 2 if aus == 4 else 1, "data": base64.b64encode(ch).decode()}}))
        xml = ""
        while time.monotonic() - t0 < timeout:
            r = json.loads(ws.recv(timeout=timeout))
            if r.get("code"):
                raise IseError(f"讯飞评测报错 {r.get('code')}：{r.get('message')}")
            d = r.get("data") or {}
            if d.get("data"):
                xml += base64.b64decode(d["data"]).decode("utf-8", "ignore")
            if d.get("status") == 2:
                break
    return parse(xml)


def parse(xml: str) -> dict:
    """讯飞英文句子题的 XML → 分数（0–100，讯飞英文原始分是 0–5 或 0–100，这里统一成 0–100）。"""
    root = ET.fromstring(xml.lstrip("﻿"))
    node = (root.find(".//read_sentence/rec_paper/read_chapter") or root.find(".//read_chapter") or root.find(".//read_sentence")
            or root.find(".//read_word/rec_paper/read_word") or root.find(".//read_word"))
    if node is None:
        raise IseError("讯飞返回里没有评测结果")
    def sc(n, k):
        v = n.get(k)
        return round(float(v), 1) if v not in (None, "") else None
    total = sc(node, "total_score")
    scale = 20 if total is not None and total <= 5 else 1   # 英文句子题有时给 0–5 分
    f = lambda v: None if v is None else round(min(100.0, v * scale), 1)
    words = []
    for w in node.iter("word"):
        content = w.get("content", "")
        if not content or content in ("sil", "fil") or w.get("dp_message") == "32":   # 静音、填充词、多读的不算
            continue
        words.append({"word": content, "score": f(sc(w, "total_score")), "err": DP.get(w.get("dp_message") or "0", ""),
                      "beg": int(w.get("beg_pos") or 0), "end": int(w.get("end_pos") or 0)})   # 帧（10 ms）
    return {"total": f(total), "accuracy": f(sc(node, "accuracy_score")), "fluency": f(sc(node, "fluency_score")),
            "integrity": f(sc(node, "integrity_score")), "words": words, "raw": xml}


if __name__ == "__main__":
    text, path = sys.argv[1], sys.argv[2]
    r = assess(text, to_pcm16k(Path(path).read_bytes()))
    r.pop("raw")
    print(json.dumps(r, ensure_ascii=False, indent=1))
