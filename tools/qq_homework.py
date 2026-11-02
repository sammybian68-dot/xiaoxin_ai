"""从 pad 的 QQ 班级群抓作业，按天存进小新（/api/homework/<day>，按天幂等）。

    .venv/bin/python -m tools.qq_homework --days 10            抓最近 10 天（已经有记录的跳过）
    .venv/bin/python -m tools.qq_homework --days 10 --force    连已有的也重抓
    .venv/bin/python -m tools.qq_homework --day 2026-10-23     只抓某一天

怎么抓（pad 上的 QQ 是自绘界面，uiautomator 只能读到标题栏，正文读不到，所以靠截图 + 视觉模型）：
  1. QQ →「勤惜2024级3班」→ 底部「作业」→「全部作业」，从上往下滚，每屏截一张；
  2. 截图交给视觉模型，按天按学科读出正文、老师、要不要提交、附图位置、正文里提到的文件名；
  3. 附图：点开全屏预览，截一张干净的大图（比裁列表里的清楚），传进小新当附件；
  4. 正文里提到「群里的 xxx.pdf」：去群文件列表找同名文件，下载下来传进小新；
  5. 按天 PUT 给小新，同一天重复抓就覆盖。

屏幕坐标会随 QQ 版本变，关键按钮都是先 uiautomator dump 找 bounds 再点（找不到才用兜底坐标）。
"""

from __future__ import annotations

import argparse
import base64
import io
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
SHOT = ROOT / "tmp" / "hw"
VISION = ["claude-sonnet-5-5", "deepseek-v4.1-flash"]
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE  # 自签证书


def env(name: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f".env 里没有 {name}")


def api(method: str, path: str, body=None, *, raw: bool = False, ctype: str = "application/json", headers=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, method=method, data=data,
                                 headers={"Authorization": f"Bearer {env('XIAOXIN_KEY')}", "Content-Type": ctype,
                                          **(headers or {})})
    out = urllib.request.urlopen(req, context=CTX, timeout=180).read()
    return out if raw else json.loads(out or b"{}")


