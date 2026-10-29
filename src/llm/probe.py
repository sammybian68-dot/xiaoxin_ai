"""定位 prompt 里哪些部分让模型回空（代理或上游的拦截）。

把文本按行均分成 N 份，每份测两次：
  only    这一份单独当 system
  without 去掉这一份的全文当 system
only 回空的份，自己就会触发；without 仍然回空，说明还有别的份也会触发。
用法：python -m src.llm probe -m opus5.5 -f docs/trip_plan/route-planning.md -n 16
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.llm import batch

QUESTION = "17*23=? 只回答数字"


def split_lines(text: str, n: int) -> list[tuple[int, int]]:
    """按行号切成 n 份，返回每份的 [start, end) 行号。"""
    total = len(text.splitlines())
    n = max(1, min(n, total))
    step, extra = divmod(total, n)
    spans, start = [], 0
    for i in range(n):
        end = start + step + (1 if i < extra else 0)
        spans.append((start, end))
        start = end
    return spans


def probe(model: str, text: str, n: int, *, workers: int = 8, question: str = QUESTION, repeat: int = 1) -> list[dict]:
    lines = text.splitlines()
    spans = split_lines(text, n)
    items = []
    for i, (a, b) in enumerate(spans):
        only = "\n".join(lines[a:b])
        without = "\n".join(lines[:a] + lines[b:])
        for k in range(repeat):
            items.append({"id": f"only-{i}-{k}", "prompt": question, "system": only})
            items.append({"id": f"without-{i}-{k}", "prompt": question, "system": without})
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "probe.jsonl"
        batch.run(model, items, out, workers=workers, max_tokens=200, retries=1, timeout=120, quiet=True)
        got = batch.latest(out)

    def state(kind: str, i: int) -> str:
        rows = [got[f"{kind}-{i}-{k}"] for k in range(repeat)]
        empty = sum(1 for r in rows if not r["ok"] and "empty content" in r.get("error", ""))
        err = sum(1 for r in rows if not r["ok"] and "empty content" not in r.get("error", ""))
        return "ERR" if err else ("EMPTY" if empty == repeat else ("MIXED" if empty else "OK"))

    return [{"part": i, "lines": f"{a + 1}-{b}", "only": state("only", i), "without": state("without", i)}
            for i, (a, b) in enumerate(spans)]


def report(rows: list[dict], text: str) -> str:
    lines = text.splitlines()
    out = []
    for r in rows:
        a, b = (int(x) for x in r["lines"].split("-"))
        flag = "  <== 单独就触发" if r["only"] != "OK" else ""
        out.append(f"part {r['part']:>2}  行 {r['lines']:>8}  only={r['only']:<5}  without={r['without']:<5}{flag}")
        if r["only"] != "OK":
            for ln in range(a - 1, b):
                out.append(f"          L{ln + 1}: {lines[ln][:110]}")
    return "\n".join(out)


def main(model: str, path: str, n: int, repeat: int = 1) -> int:
    text = Path(path).read_text(encoding="utf-8")
    rows = probe(model, text, n, repeat=repeat)
    print(report(rows, text))
    print(json.dumps({"parts": len(rows), "trigger_alone": [r["part"] for r in rows if r["only"] != "OK"],
                      "still_empty_without_it": [r["part"] for r in rows if r["without"] != "OK"]}, ensure_ascii=False))
    return 0
