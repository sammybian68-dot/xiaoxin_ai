"""英语场景对话的工具：english_scene（练 / 继续 / 按推荐挑一课）、english_scene_new（现场生成一课，后台做）、
english_scene_board（看板）。界面按工具名出卡片（static/english_scene.js）。说明在 server/prompts/english_scene.md。"""

from __future__ import annotations

import json

from server.english import api as eapi, drill
from server.tools import printer as printer_tools

SPECS = [
    {"type": "function", "function": {
        "name": "english_scene",
        "description": "英语口语场景对话练习（选角色，和小新轮流说，读不准会让重说）：打开一课。不指定就按熟练度和复习时间推荐最该练的那课；"
                       "「继续英语场景练习」接着上次暂停的。",
        "parameters": {"type": "object", "properties": {
            "lesson": {"type": "string", "description": "孩子说的课名，原话就行（「冰淇淋店」「小熊那个」「独库公路」）；没指定就不填（小新推荐）"},
            "resume": {"type": "boolean", "description": "「继续上次的 / 接着练」填 true"},
        }},
    }},
    {"type": "function", "function": {
        "name": "english_scene_new",
        "description": "孩子要一个新的英语对话场景（「生成一个在独库公路上开车看美景的对话」）：后台画画、写剧本、配音，大约一分钟，好了发到聊天里。",
        "parameters": {"type": "object", "properties": {
            "ask": {"type": "string", "description": "孩子想要的场景，用她的原话（中文也行）"},
        }, "required": ["ask"]},
    }},
    {"type": "function", "function": {
        "name": "english_scene_board",
        "description": "英语小剧场的学习情况：每课练到什么程度、最近练了多少、哪些词总读不准。屏幕上出看板。",
        "parameters": {"type": "object", "properties": {}},
    }},
]
NAMES = {"english_scene", "english_scene_new", "english_scene_board"}


def _kid() -> dict | None:
    from server.family import members

    kid = printer_tools.ctx.get().get("kid", "")
    try:
        return members.get(kid) if kid else None
    except KeyError:
        return None


_FILLER = set("一下那个的这练再昨天今天对话场景小剧场英语课吧呢啊我想要个上次")


def find(q: str, kid: str = "") -> dict | None:
    """按孩子的说法找课：id、中文名、英文名、角色名、生成时她的原话。说法常常不全（「小熊那个」「独库那个」）：
    去掉「那个、对话、练一下」这类字，剩下的字和每课的名字比，重合最多的那课。一样多时：她最近练过的优先
    （「小熊那个」冰淇淋店、水果摊、玩具店都有熊先生，她说的多半是刚练过的那个），都没练过取课程靠前的。"""
    q = q.strip().lower()
    if not q:
        return None
    Ls = drill.lessons()
    for L in Ls:
        if q == L["id"] or q == L["zh"] or q == L["name"].lower():
            return L
    want = {c for c in q if c not in _FILLER and not c.isspace()}
    words = set(q.split())

    def score(L):
        hay = " ".join([L["zh"], L["name"].lower(), L.get("ask") or "", *[r["zh"] + " " + r["name"].lower() for r in L["roles"]]])
        return sum(c in hay for c in want) + 3 * sum(len(w) > 2 and w in hay for w in words)
    last = {}
    if kid:
        for s_ in drill.sessions(kid):
            last.setdefault(s_["lesson"], s_["started"])
    best = max(Ls, key=lambda L: (score(L), last.get(L["id"], 0), -L.get("order", 999)), default=None)
    return best if best and score(best) >= 1 else None


def catalog_text() -> str:
    """课程列表（给模型认课名用）。"""
    return "；".join(f"{L['id']}={L['zh']}（{L['name']}）" for L in drill.lessons())


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    k = _kid()
    if not k:
        return "失败：这里不能练英语对话（不在孩子的聊天里）"
    if name == "english_scene_board":
        b = drill.board(k["id"])
        lv = [L for L in b["lessons"] if L.get("level")]
        return (f"屏幕上出了英语小剧场看板：一共 {len(b['lessons'])} 课，练过 {len(lv)} 课，掌握 {b['total']['mastered']} 课，"
                f"连续练了 {b['streak']} 天。\n播报：看板在屏幕上了。")
    if name == "english_scene_new":
        ask = str(args.get("ask") or "").strip()[:200]
        if not ask:
            return "失败：没说想要什么场景，先问一句"
        if len(eapi.GENERATING.get(k["id"], {})) >= 2:
            return "失败：已经在做两个场景了，等做好再要新的"
        eapi.generate_later(k, ask)
        return "已开始做新场景（画画、写对话、配音），大约一分钟，做好会发到聊天里。\n播报：好的，正在为你写对话场景，稍等一分钟。"
    # english_scene
    cur = drill.current(k["id"])
    if args.get("resume") and cur:
        L = drill.lesson(cur["lesson"])
        return f"屏幕上接着上次的练习：「{L['zh']}」，演 {cur['role']}。[en:{cur['lesson']}:{cur['id']}]\n播报：好，我们接着练「{L['zh']}」。"
    lid = str(args.get("lesson") or "").strip()
    if lid:
        L = find(lid, k["id"])
        if not L:
            return f"失败：没找到这一课（{lid}）。现有的课：{catalog_text()}"
        return f"屏幕上打开了「{L['zh']}」，选一个角色就开始。[en:{L['id']}]\n播报：好，我们来练「{L['zh']}」，选一个角色吧。"
    rec = drill.recommend(k["id"])
    if not rec["pick"]:
        return "今天推荐的都练过了。\n播报：今天的都练过啦，真棒！想再练哪一课可以告诉我。"
    L = drill.lesson(rec["pick"]["id"])
    return (f"推荐「{L['zh']}」：{rec['why']}屏幕上打开了。[en:{L['id']}]\n"
            f"播报：今天我们练「{L['zh']}」。{rec['why']}")