class Pad:
    """pad 上的 adb 操作：点、滑、截图、找控件。"""

    def __init__(self, serial: str):
        self.s = serial

    def sh(self, *args: str) -> str:
        return subprocess.run(["adb", "-s", self.s, "shell", *args], capture_output=True, text=True).stdout

    def tap(self, x: int, y: int, wait: float = 1.2) -> None:
        self.sh("input", "tap", str(x), str(y))
        time.sleep(wait)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400, wait: float = 1.4) -> None:
        self.sh("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
        time.sleep(wait)

    def back(self, wait: float = 1.2) -> None:
        self.sh("input", "keyevent", "KEYCODE_BACK")
        time.sleep(wait)

    def shot(self, name: str) -> Path:
        SHOT.mkdir(parents=True, exist_ok=True)
        p = SHOT / f"{name}.png"
        p.write_bytes(subprocess.run(["adb", "-s", self.s, "exec-out", "screencap", "-p"], capture_output=True).stdout)
        return p

    def dump(self) -> str:
        self.sh("uiautomator", "dump", "/sdcard/u.xml")
        return self.sh("cat", "/sdcard/u.xml")

    def find(self, text: str) -> tuple[int, int] | None:
        """按 text / content-desc 找控件中心点。QQ 自绘的部分找不到，返回 None。"""
        for m in re.finditer(r'(?:text|content-desc)="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', self.dump()):
            if text in m.group(1):
                x1, y1, x2, y2 = (int(m.group(i)) for i in range(2, 6))
                return (x1 + x2) // 2, (y1 + y2) // 2
        return None

    def focus(self) -> str:
        return self.sh("dumpsys", "window") or ""


# ---------------------------------------------------------------- QQ 界面

def open_homework(pad: Pad) -> None:
    """QQ → 班级群 → 底部「作业」→「全部作业」。"""
    pad.sh("svc", "power", "stayon", "usb")
    pad.sh("settings", "put", "system", "screen_off_timeout", "1800000")  # 半小时不熄屏（抓一轮要十几分钟）
    pad.sh("input", "keyevent", "KEYCODE_WAKEUP")
    pad.sh("input", "swipe", "1280", "2000", "1280", "700", "200")  # 锁屏的话划开（这台没有密码）
    time.sleep(1.5)
    pad.sh("cmd", "statusbar", "collapse")  # 通知栏拉下来的话先收起
    # 重开一次 QQ 回到会话列表：不要用返回键退（QQ 停在二级页面时连按会直接退出 QQ，跑到别的 app 上去）
    pad.sh("am", "force-stop", QQ)
    time.sleep(1.5)
    pad.sh("am", "start", "-n", f"{QQ}/com.tencent.mobileqq.activity.SplashActivity")
    time.sleep(8)
    # 会话列表的行是自绘的，uiautomator 只能读到「最后一条消息」那行文字，群名读不到：用视觉在截图里找这一行
    at = None
    for i in range(4):  # 冷启动首屏可能还在转圈：等一下再看
        at = pad.find(GROUP) or find_on_screen(pad, "会话列表", f"写着「{GROUP}」的那一行会话")
        if at:
            break
        print(f"  还没看到群（第 {i + 1} 次），等 5 秒")
        time.sleep(5)
    if at is None:
        raise SystemExit(f"会话列表里没看到「{GROUP}」，手动打开一次再跑（看 tmp/hw/find_*.png）")
    pad.tap(*at, wait=5)
    at = pad.find("作业") or find_on_screen(pad, "群聊底部工具栏", "底部工具栏里的「作业」按钮")
    if at is None:
        raise SystemExit("没找到群聊里的「作业」入口，看 tmp/hw/ 的截图")
    pad.tap(*at, wait=5)
    if pad.find("全部作业") is None and find_on_screen(pad, "作业页", "顶部的「全部作业」标签") is None:
        raise SystemExit("没进到作业页，QQ 界面可能变了，看 tmp/hw/ 的截图")


def find_on_screen(pad: Pad, what: str, target: str) -> tuple[int, int] | None:
    """QQ 自绘控件 uiautomator 读不到：截一张图让视觉模型指出要点的位置（返回屏幕坐标）。"""
    from src.llm import client

    p = pad.shot(f"find_{int(time.time())}")
    from PIL import Image

    W, H = Image.open(p).size
    rep = client.chat_full(VISION, [{"role": "user", "content": [
        {"type": "text", "text": f"这是平板上 QQ 的{what}截图（{W}×{H} 像素）。找到：{target}。\n"
         f"只输出 JSON：{{\"x\":横坐标,\"y\":纵坐标}}（这张图里的像素坐标，指向那个东西的中心）。找不到输出 {{\"x\":null}}。"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()}}]}],
        max_tokens=200, timeout=180)
    m = re.search(r"\{.*\}", rep.text, re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    if not isinstance(d.get("x"), (int, float)):
        return None
    return int(d["x"]), int(d["y"])


def scroll_shots(pad: Pad, prefix: str, limit: int = 60) -> list[Path]:
    """从当前位置往下滚，每屏一张，画面不再变化就停。返回截图路径（从上到下）。"""
    out, last = [], None
    for i in range(limit):
        if i % 10 == 0:
            pad.sh("input", "keyevent", "KEYCODE_WAKEUP")  # 别让它中途熄屏（熄了后面全是锁屏截图）
        p = pad.shot(f"{prefix}_{i:02d}")
        cur = p.read_bytes()
        if "keyguardShowing=true" in pad.sh("dumpsys", "window", "|", "grep", "keyguard"):
            p.unlink(missing_ok=True)
            pad.sh("input", "keyevent", "KEYCODE_WAKEUP")
            pad.sh("input", "swipe", "1280", "2000", "1280", "700", "200")
            time.sleep(2)
            continue
        if last is not None and _same(cur, last):
            p.unlink(missing_ok=True)
            break
        out.append(p)
        last = cur
        pad.swipe(1280, 1250, 1280, 520, 420, 1.5)  # 只滚约半屏：前后两屏大面积重叠，正文不会被屏幕边界切断
    return out


def _same(a: bytes, b: bytes) -> bool:
    """两张截图几乎一样（滚到底了）。缩成小图比，避开光标闪烁、时间变化。"""
    from PIL import Image

    def tiny(raw):
        im = Image.open(io.BytesIO(raw)).convert("L").resize((48, 30))
        return list(im.getdata())

    x, y = tiny(a), tiny(b)
    return sum(abs(p - q) for p, q in zip(x, y)) / len(x) < 2.5


# ---------------------------------------------------------------- 视觉读取

READ_PROMPT = """这是小学班级群「作业」列表的连续截图（从上往下滚，可能有重叠）。把里面每一天、每个学科的作业读出来。
只输出一个 JSON，不要别的字：
{"days":[{"date":"2026-11-02","items":[{"subject":"chinese|math|english|other","title":"11月2日语文作业","teacher":"",
  "submit":"无需提交|去完成|已完成","text":"正文，原样抄，保留 1. 2. 的编号和换行","notes":["老师的提醒、批评这类单独段落"],
  "mentions":["正文里提到的群文件名，如 第三单元练习卷.pdf，没有就空"],"has_image":true/false}]}]}
要求：
- 日期按卡片标题里的「11月2日」这种，配合「今天 / 昨天 / 星期X」的分组和下面给的今天日期算出完整年月日。
- 同一天同一学科只留一条（截图有重叠，别重复）。
- text 原样抄写，错别字也照抄，不要改写、不要补充。学号串（如「28、26、40」）照抄。
- notes 放「今天发现以下学生没带…」「友情提醒」「批评」这类和作业条目并列的段落。
- has_image：这条作业下面有没有附图（手写作业照片、黑板照片等）。
- 截图里看不全的（被下一屏截断）尽量拼起来；实在看不清的地方写「（看不清）」。"""


CHUNK = 6      # 一次给模型几张图（太多会漏读、也容易超时）
OVERLAP = 3    # 批之间重叠几张：半屏步进时一张卡片横跨 3–4 屏，重叠 3 张才保证至少有一批看到完整的


def read_chunk(shots: list[Path], today: str) -> list[dict]:
    from src.llm import client

    parts = [{"type": "text", "text": READ_PROMPT + f"\n今天是 {today}（{'一二三四五六日'[time.strptime(today, '%Y-%m-%d').tm_wday]}）。"}]
    for p in shots:
        parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()}})
    rep = client.chat_full(VISION, [{"role": "user", "content": parts}], max_tokens=12000, timeout=600)
    m = re.search(r"\{.*\}", rep.text, re.S)
    if not m:
        print(f"  × 这一批没给出 JSON：{rep.text[:160]}")
        return []
    return json.loads(m.group(0)).get("days") or []


