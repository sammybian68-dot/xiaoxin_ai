"""python -m src.llm list
python -m src.llm chat -m opus5.5 "李白是哪个朝代的"
python -m src.llm chat -m gpt-5.6-sol -s system.md -f prompt.md -o out.txt
python -m src.llm batch -m opus5.5 -s qa_system.md -i in.jsonl -o out.jsonl -w 8
python -m src.llm bench                                  # 默认四个模型测 TTFT / prefill / decode
python -m src.llm bench -m deepseek,opus5.5 -n 5
python -m src.llm image "一只猫" -o cat.png                 # 文生图，默认 gpt-image-2
python -m src.llm image "做成图标" -r me.jpg -o icon.png   # 以图改图
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.llm import batch, bench, client


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-m", "--model", default=client.DEFAULT_MODEL)
    p.add_argument("-s", "--system", help="系统提示文件")
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--temperature", type=float)
    p.add_argument("--timeout", type=float, default=600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.llm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出代理上的模型")

    p = sub.add_parser("probe", help="把文本切成 N 份，定位哪几份让模型回空")
    p.add_argument("-m", "--model", default=client.DEFAULT_MODEL)
    p.add_argument("-f", "--file", required=True)
    p.add_argument("-n", "--parts", type=int, default=16)
    p.add_argument("--repeat", type=int, default=1)

    c = sub.add_parser("chat", help="单次调用")
    _common(c)
    c.add_argument("prompt", nargs="?", help="用户消息；不给就读 -f 或 stdin")
    c.add_argument("-f", "--file", help="用户消息文件")
    c.add_argument("-o", "--out", help="输出文件")

    k = sub.add_parser("bench", help="测 TTFT、prefill、decode，结果打表")
    k.add_argument("-m", "--models", help="逗号分隔，或 all（代理上全部对话模型）。默认 " + ",".join(bench.DEFAULT_MODELS))
    k.add_argument("-p", "--parallel", type=int, default=1, help="同时跑几个模型")
    k.add_argument("--sort", default="ttft", help="表格排序列：ttft / decode_tps / prefill_tps / model")
    k.add_argument("-n", "--runs", type=int, default=3, help="每种 prompt 跑几次，取中位数")
    k.add_argument("--long-tokens", type=int, default=8000, help="长 prompt 的目标 token 数")
    k.add_argument("--max-tokens", type=int, default=600)
    k.add_argument("-o", "--out", help="原始结果 jsonl，默认 tmp/bench/<时间>.jsonl")

    g = sub.add_parser("image", help="文生图；给 -r 参考图就以图改图")
    g.add_argument("prompt", nargs="?", help="画面描述；不给就读 -f")
    g.add_argument("-f", "--file", help="画面描述文件")
    g.add_argument("-r", "--ref", action="append", help="参考图，可多次")
    g.add_argument("-o", "--out", required=True)
    g.add_argument("-m", "--model", default="gpt-image-2")
    g.add_argument("--size", default="1024x1024")
    g.add_argument("--quality", help="low / medium / high")
    g.add_argument("--background", help="transparent / opaque")

    b = sub.add_parser("batch", help="按 jsonl 一行一次循环调用，可续跑")
    _common(b)
    b.add_argument("-i", "--input", required=True, help="每行 {id, prompt} 或 {id, messages}")
    b.add_argument("-o", "--out", required=True, help="结果逐行追加，重跑跳过已成功的 id")
    b.add_argument("-w", "--workers", type=int, default=8)
    b.add_argument("--retries", type=int, default=3)

    a = ap.parse_args(argv)
    if a.cmd == "list":
        for m in client.models():
            print(m["id"])
        return 0

    if a.cmd == "probe":
        from src.llm import probe
        return probe.main(a.model, a.file, a.parts, a.repeat)

    if a.cmd == "image":
        from src.llm import image
        prompt = a.prompt or Path(a.file).read_text(encoding="utf-8")
        image.generate(prompt, a.out, ref=a.ref, model=a.model, size=a.size, quality=a.quality,
                       background=a.background)
        return 0

    if a.cmd == "bench":
        import time
        if a.models == "all":
            models = bench.all_models()
        else:
            models = [m.strip() for m in a.models.split(",")] if a.models else bench.DEFAULT_MODELS
        out = Path(a.out) if a.out else client.ROOT / "tmp" / "bench" / time.strftime("%Y%m%d-%H%M%S.jsonl")
        rows = bench.bench(models, runs=a.runs, long_tokens=a.long_tokens, max_tokens=a.max_tokens,
                           parallel=a.parallel, out=out)
        rows = bench.sort_rows(rows, a.sort)
        md = bench.report(rows, runs=a.runs, long_tokens=a.long_tokens, max_tokens=a.max_tokens)
        out.with_suffix(".md").write_text(md, encoding="utf-8")
        print(bench.table(rows))
        print(f"\n原始数据：{out}\n报告：{out.with_suffix('.md')}", file=sys.stderr)
        return 0

    system = Path(a.system).read_text(encoding="utf-8") if a.system else None
    if a.cmd == "chat":
        prompt = a.prompt or (Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read())
        rep = client.chat_full(a.model, prompt, system=system, max_tokens=a.max_tokens,
                               temperature=a.temperature, timeout=a.timeout)
        if a.out:
            Path(a.out).write_text(rep.text, encoding="utf-8")
        else:
            print(rep.text)
        print(json.dumps({"model": rep.model, "usage": rep.usage, "seconds": rep.seconds}), file=sys.stderr)
        return 0

    stats = batch.run(a.model, batch.read_jsonl(Path(a.input)), a.out, system=system, workers=a.workers,
                      max_tokens=a.max_tokens, temperature=a.temperature, timeout=a.timeout, retries=a.retries)
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
