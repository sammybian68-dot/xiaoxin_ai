"""画图：draw_image。说明在 server/prompts/draw.md。

出一张图要 30–60 秒，不在工具里干等（回答会卡住、语音通话里没声音）：工具立刻返回「开始画了」，
后台线程画好后以小新的身份往这条线程发一条带图的消息（hub 推给大家，孩子的 pad 念一遍「XX，图片画好了，请看」），
没画出来也发一条说明。这 30–60 秒里孩子照常和小新聊天，互不影响。

模型：默认 grok-imagine-image-2.0，失败换 gpt-image-2；用户点名「用 gpt / image2 画」就先用 gpt-image-2，
失败再换 grok。2026-11-04 十个场景对比：grok 中文语境（人物、水墨、常识比例）错得少，gpt 简体字准、不乱加东西。

照着一张图画（ref）：「把这张图里的人变成飞起来的样子」「把刚才那张背景改成晚上」。只用 gpt-image-2——
2026-11-07 代理上试过，grok 两个图像模型的 /v1/images/edits 都不通（multipart 超时 / 503，JSON 400 / 422）。
gpt 是「照着重画」：认得出图里有什么、什么颜色，位置细节会变，不是逐像素修图。
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
    "description": "画一张图：文生图，或者照着聊天里的一张图改画（ref）。在后台画，大约半分钟，画好了会自动发到聊天里。",
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "画面描述：主体、动作、场景、风格、光线写清楚，用中文；"
                                                    "照图改画时写清楚要怎么改、哪些保持不变"},
        "model": {"type": "string", "enum": list(ORDER),
                  "description": "auto 默认；用户点名「用 gpt」「用 image2」「gpt-image」画时填 gpt"},
        "ref": {"type": "string",
                "description": "照着哪张图画：latest = 聊天里最近一张图（刚发的照片、上一张画的图）；"
                               "也可以填 list_printables 里的文件 id（f_…）。纯文生图不填"},
    }, "required": ["prompt"]},
}}]
NAMES = {"draw_image"}
REF_MODELS = [GPT]
RECENT = 12   # ref=latest 往回找几张图

_busy = threading.BoundedSemaphore(MAX_BUSY)


def _generate(prompt: str, model: str, out, ref=None) -> None:
    """单独一层，测试里换掉。"""
    from src.llm import image

    image.generate(prompt, out, ref=ref, model=model, timeout=TIMEOUT)


def _ref_image(thread: str, ref: str) -> tuple[str, object]:
    """ref 参数 → (文件 id, 喂模型的图)。只认这条线程里的图片，找不到抛 ValueError（话直接给模型）。"""
    try:
        msgs = store.get(thread)["messages"]
    except KeyError:
        msgs = []
    imgs = [f["id"] for m in reversed(msgs) for f in reversed(m.get("files") or []) if f.get("kind") == "image"][:RECENT]
    if ref in ("latest", "最近", "刚才的", "这张"):
        if not imgs:
            raise ValueError("这段对话里还没有图片，先让对方发一张照片过来")
        fid = imgs[0]
    else:
        fid = ref[2:] if ref.startswith("f_") else ref
        if fid not in imgs:
            raise ValueError(f"这段对话里最近没有这张图：{ref}（不确定就填 latest）")
    path = store.best_image(fid)
    if path is None:
        raise ValueError("那张图已经不在了，请对方再发一次")
    return fid, path


def _work(thread: str, kid: str, prompt: str, models: list[str], ref=None) -> None:
    ref_id, ref_path = ref or (None, None)
    try:
        errors = []
        for m in models:
            fid, path = store.allocate()
            try:
                _generate(prompt, m, path, ref=ref_path)
                meta = _commit(fid, path, prompt)
            except Exception as e:  # noqa: BLE001 代理报错、超时、返回的不是图：换下一个模型
                log.warning("draw %s failed: %s", m, e)
                errors.append(f"{m}：{str(e)[:120]}")
                store.abort(fid)
                continue
            log.info("draw %s ok -> %s", m, fid)
            note = "" if m == models[0] else f"（{models[0]} 没画出来，换 {m} 画的）"
            msg = {"role": "assistant", "text": f"{_call(kid)}图片画好了，请看。{note}", "model": m,
                   "via": "draw", "prompt": prompt, "files": [meta]}
            if ref_id:
                msg["ref"] = ref_id
            store.append(thread, msg)
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
    ref = None
    want = str(args.get("ref") or "").strip()
    if want:
        try:
            ref = _ref_image(thread, want)
        except ValueError as e:
            return f"失败：{e}"
        models = REF_MODELS
    if not _busy.acquire(blocking=False):
        return "失败：手上还有图在画，等那张画好再画下一张"
    try:
        threading.Thread(target=_work, args=(thread, ctx.get("kid_name", ""), prompt, models, ref),
                         daemon=True, name="draw").start()
    except Exception:
        _busy.release()
        raise
    return f"已开始画（{models[0]}），大约半分钟，画好会自动发到聊天里。播报：好，我画一下，大概半分钟。"
