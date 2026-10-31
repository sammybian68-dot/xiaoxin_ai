"""家长端（pad 上的小新家长 APK，已配对成某个家长）端到端：

    .venv/bin/python -m tests.e2e.parent --pad SERIAL

走一遍：聊天列表 → 点进第一个孩子的群 → 点缩略图开大图 → 返回键关大图 → 发一条（不叫小新）→ 孩子的 pad（小新 App）
不刷新就出现 → 返回键回到列表。每步用 devtools 读页面状态判定，返回键用真的 adb keyevent 4。
会往第一个孩子的群里发一条「[e2e] …」测试消息（真消息，留在聊天记录里）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.request

import websockets


def adb(serial: str, *args: str) -> str:
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, timeout=60).stdout


class Page:
    def __init__(self, serial: str, pkg: str, port: int):
        self.serial, self.pkg, self.port, self.n = serial, pkg, port, 0

    async def __aenter__(self):
        pid = adb(self.serial, "shell", "pidof", self.pkg).split()[0]
        subprocess.run(["adb", "-s", self.serial, "forward", f"tcp:{self.port}", f"localabstract:webview_devtools_remote_{pid}"],
                       check=True, capture_output=True)
        pages = [p for p in json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json").read()) if p["type"] == "page"]
        self.ws = await websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()
        subprocess.run(["adb", "-s", self.serial, "forward", "--remove", f"tcp:{self.port}"], capture_output=True)

    async def js(self, expr: str, wait: bool = False):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": "Runtime.evaluate",
                                       "params": {"expression": expr, "returnByValue": True, "awaitPromise": wait}}))
        while True:
            r = json.loads(await self.ws.recv())
            if r.get("id") == self.n:
                res = r["result"]
                if "exceptionDetails" in res:
                    raise RuntimeError(res["exceptionDetails"].get("exception", {}).get("description", "js error"))
                return res.get("result", {}).get("value")

    async def until(self, expr: str, timeout: float = 8) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if await self.js(expr):
                return True
            await asyncio.sleep(0.2)
        return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", required=True)
    a = ap.parse_args()
    s = a.pad
    ok = True

    def check(name: str, good: bool) -> None:
        nonlocal ok
        ok &= good
        print(f"  {'✓' if good else '✗'} {name}")

    adb(s, "shell", "am", "force-stop", "com.xiaoxin.parent")
    adb(s, "shell", "am", "start", "-n", "com.xiaoxin.app/.TalkActivity")  # 孩子那台先开着（接 hub）
    await asyncio.sleep(5)
    adb(s, "shell", "am", "start", "-n", "com.xiaoxin.parent/.MainActivity")
    await asyncio.sleep(6)
    async with Page(s, "com.xiaoxin.parent", 9335) as p:
        check("打开是聊天列表", await p.until("document.body.classList.contains('in-chats') && document.querySelectorAll('#chat-list [data-kid]').length > 0"))
        print("    ", await p.js("[...document.querySelectorAll('#chat-list [data-kid]')].map(b=>b.innerText.replace(/\\s+/g,' ')).join(' | ')"))
        await p.js("document.querySelector('#chat-list [data-kid]').click()")
        check("点进去是群聊，标题是「…的群」", await p.until("!document.body.classList.contains('in-chats') && /的群$/.test(document.getElementById('conv-title').textContent)"))
        check("家长端没有待命 / 自由对话 / 闹钟", await p.js("['standby','free-talk','open-clock'].every(id=>getComputedStyle(document.getElementById(id)).display==='none')"))
        check("有「问小新」开关", await p.js("getComputedStyle(document.getElementById('ask')).display !== 'none'"))
        if await p.js("!!document.querySelector('#messages .att-img')"):
            await p.js("document.querySelector('#messages .att-img').closest('a').click()")
            check("点缩略图在页内开大图（不跳页）", await p.until("document.getElementById('viewer').open && location.pathname.endsWith('/xiaoxin/')"))
            adb(s, "shell", "input", "keyevent", "4")
            check("返回键关大图，还在群里", await p.until("!document.getElementById('viewer').open && !document.body.classList.contains('in-chats')"))
        text = f"[e2e] 家长端测试 {time.strftime('%H:%M:%S')}"
        await p.js(f"document.getElementById('input').value = {json.dumps(text)}; updateSend(); document.getElementById('send').click()")
        check("家长发出去了（自己右边一条）", await p.until(f"[...document.querySelectorAll('#messages .msg.me')].some(e=>e.textContent.includes({json.dumps(text)}))"))
        check("小新没有接话（没有「小新在想…」）", not await p.js("!!document.querySelector('#messages .thinking')"))
        async with Page(s, "com.xiaoxin.app", 9336) as k:
            check("孩子的 pad 不刷新就出现，在左边、署名是家长",
                  await k.until(f"[...document.querySelectorAll('#messages .msg.from-parent')].some(e=>e.textContent.includes({json.dumps(text)}))"))
        adb(s, "shell", "input", "keyevent", "4")
        check("返回键回到聊天列表，最后一条是刚发的", await p.until(f"document.body.classList.contains('in-chats') && document.getElementById('chat-list').textContent.includes({json.dumps(text[:12])})"))
        adb(s, "shell", "input", "keyevent", "4")
        await asyncio.sleep(1)
        check("在列表再按返回才退出 App", "com.xiaoxin.parent" not in adb(s, "shell", "dumpsys", "activity", "activities").split("topResumedActivity")[1][:120])
    print("结果：", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
