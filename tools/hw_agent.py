"""抓今天的班级作业：让 opus 亲自看屏幕操作 pad（agent 循环），不走写死的点击流程。

    .venv/bin/python -m tools.hw_agent              抓今天
    .venv/bin/python -m tools.hw_agent --day 2026-11-03 --max-steps 40

为什么是 agent：QQ 是自绘界面，写死坐标那版实测五个坑——开屏广告吃掉点击、点进推送视频页、
中途锁屏、点到「宠物」、跨屏图裁成窄带。这些都不是「模型读不懂」，是脚本假设了界面状态。
让模型每一步先看当前截图再决定下一步，看到广告就先关掉，走错就退回来，才扛得住。

它能用的工具都在 ACTIONS 里：看屏、点、滑、返回、重开 QQ、取消熄屏，以及 submit（交答案）。
每一步都把新截图回给模型。只抓一天，正常三五步就能到作业页，十来步出结果。

结果经 server/homework.py 校验后按天写回（幂等）。校验不过就当这次失败——让定时任务重试，
绝不写错数据进库（错作业比没作业更糟）。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = "https://47.116.13.227:8183/xiaoxin/"
QQ = "com.tencent.mobileqq"
GROUP = "勤惜2024级3班"
SHOT = ROOT / "tmp" / "agent"
# 用哪个模型看屏操作：小新设置里的 agent_model（网页上能改），没设就用这个默认链——
# 前一个失败（额度、超时）就换下一个。看屏操作要强模型，opus 实测能看懂 QQ 的各种弹窗。
DEFAULT_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "grok-4.7"]
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

SYSTEM = """你在操作一台安卓平板（屏幕已锁定横屏），用 QQ 抓班级群「勤惜2024级3班」的作业。

路径：QQ 会话列表 → 群「勤惜2024级3班」→ 群聊底部工具栏「作业」→「全部作业」列表，列表按日期从新到旧。

每一步我会给你当前屏幕截图（缩到 1280 宽，tap 的坐标就用这张图里的像素坐标），有时还附上能读到的控件和坐标——
有就优先用那个坐标，最准。你看图决定下一步，调一个工具。注意：
- 点了没反应：先 look 等一下（页面可能在加载），还不行就检查是不是点偏了，别在同一个位置反复点。
- 这个 QQ 经常弹开屏广告、活动弹窗、推送内容页。看到不是目标页面就先关掉（点 ✕ 或 back），再继续。
- 走错页面（比如点进了「宠物」「频道」或某个视频）：用 back 退回去，或者 restart_qq 从头来。
- 列表是自绘的，正文只能靠看图读。往下滑用 swipe，一次滑半屏左右，正文才不会被切断。
- 找到目标那天的作业后，把那天每个学科的作业**原样**读出来，调 submit 交上去。
- 附图：作业正文下面常跟着照片（订正范本、黑板照、作业纸），在列表里默认**折叠**，只露一条淡淡的边。
  要先点「查看完整作业」展开，再点图片打开全屏预览，确认整张图都在屏幕上了，调 capture_image 存下来，再 back 回列表。
  每张图都要这么做，存之前一定看清楚它属于哪一科（紧跟在哪条作业正文下面）。抓不到图不影响交正文。
- 读不到、翻了很多屏也没找到那天，就调 submit 交 {"found": false, "why": "..."}，不要编作业内容。

读作业的要求：
- 正文原样抄，保留 1. 2. 的编号和换行，错别字照抄，不要改写、不要补充、不要漏行。
- 老师点名批评、友情提醒这类和作业条目并列的段落放 notes，学号串照抄。
- 只输出截图里真看到正文的学科。那天只有语文和英语就只给两条，不要凭空补一条数学。
- submit 的格式：{"found": true, "date": "2026-11-03", "items": [{"subject": "chinese|math|english|other",
  "submit": "无需提交|去完成|已完成", "text": "正文", "notes": ["..."], "mentions": ["正文里提到的群文件名"]}]}
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
     "description": "把当前屏幕存成某条作业的附图。只在**已经点开了全屏图片预览、整张图都在屏幕上**的时候调；"
                    "列表里折叠着的、只露一条边的图不要存（先点「查看完整作业」展开，再点图片打开预览）。存完记得 back 回列表。",
     "parameters": {"type": "object", "properties": {
         "subject": {"type": "string", "enum": ["chinese", "math", "english", "other"], "description": "这张图属于哪科的作业"},
         "what": {"type": "string", "description": "图上是什么（如「乐林枫的默写本订正」「黑板上的数学作业」）"}},
      "required": ["subject", "what"]}}},
    {"type": "function", "function": {"name": "submit", "description": "交结果（读到了作业，或确认读不到）",
     "parameters": {"type": "object", "properties": {
         "found": {"type": "boolean"}, "date": {"type": "string"}, "why": {"type": "string"},
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "subject": {"type": "string"}, "submit": {"type": "string"}, "text": {"type": "string"},
             "notes": {"type": "array", "items": {"type": "string"}},
             "mentions": {"type": "array", "items": {"type": "string"}}}}}},
      "required": ["found"]}}},
]


