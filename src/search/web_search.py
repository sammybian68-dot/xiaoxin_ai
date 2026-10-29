"""联网搜索：博查 Web Search，从 carHer2 server/app/carher/tools/web_search 搬过来。

思路照 carHer2：信源只查信息，总结让大模型自己做。博查返回 8 条网页摘要，渲染成 markdown 给模型读，
不走 Perplexity sonar 那种「搜索 + 内置 LLM 总结」的慢路径。
一次调用 1–3 个 query 并发；同一轮回答最多搜 2 次（次数由调用方 server/main.py 管）。

失败分两种，跟 carHer2 一样：
  - key 缺、余额不足：模型应该原话告诉用户怎么办（ok=True，内容以「联网失败」开头）
  - 网络挂了、5xx、超时：ok=False，内容以 OfflineFallback 开头，模型说「现在没法联网」
2026-10-29 用 carHer2 的 key 实测：每次 0.3 秒左右，8 条结果。
key 是 .env 的 BOCHA_API_KEY，和 carHer2 用的同一个。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from src.voice.doubao import env

NAME = "search_web"  # 不叫 web_search：公司代理把这个名字的工具劫持成 Claude 自带的服务端搜索，参数是空的、结果是编的
BOCHA_URL = "https://api.bochaai.com/v1/web-search"
BOCHA_BILLING_URL = "https://open.bochaai.com/billing"
BOCHA_APPLY_URL = "https://open.bochaai.com/api-keys"
MAX_RESULTS = 8
MAX_QUERIES = 3
TIMEOUT = 12.0  # 平时 0.3 秒，留足余量
SNIPPET_CHARS = 320
FRESHNESS = {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}
_MONEY_WORDS = ("money", "quota", "insufficient", "balance", "package")

DESCRIPTION = """联网搜索实时信息和外部事实，返回网页摘要，由你读完自己总结。

