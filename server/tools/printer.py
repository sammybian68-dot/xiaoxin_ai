"""出练习纸、打印。说明（怎么选黑白彩色、页码、份数）在 server/prompts/print.md。

工具在 _llm_thread 里跑（线程池），要知道「这条线程是谁的、现在的对话里有哪些文件」：用 context() 设进来。
打印任务排好后推给孩子的 pad（hub 的 print 事件），pad 发给家里打印机，结果回来再推给大家。
"""

from __future__ import annotations

import contextvars
import json
import re

from server import printing, store, worksheet

ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("print_ctx", default={})

from server import exercises  # noqa: E402

SPECS = [
    {"type": "function", "function": {
        "name": "make_exercise",
        "description": ("出题：按学科和题型出一套练习（A4 卷子），屏幕上显示预览，可以打印。数学题由程序按规则随机出，答案一定对；"
                        "语文的词语、作文题由你按年级和课文挑好传进来。题型：\n" + exercises.catalog()),
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string", "enum": list(exercises.SUBJECTS), "description": "学科：math 数学，chinese 语文"},
            "topic": {"type": "string", "enum": list(exercises.TOPICS), "description": "题型，见上面的列表"},
            "count": {"type": "integer", "description": "题数（不给用这个题型的默认，口算一般 50–100）"},
            "title": {"type": "string", "description": "卷子标题，可空（用题型默认的，如「末尾带 0 的乘除法口算练习」）"},
            "ops": {"type": "string", "description": "数学：只要哪些运算，如「×」「÷」「+-」「+-×÷」"},
            "max": {"type": "integer", "description": "arithmetic：加减是结果上限（20、100、1000），乘除 9 = 表内"},
            "carry": {"type": "boolean", "description": "arithmetic：加减是否允许进位退位，一年级上册用 false"},
            "words": {"type": "array", "items": {"type": "string"}, "description": "pinyin_words / dictation：词语"},
            "pinyin": {"type": "object", "description": "pinyin_words：多音字、查不到的给拼音，如 {\"银行\": \"yín háng\"}"},
            "topic_text": {"type": "string", "description": "essay：作文题目"},
            "words_count": {"type": "integer", "description": "essay：字数，100–800，默认 300"},
            "hint": {"type": "string", "description": "essay：写作提示（只印在答案页，给家长看）"},
        }, "required": ["subject", "topic"]},
    }},
    {"type": "function", "function": {
        "name": "print_file",
        "description": "用家里的打印机打印聊天里的一份东西（图片、PDF、练习纸）。黑白还是彩色、打哪几页、几份按 print 规则自己选。",
        "parameters": {"type": "object", "properties": {
            "file": {"type": "string",
                     "description": "要打的东西：工具列表里的文件 id（f_…）或练习纸 id（w_…）；不确定就填 latest（最近一份）"},
            "color": {"type": "string", "enum": ["auto", "color", "bw"],
                      "description": "auto 按材料自己判断（默认）；color 彩色；bw 黑白"},
            "pages": {"type": "string", "description": "页码，如「1-3,5」「第 2 页」「前两页」；空 = 全部"},
            "copies": {"type": "integer", "description": "份数 1–5，默认 1"},
            "fit": {"type": "string", "enum": ["fit", "fill"],
                    "description": "fit 整张放进纸里留白边（默认）；fill 铺满裁边（照片想要满版时）"},
            "answers": {"type": "boolean", "description": "练习纸：是否连答案页一起打（默认 false，只有家长要时才打）"},
        }, "required": ["file"]},
    }},
    {"type": "function", "function": {
        "name": "printer_status",
        "description": "家里打印机现在怎么样：开没开、有没有纸、墨还剩多少、有没有卡纸。问「打印机有墨吗」「还有纸吗」时调。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_printables",
        "description": "列出这段对话里能打印的东西：文件名、类型、页数、彩色多少、是谁发的。决定打哪个、黑白还是彩色前先看这个。",
        "parameters": {"type": "object", "properties": {}},
    }},
]
NAMES = {"make_exercise", "make_worksheet", "print_file", "list_printables", "printer_status"}  # make_worksheet：老名字，历史里有
WID = re.compile(r"w_([0-9a-f]{16})")  # 工具结果里的练习纸 id


def _items(thread: str) -> list[dict]:
    """这条线程里能打的：文件（图片、PDF、Office）+ 练习纸；新的在前，最多 12 个。"""
    from server.family import members

    try:
        msgs = store.get(thread)["messages"]
    except KeyError:
        return []
    out, seen = [], set()
    for m in reversed(msgs):
        for a in reversed(m.get("actions") or []):
            m_ = WID.search(a.get("result") or "") if a.get("name") in ("make_exercise", "make_worksheet") else None
            if m_:
                wid = m_.group(1)
                if wid not in seen:
                    seen.add(wid)
                    out.append({"ref": "w_" + wid, "at": m.get("created", 0)})
        for f in reversed(m.get("files") or []):
            if f["id"] not in seen:
                seen.add(f["id"])
                who = m.get("from") or ""
                try:
                    who = members.get(who)["name"] if who and who not in ("admin", "xiaoxin") else who
                except KeyError:
                    pass
                out.append({"ref": "f_" + f["id"], "at": m.get("created", 0), "from": who})
        if len(out) >= 12:
            break
    return out