def env(name: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f".env 里没有 {name}")


def api(method: str, path: str, body=None, *, ctype="application/json", headers=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers={"Authorization": f"Bearer {env('XIAOXIN_KEY')}", "Content-Type": ctype,
                                          **(headers or {})})
    return json.loads(urllib.request.urlopen(req, context=CTX, timeout=180).read() or b"{}")


class Pad:
    def __init__(self, serial: str):
        self.s = serial
        self.n = 0
        self.images: list[dict] = []  # capture_image 存下的附图：{subject, what, raw}

    def sh(self, *a: str) -> str:
        return subprocess.run(["adb", "-s", self.s, "shell", *a], capture_output=True, text=True).stdout

    def shot(self) -> bytes:
        self.n += 1
        raw = subprocess.run(["adb", "-s", self.s, "exec-out", "screencap", "-p"], capture_output=True).stdout
        SHOT.mkdir(parents=True, exist_ok=True)
        (SHOT / f"{self.n:02d}.png").write_bytes(raw)
        import io

        from PIL import Image

        self.last_w = Image.open(io.BytesIO(raw)).width  # 模型报的坐标是按这一张换算的（屏幕会转）
        return raw

    def width(self) -> int:
        if not getattr(self, "_w", 0):
            m = re.search(r"(\d+)x(\d+)", self.sh("wm", "size"))
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1600, 2560)
            self._w = max(w, h)  # 横屏：长边是宽
        return self._w

    def widgets(self) -> str:
        """uiautomator 能读到的带文字的控件（换算成给模型看的那张图的坐标）。QQ 的工具栏按钮这样能拿到精确位置，
        自绘的列表读不到——读不到的就靠模型看图。"""
        self.sh("uiautomator", "dump", "/sdcard/u.xml")
        xml = self.sh("cat", "/sdcard/u.xml")
        k = VIEW_W / (getattr(self, "last_w", 0) or self.width())
        out = []
        for m in re.finditer(r'(?:text|content-desc)="([^"]{1,16})"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            t = m.group(1).strip()
            if not t or t in out:
                continue
            x1, y1, x2, y2 = (int(m.group(i)) for i in range(2, 6))
            out.append(f"「{t}」({round((x1 + x2) / 2 * k)},{round((y1 + y2) / 2 * k)})")
        return "、".join(out[:40])

    def locked(self) -> bool:
        # 这台 Android 16 上 dumpsys window 里是「mIsShowing=true」，不是旧版的「keyguardShowing=true」
        # （2026-11-03：原来按旧字段判，从来没判出锁屏，agent 第一轮在锁屏上耗了 8 步）
        return "mIsShowing=true" in self.sh("dumpsys", "window", "policy")

    def awake(self) -> None:
        """别让它熄屏 / 锁屏（熄了之后截的全是锁屏画面，白跑一轮）。这台没设密码，上划就开。"""
        self.sh("input", "keyevent", "KEYCODE_WAKEUP")
        for _ in range(3):
            if not self.locked():
                return
            self.sh("wm", "dismiss-keyguard")
            time.sleep(1.0)
            if self.locked():
                w = getattr(self, "last_w", 0) or self.width()
                h = 1600 if w >= 2000 else 2560
                self.sh("input", "swipe", str(w // 2), str(h * 9 // 10), str(w // 2), str(h // 4), "250")
                time.sleep(1.5)

    def act(self, name: str, args: dict) -> str:
        self.awake()
        if name == "look":
            time.sleep(max(0.0, min(float(args.get("wait_s") or 1.5), 8)))
            return "看了一眼"
        if name == "tap":
            # 按「模型看的那一张」的实际宽度换算。2026-11-03 实测：抓到一半 pad 自己转成竖屏（1600 宽），
            # 按缓存的横屏宽度 2560 换算，坐标全放大 1.6 倍，第二张附图怎么点都点不中
            k = (getattr(self, "last_w", 0) or self.width()) / VIEW_W
            x, y = round(int(args["x"]) * k), round(int(args["y"]) * k)
            self.sh("input", "tap", str(x), str(y))
            time.sleep(1.8)
            return f"点了 ({args['x']}, {args['y']})"
        if name == "swipe":
            amt = max(0.2, min(float(args.get("amount") or 0.5), 1.0))
            w = getattr(self, "last_w", 0) or self.width()
            h = 1600 if w >= 2000 else 2560          # 这台是 2560×1600：宽 2560 就是横屏
            dy = int(h * amt * 0.72)
            top, bot = int(h * 0.32), int(h * 0.78)   # 避开顶部标题栏和底部导航条
            y1, y2 = (bot, bot - dy) if args.get("dir") != "up" else (top, top + dy)
            self.sh("input", "swipe", str(w // 2), str(y1), str(w // 2), str(y2), "420")
            time.sleep(1.5)
            return f"往{'下' if args.get('dir') != 'up' else '上'}滑了 {amt} 屏"
        if name == "back":
            self.sh("input", "keyevent", "KEYCODE_BACK")
            time.sleep(1.8)
            return "按了返回"
        if name == "capture_image":
            raw = subprocess.run(["adb", "-s", self.s, "exec-out", "screencap", "-p"], capture_output=True).stdout
            self.images.append({"subject": args.get("subject") or "other", "what": str(args.get("what") or ""), "raw": raw})
            return f"存了一张附图（{args.get('subject')}：{args.get('what')}），一共 {len(self.images)} 张"
        if name == "restart_qq":
            self.sh("am", "force-stop", QQ)
            time.sleep(2)
            self.sh("am", "start", "-n", f"{QQ}/com.tencent.mobileqq.activity.SplashActivity")
            time.sleep(9)
            self.sh("cmd", "statusbar", "collapse")
            return "重开了 QQ，现在应该在会话列表"
        return f"不认识的动作 {name}"


def models() -> list[str]:
    try:
        want = api("GET", "api/agent").get("model")
    except Exception:  # noqa: BLE001  取不到设置就用默认
        want = None
    if not want:
        return DEFAULT_MODELS
    chain = [str(want)] + [m for m in DEFAULT_MODELS if m != want]
    return chain


def ask(msgs: list[dict], chain: list[str]) -> tuple[str, dict, str]:
    """问模型下一步。返回 (动作名, 参数, 它说的话)。流式拿不到 tool_calls（2026-11-03 实测），用非流式。"""
    import requests

    from src.llm import client

    last = ""
    for model in chain:
        body = {"model": model, "max_tokens": 4000, "tools": ACTIONS,
                "messages": [{"role": "system", "content": SYSTEM}, *msgs]}
        url, headers, body = client._endpoint(model, body)
        try:
            r = requests.post(url, headers=headers, json=body, timeout=300)
        except requests.RequestException as e:
            last = repr(e)
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            continue
        m = r.json()["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        said = (m.get("content") or "").strip()
        if not calls:
            return "look", {}, said or "（没调工具）"
        c = calls[0]["function"]
        try:
            args = json.loads(c.get("arguments") or "{}")
        except ValueError:
            args = {}
        return c["name"], args, said
    raise SystemExit(f"模型都失败了：{last}")


VIEW_W = 1280  # 给模型看的截图宽度。模型报的坐标都在这张图里，点之前按比例换回屏幕坐标


def shot_part(raw: bytes) -> dict:
    """截图缩到 VIEW_W 宽（JPEG）再给模型。不缩的话模型内部会自己缩，它报的坐标和原图对不上——
    2026-11-03 实测：「作业」在 (1840,1341)，模型一直报 (1718,1266)，差 7%，点在旁边空白上点了 20 次。"""
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im = im.resize((VIEW_W, round(im.height * VIEW_W / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


def hint(pad: Pad) -> str:
    w = pad.widgets()
    return (f"\n能读到的控件（坐标是这张截图里的，点它们用这个坐标最准）：{w}" if w else "")


def run(pad: Pad, day: str, max_steps: int) -> dict:
    """agent 循环：每一步给它当前截图，它调一个动作，直到 submit。返回 submit 的参数。"""
    pad.awake()
    pad.act("restart_qq", {})
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": f"目标：抓 {day} 那天的作业（今天是 {time.strftime('%Y-%m-%d')}）。"
                                 f"刚重开了 QQ，这是当前屏幕。按路径走到作业列表，找到 {day}，读出来后调 submit。" + hint(pad)},
        shot_part(pad.shot())]}]
    chain = models()
    print(f"  用模型：{chain[0]}" + (f"（失败就换 {chain[1]}）" if len(chain) > 1 else ""))
    for step in range(max_steps):
        name, args, said = ask(msgs, chain)
        tag = args.get("why") or args.get("dir") or ""
        print(f"  [{step + 1}] {name} {tag}" + (f"  …{said[:70]}" if said else ""))
        if name == "submit":
            return args
        out = pad.act(name, args)
        # 只保留最近 3 张截图（图很贵，更早的换成一句话）
        msgs.append({"role": "assistant", "content": f"[动作] {name} {json.dumps(args, ensure_ascii=False)[:200]}"})
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"{out}。这是现在的屏幕。" + hint(pad)}, shot_part(pad.shot())]})
        imgs = [i for i, m in enumerate(msgs) if isinstance(m.get("content"), list)]
        for i in imgs[:-3]:
            msgs[i] = {"role": "user", "content": "（这一步的截图已省略）"}
    return {"found": False, "why": f"走了 {max_steps} 步还没读到"}


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
    import io

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


def check(res: dict, day: str) -> tuple[list[dict], str]:
    """校验：不合格一律当这次失败（宁可重试，也不写错数据进库）。返回 (条目, 失败原因)。"""
    if not res.get("found"):
        return [], str(res.get("why") or "没找到这一天的作业")
    if str(res.get("date") or day) != day:
        return [], f"读到的日期是 {res.get('date')}，不是 {day}"
    items = []
    for it in res.get("items") or []:
        sub = str(it.get("subject") or "").strip()
        text = str(it.get("text") or "").strip()
        if sub not in SUBJECTS:
            return [], f"学科不对：{sub}"
        if not text or re.fullmatch(r"[（(]?看不清[）)]?", text):
            return [], f"{sub} 没读到正文"
        items.append({"subject": sub, "submit": str(it.get("submit") or ""), "text": text,
                      "notes": [str(x) for x in it.get("notes") or []],
                      "mentions": [str(x) for x in it.get("mentions") or []]})
    if not items:
        return [], "一条作业都没读到"
    if len({it["subject"] for it in items}) != len(items):
        return [], "同一个学科出现了两条"
    return items, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", default="HA2KGGAH")
    ap.add_argument("--day", default="", help="哪一天，默认今天")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="只读不写库")
    a = ap.parse_args()
    day = a.day or time.strftime("%Y-%m-%d")
    pad = Pad(a.pad)
    pad.sh("settings", "put", "system", "screen_off_timeout", "1800000")
    rot = pad.sh("settings", "get", "system", "accelerometer_rotation").strip()
    urot = pad.sh("settings", "get", "system", "user_rotation").strip()
    pad.sh("settings", "put", "system", "accelerometer_rotation", "0")  # 抓的时候锁横屏，别中途转
    pad.sh("settings", "put", "system", "user_rotation", "1")          # 1 = 横屏（实测 2560×1600）
    print(f"抓 {day}（pad {a.pad}）")
    t0 = time.monotonic()
    try:
        res = run(pad, day, a.max_steps)
    finally:
        pad.sh("settings", "put", "system", "screen_off_timeout", "120000")
        pad.sh("settings", "put", "system", "accelerometer_rotation", rot if rot in ("0", "1") else "1")
        if urot in ("0", "1", "2", "3"):
            pad.sh("settings", "put", "system", "user_rotation", urot)
    items, bad = check(res, day)
    secs = round(time.monotonic() - t0)
    if bad:
        print(f"× 这次没成：{bad}（{secs}s，{pad.n} 张截图）")
        return 1
    print(f"读到 {len(items)} 科（{secs}s，{pad.n} 张截图）：")
    for it in items:
        print(f"  [{it['subject']}] {it['submit']} {it['text'][:70].replace(chr(10), ' / ')}")
    chain = models()
    seen: set[str] = set()
    for i, img in enumerate(pad.images):
        import hashlib

        h = hashlib.md5(crop_preview(img["raw"])).hexdigest()
        if h in seen:
            print(f"  · 附图 {i + 1} 和前面一张一样，跳过")
            continue
        seen.add(h)
        ok, why = verify_image(img["raw"], chain)
        if not ok:
            print(f"  × 附图 {i + 1}（{img['subject']}：{img['what']}）没过核验，丢掉：{why}")
            continue
        it = next((x for x in items if x["subject"] == img["subject"]), None)
        if it is None:
            print(f"  × 附图 {i + 1} 说是 {img['subject']} 的，但那天没有这科作业，丢掉")
            continue
        data = crop_preview(img["raw"])
        (SHOT / f"img_{i + 1}_{img['subject']}.jpg").write_bytes(data)
        if a.dry_run:
            print(f"  ✓ 附图 {i + 1} → {img['subject']}（{img['what']}）通过核验 [dry-run 不上传]")
            continue
        meta = api("POST", "api/files", data, ctype="image/jpeg",
                   headers={"X-Filename": urllib.parse.quote(f"{day}-{img['subject']}-{i + 1}.jpg")})
        it.setdefault("images", []).append(meta["id"])
        print(f"  ✓ 附图 {i + 1} → {img['subject']}（{img['what']}）")
    if a.dry_run:
        print("（--dry-run，没写库）")
        return 0
    rec = api("PUT", f"api/homework/{day}", {"items": items, "source": "qq-agent",
                                             "note": f"opus 看屏抓的 {time.strftime('%m-%d %H:%M')}"})
    print(f"✓ 存好了 {rec['day']} 星期{rec['week']}：{len(rec['items'])} 科")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
