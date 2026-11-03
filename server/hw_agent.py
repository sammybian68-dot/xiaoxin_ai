"""抓班级作业：小新服务自己跑 agent（模型每一步看屏幕决定下一步），pad 上的无障碍服务替它执行
（android/xiaoxin/.../AgentService.java，经 server/agent_bus.py 下发动作、回传截图）。不需要 Mac。

    server.hw_agent.grab(day)                       抓某一天（定时任务、「立即抓取」都调这个）
    server.hw_agent.grab(since=..., until=...)       一次滑到底抓一段

为什么是 agent：QQ 是自绘界面，写死坐标的版本实测五个坑——开屏广告吃掉点击、点进推送视频页、
中途锁屏、点到「宠物」、跨屏图裁成窄带。都不是「模型读不懂」，是脚本假设了界面状态。
让模型每一步先看当前截图再决定，看到广告先关、走错就退回来，才扛得住。

动作：look / tap / swipe / back / restart_qq / capture_image / save_day / finish。
从新到旧读，**读完一天就 save_day 一天**：立刻校验、附图核验、写库（按天幂等）。中途断了不丢已存的日子。
校验不过就拒收、让模型重读——宁可没有，也不写错数据进库（错作业比没作业更糟）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import time

from server import homework, store
from server.agent_bus import PadOffline, bus

log = logging.getLogger(__name__)
GROUP = "勤惜2024级3班"
DEFAULT_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "grok-4.7"]
SUBJECTS = {"chinese", "math", "english", "other"}

SYSTEM = """你在操作一台安卓平板（屏幕已锁定横屏），用 QQ 抓班级群「勤惜2024级3班」的作业。

路径：QQ 会话列表 → 群「勤惜2024级3班」→ 群聊底部工具栏「作业」→「全部作业」列表。
列表按日期**从新到旧**：每天一个大标题（「今天」「星期一 11月2日」这样），下面是这天每个学科一张卡片。

每一步我会给你当前屏幕截图（缩到 1280 宽，tap 的坐标就用这张图里的像素坐标），有时附上能读到的控件和坐标——
有就优先用，最准。你看图决定下一步，**每次调一个工具**。

## 怎么走
- 这个 QQ 常弹开屏广告、活动弹窗、推送内容页。不是目标页面就先关掉（点 ✕ 或 back）再继续。
- 走错页面（「宠物」「频道」、某个视频）：back 退回，或 restart_qq 从头来。
- 点了没反应：先 look 等一下（可能在加载），还不行就是点偏了，换位置，别在同一点反复点。
- 列表往下看用 swipe down（一次半屏，正文才不会被切断）。

## 读一天
- 从最新的往旧的读。**读完一天就马上调 save_day 交这一天**，再往下读下一天。别攒着最后一起交。
- 一天里每个学科一条：subject、submit（卡片右上角的「无需提交 / 去完成 / 已完成」）、text（正文）。
- 正文**原样抄**：保留 1. 2. 的编号和换行，错别字照抄，不改写、不补充、不漏行。卡片太长被折叠时先点「查看完整作业」展开。
- 老师点名批评、友情提醒这类和作业条目并列的段落放 notes，学号串照抄。notes 里**只放老师写的原话**。
- 你自己的疑虑（某个字看不清、图底部被切掉了）放 unsure，不要写进 text 或 notes——家长看到的是老师的作业，不是你的批注。
- 只交截图里**真看到正文**的学科。那天只有语文和英语就只交两条，不要凭空补一条数学。
- 有的学科作业**整个就是一张图**（数学老师常发黑板照，卡片里没有文字）：先把图点开全屏 capture_image，
  text 写「见附图：」再抄上图上能看清的字（如「见附图：10/29 数回 大册P43…」），不要因为没文字就不交这科。
- 正文提到群里的文件（「完成群里发的 xxx.pdf」）把文件名放 mentions。