新闻、天气、比分、股价、最近发生的事、你不确定或可能过时的事实，都先搜再答；常识、讲题、闲聊不用搜。
freshness 只给真正最近的事用（今天的新闻给 day）；历史、科普、攻略不要加。
一次传 1–3 个 query，同一件事的不同角度一次传齐，不要分多轮。每轮回答最多搜 2 次，到了就基于已有结果回答，查不到就如实说。
结果里「联网失败」开头的，把原因和办法原话告诉用户；OfflineFallback 开头的，说现在没法联网。"""

SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_QUERIES,
                    "description": "1–3 个搜索，并发执行。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索词或问题"},
                            "freshness": {"type": "string", "enum": list(FRESHNESS),
                                          "description": "day/week/month/year，只给最近的事用"},
                        },
                        "required": ["query"],
                    },
                },
            },
            "required": ["queries"],
        },
    },
}


@dataclass
class Result:
    ok: bool
    content: str                      # 给模型读的
    queries: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)  # [{title, url, site}]，给界面列出处


def parse_queries(args: dict | str) -> list[tuple[str, str | None]]:
    """模型给的参数 → [(query, freshness)]。兼容 {"query": "..."} 旧格式和纯字符串；去重，最多 3 个。"""
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            args = {"query": args}
    if not isinstance(args, dict):
        return []
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def push(q, f) -> None:
        q = str(q or "").strip()
        if q and q not in seen:
            seen.add(q)
            out.append((q, f if f in FRESHNESS else None))

    raw = args.get("queries")
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                push(it.get("query"), it.get("freshness"))
            elif isinstance(it, str):
                push(it, None)
    else:
        push(args.get("query"), args.get("freshness"))
    return out[:MAX_QUERIES]


def normalize(payload) -> list[dict]:
    """博查 {code, data: {webPages: {value: [...]}}} → [{title, url, snippet, site}]。"""
    if not isinstance(payload, dict):
        return []
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    pages = inner.get("webPages") or inner.get("webpages") if isinstance(inner, dict) else None
    items = pages.get("value") if isinstance(pages, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({"title": it.get("name") or it.get("title") or "",
                        "url": it.get("url") or it.get("displayUrl") or "",
                        "snippet": it.get("summary") or it.get("snippet") or "",
                        "site": it.get("siteName") or ""})
    return out[:MAX_RESULTS]


def render(items: list[dict]) -> str:
    """给模型读的 markdown，刻意简洁。"""
    lines = [f"共 {len(items)} 条", ""]
    for i, p in enumerate(items, 1):
        head = f"**{i}. {(p['title'] or '(无标题)').strip()}**"
        if p["site"]:
            head += f" — {p['site'].strip()}"
        lines.append(head)
        if p["url"]:
            lines.append(f"<{p['url'].strip()}>")
        if p["snippet"]:
            lines.append(p["snippet"].strip()[:SNIPPET_CHARS])
        lines.append("")
    return "\n".join(lines).rstrip()


def _actionable(title: str, detail: str, action: str) -> Result:
    return Result(True, f"**联网失败（博查）**\n\n{title}\n{detail}\n{action}")


def search_one(query: str, freshness: str | None = None) -> Result:
    key = env("BOCHA_API_KEY")
    if not key:
        return _actionable("缺少博查 API key。", "小新服务端的 .env 里没有 BOCHA_API_KEY。",
                           f"在 {BOCHA_APPLY_URL} 申请 key，填进 .env 后重新部署。")
    body: dict = {"query": query, "summary": True, "count": MAX_RESULTS}
    if freshness:
        body["freshness"] = FRESHNESS[freshness]
    try:
        r = requests.post(BOCHA_URL, json=body, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        if r.status_code in (402, 403) and any(w in r.text.lower() for w in _MONEY_WORDS):
            return _actionable(f"博查账户余额或套餐不足（HTTP {r.status_code}）。", "不是网络问题。",
                               f"去 {BOCHA_BILLING_URL} 充值后再问一次。")
        if r.status_code in (401, 403):
            return _actionable(f"博查 key 无效（HTTP {r.status_code}）。", "",
                               f"在 {BOCHA_APPLY_URL} 重新生成 key，换掉 .env 里的 BOCHA_API_KEY。")
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as e:
        return Result(False, f"OfflineFallback: 联网搜索失败：{type(e).__name__}")
    except ValueError:
        return Result(False, "OfflineFallback: 博查返回的不是 JSON")

    code = payload.get("code") if isinstance(payload, dict) else None
    if code not in (None, 200, "200"):
        msg = str(payload.get("msg") or payload.get("message") or "").strip()
        if any(w in msg.lower() for w in _MONEY_WORDS):
            return _actionable(f"博查业务错误 code={code}。", msg, f"去 {BOCHA_BILLING_URL} 充值。")
        return Result(False, f"OfflineFallback: 博查业务错误 code={code}：{msg[:200]}")

    items = normalize(payload)
    if not items:
        return Result(True, f"没有相关结果：{query}", [query])
    sources = [{"title": p["title"], "url": p["url"], "site": p["site"]} for p in items if p["url"]]
    return Result(True, render(items), [query], sources)


def search(args: dict | str) -> Result:
    """模型的一次工具调用：解析参数，1–3 个 query 并发搜，按 query 分节合并。"""
    pairs = parse_queries(args)
    if not pairs:
        return Result(False, "OfflineFallback: 没有搜索词")
    if len(pairs) == 1:
        res = search_one(*pairs[0])
        res.queries = [pairs[0][0]]
        return res
    with ThreadPoolExecutor(len(pairs)) as pool:
        results = list(pool.map(lambda p: search_one(*p), pairs))
    sources, seen = [], set()
    for res in results:
        for s in res.sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                sources.append(s)
    content = "\n\n---\n\n".join(f"# 查询：{q}\n\n{res.content}" for (q, _), res in zip(pairs, results))
    return Result(any(r.ok for r in results), content, [q for q, _ in pairs], sources)
