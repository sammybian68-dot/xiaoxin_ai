"""所有工具：给模型的 SPECS，按名字分发执行。加功能：写 server/tools/<x>.py（SPECS + run）、
server/prompts/<x>.md，在这里登记模块，在 prompts.ORDER 登记 md。"""

from __future__ import annotations

from server.tools import clock as clock_tools
from server.tools import time as time_tools
from src.search import web_search

SPECS = [web_search.SPEC, *time_tools.SPECS, *clock_tools.SPECS]
SEARCH = web_search.NAME
# 本地工具（不联网、不计搜索次数）：名字 → 执行函数
LOCAL = {**{n: time_tools.run for n in time_tools.NAMES}, **{n: clock_tools.run for n in clock_tools.NAMES}}


def run_local(name: str, arguments: str) -> str:
    return LOCAL[name](name, arguments)


def listing() -> list[dict]:
    """设置页用：每个工具的名字、说明、参数 schema。"""
    return [{"name": s["function"]["name"], "description": s["function"]["description"],
             "parameters": s["function"]["parameters"]} for s in SPECS]
