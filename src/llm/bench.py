"""一键测 TTFT、prefill、decode。

每个模型跑两种 prompt，各 N 次，顺序执行（不并发，避免互相拖慢）：
  short  一句话，要求输出约 300 字。看 TTFT 和 decode 速度
  long   约 8k token 的填充文本加同一个问题。和 short 的 TTFT 相减，估 prefill 速度

指标：
  ttft      发出请求到第一个 token（含推理 token）
  ttft_txt  到第一个正文 token。推理模型会先想再答，这个才是用户真正等的
  decode    首 token 之后的 tok/s = (completion_tokens - 1) / (total - ttft)
  prefill   (long_prompt - short_prompt) / (long_ttft - short_ttft)，tok/s

注意：都经过公司代理，测到的是「代理 + 上游」的端到端，不是裸模型。
代理会在 prompt 前塞自己的系统提示（Claude 约 6.7k token），所以 short 的 prompt_tokens 也不小。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

from src.llm import client

DEFAULT_MODELS = ["deepseek-v4.1-flash", "grok-4.7", "opus5.5", "sonnet5.5", "qwen3.8-27b"]

# 代理上不是对话模型的，-m all 时跳过
NOT_CHAT = ("gpt-image", "grok-imagine")


def all_models() -> list[str]:
    """代理上的全部对话模型，加上自己部署的。"""
    proxy = sorted(m["id"] for m in client.models() if not m["id"].startswith(NOT_CHAT))
    return proxy + list(client.SELF_HOSTED)

QUESTION = "用中文写一段约300字的说明，介绍平板电脑上麦克风回声消除的原理。直接写正文，不要标题。"

# 填充段落，每段内容不同，避免上游前缀缓存把 long 的 prefill 抹掉
FILLER = ("第{i}段资料：编号{i}的设备在测试中记录了温度{t}度、电压{v}伏、采样率16000赫兹，"
          "序列号HX{i:05d}，备注：这段文字只是为了填充上下文长度，回答问题时请忽略。\n")


def long_prompt(target_tokens: int, salt: int) -> str:
    # 每段约 60 token（中文约 1 字 1 token 偏多一点），粗估即可，实际数以 usage 为准
    n = max(1, target_tokens // 60)
    body = "".join(FILLER.format(i=salt * 100000 + i, t=20 + i % 15, v=3 + i % 5) for i in range(n))
    return body + "\n" + QUESTION


def run_one(model: str, prompt: str, max_tokens: int) -> dict:
    try:
        t = client.chat_stream(model, prompt, max_tokens=max_tokens)
    except client.ProxyError as e:
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "served": t.model, "ttft": t.ttft, "ttft_content": t.ttft_content, "total": t.total,
            "prompt_tokens": t.prompt_tokens, "completion_tokens": t.completion_tokens,
            "decode_tps": t.decode_tps, "chars": len(t.text)}


def _med(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get("ok") and r.get(key) is not None]
    return statistics.median(vals) if vals else None


def summarize(model: str, short: list[dict], long: list[dict]) -> dict:
    s = {"model": model,
         "served": next((r["served"] for r in short + long if r.get("ok")), None),
         "ok": f"{sum(r['ok'] for r in short + long)}/{len(short + long)}",
         "ttft": _med(short, "ttft"), "ttft_content": _med(short, "ttft_content"),
         "decode_tps": _med(short, "decode_tps"),
         "short_prompt": _med(short, "prompt_tokens"), "long_prompt": _med(long, "prompt_tokens"),
         "long_ttft": _med(long, "ttft"), "prefill_tps": None}
    if None not in (s["ttft"], s["long_ttft"], s["short_prompt"], s["long_prompt"]):
        dt = s["long_ttft"] - s["ttft"]
        if dt > 0.05:
            s["prefill_tps"] = (s["long_prompt"] - s["short_prompt"]) / dt
    errs = [r["error"] for r in short + long if not r.get("ok")]
    if errs:
        s["error"] = errs[0]
    return s


def _f(v, fmt: str) -> str:
    return "-" if v is None else format(v, fmt)


def table(rows: list[dict]) -> str:
    head = ("| 模型 | 实际 | 成功 | TTFT s | 首正文 s | decode tok/s | 长 prompt tok | 长 TTFT s | prefill tok/s |\n"
            "|:--|:--|--:|--:|--:|--:|--:|--:|--:|")
    lines = [head]
    for s in rows:
        lines.append(f"| {s['model']} | {s['served'] or '-'} | {s['ok']} | {_f(s['ttft'], '.2f')} | "
                     f"{_f(s['ttft_content'], '.2f')} | {_f(s['decode_tps'], '.1f')} | "
                     f"{_f(s['long_prompt'], '.0f')} | {_f(s['long_ttft'], '.2f')} | {_f(s['prefill_tps'], '.0f')} |")
    errs = [f"- {s['model']}: {s['error']}" for s in rows if s.get("error")]
    return "\n".join(lines + ([""] + errs if errs else []))


def bench_model(m: str, *, runs: int, long_tokens: int, max_tokens: int) -> tuple[dict, list[dict]]:
    """一个模型内部顺序跑，short / long 交替。"""
    raw, short, long = [], [], []
    for i in range(runs):
        r = run_one(m, QUESTION, max_tokens)
        short.append(r)
        raw.append({"model": m, "kind": "short", "run": i, **r})
        print(f"  {m} short #{i + 1}: " + (f"ttft={r['ttft']:.2f}s decode={_f(r['decode_tps'], '.1f')}tok/s"
                                          if r["ok"] else r["error"][:120]), file=sys.stderr, flush=True)
        if not r["ok"] and i == 0 and "HTTP 4" in r["error"]:
            break  # 没权限或模型名不对，不用再试
        r = run_one(m, long_prompt(long_tokens, salt=int(time.time() * 1000) % 100000 + i), max_tokens)
        long.append(r)
        raw.append({"model": m, "kind": "long", "run": i, **r})
        print(f"  {m} long  #{i + 1}: " + (f"ttft={r['ttft']:.2f}s prompt={r['prompt_tokens']}tok"
                                          if r["ok"] else r["error"][:120]), file=sys.stderr, flush=True)
    return summarize(m, short, long), raw


def bench(models: list[str], *, runs: int = 3, long_tokens: int = 8000, max_tokens: int = 600,
          parallel: int = 1, out: Path | None = None) -> list[dict]:
    """parallel>1 时多个模型同时跑。不同模型走不同上游，互相影响小；同一模型内部始终顺序。"""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        results = list(ex.map(lambda m: bench_model(m, runs=runs, long_tokens=long_tokens,
                                                    max_tokens=max_tokens), models))
    rows = [r for r, _ in results]
    raw = [x for _, rs in results for x in rs]
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in raw) + "\n", encoding="utf-8")
    return rows


def sort_rows(rows: list[dict], key: str) -> list[dict]:
    """失败的排最后。速度类列从大到小，时间类从小到大。"""
    if key == "model":
        return sorted(rows, key=lambda s: s["model"])
    desc = key.endswith("_tps")
    big = float("inf")
    return sorted(rows, key=lambda s: (s.get(key) is None, -(s.get(key) or 0) if desc else (s.get(key) or big)))


def report(rows: list[dict], *, runs: int, long_tokens: int, max_tokens: int) -> str:
    return (f"# LLM 速度测试 {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"经公司代理端到端。每个模型 short / long 各 {runs} 次取中位数，长 prompt 目标 {long_tokens} token，"
            f"max_tokens={max_tokens}。\n\n" + table(rows) + "\n")