def _resolve(ref: str, thread: str) -> tuple[str, dict | None]:
    """file 参数 → (文件 id, 练习纸)。latest = 最近一份。"""
    ref = str(ref or "").strip()
    if ref in ("", "latest", "最近", "刚才的") and ctx.get().get("made"):
        ref = "w_" + ctx.get()["made"]  # 这一轮刚出的题（还没存进对话）
    if ref in ("", "latest", "最近", "刚才的"):
        items = _items(thread)
        if not items:
            raise printing.PrintError("这段对话里还没有能打印的东西")
        ref = items[0]["ref"]
    if ref.startswith("w_"):
        try:
            ws = worksheet.get(ref[2:])
        except KeyError:
            raise printing.PrintError(f"没有这份练习纸：{ref}")
        return worksheet.page_file(ws, kid_name=ctx.get().get("kid_name", ""))["id"], ws
    fid = ref[2:] if ref.startswith("f_") else ref
    if store.file_info(fid) is None:
        raise printing.PrintError(f"没有这个文件：{ref}")
    if fid not in {i["ref"][2:] for i in _items(thread) if i["ref"].startswith("f_")}:
        raise printing.PrintError("这个文件不在这段对话里")
    return fid, None


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    c = ctx.get()
    thread, kid = c.get("thread", ""), c.get("kid", "")
    try:
        if name in ("make_exercise", "make_worksheet"):
            topic = args.pop("topic", "") or args.pop("kind", "")
            if "topic_text" in args:
                args["topic"] = args.pop("topic_text")  # 作文题目（topic 这个名字被题型占了）
            ws = worksheet.create(topic, subject=str(args.pop("subject", "") or ""), title=str(args.pop("title", "") or ""),
                                  for_kid=kid, **args)
            worksheet.page_file(ws, kid_name=c.get("kid_name", ""))
            c["made"] = ws["id"]
            return (f"已出好 {worksheet.summary(ws)}（w_{ws['id']}），预览在屏幕上。\n"
                    f"播报：{ws['title']}出好了，要打印就说「打印出来」。")
        if name == "printer_status":
            return printing.describe_status(printing.status())
        if name == "list_printables":
            items = _items(thread)
            if not items:
                return "这段对话里还没有能打印的东西"
            lines = []
            for it in items:
                if it["ref"].startswith("w_"):
                    ws = worksheet.get(it["ref"][2:])
                    lines.append(f"- {it['ref']} 练习纸「{ws['title']}」1 页，黑白就行（小新出的）")
                    continue
                try:
                    info = printing.inspect(it["ref"][2:])
                except printing.PrintError as e:
                    lines.append(f"- {it['ref']} 现在打不了：{e}")
                    continue
                kind = {"image": "图片", "pdf": "PDF", "office": "Office 文档（打不了）", "other": "文件（打不了）"}[info["kind"]]
                pages = f"{info['pages']} 页" if info["pages"] else ""
                lines.append(f"- {it['ref']} {kind}「{info['name']}」{pages}，彩色占比 {info['color']:.0%}"
                             + (f"，{it['from']}发的" if it.get("from") else ""))
            return "能打印的（新的在前）：\n" + "\n".join(lines)
        if name == "print_file":
            fid, ws = _resolve(args.get("file"), thread)
            color = {"color": True, "bw": False}.get(str(args.get("color") or "auto"))
            if ws is not None and color is None:
                color = False  # 练习纸都是黑白排的
            batch = c.setdefault("batch", store._new_id())  # 这一轮里打的几个算一批，全部打完说一次总结
            sent = c.setdefault("sent", {})                  # 这一轮已经交过的：(文件, 页码) → 任务
            key = (fid, str(args.get("pages") or ""))
            if key in sent:
                # 同一轮又交了一遍同一份（2026-11-01 实测：先交练习纸、又带 answers 再交一遍，多出一张）：
                # 不再打第二份；要答案页就只补答案页
                if ws is not None and args.get("answers") and not sent[key].get("answers"):
                    ans = worksheet.page_file(ws, answers=True, kid_name=c.get("kid_name", ""))
                    _push(printing.create_job(thread, ans["id"], color=False, by=c.get("by", ""), batch=batch))
                    sent[key]["answers"] = True
                    return "已补上答案页 1 张（练习纸刚才已经交了，不再重复打）。\n播报：好的，答案页也一起打。"
                return "这份刚才已经交给打印机了，不重复打。要多打几份就用 copies。\n播报：已经在打了。"
            job = printing.create_job(thread, fid, pages=args.get("pages"), color=color,
                                      copies=args.get("copies") or 1, fit=str(args.get("fit") or "fit"), by=c.get("by", ""),
                                      batch=batch)
            sent[key] = {"job": job["id"], "answers": bool(ws is not None and args.get("answers"))}
            extra = []
            if ws is not None and args.get("answers"):
                ans = worksheet.page_file(ws, answers=True, kid_name=c.get("kid_name", ""))
                extra.append(printing.create_job(thread, ans["id"], color=False, by=c.get("by", ""), batch=batch))
            for j in [job, *extra]:
                _push(j)
            how = "自动选了" + ("彩色" if job["color"] else "黑白") if job["auto_color"] else ("彩色" if job["color"] else "黑白")
            ids = " ".join("j_" + j["id"] for j in [job, *extra])  # 界面按这个认打印条（不念）
            return (f"已发给打印机：{printing.describe(job)}（{how}）" + ("，另加答案页 1 张" if extra else "") + f"。任务 {ids}\n"
                    f"播报：好的，{'彩色' if job['color'] else '黑白'}打印 {job['sheets']} 张，马上出纸。")
    except printing.PrintError as e:
        return f"失败：{e}"
    except worksheet.WorksheetError as e:
        return f"失败：{e}"
    except KeyError as e:
        return f"失败：没有 {e}"
    return f"失败：没有工具 {name}"


def _push(job: dict) -> None:
    """推给能进这条线程的设备；只有孩子的 pad（在家、连着打印机）会去打。"""
    from server.family import hub

    hub.publish(job["thread"], {"type": "print", "job": job})
