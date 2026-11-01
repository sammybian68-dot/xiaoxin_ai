"""视频来电端到端：Mac 无头 Chrome 当「爸爸」（临时配一台家长浏览器），打给 pad 上的小新（第一个孩子）。

    .venv/bin/python -m tests.e2e.incall --pad SERIAL

看：pad 弹全屏「爸爸来电」+ 倒计时 → 到 0 自动接通 → 两边视频都通（读 iframe 里的 callStats）→ 爸爸挂断 → pad 回到聊天、
待命恢复 → 线程里多一条「📹 视频通话 N 秒」。每一步截 pad 的屏（tmp/shot/incall_*.png）。
临时家长设备跑完踢掉（成员「爸爸」留着）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

from tests.e2e.call import BASE, CHROME, PORT, Tab, adb, env
from tests.e2e.hub import CTX, api


async def pad_page(serial: str):
    pid = adb(serial, "shell", "pidof", "com.xiaoxin.app").split()[0]
    subprocess.run(["adb", "-s", serial, "forward", "tcp:9342", f"localabstract:webview_devtools_remote_{pid}"], check=True,
                   capture_output=True)
    pages = [p for p in json.loads(urllib.request.urlopen("http://127.0.0.1:9342/json").read()) if p["type"] == "page"]
    main = next(p for p in pages if "calltest" not in p["url"])
    return Tab(await websockets.connect(main["webSocketDebuggerUrl"], max_size=None))


def shot(serial: str, name: str) -> None:
    subprocess.run(f"adb -s {serial} exec-out screencap -p > tmp/shot/incall_{name}.png", shell=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", required=True)
    a = ap.parse_args()
    key = env("XIAOXIN_KEY")
    ms = api("GET", "api/members", key)
    dad = next(m for m in ms["parents"] if m["name"] == "爸爸")
    kid = ms["kids"][0]
    code = api("POST", f"api/members/{dad['id']}/pair", key, {"kind": "web", "name": "e2e 来电测试（跑完踢掉）"})["code"]
    req = urllib.request.Request(BASE + "api/pair", method="POST", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, context=CTX, timeout=20).read())["token"]
    profile = tempfile.mkdtemp(prefix="xx-incall-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                               "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--ignore-certificate-errors",
                               "--autoplay-policy=no-user-gesture-required", "--no-first-run", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = True

    def check(name, good):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'✓' if good else '✗'} {name}")

    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except OSError:
                await asyncio.sleep(0.25)
        adb(a.pad, "shell", "am", "start", "-n", "com.xiaoxin.app/.TalkActivity")
        await asyncio.sleep(3)
        pad = await pad_page(a.pad)
        was_standby = await pad.eval("standby.on")
        dad_tab = await Tab.open()
        await dad_tab.cmd("Page.enable")
        await dad_tab.go(BASE + "?k=" + token)
        await asyncio.sleep(3)
        await dad_tab.cmd("Runtime.evaluate", awaitPromise=True, expression=f"enterChat({json.dumps(kid['id'])})")
        await asyncio.sleep(1)
        # 在 pad 页里记：收到 call.invite 的时刻、来电界面画出来的时刻（同一个时钟）；Mac 这边记点击时刻 → 用服务端回包时间对齐
        await pad.eval("window.__inv=0; window.__shown=0; (()=>{ const _o=hub.ws.onmessage; hub.ws.onmessage=(e)=>{ if(e.data.includes('call.invite')) window.__inv=performance.now(); _o(e); if(e.data.includes('call.invite')) requestAnimationFrame(()=>{ window.__shown=performance.now() }) } })()")
        t0 = time.monotonic()
        res = await dad_tab.cmd("Runtime.evaluate", awaitPromise=True, returnByValue=True, expression=(
            "(async()=>{ const t=performance.now(); document.getElementById('call-kid').click();"
            " while(!call.cur) await new Promise(r=>setTimeout(r,10)); return Math.round(performance.now()-t) })()"))
        post_ms = res.get("result", {}).get("value")
        rang = False
        for _ in range(30):
            if await pad.eval("window.__shown > 0"):
                rang = True
                break
            await asyncio.sleep(0.3)
        lag = await pad.eval("Math.round(window.__shown - window.__inv)")
        check(f"pad 弹出来电：家长点击到服务端开好房间 {post_ms} ms，pad 收到推送到画出来电界面 {lag} ms", rang)
        print("     ", await pad.eval("document.getElementById('call-title').textContent"))
        await asyncio.sleep(1.2)
        shot(a.pad, "ring")
        n1 = await pad.eval("document.getElementById('call-count').textContent")
        await asyncio.sleep(1.1)
        n2 = await pad.eval("document.getElementById('call-count').textContent")
        check(f"倒计时在走：{n1} → {n2}", n1 and n2 and int(n2) < int(n1))
        check("来电时待命停了（让出麦克风）", not await pad.eval("standby.on") or not was_standby)
        talking = False
        for _ in range(40):
            if await pad.eval("!document.getElementById('call-frame').hidden && document.getElementById('call-frame').src.includes('calltest')"):
                talking = True
                break
            await asyncio.sleep(0.25)
        check(f"到 0 自动接通（{time.monotonic() - t0:.1f}s）", talking)
        # 两边视频：爸爸这边的 iframe、pad 那边的 iframe 都读 callStats
        conn = {}
        for _ in range(40):
            await asyncio.sleep(1)
            d = await dad_tab.eval("JSON.stringify(document.getElementById('call-frame').contentWindow.callStats||{})")
            p = await pad.eval("JSON.stringify(document.getElementById('call-frame').contentWindow.callStats||{})")
            conn = {"dad": json.loads(d or "{}"), "pad": json.loads(p or "{}")}
            if all(v.get("conn") == "connected" and v.get("res") not in (None, "-") for v in conn.values()):
                break
        for k, v in conn.items():
            print(f"     {k}: conn={v.get('conn')} 路径={v.get('path')} 收到 {v.get('res')}@{v.get('fps')}fps")
        check("两边视频都通", all(v.get("conn") == "connected" and v.get("res") not in (None, "-") for v in conn.values()))
        shot(a.pad, "talk")
        await asyncio.sleep(3)
        await dad_tab.eval("document.getElementById('call-frame').contentWindow.document.getElementById('end').click()")
        closed = False
        for _ in range(30):
            await asyncio.sleep(0.3)
            if await pad.eval("document.getElementById('call').hidden"):
                closed = True
                break
        check("爸爸挂断后 pad 回到聊天", closed)
        await asyncio.sleep(2)
        check("待命恢复", (await pad.eval("standby.on")) == was_standby)
        last = await pad.eval("[...document.querySelectorAll('#messages .msg')].pop().textContent")
        check(f"群里记了一条：{(last or '').strip()[:30]}", "视频通话" in (last or ""))
        shot(a.pad, "after")
        print("结果：", "通过" if ok else "失败")
        return 0 if ok else 1
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)
        subprocess.run(["adb", "-s", a.pad, "forward", "--remove", "tcp:9342"], capture_output=True)
        for d in api("GET", "api/members", key)["parents"]:
            for dev in d["devices"]:
                if dev["name"].startswith("e2e 来电测试"):
                    api("DELETE", f"api/devices/{dev['id']}", key)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
