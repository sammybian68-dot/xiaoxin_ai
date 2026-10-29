# src/llm

从 gpu2 `mainbrain_27B_model/src/llm` 移植（2026-10-28），加了流式计时、一键 benchmark 和文生图。本仓库默认模型是 `deepseek-v4.1-flash`，用 `-m` 或环境变量 `AUTOLINK_LLM_MODEL` 换。

这个模块直连公司的 LiteLLM 代理（`https://cc.auto-link.com.cn/pro`）。用一个 key 就能调 Claude、GPT、Grok，按次计费，不走 Cursor 套餐，也不走 harness。适合质检、改写、打分这类一行一次的循环任务。

## key

先读环境变量 `AUTOLINK_LLM_KEY`，没有再读仓库根目录 `.env` 里的同名项。`.env` 已加进 gitignore。地址可以用 `AUTOLINK_LLM_BASE_URL` 覆盖。

## 命令行

```bash
python -m src.llm list                                   # 代理上的全部模型名
python -m src.llm chat -m opus5.5 "李白是哪个朝代的"       # 单次调用，用量打到 stderr
python -m src.llm chat -m gpt-5.6-sol -s sys.md -f q.md -o out.txt
python -m src.llm batch -m opus5.5 -s qa_system.md -i in.jsonl -o out.jsonl -w 8
```

`batch` 的输入每行写成 `{"id": ..., "prompt": "..."}`。多轮对话就把 `prompt` 换成 `"messages": [...]`。某一行要单独换系统提示，就加 `"system"`。

输出每行写成 `{"id", "ok", "reply", "model", "usage", "seconds"}`，失败的行是 `ok=false` 加 `error`。每完成一条就追加一行。中途断了，用同一个 `-o` 重跑：成功过的 id 会跳过，失败的会重试。跑完会打印汇总，包括成功、失败、token 合计和失败的 id。

## 自己部署的模型

`qwen3.8-27b`（也可以写 `qwen`、`27b`、`mainbrain`）是主脑 Qwen3.8-27B，部署在 H100.2 上：FP8，TP=2，MTP1，上下文 64k。它不走公司代理，直接连 `http://117.157.77.4:50080`（H100.2 的公网端口，没有 key），地址可以用 `MAINBRAIN_BASE_URL` 覆盖。

- 默认关闭思考，首字约 0.3 s。要思考就用 `qwen3.8-27b-think`，首字约 0.6 s。
- 纯文本模型，不能看图。
- 定义在 `client.SELF_HOSTED`，以后再加自己部署的模型也加在这里。

## 速度测试

```bash
python -m src.llm bench                          # 默认 deepseek-v4.1-flash, grok-4.7, opus5.5, sonnet5.5
python -m src.llm bench -m deepseek,kimi -n 5    # 自选模型，每种 prompt 跑 5 次
python -m src.llm bench -m all -p 6              # 代理上全部对话模型，6 个模型并行
```

每个模型跑短 prompt 和约 8k token 的长 prompt，取中位数。表里 TTFT 是首个 token（含推理），首正文是第一个正文 token，decode 是首 token 之后的 tok/s，prefill 用长短两次的 TTFT 差估。结果写 `tmp/bench/<时间>.jsonl` 和同名 `.md`。都是经代理的端到端数字，代理自己塞的系统提示也算在 prompt 里。

## 文生图

```bash
python -m src.llm image "一只猫" -o cat.png
python -m src.llm image "做成 App 图标" -r me.jpg -o icon.png --quality medium
```

默认 `gpt-image-2`。给 `-r` 走 `/v1/images/edits`，参考图先缩到 1024 再传。high 质量一张可能超过 5 分钟，超时设的 600 秒。

## Python

```python
from src.llm import chat, chat_full, run_batch, latest

chat("opus5.5", "...", system="...")                 # 返回文本
chat_full("grok", "...").usage                       # 文本、实际模型、token、耗时

items = [{"id": t["id"], "prompt": render(t)} for t in tickets]
stats = run_batch("opus5.5", items, "runs/x/qa.jsonl", system=SYS, workers=8)
replies = {k: r["reply"] for k, r in latest(Path("runs/x/qa.jsonl")).items() if r["ok"]}
```

## 模型

短名定义在 `client.ALIASES`，不在表里的名字原样传给代理。下面是 2026-10-22 实测能用的：

| 写法 | 实际模型 | 代理自己加的系统提示 |
|:--|:--|--:|
| `opus5.5` / `opus5` / `sonnet5.5` / `haiku4.5` | claude-* | 约 6.7k / 6.7k / 6.5k / 4.1k token |
| `gpt-5.6-sol` / `-terra` / `-luna` | gpt-5.6-* | 约 1.6k token |
| `grok` | grok-4.6 | 约 2.6k token |

## 互相 fallback

`-m` 或 `model` 可以写 `strong`、`a,b,c`，也可以传一个列表。前一个模型重试完还失败，就换下一个。实际是谁回答的，看结果里的 `model` 字段。

`strong` 的默认顺序是 claude-opus-5-5 → claude-opus-5 → claude-opus-4-8 → gpt-5.6-sol → gpt-6.1-sol，定义在 `client.CHAINS`。质检兜底先换同一家的 Opus，判断风格最接近；GPT 偏宽，放最后。

```bash
python -m src.llm batch -m strong -s qa_system.md -i in.jsonl -o out.jsonl -w 8
```

## 注意

- Claude 只要 prompt 里带 `route-planning.md` 就回空：返回 200，content 为空，只出 1 个 token。用同样长度的普通文本做系统提示，或者只放其中某一句，都没有这个问题。删掉 runtime 里的工具调用句，或者把 runtime 包成「仅供参考」，也照样回空。推测是代理或上游做了过滤，具体原因没有查清。所以跟 trip_plan 有关的质检，先用 GPT。

- 2026-10-22 实测代理上有两个名字是挂羊头：`grok-4.7` 由 `custom_openai/deepseek-flash` 回答，`gpt-5.5` 实际是 `gpt-5.6-luna`。2026-10-28 复测 `grok-4.7` 已经是真 Grok（自称 xAI，代理系统提示约 3.3k token，带 xAI 的 cached_tokens），`gpt-5.5` 没复测。每次调用后可以看 `Reply.model` 或 batch 输出里的 `model`，确认实际是哪个模型在回答。
- 写裸名 `gpt-5.6` 会返回 403，要写 sol、terra、luna 这三个变体之一。
- 代理会在每次请求前面加一段系统提示。拿来质检、判对错影响不大；直接生成训练用的文本时，风格可能被带偏。
- 代理会丢掉 `thinking` 和 `reasoning_effort`，所以推理档选不了。
- 遇到 5xx、429、网络错误或空回复会自动重试；其余 4xx（比如没有权限、模型名写错）直接失败。
- Claude 一次放 15 条以上时偶尔会回空内容，建议每次请求放 10 条以内，或者干脆一条一次。
