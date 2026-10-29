"""联网搜索（博查）：参数解析、结果归一和渲染、各种失败怎么分类、多 query 并发合并。HTTP 用假的 requests.post。"""

from __future__ import annotations

import json

import pytest
import requests

from src.search import web_search as ws


class Resp:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data = status, data
        self.text = text if text is not None else json.dumps(data or {})

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def bocha_ok(n=3, query="q"):
    return {"code": 200, "data": {"webPages": {"value": [
        {"name": f"{query} 标题{i}", "url": f"https://site{i}.example/{query}", "summary": "摘" * 400,
         "siteName": f"站{i}"} for i in range(n)]}}}


@pytest.fixture
def post(monkeypatch):
    """记下发给博查的请求；responder(body) 决定回什么。"""
    sent = []
    state = {"responder": lambda body: Resp(data=bocha_ok(query=body["query"]))}

    def fake(url, json=None, timeout=None, headers=None):
        sent.append({"url": url, "body": json, "headers": headers})
        r = state["responder"](json)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ws.requests, "post", fake)
    monkeypatch.setenv("BOCHA_API_KEY", "fake-bocha")
    return sent, state


# ---------------------------------------------------------------- 参数

@pytest.mark.parametrize("args,expect", [
    ({"queries": [{"query": "a", "freshness": "day"}, {"query": "b"}]}, [("a", "day"), ("b", None)]),
    ('{"queries": [{"query": "a"}]}', [("a", None)]),                    # 模型给的是 JSON 字符串
    ({"query": "旧格式", "freshness": "week"}, [("旧格式", "week")]),
    ({"queries": ["纯字符串"]}, [("纯字符串", None)]),
    ({"queries": [{"query": "a", "freshness": "hour"}]}, [("a", None)]),  # 非法 freshness 忽略
    ({"queries": [{"query": " a "}, {"query": "a"}, {"query": ""}]}, [("a", None)]),  # 去空白、去重、去空
    ({"queries": [{"query": str(i)} for i in range(5)]}, [("0", None), ("1", None), ("2", None)]),  # 最多 3 个
    ("不是 JSON", [("不是 JSON", None)]),
    ({}, []),
])
def test_parse_queries(args, expect):
    assert ws.parse_queries(args) == expect


def test_spec_matches_parser():
    fn = ws.SPEC["function"]
    assert fn["name"] == ws.NAME != "web_search"  # web_search 这个名字会被公司代理劫持
    assert fn["parameters"]["properties"]["queries"]["maxItems"] == ws.MAX_QUERIES
    assert set(fn["parameters"]["properties"]["queries"]["items"]["properties"]["freshness"]["enum"]) == set(ws.FRESHNESS)


# ---------------------------------------------------------------- 请求和结果

def test_request_format_and_freshness_mapping(post):
    sent, _ = post
    ws.search_one("今日新闻", "day")
    req = sent[0]
    assert req["url"] == ws.BOCHA_URL
    assert req["headers"]["Authorization"] == "Bearer fake-bocha"
    assert req["body"] == {"query": "今日新闻", "summary": True, "count": 8, "freshness": "oneDay"}
    ws.search_one("历史")
    assert "freshness" not in sent[1]["body"]


def test_results_rendered_for_model_and_sources_for_ui(post):
    res = ws.search_one("天气")
    assert res.ok and res.queries == ["天气"]
    assert "**1. 天气 标题0** — 站0" in res.content and "<https://site0.example/天气>" in res.content
    assert "摘" * ws.SNIPPET_CHARS in res.content and "摘" * (ws.SNIPPET_CHARS + 1) not in res.content
    assert res.sources[0] == {"title": "天气 标题0", "url": "https://site0.example/天气", "site": "站0"}


def test_normalize_tolerates_shapes():
    assert ws.normalize(None) == []
    assert ws.normalize({"data": {"webPages": {"value": "x"}}}) == []
    assert ws.normalize({"webPages": {"value": [{"title": "t", "displayUrl": "u", "snippet": "s"}]}}) == [
        {"title": "t", "url": "u", "snippet": "s", "site": ""}]
    assert len(ws.normalize(bocha_ok(20))) == ws.MAX_RESULTS


def test_no_results(post):
    _, state = post
    state["responder"] = lambda b: Resp(data={"code": 200, "data": {"webPages": {"value": []}}})
    res = ws.search_one("冷门")
    assert res.ok and "没有相关结果" in res.content and res.sources == []


# ---------------------------------------------------------------- 失败分类：要用户处理的 ok=True，网络问题 ok=False

def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    res = ws.search_one("x")
    assert res.ok and res.content.startswith("**联网失败") and ws.BOCHA_APPLY_URL in res.content


@pytest.mark.parametrize("status,text,needle", [
    (402, '{"msg": "Insufficient balance"}', "余额"),
    (403, '{"msg": "package exhausted"}', "余额"),
    (401, '{"msg": "invalid key"}', "key 无效"),
    (403, '{"msg": "forbidden"}', "key 无效"),
])
def test_auth_and_billing_errors_are_actionable(post, status, text, needle):
    _, state = post
    state["responder"] = lambda b: Resp(status, data=None, text=text)
    res = ws.search_one("x")
    assert res.ok and "联网失败" in res.content and needle in res.content


def test_business_error_with_money_words_is_actionable(post):
    _, state = post
    state["responder"] = lambda b: Resp(data={"code": 403, "msg": "account balance not enough"})
    res = ws.search_one("x")
    assert res.ok and ws.BOCHA_BILLING_URL in res.content


@pytest.mark.parametrize("responder", [
    lambda b: requests.ConnectTimeout("slow"),
    lambda b: Resp(502, data=None, text="bad gateway"),
    lambda b: Resp(200, data=None, text="<html>"),
    lambda b: Resp(data={"code": 500, "msg": "internal"}),
])
def test_network_and_server_errors_are_offline_fallback(post, responder):
    _, state = post
    state["responder"] = responder
    res = ws.search_one("x")
    assert res.ok is False and res.content.startswith("OfflineFallback")


# ---------------------------------------------------------------- 一次调用多个 query

def test_multi_query_runs_all_and_merges(post):
    sent, _ = post
    res = ws.search({"queries": [{"query": "甲"}, {"query": "乙"}]})
    assert {r["body"]["query"] for r in sent} == {"甲", "乙"} and len(sent) == 2
    assert res.ok and res.queries == ["甲", "乙"]
    assert "# 查询：甲" in res.content and "# 查询：乙" in res.content
    assert res.content.index("# 查询：甲") < res.content.index("# 查询：乙")  # 按 query 顺序，不按完成顺序


def test_multi_query_dedupes_sources(post):
    _, state = post
    same = {"code": 200, "data": {"webPages": {"value": [{"name": "同一篇", "url": "https://same.example"}]}}}
    state["responder"] = lambda b: Resp(data=same)
    res = ws.search({"queries": ["a", "b"]})
    assert [s["url"] for s in res.sources] == ["https://same.example"]


def test_multi_query_ok_if_any_succeeds(post):
    _, state = post
    state["responder"] = lambda b: Resp(data=bocha_ok()) if b["query"] == "好" else requests.ConnectionError("x")
    res = ws.search({"queries": ["好", "坏"]})
    assert res.ok and "OfflineFallback" in res.content


def test_empty_args():
    res = ws.search({})
    assert res.ok is False and "没有搜索词" in res.content
