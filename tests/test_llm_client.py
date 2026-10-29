"""LLM 客户端的流式解析：正文、推理、usage，以及工具调用一段段拼起来。HTTP 用假的 requests.post。"""

from __future__ import annotations

import json

import pytest

from src.llm import client


class StreamResp:
    def __init__(self, chunks, status=200):
        self.status_code, self.text = status, "err"
        self._lines = [b"data: " + json.dumps(c, ensure_ascii=False).encode() for c in chunks] + [b"data: [DONE]"]

    def iter_lines(self):
        yield b": keep-alive"
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def stream(monkeypatch):
    sent = []
    state = {"chunks": [], "status": 200}

    def post(url, headers=None, json=None, stream=None, timeout=None):
        sent.append(json)
        return StreamResp(state["chunks"], state["status"])

    monkeypatch.setattr(client.requests, "post", post)
    monkeypatch.setenv("AUTOLINK_LLM_KEY", "fake")
    return sent, state


def delta(**d):
    return {"model": "m-served", "choices": [{"delta": d}]}


def test_content_reasoning_usage(stream):
    sent, state = stream
    state["chunks"] = [delta(reasoning_content="想"), delta(content="你"), delta(content="好"),
                       {"model": "m-served", "choices": [], "usage": {"total_tokens": 3}}]
    out = list(client.iter_stream("deepseek-v4.1-flash", "hi", system="sys"))
    assert out == [("model", "m-served"), ("reasoning", "想"), ("content", "你"), ("content", "好"),
                   ("usage", {"total_tokens": 3})]
    assert sent[0]["messages"][0] == {"role": "system", "content": "sys"}
    assert "tools" not in sent[0]


def test_tool_calls_stitched_across_chunks(stream):
    sent, state = stream
    tc = lambda i, **kw: delta(tool_calls=[{"index": i, **kw}])  # noqa: E731
    state["chunks"] = [
        delta(content="我查一下。"),
        tc(0, id="call_a", type="function", function={"name": "search_web", "arguments": ""}),
        tc(0, function={"arguments": '{"queries": [{"qu'}),
        tc(1, id="call_b", function={"name": "search_web", "arguments": '{"query": "b"}'}),
        tc(0, function={"arguments": 'ery": "a"}]}'}),
    ]
    tools = [{"type": "function", "function": {"name": "search_web"}}]
    out = list(client.iter_stream("deepseek-v4.1-flash", "hi", tools=tools))
    assert sent[0]["tools"] == tools
    assert out[-1] == ("tool_calls", [
        {"id": "call_a", "name": "search_web", "arguments": '{"queries": [{"query": "a"}]}'},
        {"id": "call_b", "name": "search_web", "arguments": '{"query": "b"}'}])
    assert ("content", "我查一下。") in out


def test_no_tool_calls_event_without_calls(stream):
    _, state = stream
    state["chunks"] = [delta(content="hi")]
    assert all(k != "tool_calls" for k, _ in client.iter_stream("deepseek-v4.1-flash", "hi", tools=[{}]))


def test_http_error_raises(stream):
    _, state = stream
    state["status"] = 500
    with pytest.raises(client.ProxyError, match="500"):
        list(client.iter_stream("deepseek-v4.1-flash", "hi"))
