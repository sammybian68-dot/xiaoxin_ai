"""批改作业照片：check_homework。孩子 / 家长在聊天里发了作业照片说「小新批一下」时调。
不让聊天主模型（DeepSeek）自己看图判对错：判对错要逐题、逐字比，主模型看图会漏、会编。交给批改模型（和练习模式交卷批改同一套：
Sonnet → Opus），返回逐题结果；主模型照结果说。说明在 server/prompts/check_work.md。"""

from __future__ import annotations

import json
import re

from server import store
from server.tools import printer as printer_tools

SPECS = [{"type": "function", "function": {
    "name": "check_homework",
    "description": "批改聊天里刚发的作业照片（数学、语文、英语都行）：逐题判对错，指出错在哪。"
                   "「小新批一下」「帮我看看对不对」「检查一下作业」都用这个，不要自己看图判。",
    "parameters": {"type": "object", "properties": {
        "subject": {"type": "string", "enum": ["math", "chinese", "english", "auto"], "description": "学科；看不出来填 auto"},
        "photos": {"type": "integer", "description": "批最近几张照片，默认 1（孩子一次发了好几页就填页数）"},
        "note": {"type": "string", "description": "孩子 / 家长另外说的要求，如「只看第 3 题」「这是昨天的口算」"},
    }},
}}]
NAMES = {"check_homework"}
MAX_PHOTOS = 6

PROMPT = """你是小学老师，批改孩子的作业照片（学科：{subject}）。{note}
照片里可能是练习册、作业本、试卷或口算本。逐题看：
- 数学：自己把每道题算一遍再和孩子的答案比，竖式看每一步；应用题看列式和答案。
- 语文：看字有没有写错（错别字、少笔画、多笔画）、拼音声调、填空对不对。
- 英语：看单词拼写、大小写不算错，句子漏词、多词算错。
- 题目本身看不清、或孩子写的看不清：写「看不清」，不要猜。没写的题算没做。
只输出一个 JSON，不要别的字：
{{"subject":"数学/语文/英语","items":[{{"no":"题号或题目开头几个字","got":"孩子写的","want":"正确答案","ok":true/false,"why":"错在哪（对了就空）"}}],
"right":对了几道,"total":一共几道,"unclear":看不清几道,"comment":"一两句给孩子的话，先夸再说要注意的"}}"""


def recent_photos(thread: str, n: int) -> list[dict]:
    """这条线程最近 n 张图片（新的在后）。"""
    try:
        msgs = store.get(thread)["messages"]
    except KeyError:
        return []
    out = []
    for m in reversed(msgs):
        for f in reversed(m.get("files") or []):
            if f.get("kind") == "image":
                out.append(f)
                if len(out) >= n:
                    return list(reversed(out))
    return list(reversed(out))


def grade(photos: list[dict], subject: str = "auto", note: str = "") -> dict:
    from server import practice
    from src.llm import client

    zh = {"math": "数学", "chinese": "语文", "english": "英语"}.get(subject, "看照片判断")
    parts = [{"type": "text", "text": PROMPT.format(subject=zh, note=("另外：" + note) if note else "")}]
    for f in photos:
        img = store.best_image(f["id"])
        if img:
            parts.append({"type": "image_url", "image_url": {"url": store.data_url(img, "image/jpeg")}})
    if len(parts) == 1:
        raise ValueError("照片读不出来")
    rep = client.chat_full(practice.GRADE_MODELS, [{"role": "user", "content": parts}], max_tokens=6000, timeout=180)
    m = re.search(r"\{.*\}", rep.text or "", re.S)
    if not m:
        raise ValueError("批改结果看不懂")
    g = json.loads(m.group(0))
    items = [x for x in g.get("items") or [] if isinstance(x, dict)]
    return {"subject": g.get("subject") or zh, "items": items, "right": sum(1 for x in items if x.get("ok")),
            "total": len(items), "unclear": int(g.get("unclear") or 0), "comment": str(g.get("comment") or "")[:200],
            "model": rep.model}


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}") or {}
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    thread = printer_tools.ctx.get().get("thread", "")
    if not thread:
        return "失败：这里批不了（不在聊天里）"
    n = max(1, min(int(args.get("photos") or 1), MAX_PHOTOS))
    photos = recent_photos(thread, n)
    if not photos:
        return "失败：聊天里还没有作业照片，先拍一张发过来"
    try:
        g = grade(photos, str(args.get("subject") or "auto"), str(args.get("note") or "")[:200])
    except Exception as e:  # noqa: BLE001 模型挂了、照片坏了
        return f"失败：批改没成功（{str(e)[:120]}），等一下再试"
    wrong = [x for x in g["items"] if not x.get("ok")]
    lines = [f"{g['subject']}，批了 {len(photos)} 张照片：对了 {g['right']} / {g['total']}"
             + (f"，{g['unclear']} 道看不清" if g["unclear"] else "") + "。"]
    for x in wrong[:12]:
        lines.append(f"- {x.get('no')}：写的「{x.get('got', '')}」，应为「{x.get('want', '')}」" + (f"（{x['why']}）" if x.get("why") else ""))
    if len(wrong) > 12:
        lines.append(f"- 还有 {len(wrong) - 12} 道错的")
    lines.append(f"老师的话：{g['comment']}")
    return "\n".join(lines)