def _score(it: dict) -> tuple[int, int]:
    """这份读出来的正文有多可信：(有没有干净的正文, 多长)。空的最差，带「看不清」的次差。"""
    t = str(it.get("text") or "").strip()
    if not t:
        return (0, 0)
    return (1 if "看不清" not in t else 0, len(t))


def read_shots(shots: list[Path], today: str) -> tuple[list[dict], dict]:
    """分批读（每批 CHUNK 张、批间重叠 OVERLAP 张），按 (日期, 学科) 合并：正文取最全的那一份。
    返回 (天列表, {(日期,学科): 这条出现在哪几张截图里})——附图按截图归属认领。"""
    merged: dict[tuple[str, str], dict] = {}
    where: dict[tuple[str, str], list[str]] = {}
    step = max(1, CHUNK - OVERLAP)
    for i in range(0, len(shots), step):
        batch = shots[i:i + CHUNK]
        if not batch:
            break
        for d in read_chunk(batch, today):
            day = str(d.get("date") or "")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                continue
            for it in d.get("items") or []:
                k = (day, str(it.get("subject") or "other"))
                where.setdefault(k, []).extend(p.stem for p in batch)
                old = merged.get(k)
                # 同一条在好几批里都读到：挑最可信的那份（没抓到的、带「看不清」的排在后面，同档比长度），
                # notes、mentions 取并集。实测：被屏幕切断的那批会读成「（看不清）」，不能按长度盖掉完整的那份
                if old is None or _score(it) > _score(old):
                    it = dict(it)
                    if old:
                        it["notes"] = list(dict.fromkeys((old.get("notes") or []) + (it.get("notes") or [])))
                        it["mentions"] = list(dict.fromkeys((old.get("mentions") or []) + (it.get("mentions") or [])))
                        it["has_image"] = bool(old.get("has_image")) or bool(it.get("has_image"))
                    merged[k] = it
                else:
                    old["notes"] = list(dict.fromkeys((old.get("notes") or []) + (it.get("notes") or [])))
                    old["mentions"] = list(dict.fromkeys((old.get("mentions") or []) + (it.get("mentions") or [])))
                    old["has_image"] = bool(old.get("has_image")) or bool(it.get("has_image"))
        print(f"  读到第 {min(i + CHUNK, len(shots))}/{len(shots)} 张，已有 {len(merged)} 条")
    days: dict[str, dict] = {}
    for (day, sub), it in sorted(merged.items(), reverse=True):
        days.setdefault(day, {"date": day, "items": []})["items"].append({**it, "subject": sub})
    return list(days.values()), where


