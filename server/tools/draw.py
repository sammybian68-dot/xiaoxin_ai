"""画图：draw_image。说明在 server/prompts/draw.md。

出一张图要 30–60 秒，不在工具里干等（回答会卡住、语音通话里没声音）：工具立刻返回「开始画了」，
后台线程画好后以小新的身份往这条线程发一条带图的消息（hub 推给大家，孩子的 pad 念一遍「XX，图片画好了，请看」），
没画出来也发一条说明。这 30–60 秒里孩子照常和小新聊天，互不影响。

模型：默认 grok-imagine-image-2.0，失败换 gpt-image-2；用户点名「用 gpt / image2 画」就先用 gpt-image-2，
失败再换 grok。2026-11-04 十个场景对比：grok 中文语境（人物、水墨、常识比例）错得少，gpt 简体字准、不乱加东西。
"""

from __future__ import annotations

import io
import json
import logging
import threading

from server import store
from server.tools import printer as printer_tools

log = logging.getLogger("xiaoxin.draw")

GROK, GPT = "grok-imagine-image-2.0", "gpt-image-2"
ORDER = {"auto": [GROK, GPT], "gpt": [GPT, GROK]}
TIMEOUT = 180       # 一个模型最多等多久（实测 30–60 秒）
MAX_BUSY = 2        # 同时最多画几张，再多就让用户等一下
MAX_PROMPT = 1000

SPECS = [{"type": "function", "function": {
    "name": "draw_image",
    "description": "画一张图（文生图）。在后台画，大约半分钟，画好了会自动发到聊天里。",
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "画面描述：主体、动作、场景、风格、光线写清楚，用中文"},
        "model": {"type": "string", "enum": list(ORDER),
                  "description": "auto 默认；用户点名「用 gpt」「用 image2」「gpt-image」画时填 gpt"},
    }, "required": ["prompt"]},
}}]
NAMES = {"draw_image"}

_busy = threading.BoundedSemaphore(MAX_BUSY)


def _generate(prompt: str, model: str, out) -> None:
    """单独一层，测试里换掉。"""
    from src.llm import image

    image.generate(prompt, out, model=model, timeout=TIMEOUT)


def _work(thread: str, kid: str, prompt: str, models: list[str]) -> None:
    try:
        errors = []
        for m in models:
            fid, path = store.allocate()
            try:
                _generate(prompt, m, path)
                meta = _commit(fid, path, prompt)
            except Exception as e:  # noqa: BLE001 代理报错、超时、返回的不是图：换下一个模型
                log.warning("draw %s failed: %s", m, e)
                errors.append(f"{m}：{str(e)[:120]}")
                store.abort(fid)
                continue
            log.info("draw %s ok -> %s", m, fid)
            note = "" if m == models[0] else f"（{models[0]} 没画出来，换 {m} 画的）"
            store.append(thread, {"role": "assistant", "text": f"{_call(kid)}图片画好了，请看。{note}", "model": m,
                                  "via": "draw", "prompt": prompt, "files": [meta]})
            return
        store.append(thread, {"role": "assistant", "text": f"{_call(kid)}这张没画出来，等会儿再试试吧。", "model": "",
                              "via": "draw", "error": "；".join(errors)[:500]})
    finally:
        _busy.release()


def _call(kid: str) -> str:
    """开头叫一声名字（「卞依一，」），没有名字就不叫。"""
    return f"{kid}，" if kid else ""


def _commit(fid: str, path, prompt: str) -> dict:
    """生成的图存成普通文件（三档都有，聊天里能看大图、能打印）。返回的不是图就抛错。"""
    from PIL import Image

    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as im:
        fmt, (w, h) = (im.format or "PNG").lower(), im.size
    mime = {"jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "image/png")
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    name = "".join(c for c in prompt[:16] if c not in '\\/:*?"<>|\n') or "画"
    store.commit(fid, f"{name}.{ext}", mime, len(data), owner="xiaoxin")
    store.variant_path(fid, "thumb")
    store.variant_path(fid, "screen")
    return store.update_file(fid, width=w, height=h)


def run(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "失败：参数不是 JSON 对象"
    prompt = str(args.get("prompt") or "").strip()[:MAX_PROMPT]
    if not prompt:
        return "失败：没有画面描述，先问用户想画什么"
    models = ORDER.get(str(args.get("model") or "auto"), ORDER["auto"])
    ctx = printer_tools.ctx.get()
    thread = ctx.get("thread", "")
    if not thread:
        return "失败：这里不能画图（不在聊天里）"
    if not _busy.acquire(blocking=False):
        return "失败：手上还有图在画，等那张画好再画下一张"
    try:
        threading.Thread(target=_work, args=(thread, ctx.get("kid_name", ""), prompt, models), daemon=True, name="draw").start()
    except Exception:
        _busy.release()
        raise
    return f"已开始画（{models[0]}），大约半分钟，画好会自动发到聊天里。播报：好，我画一下，大概半分钟。"
