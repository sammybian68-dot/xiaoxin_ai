"""批量循环调用：一行输入一次请求，结果逐行追加到输出 jsonl，可断点续跑。

输入每行至少有 `id`，再加 `prompt`（一句话）或 `messages`（OpenAI 格式）。可选 `system` 覆盖整批的系统提示。
输出每行：id、ok、reply、model、usage、seconds，失败时 ok=false 并带 error。
重跑同一个输出文件时，ok 的 id 跳过，失败的重试。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from src.llm import client


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def done_ids(out: Path) -> set[str]:
    return {str(r["id"]) for r in read_jsonl(out) if r.get("ok")}


def latest(out: Path) -> dict[str, dict]:
    """输出文件里每个 id 最后一次的结果；续跑后同一 id 可能有失败行和成功行，成功的优先。"""
    rows: dict[str, dict] = {}
    for r in read_jsonl(out):
        k = str(r["id"])
        if r.get("ok") or not rows.get(k, {}).get("ok"):
            rows[k] = r
    return rows


def run(
    model: str,
    items: Iterable[dict],
    out: str | Path,
    *,
    system: str | None = None,
    workers: int = 8,
    max_tokens: int = 8192,
    temperature: float | None = None,
    timeout: float = 600,
    retries: int = 3,
    on_result: Callable[[dict], None] | None = None,
    quiet: bool = False,
) -> dict:
    """跑完返回汇总：总数、跳过、成功、失败、token 合计、失败的 id。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    items = list(items)
    ids = [str(it["id"]) for it in items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate id in items")
    skip = done_ids(out)
    todo = [it for it in items if str(it["id"]) not in skip]

    lock = threading.Lock()
    stats = {"total": len(items), "skipped": len(items) - len(todo), "ok": 0, "failed": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "failed_ids": []}
    t0 = time.monotonic()

    def one(it: dict) -> dict:
        msgs = it.get("messages") or it["prompt"]
        try:
            rep = client.chat_full(model, msgs, system=it.get("system", system), max_tokens=max_tokens,
                                   temperature=temperature, timeout=timeout, retries=retries)
            return {"id": it["id"], "ok": True, "reply": rep.text, "model": rep.model,
                    "usage": rep.usage, "seconds": rep.seconds}
        except (client.ProxyError, KeyError) as e:
            return {"id": it["id"], "ok": False, "error": str(e)}

    with out.open("a", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(one, it) for it in todo]
        for n, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            with lock:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                if row["ok"]:
                    stats["ok"] += 1
                    stats["prompt_tokens"] += row["usage"].get("prompt_tokens") or 0
                    stats["completion_tokens"] += row["usage"].get("completion_tokens") or 0
                else:
                    stats["failed"] += 1
                    stats["failed_ids"].append(row["id"])
            if on_result:
                on_result(row)
            if not quiet and (n % 10 == 0 or n == len(todo)):
                print(f"[{n}/{len(todo)}] ok={stats['ok']} failed={stats['failed']} "
                      f"{time.monotonic() - t0:.0f}s", file=sys.stderr, flush=True)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats
