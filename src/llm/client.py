"""公司 LiteLLM 代理：一个 key 调 Claude、GPT、Grok 等，按次计费，不走 Cursor 套餐，也不走 harness。

key 和地址读环境变量 AUTOLINK_LLM_KEY / AUTOLINK_LLM_BASE_URL，没有就读仓库根目录的 .env（已 gitignore）。
代理会在请求前塞自己的系统提示：Claude 约 6.7k token，GPT 约 1.6k，Grok 几乎没有。
thinking / reasoning_effort 这类参数代理会丢掉，选不了推理档。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://cc.auto-link.com.cn/pro"

# 短名。代理上没有的名字原样透传，比如 gpt-5.6-sol、kimi-k3。
# gpt-5.6 只有 sol / terra / luna 三个变体，裸写 gpt-5.6 会 403。
# 2026-10-22 实测，代理有两个名字是挂羊头：grok-4.7 实际回的是 custom_openai/deepseek-flash，
# gpt-5.5 实际回的是 chatgpt-gpt-5.6-luna。所以 grok 指向 grok-4.6，不给 grok-4.7 和 gpt-5.5 短名。
# 2026-10-28 复测 grok-4.7 已是真 Grok（自称 xAI），所以补了 grok4.7 短名。
# 用 Reply.model 能看到实际服务的模型。
ALIASES = {
    "opus5": "claude-opus-5",
    "opus5.5": "claude-opus-5-5",
    "opus4.8": "claude-opus-4-8",
    "sonnet5": "claude-sonnet-5",
    "sonnet5.5": "claude-sonnet-5-5",
    "haiku4.5": "claude-haiku-4-5",
    "gpt5.6-sol": "gpt-5.6-sol",
    "gpt5.6-terra": "gpt-5.6-terra",
    "gpt5.6-luna": "gpt-5.6-luna",
    "gpt6": "gpt-6-sol",
    "gpt6.1": "gpt-6.1-sol",
    "grok": "grok-4.6",
    "grok4.6": "grok-4.6",
    "gemini": "gemini-3.8-flash",
    "deepseek": "deepseek-v4.1-flash",
    "deepseek4.1": "deepseek-v4.1-flash",
    "grok4.7": "grok-4.7",
    "kimi": "kimi-k3",
    "qwen": "qwen3.8-27b",
    "mainbrain": "qwen3.8-27b",
    "27b": "qwen3.8-27b",
}

# 本仓库默认模型。命令行 -m 或环境变量 AUTOLINK_LLM_MODEL 可以换。
DEFAULT_MODEL = os.environ.get("AUTOLINK_LLM_MODEL", "deepseek-v4.1-flash")


class ProxyError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    model: str
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0


def _dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


# 不走公司代理的模型：自己的 vLLM。模型名 -> (地址环境变量, 默认地址, 发给 vLLM 的模型名, 额外请求参数)。
# 主脑 Qwen3.8-27B 在 H100.2（8×H100）上，TP=2 + MTP1，FP8，对外是 117.157.77.4:50080，没有 key。
# 默认关掉思考：开着首字 0.7 s，关掉 0.3 s，日常聊天不需要。要思考就用 qwen3.8-27b-think。
SELF_HOSTED = {
    "qwen3.8-27b": ("MAINBRAIN_BASE_URL", "http://117.157.77.4:50080", "qwen3.8-27b",
                    {"chat_template_kwargs": {"enable_thinking": False}}),
    "qwen3.8-27b-think": ("MAINBRAIN_BASE_URL", "http://117.157.77.4:50080", "qwen3.8-27b", {}),
}


def config(env_file: Path | None = None, model: str | None = None) -> tuple[str, str]:
    """(key, base)。给了 model 且是自己部署的，就返回那台 vLLM 的地址，key 为空串。"""
    file_env = _dotenv(env_file or ROOT / ".env")
    if model in SELF_HOSTED:
        var, default, _, _ = SELF_HOSTED[model]
        return "", (os.environ.get(var) or file_env.get(var) or default).rstrip("/")
    key = os.environ.get("AUTOLINK_LLM_KEY") or file_env.get("AUTOLINK_LLM_KEY")
    base = os.environ.get("AUTOLINK_LLM_BASE_URL") or file_env.get("AUTOLINK_LLM_BASE_URL") or DEFAULT_BASE_URL
    if not key:
        raise ProxyError("missing AUTOLINK_LLM_KEY (env or .env at repo root)")
    return key, base.rstrip("/")


def _endpoint(model: str, body: dict) -> tuple[str, dict, dict]:
    """返回 (url, headers, body)。自己部署的模型换地址、换模型名、补参数。"""
    key, base = config(model=model)
    if model in SELF_HOSTED:
        _, _, served, extra = SELF_HOSTED[model]
        body = {**body, "model": served, **extra}
        return f"{base}/v1/chat/completions", {}, body
    return f"{base}/v1/chat/completions", {"Authorization": f"Bearer {key}"}, body


def resolve_model(name: str) -> str:
    return ALIASES.get(name.lower(), name)


def models() -> list[dict]:
    key, base = config()
    r = requests.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


# 滚动兜底的默认顺序，按 2026-10-22 实测的稳定性排。
# L14 改过之后 Claude 带 runtime 能正常回；偶尔回空就换下一个。
CHAINS = {
    # 质检兜底优先同一家：opus5.5 回空先换 opus5、opus4.8，判断风格最接近；GPT 偏宽，放最后
    "strong": ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "gpt-5.6-sol", "gpt-6.1-sol"],
}


def resolve_chain(model: str | list[str]) -> list[str]:
    """'strong'、'a,b,c' 或列表，都展开成按顺序尝试的模型列表。"""
    if isinstance(model, str):
        model = CHAINS.get(model.lower()) or [m.strip() for m in model.split(",") if m.strip()]
    return [resolve_model(m) for m in model]


def chat_full(
    model: str | list[str],
    messages: list[dict] | str,
    **kw,
) -> Reply:
    """一轮对话，返回文本、实际模型名、token 用量和耗时。messages 可以直接给一句话。

    model 可以是一个模型，也可以是 'strong'、'a,b,c' 或列表：前一个重试完还失败，就换下一个。
    Reply.model 是实际回答的那个。
    """
    errors = []
    for m in resolve_chain(model):
        try:
            return _chat_one(m, messages, **kw)
        except ProxyError as e:
            errors.append(f"{m}: {e}")
    raise ProxyError(" | ".join(errors))


def _chat_one(
    model: str,
    messages: list[dict] | str,
    *,
    system: str | None = None,
    max_tokens: int = 8192,
    temperature: float | None = None,
    timeout: float = 600,
    retries: int = 3,
) -> Reply:
    """单个模型。5xx、429、网络错误、空内容会重试；其余 4xx（key 无权限、模型名不对）直接报错。"""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if system:
        messages = [{"role": "system", "content": system}, *messages]
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    url, headers, body = _endpoint(model, body)
    last = ""
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            r = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout,
            )
        except requests.RequestException as e:
            last = repr(e)
        else:
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"].get("content")
                if content:
                    return Reply(content, data.get("model") or body["model"], data.get("usage") or {},
                                 round(time.monotonic() - t0, 2))
                last = f"empty content: {str(data)[:500]}"
            else:
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code < 500 and r.status_code != 429:
                    break
        if attempt < retries - 1:
            time.sleep(2 ** attempt * 3)
    raise ProxyError(last)


@dataclass
class Timing:
    """一次流式请求的计时。时间都从发出请求算起，单位秒。"""
    model: str
    text: str
    ttft: float | None          # 第一个非空 token，含 reasoning_content
    ttft_content: float | None  # 第一个正文 token；有推理的模型会比 ttft 晚
    total: float
    prompt_tokens: int
    completion_tokens: int      # 代理返回的数，含推理 token
    chunks: int

    @property
    def decode_tps(self) -> float | None:
        """首 token 之后的生成速度。usage 没回来就按 chunk 数估。"""
        if self.ttft is None or self.total <= self.ttft:
            return None
        n = self.completion_tokens or self.chunks
        return (n - 1) / (self.total - self.ttft) if n > 1 else None


def iter_stream(
    model: str,
    messages: list[dict] | str,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
    temperature: float | None = None,
    timeout: float = 300,
):
    """单个模型，一次流式请求，不重试。逐块 yield (kind, value)：
    ('model', 实际模型名)、('reasoning', 推理文本)、('content', 正文)、('usage', dict)。"""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if system:
        messages = [{"role": "system", "content": system}, *messages]
    model = resolve_model(model)
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True,
                  "stream_options": {"include_usage": True}}
    if temperature is not None:
        body["temperature"] = temperature
    url, headers, body = _endpoint(model, body)
    try:
        r = requests.post(url, headers=headers, json=body, stream=True, timeout=timeout)
    except requests.RequestException as e:
        raise ProxyError(repr(e)) from e
    if r.status_code != 200:
        raise ProxyError(f"HTTP {r.status_code}: {r.text[:500]}")
    served = None
    with r:
        for line in r.iter_lines():
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            data = json.loads(payload)
            if data.get("model") and data["model"] != served:
                served = data["model"]
                yield "model", served
            if data.get("usage"):
                yield "usage", data["usage"]
            for ch in data.get("choices") or []:
                delta = ch.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield "reasoning", reasoning
                if delta.get("content"):
                    yield "content", delta["content"]


def chat_stream(
    model: str,
    messages: list[dict] | str,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
    temperature: float | None = None,
    timeout: float = 300,
) -> Timing:
    """单个模型，一次流式请求，不重试。用来测 TTFT 和 decode。"""
    t0 = time.monotonic()
    ttft = ttft_content = None
    served, usage, parts, chunks = resolve_model(model), {}, [], 0
    for kind, value in iter_stream(model, messages, system=system, max_tokens=max_tokens,
                                   temperature=temperature, timeout=timeout):
        if kind == "model":
            served = value
        elif kind == "usage":
            usage = value
        else:
            chunks += 1
            if ttft is None:
                ttft = time.monotonic() - t0
            if kind == "content":
                parts.append(value)
                if ttft_content is None:
                    ttft_content = time.monotonic() - t0
    total = time.monotonic() - t0
    if ttft is None:
        raise ProxyError(f"empty stream from {served}")
    return Timing(served, "".join(parts), ttft, ttft_content, total,
                  usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0, chunks)


def chat(model: str, messages: list[dict] | str, **kw) -> str:
    """同 chat_full，只返回文本。"""
    return chat_full(model, messages, **kw).text


def chat_many(model: str, prompts: list[str], *, workers: int = 8, **kw) -> list[str | ProxyError]:
    """并发跑多条，顺序和 prompts 一致；单条失败返回 ProxyError，不打断其余。不落盘，大批量用 batch.run。"""

    def one(p: str) -> str | ProxyError:
        try:
            return chat(model, p, **kw)
        except ProxyError as e:
            return e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, prompts))
