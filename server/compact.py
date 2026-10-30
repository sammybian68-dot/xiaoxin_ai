"""上下文压缩（compact）：照 carHer2 的「异步压缩 + swap」。

- 计量：每轮主脑请求回来的 usage.prompt_tokens（精确值，不是估），存在对话的 ctx_tokens，左侧栏显示成电量条。
- 触发：一轮答完，ctx_tokens 超过阈值（设置里，默认 80k）→ 后台用主脑（关思考）压缩。期间照常聊，新轮次接在后面。
- 切点：保留最后 KEEP_TURNS（5）个用户轮次原文；切点只落在用户消息开头，不会把工具调用和结果切开。
  切点前的原文 + 上一次的摘要 → 压缩（第一次走 INITIAL 模板，之后走 UPDATE，旧摘要放 <previous-summary>）。
- swap：压好后下一轮开始时一瞬间换上：epoch +1、summary 换成新的、epoch_start 指到切点。之后发给主脑的是
  system prompt + 摘要（第一条 user）+ 切点之后的原文。原文一条不删，界面照样能往上翻。
- 失败（缺标题、超时、报错）：这次不换，状态回到空闲，下一轮还超阈值就再试；用户感觉不到。
- 「立即压缩」按钮：不看阈值，直接跑一次（轮次不够 KEEP_TURNS 时保留能保留的）。

对话 json 里的字段：ctx_tokens、epoch（压缩过几次，默认 0）、summary、epoch_start（这个 epoch 的第一条消息下标）、
compact = {state: idle|running|ready, ...}（ready 时带 pending: {summary, cut}）、compacted_at。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from server import store

log = logging.getLogger("xiaoxin.compact")

KEEP_TURNS = 5
DEFAULT_LIMIT = 80_000
LIMIT_RANGE = (16_000, 60_000)  # 主脑 Qwen3.8-27B 上下文 65536，留出回答和摘要本身的空间
MODEL = "qwen3.8-27b"           # 关思考的那个
HEADINGS = ("## 在聊什么", "## 学习进度", "## 学会的和没学会的", "## 还没完的事", "## 说话要求", "## 名词和细节")
MAX_SUMMARY_CHARS = 4000
TIMEOUT_S = 120
SUMMARY_PREFIX = "前面的对话已经压缩成下面这份摘要（小新自己写的，接着它聊）：\n\n"

_running: set[str] = set()
_lock = threading.Lock()


def limit() -> int:
    v = store.settings().get("ctx_limit")
    return v if isinstance(v, int) and LIMIT_RANGE[0] <= v <= LIMIT_RANGE[1] else min(DEFAULT_LIMIT, LIMIT_RANGE[1])


def set_limit(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError("压缩阈值要整数（token）") from None
    if not LIMIT_RANGE[0] <= n <= LIMIT_RANGE[1]:
        raise ValueError(f"压缩阈值 {LIMIT_RANGE[0] // 1000}k–{LIMIT_RANGE[1] // 1000}k")
    store.update_settings(ctx_limit=n)
    return n


def _sections(name: str) -> str:
    raw = (Path(__file__).parent / "prompts" / "compact.md").read_text(encoding="utf-8")
    m = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", raw, re.S)
    return m.group(1).strip() if m else ""


def prompt_text() -> str:
    return (Path(__file__).parent / "prompts" / "compact.md").read_text(encoding="utf-8")


def cut_index(msgs: list[dict], start: int, keep: int = KEEP_TURNS) -> int | None:
    """[start, cut) 要压缩、[cut, end) 留原文。cut 落在倒数第 keep 个「真的用户消息」上（note 不算）。
    这个 epoch 里用户轮次不到 keep+1 个（压了也省不了多少）返回 None。"""
    users = [i for i in range(start, len(msgs)) if msgs[i].get("role") == "user"]
    if len(users) <= keep:
        return None
    return users[-keep]


def serialize(msgs: list[dict]) -> str:
    """原文转成给压缩模型读的纯文本：谁说的、说了什么；工具调用压成一行结果；[system] 现状只留文字。"""
    lines = []
    for m in msgs:
        role = m.get("role")
        if role == "note":
            lines.append(m.get("text", ""))
        elif role == "user":
            files = "".join(f"[附件 {f.get('name')}]" for f in m.get("files") or [])
            lines.append(f"孩子: {files}{m.get('text', '')}")
        elif role == "assistant":
            for a in m.get("actions") or []:
                lines.append(f"[工具 {a['name']}] {a['result'][:300]}")
            if m.get("text"):
                lines.append(f"小新: {m['text']}")
    return "\n".join(lines)


def build_request(prev_summary: str, msgs: list[dict]) -> list[dict]:
    convo = f"<conversation>\n{serialize(msgs)}\n</conversation>"
    if prev_summary:
        body = f"{convo}\n\n<previous-summary>\n{prev_summary}\n</previous-summary>\n\n{_sections('UPDATE')}"
    else:
        body = f"{convo}\n\n{_sections('INITIAL')}"
    return [{"role": "system", "content": _sections("SYSTEM")}, {"role": "user", "content": body}]


def check(summary: str) -> str:
    """校验：6 个标题都在、不太长。不行抛 ValueError。"""
    summary = (summary or "").strip()
    missing = [h for h in HEADINGS if h not in summary]
    if missing:
        raise ValueError("摘要缺标题：" + "、".join(missing))
    if len(summary) > MAX_SUMMARY_CHARS * 1.5:
        raise ValueError(f"摘要太长：{len(summary)} 字")
    return summary


def _summarize(prev_summary: str, msgs: list[dict]) -> str:
    from src.llm import client

    reply = client.chat_full(MODEL, build_request(prev_summary, msgs), max_tokens=3000, timeout=TIMEOUT_S, retries=1)
    return check(reply.text)


def state(conv: dict) -> dict:
    """给界面的：用了多少、上限、压缩过几次、现在在不在压。"""
    c = conv.get("compact") or {}
    return {"tokens": conv.get("ctx_tokens") or 0, "limit": limit(), "epoch": conv.get("epoch") or 0,
            "state": c.get("state") or "idle", "error": c.get("error") or "", "compacted_at": conv.get("compacted_at")}


def maybe_kick(cid: str, *, force: bool = False) -> bool:
    """一轮答完调（或「立即压缩」）：超阈值、没在压，就后台开压。返回有没有开。"""
    try:
        conv = store.get(cid)
    except KeyError:
        return False
    c = conv.get("compact") or {}
    if c.get("state") in ("running", "ready") or cid in _running:
        return False
    if not force and (conv.get("ctx_tokens") or 0) < limit():
        return False
    start = conv.get("epoch_start") or 0
    cut = cut_index(conv["messages"], start, KEEP_TURNS)
    if cut is None and force:
        cut = cut_index(conv["messages"], start, 1)  # 立即压缩：轮次少也压，留最后一轮
    if cut is None or cut <= start:
        if force:
            store.update_conv(cid, compact={"state": "idle", "error": "对话太短，没什么可压的"})
        return False
    with _lock:
        if cid in _running:
            return False
        _running.add(cid)
    store.update_conv(cid, compact={"state": "running", "since": time.time(), "cut": cut})
    threading.Thread(target=_run, args=(cid, conv.get("summary") or "", conv["messages"][start:cut], cut),
                     name=f"compact-{cid}", daemon=True).start()
    log.info("compact kick %s epoch=%s tokens=%s cut=%d/%d force=%s", cid, conv.get("epoch") or 0,
             conv.get("ctx_tokens"), cut, len(conv["messages"]), force)
    return True


def _run(cid: str, prev: str, msgs: list[dict], cut: int) -> None:
    t0 = time.monotonic()
    try:
        summary = _summarize(prev, msgs)
        store.update_conv(cid, compact={"state": "ready", "pending": {"summary": summary, "cut": cut},
                                        "seconds": round(time.monotonic() - t0, 1)})
        log.info("compact ready %s in %.1fs, %d chars", cid, time.monotonic() - t0, len(summary))
    except Exception as e:  # 不换，下一轮再试
        log.warning("compact failed %s: %s", cid, e)
        store.update_conv(cid, compact={"state": "idle", "error": str(e)[:200]})
    finally:
        with _lock:
            _running.discard(cid)


def maybe_swap(cid: str) -> bool:
    """新一轮开始前调：有压好的就换上。返回换了没有。"""
    try:
        conv = store.get(cid)
    except KeyError:
        return False
    c = conv.get("compact") or {}
    if c.get("state") != "ready" or not c.get("pending"):
        return False
    p = c["pending"]
    epoch = (conv.get("epoch") or 0) + 1
    store.update_conv(cid, epoch=epoch, summary=p["summary"], epoch_start=p["cut"], compacted_at=time.time(),
                      compact={"state": "idle", "last_seconds": c.get("seconds")}, ctx_tokens=0)
    log.info("compact swap %s → epoch %d (keep from #%d)", cid, epoch, p["cut"])
    return True