## 附图
- 卡片正文下面常跟着照片（订正范本、黑板照、作业纸），默认折叠只露一条淡边。
- 先展开卡片，再点图片打开**全屏预览**，确认整张图都在屏幕上，调 capture_image（写清是哪天哪科），再 back 回列表。
- 图片属于哪天哪科，看它**紧跟在哪张卡片的正文下面**。抓不到图不影响交正文。
- **每张图只抓一次**。我每步会列出「已经抓好的附图」，列表里有的不要回头再抓（早先的截图会省略，别因为看不到就以为没抓）。
- 附图要在交这一天之前抓完：save_day 会把你前面为这一天 capture 的图一起交上去。

## 结束
- 读到目标范围最早那天（或更早）、或者列表到底了，调 finish。
"""

ACTIONS = [
    {"type": "function", "function": {"name": "look", "description": "重新看一眼当前屏幕（等页面加载完、或不确定现在在哪时用）",
     "parameters": {"type": "object", "properties": {"wait_s": {"type": "number", "description": "先等几秒，默认 1.5"}}}}},
    {"type": "function", "function": {"name": "tap", "description": "点屏幕某个位置（截图里的像素坐标）",
     "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                                     "why": {"type": "string", "description": "点这里是为了什么"}},
                    "required": ["x", "y", "why"]}}},
    {"type": "function", "function": {"name": "swipe", "description": "滑动。往下看内容用 down（滑半屏），往上回看用 up",
     "parameters": {"type": "object", "properties": {"dir": {"type": "string", "enum": ["down", "up"]},
                                                     "amount": {"type": "number", "description": "滑多少屏，默认 0.5"}},
                    "required": ["dir"]}}},
    {"type": "function", "function": {"name": "back", "description": "按返回键（退出走错的页面）",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "restart_qq", "description": "强杀并重开 QQ，回到会话列表（彻底走错时用）",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "capture_image",
     "description": "把当前屏幕存成某天某科作业的附图。只在**已经点开全屏图片预览、整张图都在屏幕上**时调；"
                    "列表里折叠着只露一条边的图不要存。存完记得 back 回列表。",
     "parameters": {"type": "object", "properties": {
         "date": {"type": "string", "description": "这张图属于哪天，YYYY-MM-DD"},
         "subject": {"type": "string", "enum": ["chinese", "math", "english", "other"], "description": "属于哪科"},
         "what": {"type": "string", "description": "图上是什么（如「乐林枫的默写本订正」）"}},
      "required": ["date", "subject", "what"]}}},
    {"type": "function", "function": {"name": "save_day", "description": "读完一天就交这一天（连同前面为这天抓的附图）。交完接着往下读下一天",
     "parameters": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD"},
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "subject": {"type": "string", "enum": ["chinese", "math", "english", "other"]},
             "submit": {"type": "string"}, "text": {"type": "string"},
             "notes": {"type": "array", "items": {"type": "string"}, "description": "老师写的提醒、批评原话"},
             "mentions": {"type": "array", "items": {"type": "string"}},
             "unsure": {"type": "string", "description": "你自己拿不准的地方（只记录，不给家长看）"}},
           "required": ["subject", "text"]}}},
      "required": ["date", "items"]}}},
    {"type": "function", "function": {"name": "finish", "description": "目标范围读完了（或列表到底了），结束",
     "parameters": {"type": "object", "properties": {"why": {"type": "string"}}}}},]




class Pad:
    """pad 那一头：动作经总线发给 pad 上的无障碍服务，回来的截图存在 last。坐标换算在这里做：
    模型看的是缩到 VIEW_W 宽的截图，报的坐标按「那一张」的实际尺寸换回屏幕像素（屏幕会转，横竖都对）。"""

    def __init__(self):
        self.n = 0
        self.images: list[dict] = []      # capture_image 存下的附图：{date, subject, what, raw}
        self.last: dict = {}               # 最近一次 pad 回的结果（shot、w、h、widgets）
        self.raw = b""

    def _call(self, name: str, args: dict | None = None) -> str:
        r = bus.call(name, args or {})
        if r.get("shot"):
            self.n += 1
            self.raw = base64.b64decode(r["shot"])
            self.last = r
        return str(r.get("out") or "")

    def shot(self) -> bytes:
        if not self.raw:
            self._call("shot")
        return self.raw

    @property
    def size(self) -> tuple[int, int]:
        return int(self.last.get("w") or 2560), int(self.last.get("h") or 1600)

    def widgets(self) -> str:
        k = VIEW_W / self.size[0]
        out, seen = [], set()
        for w in self.last.get("widgets") or []:
            t = str(w.get("t") or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(f"「{t}」({round(w['x'] * k)},{round(w['y'] * k)})")
        return "、".join(out[:40])

    def act(self, name: str, args: dict) -> str:
        """执行一个动作，返回给模型看的一句话。截图在 self.raw（执行完 pad 顺手截好传回来了）。"""
        w, h = self.size
        k = w / VIEW_W
        if name == "look":
            return self._call("shot", {"wait_s": max(0.0, min(float(args.get("wait_s") or 1.5), 8))})
        if name == "tap":
            x, y = round(int(args["x"]) * k), round(int(args["y"]) * k)
            return self._call("tap", {"x": x, "y": y}) + f" ({args['x']}, {args['y']})"
        if name == "swipe":
            amt = max(0.2, min(float(args.get("amount") or 0.5), 1.0))
            dy = int(h * amt * 0.72)
            top, bot = int(h * 0.32), int(h * 0.78)   # 避开顶部标题栏和底部导航条
            y1, y2 = (bot, bot - dy) if args.get("dir") != "up" else (top, top + dy)
            self._call("swipe", {"x1": w // 2, "y1": y1, "x2": w // 2, "y2": y2, "ms": 420})
            return f"往{'下' if args.get('dir') != 'up' else '上'}滑了 {amt} 屏"
        if name == "back":
            return self._call("back")
        if name == "capture_image":
            raw = self.raw or self.shot()
            self.images.append({"date": str(args.get("date") or ""), "subject": args.get("subject") or "other",
                                "what": str(args.get("what") or ""), "raw": raw})
            return f"存了一张附图（{args.get('date')} {args.get('subject')}：{args.get('what')}）"
        if name == "restart_qq":
            return self._call("open_qq") + "，现在应该在会话列表"
        return f"不认识的动作 {name}"


def models() -> list[str]:
    want = (store.settings().get("agent") or {}).get("model")
    if not want:
        return DEFAULT_MODELS
    return [str(want)] + [m for m in DEFAULT_MODELS if m != want]

VIEW_W = 1280  # 给模型看的截图宽度。模型报的坐标都在这张图里，点之前按比例换回屏幕坐标


def shot_part(raw: bytes) -> dict:
    """截图缩到 VIEW_W 宽（JPEG）再给模型。不缩的话模型内部会自己缩，它报的坐标和原图对不上——
    2026-11-03 实测：「作业」在 (1840,1341)，模型一直报 (1718,1266)，差 7%，点在旁边空白上点了 20 次。"""
    from PIL import Image

    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im = im.resize((VIEW_W, round(im.height * VIEW_W / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


def thumb(raw: bytes, w: int = 420) -> str:
    """给「作业 agent」面板看的小截图（data URL）。420 宽、质量 55，一步约 20–30 KB。"""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im = im.resize((w, max(1, round(im.height * w / im.width))))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=55)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return ""


def hint(pad: Pad) -> str:
    w = pad.widgets()
    out = (f"\n能读到的控件（坐标是这张截图里的，点它们用这个坐标最准）：{w}" if w else "")
    if pad.images:
        got = "；".join(f"{x['date']} {x['subject']}：{x['what']}" for x in pad.images)
        out += f"\n已经抓好、还没交的附图（不用再抓）：{got}"
    return out



def ask(msgs: list[dict], chain: list[str]) -> dict:
    """问模型下一步，返回它的 assistant 消息（带 tool_calls）。流式拿不到 tool_calls（2026-11-03 实测），用非流式。
    用真的 tool_call / tool 消息记历史：之前把动作写成「[动作] tap …」文字塞回去，模型学着用文字回答、不调工具，
    白白浪费了好几步。"""
    import requests

    from src.llm import client

    last = ""
    for model in chain:
        body = {"model": model, "max_tokens": 6000, "tools": ACTIONS,
                "messages": [{"role": "system", "content": SYSTEM}, *msgs]}
        url, headers, body = client._endpoint(model, body)
        for attempt in range(4):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=300)
            except requests.RequestException as e:
                # 网断一下（实测一次 DNS 解析失败就把一整轮丢了）：等一等再试，不急着换模型
                last = repr(e)[:200]
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 200:
                m = r.json()["choices"][0]["message"]
                return {"role": "assistant", "content": m.get("content") or None, "tool_calls": m.get("tool_calls") or []}
            last = f"{model} HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code < 500 and r.status_code != 429:
                break
    raise RuntimeError(f"模型都失败了：{last}")


def day_range(since: str, until: str) -> list[str]:
    t, end, out = time.mktime(time.strptime(since, "%Y-%m-%d")), time.mktime(time.strptime(until, "%Y-%m-%d")), []
    while t <= end + 3600:
        out.append(time.strftime("%Y-%m-%d", time.localtime(t)))
        t += 86400
    return out


KEEP = 48  # 历史最多留多少条（一次滑到底抓几十天要走几百步，全留着又慢又贵）


def trim(msgs: list[dict], saved: list[str]) -> list[dict]:
    """历史太长：留开头的目标那条 + 最近 KEEP 条，中间换成一句「已经存好了哪几天」。
    切点要落在一条普通 user 消息上（不能把 assistant 的 tool_calls 和它的 tool 结果拆开）。"""
    if len(msgs) <= KEEP + 1:
        return msgs
    cut = len(msgs) - KEEP
    while cut < len(msgs) and not (msgs[cut]["role"] == "user"):
        cut += 1
    if cut >= len(msgs):
        return msgs
    done = "、".join(saved) or "还没有"
    note = {"role": "user", "content": f"（前面的步骤省略了。已经存好的日子：{done}。接着往下读更早的日子。）"}
    head = msgs[0]
    first = msgs[cut]
    if isinstance(first.get("content"), str):
        first = {"role": "user", "content": note["content"] + "\n" + first["content"]}
        return [head, {"role": "assistant", "content": "好的。"}, first, *msgs[cut + 1:]]
    return [head, {"role": "assistant", "content": "好的。"}, {**first, "content": [{"type": "text", "text": note["content"]}, *first["content"]]},
            *msgs[cut + 1:]]


def run(pad: Pad, since: str, until: str, max_steps: int, on_day) -> dict:
    """agent 循环：从最新往旧读，读完一天就 save_day（on_day 立刻校验写库），直到 finish 或步数用完。
    中途失败也不丢已经存好的那几天。返回 {"saved": [...], "why": 结束原因}。"""
    pad._call("wake")            # 孩子房间里 pad 平时熄屏：先点亮、过掉锁屏
    pad.act("restart_qq", {})
    # 日期写全、强调年份：QQ 列表只写「11月3日」不带年份，模型会自己猜年份（实测猜成 2024、2025 都有）
    today = time.strftime("%Y-%m-%d")
    yr = f"年份一律是 {today[:4]} 年（列表里只写月日，别自己猜年份）。"
    goal = (f"目标：抓 {since} 到 {until} 每一天的作业（今天是 {today}）。{yr}" if since != until
            else f"目标：只抓 {since} 这一天的作业（今天是 {today}）。{yr}这一天交完就 finish，不要往下读更早的日子。")
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": goal + "刚重开了 QQ，这是当前屏幕。走到作业列表，从新到旧读，读完一天 save_day 一天，"
                                        f"读到 {since} 或更早就 finish。" + hint(pad)},
        shot_part(pad.shot())]}]
    chain = models()
    log.info("hw agent %s~%s 用模型 %s", since, until, chain[0])
    saved: list[str] = []
    steps: list[dict] = []
    idle = 0
    since_save = 0       # 距离上一次 save_day 走了几步：一天里打转太久就提醒它先交（实测 10/15 一天走了 30 多步）
    for step in range(max_steps):
        try:
            m = ask(msgs, chain)
        except RuntimeError as e:
            return {"saved": saved, "why": str(e), "steps": steps}
        calls = m["tool_calls"]
        if not calls:
            # 没调工具：提醒一次，再不调就算这一步空转
            idle += 1
            msgs.append({"role": "assistant", "content": m["content"] or "（没说话）"})
            msgs.append({"role": "user", "content": "请调一个工具（look / tap / swipe / back / restart_qq / capture_image / save_day / finish），不要只回文字。"})
            steps.append({"i": step + 1, "name": "（没调工具）", "said": (m["content"] or "")[:300]})
            if idle >= 3:
                return {"saved": saved, "why": "模型连着三次不调工具", "steps": steps}
            continue
        idle = 0
        c = calls[0]
        name = c["function"]["name"]
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        tag = args.get("why") or args.get("date") or args.get("dir") or args.get("what") or ""
        log.info("hw agent [%d] %s %s", step + 1, name, tag)
        msgs.append({"role": "assistant", "content": m["content"], "tool_calls": [c]})
        if name == "finish":
            steps.append({"i": step + 1, "name": name, "why": str(args.get("why") or ""), "said": (m["content"] or "")[:300]})
            return {"saved": saved, "why": str(args.get("why") or "读完了"), "steps": steps}
        since_save += 1
        if name == "save_day":
            out = on_day(args, pad)
            if out.startswith("✓"):
                saved.append(str(args.get("date")))
                since_save = 0
            log.info("hw agent save_day %s: %s", args.get("date"), out)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out + " 继续往下读下一天。"})
            raw = pad.raw
            steps.append({"i": step + 1, "name": name, "why": tag, "out": out[:300], "said": (m["content"] or "")[:200],
                          "args": {k: v for k, v in args.items() if k != "items"}, "shot": thumb(raw)})
            msgs.append({"role": "user", "content": [{"type": "text", "text": "这是现在的屏幕。" + hint(pad)}, shot_part(raw)]})
        else:
            out = pad.act(name, args)
            if since_save == 25:
                out += ("。（这一天已经走了 25 步：附图抓够了就先 save_day 交掉。正文最重要，"
                        "实在抓不到的图不抓了，别在同一天里来回翻。）")
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
            raw = pad.raw                # pad 执行完顺手截好传回来了
            steps.append({"i": step + 1, "name": name, "why": tag, "out": out[:300], "said": (m["content"] or "")[:200],
                          "args": args, "shot": thumb(raw)})
            msgs.append({"role": "user", "content": [{"type": "text", "text": "这是现在的屏幕。" + hint(pad)}, shot_part(raw)]})
        # 只留最近 3 张截图（图很贵），更早的换成一句话；tool 消息和 assistant 的 tool_calls 必须成对留着
        imgs = [i for i, x in enumerate(msgs) if x["role"] == "user" and isinstance(x.get("content"), list)]
        for i in imgs[:-3]:
            msgs[i] = {"role": "user", "content": "（这一步的截图省略了）"}
        msgs = trim(msgs, saved)
    return {"saved": saved, "why": f"走了 {max_steps} 步，没走完", "steps": steps}


SUBJECTS = {"chinese", "math", "english", "other"}

VERIFY_PROMPT = """这是平板截图。判断：屏幕上是不是**一张完整的照片 / 图片在全屏预览**（例如作业本照片、黑板照片、试卷照片），
图片完整、没被裁掉一大块，不是 QQ 的列表页、聊天页、广告、视频或别的界面。
只输出 JSON：{"ok": true/false, "why": "一句话"}"""


def verify_image(raw: bytes, chain: list[str]) -> tuple[bool, str]:
    """附图入库前独立核验一次（不是 agent 自己说了算）：上次附图全是坏的——裁成窄带、截成列表页——
    就是因为没人检查「存下来的到底是什么」。"""
    import requests

    from src.llm import client

    for model in chain[:2]:
        body = {"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": [
            {"type": "text", "text": VERIFY_PROMPT}, shot_part(raw)]}]}
        url, headers, body = client._endpoint(model, body)
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
            m = re.search(r"\{.*\}", r.json()["choices"][0]["message"].get("content") or "", re.S)
            d = json.loads(m.group(0)) if m else {}
            return bool(d.get("ok")), str(d.get("why") or "")
        except Exception as e:  # noqa: BLE001
            last = repr(e)
    return False, f"核验失败：{last}"


def crop_preview(raw: bytes) -> bytes:
    """全屏预览截图裁掉状态栏和底部导航条，再把四周纯黑边去掉，剩下图片本身。"""
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(raw)).convert("RGB")
    W, H = im.size
    im = im.crop((0, int(H * 0.04), W, int(H * 0.965)))
    bbox = ImageOps.invert(im.convert("L")).point(lambda v: 255 if v < 235 else 0).getbbox()  # 非黑区域
    if bbox and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 0.2 * im.width * im.height:
        im = im.crop(bbox)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fix_year(day: str, since: str, until: str) -> str:
    """模型偶尔把年份写错（2026-11-03 实测写成 2024-11-03，那张附图被当成范围外丢了）：
    月日在范围里、只是年份不对的，按范围纠正。范围跨年时取能对上的那一年。"""
    day = str(day or "").strip()
    if not DAY.match(day) or since <= day <= until:
        return day
    md = day[5:]
    for y in sorted({since[:4], until[:4]}):
        cand = f"{y}-{md}"
        if since <= cand <= until:
            return cand
    return day


def check(args: dict, since: str, until: str) -> tuple[str, list[dict], str]:
    """校验 save_day 交上来的一天：不合格一律拒收（宁可没有，也不写错数据进库）。返回 (日期, 条目, 拒收原因)。"""
    day = fix_year(str(args.get("date") or "").strip(), since, until)
    if not DAY.match(day):
        return day, [], f"日期格式不对：{day}"
    if day > time.strftime("%Y-%m-%d"):
        return day, [], f"{day} 还没到"
    items = []
    for it in args.get("items") or []:
        sub = str(it.get("subject") or "").strip()
        text = str(it.get("text") or "").strip()
        if sub not in SUBJECTS:
            return day, [], f"学科不对：{sub}"
        if not text or re.fullmatch(r"[（(]?看不清[）)]?", text):
            return day, [], f"{sub} 没读到正文（如果这科作业整个就是一张图，text 写「见附图」加上图上能看清的字，并先把图 capture 下来）"
        items.append({"subject": sub, "submit": str(it.get("submit") or ""), "text": text,
                      "notes": [str(x) for x in it.get("notes") or [] if str(x).strip()],
                      "mentions": [str(x) for x in it.get("mentions") or [] if str(x).strip()],
                      "unsure": str(it.get("unsure") or "").strip()[:200],
                      "images": []})
    if not items:
        return day, [], "一条作业都没读到"
    if len({it["subject"] for it in items}) != len(items):
        return day, [], "同一个学科出现了两条"
    return day, items, ""


class Saver:
    """save_day 的处理：校验 → 这天的附图核验、裁边、去重、上传 → 按天写库（幂等）。每交一天立刻落库。"""

    def __init__(self, since: str, until: str, dry_run: bool, only_missing: bool):
        self.since, self.until, self.dry, self.only_missing = since, until, dry_run, only_missing
        self.chain = models()
        self.done: dict[str, int] = {}     # 存好的：日期 → 附图张数
        self.rejected: dict[str, str] = {}
        self.have = set() if not only_missing else {d["day"] for d in homework.range_days(since, until)}

    def __call__(self, args: dict, pad: Pad) -> str:
        day, items, bad = check(args, self.since, self.until)
        if bad:
            self.rejected[day] = bad
            return f"× {day} 没收：{bad}。重新看清楚这一天，读对了再 save_day 一次。"
        if not (self.since <= day <= self.until):
            if self.done and day < self.since:   # 范围里的都交了、已经读到更早的了：别再往下翻
                return f"· {day} 比要抓的范围（{self.since}～{self.until}）还早，范围里的都存好了，现在调 finish。"
            return f"· {day} 不在要抓的范围（{self.since}～{self.until}），跳过。"
        if day in self.have:
            return f"· {day} 已经有记录，跳过。"
        for x in pad.images:
            x["date"] = fix_year(x["date"], self.since, self.until)
        imgs = [x for x in pad.images if x["date"] == day]
        pad.images = [x for x in pad.images if x["date"] != day]
        seen, n_img, notes = set(), 0, []
        for img in imgs:
            data = crop_preview(img["raw"])
            h = hashlib.md5(data).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            it = next((x for x in items if x["subject"] == img["subject"]), None)
            if it is None:
                notes.append(f"一张图说是 {img['subject']} 的，但这天没有这科，丢了")
                continue
            ok, why = verify_image(img["raw"], self.chain)
            if not ok:
                notes.append(f"一张图没过核验（{why}），丢了")
                continue
            if not self.dry:
                fid, path = store.allocate()
                path.write_bytes(data)
                meta = store.commit(fid, f"{day}-{img['subject']}-{n_img + 1}.jpg", "image/jpeg", len(data), owner="xiaoxin")
                it["images"].append(meta["id"])
            n_img += 1
        # 「见附图」的科目：附图一张都没收下，这科就等于没有内容，拒收让它补抓图
        empty = [x["subject"] for x in items if x["text"].startswith("见附图") and not (x["images"] or self.dry and n_img)]
        if empty:
            self.rejected[day] = f"{'、'.join(empty)} 写的「见附图」，但没收到图"
            return (f"× {day} 没收：{'、'.join(empty)} 写的是「见附图」，可是这科没有抓到图（或图没过核验）。"
                    f"先把这科的图点开全屏 capture_image，再 save_day。")
        if not self.dry:
            homework.save_day(day, items, source="qq-agent", note=f"agent 看屏抓的 {time.strftime('%m-%d %H:%M')}")
        self.done[day] = n_img
        self.rejected.pop(day, None)
        subs = "、".join(x["subject"] for x in items)
        return f"✓ {day} 存好了：{subs}，附图 {n_img} 张" + (f"（{'；'.join(notes)}）" if notes else "") + ("［dry-run］" if self.dry else "")




_running = False


def busy() -> bool:
    return _running


def grab(day: str = "", *, since: str = "", until: str = "", trigger: str = "manual",
         max_steps: int = 0, only_missing: bool = False) -> dict:
    """跑一轮：抓某一天（默认今天），或 since～until 一段。同步跑（几分钟），调用方放线程里。
    同一时间只跑一轮（pad 只有一台）。跑完写一条运行记录（带每一步和小截图，「作业 agent」面板看）。
    返回 {"ok", "saved", "rejected", "why", "secs"}。"""
    global _running
    if _running:
        return {"ok": False, "saved": [], "rejected": {}, "why": "已经在抓了", "secs": 0}
    _running = True
    today = time.strftime("%Y-%m-%d")
    until = until or day or today
    since = since or day or today
    days = day_range(since, until)
    steps_max = max_steps or (40 if len(days) == 1 else 40 + 12 * len(days))
    pad = Pad()
    saver = Saver(since, until, False, only_missing)
    t0 = time.monotonic()
    try:
        res = run(pad, since, until, steps_max, saver)
    except PadOffline as e:
        res = {"saved": [], "why": str(e), "steps": []}
    except Exception as e:  # noqa: BLE001  抓作业出错不能把服务带崩
        log.exception("hw agent")
        res = {"saved": [], "why": f"出错了：{e}", "steps": []}
    finally:
        _running = False
    secs = round(time.monotonic() - t0)
    out = {"ok": bool(saver.done), "saved": sorted(saver.done), "rejected": saver.rejected, "why": res["why"], "secs": secs}
    homework.log_run({"since": since, "until": until, **out, "shots": pad.n, "model": saver.chain[0],
                      "trigger": trigger, "steps": res.get("steps") or []})
    log.info("hw agent 结束 %s：存了 %s（%ss）", res["why"], out["saved"], secs)
    return out
