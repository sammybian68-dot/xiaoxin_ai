"""实时同步端到端：连线上的小新，两个无头 Chrome 标签以两种身份打开同一个孩子的线程，一边发、另一边不刷新就出现。

    .venv/bin/python -m tests.e2e.hub

用一个临时测试孩子「e2e 测试」和临时家长「e2e 家长」，不碰真实孩子的线程；跑完彻底删掉（线程先打包进 backup/，这是正常删除流程）。
key 从 .env 读，只经 HTTP header 和 CDP 传，不出现在命令行和输出里。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

from tests.e2e.call import BASE, CHROME, PORT, Tab, env

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE  # 自签证书，只用于这个测试


def api(method: str, path: str, token: str, body: dict | None = None, raw: bool = False):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    data = urllib.request.urlopen(req, context=CTX, timeout=120).read()
    return data if raw else json.loads(data or b"{}")


async def main() -> int:
    key = env("XIAOXIN_KEY")
    stamp = time.strftime("%H%M%S")
    kid = api("POST", "api/members", key, {"role": "kid", "name": f"e2e测试{stamp}"})
    dad = api("POST", "api/members", key, {"role": "parent", "name": f"e2e家长{stamp}"})
    code = api("POST", f"api/members/{dad['id']}/pair", key, {"kind": "web"})["code"]
    req = urllib.request.Request(BASE + "api/pair", method="POST", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"})
    dad_token = json.loads(urllib.request.urlopen(req, context=CTX, timeout=20).read())["token"]
    profile = tempfile.mkdtemp(prefix="xx-hub-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                               "--ignore-certificate-errors", "--no-first-run", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except OSError:
                await asyncio.sleep(0.25)
        admin = await Tab.open()
        await admin.cmd("Page.enable")
        await admin.go(BASE + "?k=" + key)
        # 管理员看这个测试孩子的线程：openThread 默认第一个孩子，这里直接换成测试孩子的再接 hub
        await admin.cmd("Runtime.evaluate", awaitPromise=True, expression=f"""(async()=>{{state.conv = await api('api/thread?kid={kid["id"]}').then(json); renderThread(); hubConnect();}})()""")
        assert await admin.eval("state.conv.id") == kid["thread"]
        # 页面自己记收到每条 hub 消息的时刻（Date.now，和这边同一台机器的时钟），不靠轮询 DOM 估
        await admin.eval("window.__hubAt={}; const _o=hub.ws.onmessage; hub.ws.onmessage=(e)=>{const d=JSON.parse(e.data);"
                         "if(d.message) window.__hubAt[d.message.role]=window.__hubAt[d.message.role]||Date.now(); _o(e)}")
        await asyncio.sleep(1)
        assert await admin.eval("hub.ws && hub.ws.readyState") == 1, "hub 没连上"
        text = f"e2e 同步测试 {stamp}"
        loop = asyncio.get_running_loop()
        t0 = time.time() * 1000
        reply = await loop.run_in_executor(None, lambda: api("POST", f"api/conversations/{kid['thread']}/messages", dad_token,
                                                             {"text": text}, raw=True))
        t_sse_done = time.time() * 1000 - t0  # 家长那台：发出去到收完小新回答
        await asyncio.sleep(1.5)
        at = await admin.eval("window.__hubAt") or {}
        t_user = at.get("user") and at["user"] - t0
        t_reply = at.get("assistant") and at["assistant"] - t0
        dup = await admin.eval(f"[...document.querySelectorAll('#messages .msg.me')].filter(e=>e.textContent.includes({json.dumps(text)})).length")
        print(f"管理员页面（没刷新）收到家长那句：发出后 {t_user:.0f} ms；收到小新回答：{t_reply:.0f} ms"
              f"（家长那边 {t_sse_done:.0f} ms 收完回答，差 {t_reply - t_sse_done:+.0f} ms）" if t_user and t_reply else f"没收到：{at}")
        print(f"家长那句在页面上 {dup} 行（应为 1）")
        ok = bool(t_user and t_reply) and t_user < 1000 and t_reply - t_sse_done < 1000 and dup == 1
        return 0 if ok else 1
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)
        for m in (kid, dad):
            api("DELETE", f"api/members/{m['id']}?hard=1", key)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
