"""汉字笔顺：笔画轮廓 + 中线（给前端 Hanzi Writer 一笔一划地画）、每笔的名字、拼音、部首、结构。

数据在 XIAOXIN_DATA/hanzi/hanzi.json（不进仓库，31 MB，9565 个字），由下面三个开源包合成：
  hanzi-writer-data 2.0.1   笔画轮廓和中线（Make Me a Hanzi，Arphic Public License）
  cnchar-order 3.2.6        每笔的名字（点、横折钩…），MIT；6807 个字有，笔画数和轮廓对不上的不用
  cnchar / cnchar-radical   拼音（多音字列全）、部首、结构，MIT
每个字：{s: 轮廓 path, m: 中线, r: 部首是第几笔, n: 笔画名, p: 拼音, b: 部首, t: 结构}
"""

from __future__ import annotations

import json
import threading

from server import store

_lock = threading.Lock()
_data: dict | None = None


def _load() -> dict:
    global _data
    with _lock:
        if _data is None:
            try:
                _data = json.loads((store.DATA / "hanzi" / "hanzi.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _data = {}
        return _data


def lookup(ch: str) -> dict | None:
    """一个字的全部信息（前端画图用）；没有返回 None。"""
    e = _load().get(ch)
    if e is None:
        return None
    return {"char": ch, "strokes": e["s"], "medians": e["m"], "radStrokes": e.get("r") or [],
            "names": e.get("n") or [], "pinyin": e.get("p") or [], "radical": e.get("b") or "",
            "structure": e.get("t") or "", "count": len(e["s"])}


def chars_of(text: str) -> list[str]:
    return [c for c in str(text or "") if "㐀" <= c <= "鿿" or "豈" <= c <= "﫿"]


def summary(ch: str) -> str:
    """给模型读的一行：字、拼音、部首、结构、笔画数、每一笔。"""
    e = lookup(ch)
    if e is None:
        return f"「{ch}」没有笔顺数据"
    names = e["names"]
    order = "、".join(f"{i + 1}{n}" for i, n in enumerate(names)) if names else "（笔画名缺）"
    py = "/".join(e["pinyin"]) or "?"
    return (f"「{ch}」{py}，部首{e['radical'] or '?'}，{e['structure'] or '?'}结构，共{e['count']}画：{order}")
