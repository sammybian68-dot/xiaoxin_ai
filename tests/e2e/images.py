"""图片分档端到端：Mac 无头 Chrome 以临时家长身份，往临时孩子的群里发一张 4000x3000 的照片，量真实流量。

    .venv/bin/python -m tests.e2e.images

两次：不勾原图（只传 thumb + screen）、勾原图（再后台分片传原图，传完和本地字节完全一样）。
每次量：上传的字节数（Chrome 的 Network 事件）、消息多久出现在服务端、原图多久传完。
临时成员跑完彻底删（线程先打包进 backup/）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from tests.e2e.call import BASE, CHROME, PORT, Tab, env
from tests.e2e.hub import CTX, api


def photo(seed: int) -> bytes:
    """4000x3000 的 JPEG，像手机拍的作业纸：浅色纸面 + 渐变光照 + 一些字和细噪声（3～5 MB）。
    满屏随机噪声是 JPEG 最坏情况，2048 也有 1 MB 多，不像真照片。"""
    import random

    from PIL import Image, ImageDraw, ImageFilter

    random.seed(seed)
    im = Image.linear_gradient("L").resize((4000, 3000)).convert("RGB")
    im = Image.blend(Image.new("RGB", (4000, 3000), (245, 238, 220)), im, 0.25)
    d = ImageDraw.Draw(im)
    for i in range(40):
        y = 150 + i * 70
        d.line([(200, y + 40), (3800, y + 40)], fill=(180, 200, 230), width=3)
        for j in range(30):
            x = 220 + j * 118 + random.randint(-10, 10)
            d.rectangle([x, y, x + random.randint(40, 90), y + 30], fill=(40, 40, 60))
    noise = Image.effect_noise((4000, 3000), 18).convert("RGB")
    im = Image.blend(im, noise, 0.06).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


async def run_once(tab: Tab, kid: dict, data: bytes, orig: bool) -> dict:
    b64 = base64.b64encode(data).decode()
    await tab.cmd("Network.enable")
    sent_bytes = 0
    t0 = time.monotonic()
    # 把文件塞进页面，走正常的 upload()（本地压图 → 分档上传），再点发送
    js = f"""(async()=>{{
      const bin = Uint8Array.from(atob({json.dumps(b64)}), c => c.charCodeAt(0));
      const file = new File([bin], "IMG_{int(orig)}.jpg", {{ type: "image/jpeg" }});
      state.sendOrig = {str(orig).lower()};
      await upload(file);
      const m = state.pending.at(-1).meta;
      document.getElementById("input").value = "e2e 图片 {'原图' if orig else '不带原图'}";
      updateSend();
      await send(document.getElementById("input").value.trim());
      return JSON.stringify(m);
    }})()"""
    r = await tab.cmd("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)
    if "exceptionDetails" in r:
        raise RuntimeError(r["exceptionDetails"])
    meta = json.loads(r["result"]["value"])
    t_msg = time.monotonic() - t0
    fid = meta["id"]
    done_at = None
    if orig:
        for _ in range(600):
            st = json.loads(urllib.request.urlopen(urllib.request.Request(
                BASE + f"api/files/{fid}/original/status", headers={"Authorization": f"Bearer {tab.token}"}),
                context=CTX, timeout=20).read())
            if st["received"] >= st["size"]:
                done_at = time.monotonic() - t0
                break
            await asyncio.sleep(0.5)
    # 量：服务端拿到的各档大小
    info = json.loads(urllib.request.urlopen(urllib.request.Request(
        BASE + f"api/thread?kid={kid['id']}", headers={"Authorization": f"Bearer {tab.token}"}), context=CTX).read())
    files = [f for m in info["messages"] for f in (m.get("files") or []) if f["id"] == fid]
    got = urllib.request.urlopen(urllib.request.Request(BASE + f"api/files/{fid}", headers={"Authorization": f"Bearer {tab.token}"}),
                                 context=CTX, timeout=120).read()
    v = (files[0] if files else meta).get("variants") or {}
    sent_bytes = v.get("thumb", 0) + v.get("screen", 0) + (len(data) if orig else 0)
    return {"t_msg": t_msg, "t_orig": done_at, "thumb": v.get("thumb"), "screen": v.get("screen"),
            "sent": sent_bytes, "same": hashlib.sha256(got).hexdigest() == hashlib.sha256(data).hexdigest(), "got": len(got)}


async def main() -> int:
    key = env("XIAOXIN_KEY")
    stamp = time.strftime("%H%M%S")
    kid = api("POST", "api/members", key, {"role": "kid", "name": f"e2e图{stamp}"})
    dad = api("POST", "api/members", key, {"role": "parent", "name": f"e2e图家长{stamp}"})
    code = api("POST", f"api/members/{dad['id']}/pair", key, {"kind": "web"})["code"]
    req = urllib.request.Request(BASE + "api/pair", method="POST", data=json.dumps({"code": code}).encode(),
                                 headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, context=CTX, timeout=20).read())["token"]
    profile = tempfile.mkdtemp(prefix="xx-img-")
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
                               "--ignore-certificate-errors", "--no-first-run", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = True
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except OSError:
                await asyncio.sleep(0.25)
        tab = await Tab.open()
        tab.token = token
        await tab.cmd("Page.enable")
        await tab.go(BASE + "?k=" + token)  # 家长设备的身份
        await asyncio.sleep(2)
        await tab.cmd("Runtime.evaluate", awaitPromise=True,
                      expression=f"(async()=>{{ await enterChat({json.dumps(kid['id'])}); }})()")
        for orig, seed in ((False, 1), (True, 2)):
            data = photo(seed)
            r = await run_once(tab, kid, data, orig)
            mb = lambda n: f"{n / 1048576:.2f} MB" if n else "-"  # noqa: E731
            print(f"{'勾原图' if orig else '不勾原图'}：照片 {mb(len(data))}；上传 thumb {r['thumb'] and r['thumb'] // 1024} KB + "
                  f"screen {r['screen'] and r['screen'] // 1024} KB{' + 原图' if orig else ''}，共 {mb(r['sent'])}；"
                  f"消息 {r['t_msg']:.1f}s 发出" + (f"，原图 {r['t_orig']:.1f}s 传完" if orig and r['t_orig'] else ""))
            if orig:
                good = r["same"] and r["t_orig"] is not None
                print(f"  {'✓' if good else '✗'} 下载回来的原图和本地字节一样（{mb(r['got'])}）")
            else:
                good = r["sent"] < len(data) / 3 and not r["same"]
                print(f"  {'✓' if good else '✗'} 没传原图：对方拿到的是 2048 的大图（{mb(r['got'])}），省了 {mb(len(data) - r['sent'])}")
            ok &= good
        print("结果：", "通过" if ok else "失败")
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
