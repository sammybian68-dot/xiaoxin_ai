"""录唱歌声音时跟着唱的歌：歌单，每首截一段（30 秒出头）带时间的歌词，还能先听这段原唱找调。

小朋友清唱想不起词、起不了调，所以给一首会唱的歌、跟着歌词走。歌词和原唱都现从 QQ 音乐拿（歌词接口不用登录，
原唱要登录），这里只登记歌名、歌手、从哪儿唱起，查到的存在 DATA/sing/practice/<id>.json。
挑歌的讲究：旋律连着唱、有长音、高低都有一点——录到的才像本人平时唱歌（翻唱靠这段学音色）；说唱、喊麦、念白多的不要。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path

from server import store
from server.sing import job, qq

log = logging.getLogger("xiaoxin.sing")

GROUPS = [("kids", "儿歌"), ("pop", "流行"), ("classic", "爸爸妈妈那时候")]
# start：top 从第一句唱；chorus 从副歌（头一段前后两句都重复出现的）；别的字符串 = 从含这几个字的那句唱
SONGS = [
    {"id": "shuyazi", "title": "数鸭子", "artist": "", "group": "kids", "start": "top"},
    {"id": "dashou", "title": "大手牵小手", "artist": "", "group": "kids", "start": "top"},
    {"id": "chongerfei", "title": "虫儿飞", "artist": "", "group": "kids", "start": "top"},
    {"id": "shuangjiang", "title": "让我们荡起双桨", "artist": "", "group": "kids", "start": "top"},
    {"id": "xiaoxingxing", "title": "小星星", "artist": "", "group": "kids", "start": "top"},
    {"id": "xiaoyanzi", "title": "小燕子", "artist": "", "group": "kids", "start": "top"},
    {"id": "mamahao", "title": "世上只有妈妈好", "artist": "", "group": "kids", "start": "top"},
    {"id": "zhongtaiyang", "title": "种太阳", "artist": "", "group": "kids", "start": "top"},
    {"id": "haohaizi", "title": "我们都是好孩子", "artist": "王筝", "group": "kids", "start": "chorus"},
    {"id": "liangzhilaohu", "title": "两只老虎", "artist": "", "group": "kids", "start": "top"},
    {"id": "nuannuan", "title": "暖暖", "artist": "梁静茹", "group": "pop", "start": "细腻的喜欢"},
    {"id": "tingmama", "title": "听妈妈的话", "artist": "周杰伦", "group": "pop", "start": "听妈妈的话 别让她"},
    {"id": "daoxiang", "title": "稻香", "artist": "周杰伦", "group": "pop", "start": "还记得你说家"},
    {"id": "qingtian", "title": "晴天", "artist": "周杰伦", "group": "pop", "start": "刮风这天"},
    {"id": "qilixiang", "title": "七里香", "artist": "周杰伦", "group": "pop", "start": "chorus"},
    {"id": "woniu", "title": "蜗牛", "artist": "周杰伦", "group": "pop", "start": "chorus"},
    {"id": "guyongzhe", "title": "孤勇者", "artist": "陈奕迅", "group": "pop", "start": "chorus"},
    {"id": "chibang", "title": "隐形的翅膀", "artist": "张韶涵", "group": "pop", "start": "chorus"},
    {"id": "xiaoxingyun", "title": "小幸运", "artist": "田馥甄", "group": "pop", "start": "chorus"},
    {"id": "qifengle", "title": "起风了", "artist": "买辣椒也用券", "group": "pop", "start": "chorus"},
    {"id": "xuancaohua", "title": "萱草花", "artist": "张小斐", "group": "pop", "start": "chorus"},
    {"id": "yueliang", "title": "月亮代表我的心", "artist": "邓丽君", "group": "classic", "start": "top"},
    {"id": "tianmimi", "title": "甜蜜蜜", "artist": "邓丽君", "group": "classic", "start": "top"},
    {"id": "tongnian", "title": "童年", "artist": "罗大佑", "group": "classic", "start": "top"},
    {"id": "pengyou", "title": "朋友", "artist": "周华健", "group": "classic", "start": "chorus"},
    {"id": "houlai", "title": "后来", "artist": "刘若英", "group": "classic", "start": "chorus"},
    {"id": "pingfan", "title": "平凡之路", "artist": "朴树", "group": "classic", "start": "chorus"},
]
BY_ID = {s["id"]: s for s in SONGS}
SEG_S = 34          # 录 30 秒，词多给几秒
MAX_GAP_S = 12      # 中间隔这么久没词（间奏）就截在这儿
MAX_TRIES = 6       # 最多试搜出来的前几个版本
MIN_LINES = 4
_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_CREDIT = re.compile(r"^[^：:]{0,14}[：:]|^.{0,40} - .{0,40}$")   # 「词：某某」「稻香 - 周杰伦」
_lock = threading.Lock()


class PracticeError(RuntimeError):
    pass


def _dir() -> Path:
    return store.DATA / "sing" / "practice"


def songs() -> list[dict]:
    return [{k: s[k] for k in ("id", "title", "artist", "group")} for s in SONGS]


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """LRC → [(秒, 这句)]，按时间排；作词作曲、「歌名 - 歌手」这种不是歌词的行去掉。"""
    shift = 0.0
    out = []
    for raw in (text or "").splitlines():
        if m := re.match(r"^\[offset:\s*(-?\d+)\s*\]", raw, re.I):
            shift = int(m.group(1)) / 1000
            continue
        tags = _TAG.findall(raw)
        words = re.sub(r"\[[^\]]*\]", "", raw).replace("&apos;", "'").replace("&quot;", '"').replace("&amp;", "&").strip()
        if not tags or not words or _CREDIT.search(words):
            continue
        for mm, ss in tags:
            out.append((int(mm) * 60 + float(ss.replace(":", ".")) - shift, words))
    return sorted(out)


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", s).lower()


def start_index(lines: list[tuple[float, str]], start: str) -> int:
    if not lines or start == "top":
        return 0
    if start != "chorus":
        want = _norm(start)
        return next((i for i, (_, w) in enumerate(lines) if want in _norm(w)), 0)
    n = Counter(_norm(w) for _, w in lines)
    for i in range(len(lines) - 1):
        if n[_norm(lines[i][1])] >= 2 and n[_norm(lines[i + 1][1])] >= 2:
            return i
    return 0


def segment(lines: list[tuple[float, str]], start: str) -> dict:
    """从 start 那句起截 SEG_S 秒：{start（这句在原唱里的秒）, end, lines: [{t（从 start 算）, text}]}。"""
    if not lines:
        return {"start": 0.0, "end": 0.0, "lines": []}
    i = start_index(lines, start)
    t0 = lines[i][0]
    seg = [lines[i]]
    for t, w in lines[i + 1:]:
        if t - t0 >= SEG_S or t - seg[-1][0] > MAX_GAP_S:
            break
        seg.append((t, w))
    nxt = next((t for t, _ in lines if t > seg[-1][0]), None)
    end = min(nxt if nxt is not None and nxt - seg[-1][0] <= MAX_GAP_S else seg[-1][0] + 5, t0 + SEG_S + 6)
    return {"start": round(t0, 2), "end": round(end, 2),
            "lines": [{"t": round(t - t0, 2), "text": w} for t, w in seg]}


def get(song_id: str) -> dict:
    """这首的那一段：{id, title, artist, mid, info, start, end, lines}。查过的直接读缓存。"""
    s = BY_ID.get(song_id)
    if s is None:
        raise PracticeError("歌单里没有这首")
    p = _dir() / f"{song_id}.json"
    with _lock:
        if p.exists():
            try:
                cached = json.loads(p.read_text(encoding="utf-8"))
                if cached.get("start_rule") == s["start"] and cached.get("lines"):
                    return cached
            except (OSError, json.JSONDecodeError):
                pass
    try:
        found = job.candidates(s["title"], s["artist"])
    except qq.QQError as e:
        raise PracticeError(f"《{s['title']}》没搜到：{e}") from e
    info, seg = None, {"lines": []}
    for c in found[:MAX_TRIES]:   # 儿歌版本多，排第一的常常没有带时间的歌词，往下找
        try:
            seg = segment(parse_lrc(qq.lyric(c["mid"])), s["start"])
        except qq.QQError as e:
            log.warning("practice lyric %s %s: %s", song_id, c["mid"], e)
            continue
        if len(seg["lines"]) >= MIN_LINES:
            info = c
            break
    if info is None:
        raise PracticeError(f"《{s['title']}》QQ 音乐上没有带时间的歌词")
    out = {"id": song_id, "title": s["title"], "artist": info["artist"] or s["artist"], "mid": info["mid"], "info": info,
           "start_rule": s["start"], "at": time.time(), **seg}
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def original(song_id: str) -> Path:
    """原唱整首（翻唱那边的缓存，没有就下）；前端自己从 start 放到 end。要 QQ 音乐登录。"""
    try:
        return job._original(get(song_id)["info"])
    except job.SingError as e:
        raise PracticeError(str(e)) from e