# ---------------------------------------------------------------- 附图、群文件

def grab_images(pad: Pad, shots: list[Path]) -> dict[str, list[str]]:
    """列表里的附图：点开全屏预览截一张干净的，传进小新。返回 {截图名: [file_id]}，按截图归属（后面和作业条目对上）。

    点哪里：在每张列表截图里找「看起来是照片」的大块区域（和纯白背景差别大的连续块）。QQ 自绘读不到控件，
    所以按像素找，再点那个块的中心。"""
    out: dict[str, list[str]] = {}
    for p in shots:
        for cx, cy, y0, y1 in photo_spots(p):
            pad.shot("tmp_before")
            pad.tap(cx, cy, wait=2.5)
            full = pad.shot(f"img_{p.stem}_{cx}_{cy}")
            if _is_preview(full):
                fid = upload_image(full)          # 全屏预览：整张都是图，裁掉边框就行
                print(f"  附图（全屏）{full.name} → {fid}")
                pad.back(1.6)
            else:
                # 没点开（点在空白处，或这版 QQ 列表里的图不能点）：退而从列表截图里把那一块裁出来
                full.unlink(missing_ok=True)
                fid = upload_crop(p, y0, y1, f"{p.stem}_{y0}")
                if fid is None:
                    continue
                print(f"  附图（列表裁切）{p.stem} y={y0}-{y1} → {fid}")
            out.setdefault(p.stem, []).append(fid)
    (SHOT / "tmp_before.png").unlink(missing_ok=True)
    return out


