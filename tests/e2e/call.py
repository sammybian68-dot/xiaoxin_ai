"""视频通话端到端：在这台 Mac 上开无头 Chrome（假摄像头 / 假麦克风），连线上的小新，真的走 WebRTC。

    .venv/bin/python -m tests.e2e.call                 Mac 两个标签互打：一次局域网/直连，一次强制只走 ECS 中继
    .venv/bin/python -m tests.e2e.call --pad SERIAL    Mac ↔ pad 上的 App（默认小新，--app parent 换小新家长）互打

判定：两端 connectionState=connected，并且都收到了对方的视频（分辨率不为空、下行码率 > 0）。中继那次还要求路径是 relay。
不进 pytest 默认集：要连 gpu2 和 ECS，要本机装 Chrome。key 从 .env 读，只经 CDP 传给浏览器，不出现在命令行和输出里。
自签证书：这里给 Chrome 加 --ignore-certificate-errors，只用于这个一次性的测试浏览器（独立 profile，跑完删）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://47.116.13.227:8183/xiaoxin/"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
APPS = {"xiaoxin": "com.xiaoxin.app/.TalkActivity", "parent": "com.xiaoxin.parent/.MainActivity"}


def env(name: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f".env 里没有 {name}")


class Tab:
    """一个 Chrome 标签，CDP 直连（不装 puppeteer）。"""

    def __init__(self, ws):
        self.ws, self.n = ws, 0

    @classmethod
    async def open(cls) -> "Tab":
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
        info = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return cls(await websockets.connect(info["webSocketDebuggerUrl"], max_size=None))

    async def cmd(self, method: str, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    async def go(self, url: str) -> None:
        await self.cmd("Page.navigate", url=url)
        await asyncio.sleep(1.5)

    async def eval(self, expr: str):
        r = await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")

    async def stats(self) -> dict:
        return await self.eval("window.callStats ? JSON.stringify(window.callStats) : null") and json.loads(
            await self.eval("JSON.stringify(window.callStats)")) or {}


def ok(st: dict, need_relay: bool) -> bool:
    got_video = st.get("res") not in (None, "-") and isinstance(st.get("kbpsIn"), int) and st["kbpsIn"] > 0
    got_audio = isinstance(st.get("audioKbps"), int) and st["audioKbps"] > 0  # 音频包在走（电平另看，pad 那边是真麦克风，屋里安静就接近 0）
    return st.get("conn") == "connected" and got_video and got_audio and (not need_relay or st.get("path") == "经 ECS 中继")


def line(name: str, st: dict) -> str:
    return (f"  {name:6s} conn={st.get('conn')} 路径={st.get('path')}（{st.get('local')} / {st.get('remote')}） "
            f"收到 {st.get('res')}@{st.get('fps')}fps 下行 {st.get('kbpsIn')}kbps 上行 {st.get('kbpsOut')}kbps "
            f"RTT {st.get('rtt')}ms 对方声音 {st.get('audioIn')}（{st.get('audioKbps')}kbps） {('错误：' + st['err']) if st.get('err') else ''}")


async def wait(tabs: dict, need_relay: bool, timeout: float = 40) -> bool:
    t0 = time.monotonic()
    sts = {}
    while time.monotonic() - t0 < timeout:
        await asyncio.sleep(2)
        sts = {k: await v() for k, v in tabs.items()}
        if all(ok(s, need_relay) for s in sts.values()):
            await asyncio.sleep(4)  # 稳一会儿再看一次码率
            sts = {k: await v() for k, v in tabs.items()}
            for k, s in sts.items():
                print(line(k, s))
            return all(ok(s, need_relay) for s in sts.values())
    for k, s in sts.items():
        print(line(k, s))
    return False


def adb(serial: str, *args: str) -> str:
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, timeout=60).stdout


async def pad_stats(serial: str, app: str):
    """从 pad 的 WebView 读 window.callStats：adb forward 到它的 devtools socket，再走 CDP。"""
    pkg = APPS[app].split("/")[0]
    pid = adb(serial, "shell", "pidof", pkg).strip().split()[0]
    sock = f"webview_devtools_remote_{pid}"
    if sock not in adb(serial, "shell", "cat", "/proc/net/unix"):
        raise SystemExit(f"{pkg} 里没有 WebView（没配 key？）")
    subprocess.run(["adb", "-s", serial, "forward", "tcp:9334", f"localabstract:{sock}"], check=True, capture_output=True)
    pages = json.loads(urllib.request.urlopen("http://127.0.0.1:9334/json", timeout=5).read())
    page = next(p for p in pages if "calltest" in p.get("url", ""))
    tab = Tab(await websockets.connect(page["webSocketDebuggerUrl"], max_size=None))
    return tab.stats


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", help="pad 的 adb 序列号：Mac ↔ pad 互打")
    ap.add_argument("--app", default="xiaoxin", choices=sorted(APPS))
    ap.add_argument("--relay", action="store_true", help="只测强制走 ECS 中继（--pad 时也是）")
    a = ap.parse_args()
    key = env("XIAOXIN_KEY")
    subprocess.run(["pkill", "-f", "user-data-dir=.*xx-call-"], capture_output=True)  # 上次没退干净的测试浏览器
    await asyncio.sleep(1)
    profile = tempfile.mkdtemp(prefix="xx-call-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                               "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                               "--ignore-certificate-errors", "--autoplay-policy=no-user-gesture-required",
                               "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    passed = True
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except OSError:
                await asyncio.sleep(0.25)
        mac = await Tab.open()
        await mac.cmd("Page.enable")
        await mac.go(BASE + "?k=" + key)  # 种 cookie
        if a.pad:
            room = "e2e" + secrets.token_hex(3)
            relay = "&relay=1" if a.relay else ""
            print(f"Mac ↔ pad（{a.app}）房间 {room}{'，只走中继' if a.relay else ''}")
            # 单引号：adb shell 在 pad 上再过一遍 sh，不包的话 & 会把后面的参数切掉（relay、mute 收不到）
            adb(a.pad, "shell", "am", "start", "-n", APPS[a.app], "--es", "page", f"'calltest?room={room}&who=pad&mute=1{relay}'")
            await asyncio.sleep(4)
            await mac.go(BASE + f"calltest?room={room}&who=mac&mute=1{relay}")
            passed = await wait({"mac": mac.stats, "pad": await pad_stats(a.pad, a.app)}, a.relay, timeout=60)
            await mac.eval("document.getElementById('hang').click()")
            adb(a.pad, "shell", "am", "start", "-n", APPS[a.app], "--es", "page", "./")  # 回到聊天页，关掉摄像头
        else:
            for relay in ((True,) if a.relay else (False, True)):
                room = "e2e" + secrets.token_hex(3)
                print(f"Mac 两个标签互打，房间 {room}，{'只走 ECS 中继' if relay else '不限路径'}")
                peer = await Tab.open()
                await peer.cmd("Page.enable")
                q = f"calltest?room={room}&mute=1" + ("&relay=1" if relay else "")
                await mac.go(BASE + q + "&who=a")
                await peer.go(BASE + q + "&who=b")
                good = await wait({"a": mac.stats, "b": peer.stats}, relay)
                print("  →", "通过" if good else "失败")
                passed &= good
                await peer.eval("document.getElementById('hang').click()")
                await mac.eval("document.getElementById('hang').click()")
        print("结果：", "通过" if passed else "失败")
        return 0 if passed else 1
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)
        subprocess.run(["adb", "forward", "--remove", "tcp:9334"], capture_output=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