def _is_preview(path: Path) -> bool:
    """点开后是不是进了全屏图片预览：QQ 预览是黑底、图片铺满，边角很暗。"""
    from PIL import Image

    im = Image.open(path).convert("L")
    W, H = im.size
    corners = [im.getpixel(xy) for xy in ((8, H // 2), (W - 8, H // 2), (W // 2, 8), (W // 2, H - 8))]
    return sum(1 for c in corners if c < 60) >= 3


def upload_crop(path: Path, y0: int, y1: int, tag: str) -> str | None:
    """从列表截图里裁出附图那一块传上去（点不开全屏预览时的兜底）。"""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    W, H = im.size
    y0, y1 = max(0, y0 - 10), min(H, y1 + 10)
    if y1 - y0 < H * 0.12:
        return None
    im = im.crop((int(W * 0.03), y0, int(W * 0.78), y1))  # 列表卡片大致占左边 3%–78%
    out = SHOT / f"crop_{tag}.jpg"
    im.save(out, "JPEG", quality=88)
    meta = api("POST", "api/files", out.read_bytes(), ctype="image/jpeg",
               headers={"X-Filename": urllib.parse.quote(out.name)})
    return meta["id"]


def photo_spots(path: Path, min_frac: float = 0.06) -> list[tuple[int, int, int, int]]:
    """截图里像照片的区域：返回 (点击点 x, 点击点 y, 上边界, 下边界)。照片的特征：颜色杂、不是白底黑字。"""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    W, H = im.size
    sm = im.resize((W // 16, H // 16))
    w, h = sm.size
    px = sm.load()
    rows = []
    for y in range(h):
        n = 0
        for x in range(w):
            r, g, b = px[x, y]
            if max(r, g, b) - min(r, g, b) > 18 or (r + g + b) / 3 < 205:  # 有颜色 或 偏暗 = 不是白底
                n += 1
        rows.append(n / w)
    spots, y = [], 0
    while y < h:
        if rows[y] > 0.55:  # 这一行大半都不是白底：照片的一行
            y0 = y
            while y < h and rows[y] > 0.45:
                y += 1
            if (y - y0) / h > min_frac:
                spots.append((W // 2, (y0 + y) // 2 * 16, y0 * 16, y * 16))  # (点哪里, 这块的上下边界)
        y += 1
    return spots[:4]


def upload_image(path: Path) -> str:
    """传一张图进小新（当作业附件）。顺手裁掉顶部状态栏和底部导航条。"""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    W, H = im.size
    im = im.crop((0, int(H * 0.035), W, int(H * 0.975)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    meta = api("POST", "api/files", buf.getvalue(), raw=False, ctype="image/jpeg",
               headers={"X-Filename": urllib.parse.quote(path.name.replace(".png", ".jpg"))})
    return meta["id"]


def group_files(pad: Pad, wanted: set[str]) -> dict[str, str]:
    """正文里提到的群文件：群文件列表里找同名的，「保存到手机」再 pull 上来。返回 {文件名: file_id}。"""
    if not wanted:
        return {}
    print(f"  正文提到 {len(wanted)} 个文件，去群文件里找：{'、'.join(wanted)}")
    got: dict[str, str] = {}
    pad.back(1.5)  # 从作业页退回群聊
    at = pad.find("文件")
    if at is None:
        print("  × 没找到群文件入口，跳过")
        return got
    pad.tap(*at, wait=4)
    for name in wanted:
        key = re.sub(r"\.[a-z0-9]+$", "", name)[:10]
        # 群文件列表按日期倒序、可能很长：先在当前屏找，找不到往下滚几屏再找（文件名有时也是自绘的，兜底用视觉）
        spot = None
        for page in range(6):
            spot = pad.find(key) or find_on_screen(pad, "群文件列表", f"文件名里含「{key}」的那一行（没有就说找不到）")
            if spot:
                break
            pad.swipe(1280, 1250, 1280, 500, 420, 1.4)
        if spot is None:
            print(f"  × 群文件里没有「{name}」（翻了 6 屏）")
            continue
        pad.tap(*spot, wait=5)
        save = pad.find("保存到手机") or pad.find("更多")
        if save:
            pad.tap(*save, wait=2)
            s2 = pad.find("保存到手机")
            if s2:
                pad.tap(*s2, wait=4)
        pulled = pull_download(pad, key)
        if pulled:
            got[name] = pulled
            print(f"  群文件「{name}」→ {pulled}")
        pad.back(2)
    pad.back(2)
    return got


def pull_download(pad: Pad, key: str) -> str | None:
    """从 /sdcard/Download/QQ/ 把刚存的文件拉下来传进小新。"""
    out = pad.sh("ls", "-t", "/sdcard/Download/QQ/")
    for line in out.splitlines():
        f = line.strip()
        if f and key[:6] in f:
            local = SHOT / f
            subprocess.run(["adb", "-s", pad.s, "pull", f"/sdcard/Download/QQ/{f}", str(local)], capture_output=True)
            if not local.is_file():
                return None
            meta = api("POST", "api/files", local.read_bytes(), ctype="application/octet-stream",
                       headers={"X-Filename": urllib.parse.quote(f)})
            return meta["id"]
    return None


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", default="HA2KGGAH", help="pad 的 adb 序列号")
    ap.add_argument("--days", type=int, default=10, help="抓最近几天")
    ap.add_argument("--day", default="", help="只抓这一天（YYYY-MM-DD）")
    ap.add_argument("--force", action="store_true", help="已经有记录的也重抓")
    ap.add_argument("--no-images", action="store_true", help="不抓附图（快，调试用）")
    ap.add_argument("--from-shots", action="store_true", help="复用 tmp/hw 里已有的列表截图，不重新滚（读取中途失败时接着跑）")
    a = ap.parse_args()
    today = time.strftime("%Y-%m-%d")
    want = [a.day] if a.day else [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(a.days)]
    have = {d["day"] for d in api("GET", "api/homework")["days"]}
    todo = want if a.force else [d for d in want if d not in have]
    print(f"要覆盖的日子：{'、'.join(want)}；已有记录：{'、'.join(sorted(have)) or '无'}")
    if not todo:
        print("都抓过了（--force 可以重抓）")
        return 0

    pad = Pad(a.pad)
    if a.from_shots:
        shots = sorted(SHOT.glob("list_*.png"))
        if not shots:
            raise SystemExit("tmp/hw 里没有列表截图，去掉 --from-shots 重新抓")
        print(f"复用已有截图 {len(shots)} 屏")
    else:
        open_homework(pad)
        shots = scroll_shots(pad, "list")
        print(f"列表截了 {len(shots)} 屏")
    days, where = read_shots(shots, today)
    print(f"读出 {len(days)} 天：{'、'.join(d.get('date', '?') for d in days)}")

    imgs = {} if a.no_images else grab_images(pad, shots)   # {截图名: [file_id]}
    wanted = {m for d in days for it in d.get("items") or [] for m in it.get("mentions") or []}
    files = group_files(pad, wanted) if wanted else {}
    used: set[str] = set()

    n = 0
    for d in days:
        day = str(d.get("date") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or (not a.force and day in have) or day > today:
            continue
        items = []
        for it in d.get("items") or []:
            pics = []
            if it.get("has_image"):
                # 这条作业出现在哪几屏里，就认领那几屏上点开过的附图（同一张图只归一条）
                for shot in dict.fromkeys(where.get((day, it.get("subject")), [])):
                    for fid in imgs.get(shot, []):
                        if fid not in used:
                            used.add(fid)
                            pics.append(fid)
            items.append({**it, "images": pics,
                          "files": [{"name": m, "fid": files[m]} for m in it.get("mentions") or [] if m in files]})
        rec = api("PUT", f"api/homework/{day}", {"items": items, "source": "qq", "note": f"pad 采集 {time.strftime('%m-%d %H:%M')}"})
        print(f"✓ {day} 星期{rec['week']}：{len(rec['items'])} 科" + (f"，附图 {sum(len(x['images']) for x in rec['items'])} 张" if rec["items"] else ""))
        n += 1
    pad.sh("svc", "power", "stayon", "false")
    pad.sh("settings", "put", "system", "screen_off_timeout", "60000")
    print(f"存了 {n} 天。还缺：{'、'.join(d for d in want if d not in {x['day'] for x in api('GET', 'api/homework')['days']}) or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
