"""小新 web：对话（文字 / 图片 / 文件 / 按住说话）和声音录制。

只听 127.0.0.1:8184，对外由 gpu2 上 mainbrain 的 8183（https）在 /xiaoxin/ 下转发过来。
不输密码，用免密链接：打开一次 /xiaoxin/?k=<XIAOXIN_KEY>，服务端种 cookie 并跳回 /xiaoxin/，之后浏览器记住一年。
APK 用 Authorization: Bearer <XIAOXIN_KEY>。换 key 后旧链接、旧 cookie 全部失效。
"""

from __future__ import annotations

import asyncio

import hashlib
import hmac
import io
import json
import logging
import re
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server import clock, prompts, store, voices
from server.tools import registry
from src.llm import client
from src.search import web_search
from src.voice import doubao, paraformer

STATIC = Path(__file__).resolve().parent / "static"
PORT = 8184
COOKIE = "xiaoxin_auth"

# 下拉里给的模型。第一个是默认；带图片的消息自动换成 VISION（deepseek 不看图）。
MODELS = [
    {"id": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash"},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
    {"id": "grok-4.7", "label": "Grok 4.7"},
    {"id": "qwen3.8-27b", "label": "主脑 Qwen3.8 27B"},
]
VISION = "claude-sonnet-5-5"
FALLBACK = "claude-sonnet-5-5"  # 选的模型连着失败两次就换它
NO_VISION = {"deepseek-v4.1-flash", "qwen3.8-27b"}  # 主脑是纯文本模型

# system prompt 按功能拆在 server/prompts/*.md，prompts.build() 拼起来（填上当前时间、闹钟状态）
MAX_SEARCHES = 2  # 一轮回答最多搜几次，照 carHer2 的 _WEB_SEARCH_TURN_LIMIT
MAX_TOOL_ROUNDS = MAX_SEARCHES + 1  # 到上限后再给模型一次机会，它还要调工具就算这轮失败
SEARCH_LIMIT_RESULT = (f"已经搜了 {MAX_SEARCHES} 次，到上限了，这次没有再搜。"
                       "请基于上面已有的搜索结果直接回答用户；查不到就如实说。")

log = logging.getLogger("xiaoxin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="xiaoxin", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- 登录

def _key() -> str:
    key = doubao.env("XIAOXIN_KEY")
    if len(key) < 20:
        raise HTTPException(503, "服务端没配 XIAOXIN_KEY（至少 20 位随机串）")
    return key


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


def _token(key: str) -> str:
    # cookie 里放派生值，不放 key 本身
    return hmac.new(key.encode(), b"xiaoxin-web", hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    key = _key()
    cookie = request.cookies.get(COOKIE) or ""
    if cookie and _same(cookie, _token(key)):
        return True
    auth = request.headers.get("authorization") or ""
    given = auth[7:] if auth.lower().startswith("bearer ") else ""
    return bool(given) and _same(given, key)


def _remember(request: Request, resp: Response, key: str) -> Response:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    # 挂在 8183/xiaoxin/ 下时，cookie 只给 /xiaoxin/ 用，不带给同域的训练平台
    prefix = (request.headers.get("x-forwarded-prefix") or "").rstrip("/")
    resp.set_cookie(COOKIE, _token(key), max_age=365 * 86400, httponly=True, samesite="lax", secure=secure,
                    path=prefix + "/" if prefix.startswith("/") else "/")
    return resp


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/health":
        try:
            ok = _authed(request)
        except HTTPException as e:
            return Response(json.dumps({"detail": e.detail}, ensure_ascii=False), e.status_code,
                            media_type="application/json")
        if not ok:
            return Response('{"detail":"要先登录"}', 401, media_type="application/json")
    response = await call_next(request)
    # 页面和 static/ 下的 js / css 都要每次跟服务端确认：不然部署后 pad 的 WebView 还拿着旧的 app.js / app.css，
    # 新旧混着用，样式和逻辑对不上（2026-10-30 端侧调试列表就因为旧 css 挤成一团）
    if response.headers.get("content-type", "").startswith("text/html") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.post("/api/logout")
def logout(request: Request) -> Response:
    prefix = (request.headers.get("x-forwarded-prefix") or "").rstrip("/")
    resp = Response('{"ok":true}', media_type="application/json")
    resp.delete_cookie(COOKIE, path=prefix + "/" if prefix.startswith("/") else "/")
    return resp


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "xiaoxin", "port": PORT}


@app.get("/api/me")
def me() -> dict:
    return {"ok": True, "models": MODELS, "default_model": MODELS[0]["id"], "system_prompt": prompts.build(),
            "vision_model": VISION, "fallback_model": FALLBACK}


def _index_html() -> Response:
    """index.html 里的 app.js / app.css 地址带上文件修改时间，部署后浏览器和 pad 一定拿新的。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css", "clock.js", "clock.css", "hanzi.js", "hanzi.css", "timecard.js", "timecard.css"):
        v = int((STATIC / name).stat().st_mtime)
        html = html.replace(f'static/{name}"', f'static/{name}?v={v}"')
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index(request: Request, k: str = "") -> Response:
    """带 ?k= 是免密链接：对了就种 cookie，再跳回不带 k 的地址，免得 key 留在地址栏和历史记录里。"""
    if k:
        key = _key()
        if not _same(k, key):
            resp = _index_html()
            resp.headers["X-Xiaoxin-Link"] = "bad"
            return resp
        resp = RedirectResponse("./", status_code=303)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return _remember(request, resp, key)
    return _index_html()


@app.get("/voice")
def voice_page() -> RedirectResponse:
    return RedirectResponse("./#settings/voice")


# 挑 CosyVoice 男声用的试听页：页面和音频放在数据目录 voice/pick/ 下，不进仓库。和 /api 一样要登录。
PICK_DIR = store.DATA / "voice" / "pick"


@app.get("/pick/{name}")
def voice_pick(request: Request, name: str = "index.html") -> FileResponse:
    if not _authed(request):
        raise HTTPException(401, "要先用免密链接打开一次")
    path = (PICK_DIR / name).resolve()
    if PICK_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "没有这个文件")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- 提示词和工具（设置页查看）

@app.get("/api/prompts")
def prompt_view(conversation: str = "") -> dict:
    """每个 md 的原文、当前拼好的完整 system prompt（文字 / 语音两份）、注册的工具、压缩用的提示词；
    给了 ?conversation= 再带上这段对话上一次压缩出来的摘要。"""
    from server import compact

    out = {"parts": prompts.parts(), "system": prompts.build(), "system_voice": prompts.build(voice=True),
           "tools": registry.listing(), "compact": compact.prompt_text()}
    if conversation:
        try:
            conv = store.get(conversation)
        except KeyError:
            conv = None
        if conv:
            out["summary"] = {"title": conv.get("title") or "新对话", "text": conv.get("summary") or "",
                              **compact.state(conv)}
    return out


@app.get("/api/debug/{cid}")
def debug_turns(cid: str) -> dict:
    """调试面板：这段对话最近 20 轮里每次发给主脑的请求原样（system、messages、tools、回了什么、用量、耗时），
    加上当前的上下文状态和压缩摘要。只在内存里，服务重启就没了。"""
    from server import compact, trace

    out = {"turns": trace.turns(cid)}
    try:
        conv = store.get(cid)
        out["ctx"] = compact.state(conv)
        out["summary"] = conv.get("summary") or ""
        out["epoch_start"] = conv.get("epoch_start") or 0
    except KeyError:
        pass
    return out


@app.get("/api/conversations/{cid}/ctx")
def conversation_ctx(cid: str) -> dict:
    """左侧栏电量条：用了多少 token、阈值、压缩过几次、现在在不在压。"""
    from server import compact

    try:
        return compact.state(store.get(cid))
    except KeyError:
        raise HTTPException(404, "没有这段对话")


@app.post("/api/conversations/{cid}/compact")
def conversation_compact(cid: str) -> dict:
    """「立即压缩」：不看阈值，后台压一次；压好后下一轮开始时换上。"""
    from server import compact

    try:
        store.get(cid)
    except KeyError:
        raise HTTPException(404, "没有这段对话")
    started = compact.maybe_kick(cid, force=True)
    return {"started": started, **compact.state(store.get(cid))}


@app.put("/api/ctx_limit")
def ctx_limit_set(body: dict = Body(...)) -> dict:
    from server import compact

    try:
        return {"limit": compact.set_limit(body.get("limit"))}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 笔顺

@app.get("/api/hanzi/{ch}")
def hanzi_char(ch: str) -> Response:
    """一个字的笔画轮廓、中线、笔画名、拼音、部首，前端 Hanzi Writer 画动画用。按字缓存一天。"""
    from server import hanzi

    e = hanzi.lookup(ch[:1]) if ch else None
    if e is None:
        raise HTTPException(404, f"没有「{ch[:1]}」的笔顺数据")
    return Response(json.dumps(e, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- 闹钟 / 倒计时 / 正计时

def _clock_call(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except clock.ClockError as e:
        raise HTTPException(400, str(e))
    return clock.snapshot()


@app.get("/api/clock")
async def clock_state(since: int = -1, wait: float = 0) -> dict:
    """since 是上次拿到的 version：有变化马上返回，没有就最多等 wait 秒（pad 长轮询用，最多 55 秒）。"""
    if since >= 0 and wait > 0:
        return await asyncio.to_thread(clock.wait_change, since, min(wait, 55.0))
    return clock.snapshot()


@app.post("/api/clock/alarms")
def alarm_add(body: dict = Body(...)) -> dict:
    return _clock_call(clock.add_alarm, body.get("time"), body.get("repeat"), body.get("label") or "",
                       body.get("date"), body.get("say") or "")


@app.patch("/api/clock/alarms/{aid}")
def alarm_edit(aid: str, body: dict = Body(...)) -> dict:
    fields = {k: body[k] for k in ("time", "repeat", "date", "label", "say", "enabled") if k in body}
    return _clock_call(clock.update_alarm, aid, **fields)


@app.post("/api/clock/alarms/{aid}/snooze")
def alarm_snooze(aid: str, body: dict | None = Body(None)) -> dict:
    return _clock_call(clock.snooze_alarm, aid, (body or {}).get("minutes"))


@app.post("/api/clock/alarms/{aid}/dismiss")
def alarm_dismiss(aid: str) -> dict:
    return _clock_call(clock.dismiss_alarm, aid)


@app.delete("/api/clock/alarms/{aid}")
def alarm_remove(aid: str) -> dict:
    return _clock_call(clock.delete_alarms, [aid])


@app.post("/api/clock/timers")
def timer_add(body: dict = Body(...)) -> dict:
    return _clock_call(clock.start_timer, body.get("seconds"), body.get("label") or "", body.get("say") or "")


@app.post("/api/clock/timers/{tid}/{action}")
def timer_act(tid: str, action: str, body: dict | None = Body(None)) -> dict:
    return _clock_call(clock.control_timer, tid, action, (body or {}).get("seconds"))


@app.put("/api/clock/ring_text")
def ring_text_set(body: dict = Body(...)) -> dict:
    return _clock_call(clock.set_ring_text, body.get("text"))


@app.post("/api/clock/stopwatch/{action}")
def stopwatch_act(action: str) -> dict:
    return _clock_call(clock.control_stopwatch, action)


# ---------------------------------------------------------------- 对话

@app.get("/api/conversations")
def conversations() -> dict:
    return {"conversations": store.list_conversations()}


@app.post("/api/conversations")
def new_conversation() -> dict:
    return store.create()


@app.get("/api/conversations/{cid}")
def conversation(cid: str) -> dict:
    try:
        return store.get(cid)
    except KeyError:
        raise HTTPException(404, "没有这段对话")


@app.get("/api/conversations/{cid}/audio/{mid}")
def utterance(cid: str, mid: str) -> FileResponse:
    """自由对话里用户那句的原声（服务端断句后送去识别的那段），气泡上的小喇叭回放用。"""
    try:
        path = store.utterance_path(cid, mid)
    except KeyError:
        path = None
    if path is None:
        raise HTTPException(404, "这句没存原声")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str) -> dict:
    try:
        store.delete(cid)
    except KeyError:
        raise HTTPException(404, "没有这段对话")
    return {"ok": True}


def _attachments(file_ids: list) -> list[dict]:
    out = []
    for fid in file_ids[:10]:
        info = store.file_info(str(fid))
        if info is None:
            raise HTTPException(400, f"附件 {fid} 不存在")
        out.append(info[1])
    return out


def _llm_messages(conv: dict) -> list[dict]:
    """对话转成 OpenAI 格式：压缩摘要（有的话）+ 这个 epoch 的原文（含 `[system]` 现状消息，按原位置回放）。

    前缀稳定（KV cache）：同一个 epoch 里，前面发过的消息每轮都原样重放，只在末尾追加；不按条数截断
    （截了前缀就变）。长了靠 compact 换 epoch，见 server/compact.py。
    只有最后一条用户消息带图片本体，之前的图片换成一句占位，免得每轮重传。"""
    from server import compact

    msgs = conv["messages"][conv.get("epoch_start") or 0:]
    last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
    out = []
    if conv.get("summary"):
        out.append({"role": "user", "content": compact.SUMMARY_PREFIX + conv["summary"]})
    for i, m in enumerate(msgs):
        if m["role"] == "note":
            out.append({"role": "user", "content": m["text"]})
            continue
        if m["role"] == "assistant":
            # 这一轮调过闹钟类工具：按原样回放「调用 → 结果」，不然模型只看到自己说「设好了」，
            # 下一轮会学着不调工具直接说「删掉了」（2026-10-30 27B 实测 3 轮里 2 轮这样）
            for k, a in enumerate(m.get("actions") or []):
                cid = f"h{m['id']}_{k}"
                out.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": cid, "type": "function",
                     "function": {"name": a["name"], "arguments": a.get("arguments") or "{}"}}]})
                out.append({"role": "tool", "tool_call_id": cid, "content": a["result"]})
            if m.get("text"):
                out.append({"role": "assistant", "content": strip_role_tag(m["text"])})
            continue
        parts: list[dict] = []
        for meta in m.get("files") or []:
            info = store.file_info(meta["id"])
            if info is None:
                parts.append({"type": "text", "text": f"[附件 {meta['name']} 已经不在了]"})
            elif meta["kind"] == "image" and i == last_user:
                parts.append({"type": "image_url", "image_url": {"url": store.data_url(info[0], meta["mime"])}})
            elif meta["kind"] == "image":
                parts.append({"type": "text", "text": f"[之前发过一张图片：{meta['name']}]"})
            elif meta["kind"] == "text":
                parts.append({"type": "text", "text": f"文件 {meta['name']}：\n```\n{store.read_text(info[0])}\n```"})
            else:
                parts.append({"type": "text", "text": f"[文件 {meta['name']}，{meta['size']} 字节，{meta['mime']}，"
                                                      "这里读不了内容]"})
        if m.get("text"):
            parts.append({"type": "text", "text": m["text"]})
        if parts:
            only_text = all(p["type"] == "text" for p in parts)
            out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts) if only_text else parts})
    return out


def _sse(event: str, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _run_tool(name: str, arguments: str) -> web_search.Result:
    if name != web_search.NAME:
        return web_search.Result(False, f"没有这个工具：{name}")
    return web_search.search(arguments)


# 回答开头不该出现的角色标记：对话里有「[system] …」现状消息，27B 偶尔学着在自己回答开头写「[assistant]」
ROLE_TAG = re.compile(r"^\s*(\[(assistant|system|user)\]|assistant\s*[:：]|小新\s*[:：])\s*", re.I)
ROLE_TAG_PREFIXES = ("[assistant]", "[system]", "[user]", "assistant:", "assistant：", "小新:", "小新：")


def strip_role_tag(text: str) -> str:
    return ROLE_TAG.sub("", text or "", count=1)


class RoleTagFilter:
    """流式地剥掉回答最开头的角色标记。开头几个字可能是标记的前半截（「[assi」），先压着，
    确定是标记就剥掉、确定不是就原样放出；过了开头就直通。"""

    def __init__(self):
        self.buf, self.done, self.trim = "", False, False

    def feed(self, chunk: str) -> str:
        if self.done:
            if self.trim:  # 刚剥掉标记：紧跟的空白也不要
                chunk = chunk.lstrip()
                self.trim = not chunk
            return chunk
        self.buf += chunk
        head = self.buf.lstrip().lower()
        if ROLE_TAG.match(self.buf) and not any(p.startswith(head) and p != head for p in ROLE_TAG_PREFIXES):
            out, self.done = strip_role_tag(self.buf).lstrip(), True
            self.buf, self.trim = "", not out
            return out
        if head and any(p.startswith(head) for p in ROLE_TAG_PREFIXES):
            return ""  # 还像标记的前半截，再等一块
        out, self.done, self.buf = self.buf, True, ""
        return out


MAX_CLOCK_CALLS = 8  # 一轮里本地工具（时间、闹钟）最多调几次（「把闹钟全删了再设两个」也够）


def _llm_thread(model: str, history: list[dict], q, loop, voice: bool = False, cid: str | None = None) -> None:
    """在线程里跑同步的流式请求，每个事件丢回事件循环的队列。

    工具循环：模型要搜就搜（发 search 事件），把结果接在后面再问一次，一轮最多搜 MAX_SEARCHES 次。
    到了上限工具照样给，只是不真搜，回一句「到上限了，用已有结果回答」（照 carHer2）。
    不能直接把 tools 拿掉：主脑 27B 看到历史里有工具调用还会接着调，vLLM 没收到 tools 就不解析，
    <tool_call> 被当特殊 token 过滤掉，正文是空的（2026-10-29 复现 6 次空 4 次）。
    重试和兜底：还没吐出正文、也还没搜过就失败（报错或空回答），同一个模型再试一次，再不行换 FALLBACK；
    已经吐了字或搜过了就不换，免得前后两截、重复搜。"""
    def put(*item):
        loop.call_soon_threadsafe(q.put_nowait, item)

    from server import trace

    tr = trace.new_turn(cid, model=model, voice=voice)
    tries = [model, model] + ([FALLBACK] if model != FALLBACK else [])
    got, usage, served, last = False, {}, model, ""
    msgs, searches, searched, rounds, clock_calls, actions = list(history), 0, [], 0, 0, []
    attempt = 0
    while attempt < len(tries):
        m = tries[attempt]
        if attempt and m != tries[attempt - 1]:
            put("model", m)
        served = m
        calls: list[dict] = []
        text = ""
        head_filter = RoleTagFilter()
        tools = registry.SPECS
        system = prompts.build(voice=voice)
        rec = trace.add_call(tr, model=m, system=system, messages=[dict(x) for x in msgs],
                             tools=[t["function"]["name"] for t in tools])
        t_req = time.monotonic()
        try:
            for kind, value in client.iter_stream(m, msgs, system=system, max_tokens=4096, timeout=180,
                                                  tools=tools):
                if kind == "model":
                    served = value
                    put("model", value)
                elif kind == "usage":
                    usage = value
                elif kind == "reasoning":
                    put("reasoning", value)
                elif kind == "tool_calls":
                    calls = value
                else:
                    value = head_filter.feed(value)  # 剥掉回答开头的「[assistant]」之类（见 RoleTagFilter）
                    if not value:
                        continue
                    got = True
                    text += value
                    put("delta", value)
            if head_filter.buf and not head_filter.done:  # 整个回答就几个像标记的字：原样给出去
                value, head_filter.buf, head_filter.done = head_filter.buf, "", True
                got = True
                text += value
                put("delta", value)
            rec.update(served=served, reply=text, tool_calls=calls, usage=usage,
                       seconds=round(time.monotonic() - t_req, 2))
        except client.ProxyError as e:
            rec.update(error=str(e)[:300], seconds=round(time.monotonic() - t_req, 2))
            last = str(e)[:300]
            log.warning("chat %s attempt=%d failed (got text=%s, searches=%d): %s", m, attempt + 1, got,
                        searches, last)
            if got or searches:
                break
            attempt += 1
            continue
        if calls:
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                last = f"{served} 到了搜索上限还在调工具"
                log.warning("chat %s keeps calling tools after limit; give up", m)
                break
            # assistant 那条只带正文和 tool_calls，不带 reasoning_content（deepseek 带了会一直重复调工具）
            msgs.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls]})
            for c in calls:
                if c["name"] in registry.LOCAL:
                    clock_calls += 1
                    out = (registry.run_local(c["name"], c["arguments"]) if clock_calls <= MAX_CLOCK_CALLS
                           else "失败：这一轮调太多次了，先停下，告诉用户已经做了什么")
                    log.info("tool %s %s -> %s", c["name"], c["arguments"][:120], out[:120])
                    actions.append({"name": c["name"], "arguments": c["arguments"], "result": out})
                    put("tool", {"name": c["name"], "arguments": c["arguments"], "result": out})
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                    continue
                if searches >= MAX_SEARCHES:
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": SEARCH_LIMIT_RESULT})
                    continue
                searches += 1
                queries = [q for q, _ in web_search.parse_queries(c["arguments"])]
                put("search", {"queries": queries, "state": "start"})
                res = _run_tool(c["name"], c["arguments"])
                log.info("search %s ok=%s sources=%d", queries, res.ok, len(res.sources))
                put("search", {"queries": res.queries or queries, "state": "done", "ok": res.ok,
                               "sources": res.sources})
                searched.append({"queries": res.queries or queries, "ok": res.ok, "sources": res.sources})
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": res.content})
            continue  # 带着搜索结果再问一次，模型不变、不算重试
        if got:
            break
        last = f"{served} 回了空内容"
        log.warning("chat %s attempt=%d empty reply usage=%s", m, attempt + 1, usage)
        if searches:
            break
        attempt += 1
    trace.finish(tr, served=served, error="" if got else (last or "没收到回答"), actions=actions)
    put("end", {"served": served, "usage": usage, "error": "" if got else (last or "没收到回答"),
                "searches": searched, "actions": actions})


def _turn_begin(cid: str) -> None:
    """新一轮开始前：有压好的摘要就换上（epoch +1）；该说现状（时间粗到半小时、闹钟变了）就插一条 `[system]`。"""
    from server import compact, context

    compact.maybe_swap(cid)
    note = context.note_for(store.get(cid))
    if note:
        store.append(cid, {"role": "note", **note})


def _turn_end(cid: str, end: dict) -> dict:
    """一轮答完：记下这一轮实际的 prompt token 数（电量条），超阈值就后台开压。返回给界面的 ctx 状态。"""
    from server import compact

    tokens = int((end.get("usage") or {}).get("prompt_tokens") or 0)
    if tokens:
        store.update_conv(cid, ctx_tokens=tokens)
    compact.maybe_kick(cid)
    return compact.state(store.get(cid))


def _reply_record(text: str, end: dict) -> dict:
    """存进对话的那条 assistant 消息。搜过就带上搜了什么、出处，界面上列来源用。"""
    rec = {"role": "assistant", "text": text, "model": end["served"], "usage": end["usage"]}
    if end.get("searches"):
        rec["searches"] = end["searches"]
    if end.get("actions"):
        rec["actions"] = end["actions"]  # 这一轮调了哪些闹钟工具、结果是什么，界面上列出来
    return rec


def _turn_speech():
    """按设置里的 TTS 引擎和音色建一轮朗读。返回 (TurnSpeech, 说明)；CosyVoice 用不了时换豆包，说明里写原因。"""
    from server import speech

    cur, engine, note = voices.resolve()
    if engine == "cosyvoice":
        try:
            return speech.TurnSpeech(cosyvoice=voices.reference(cur)), note
        except (voices.VoiceError, OSError) as e:
            if cur["kind"] == "clone":
                cur = voices.find(voices.catalog()["doubao_voice"])
            note = f"{e}，这次用豆包念"
    return speech.TurnSpeech(cur["id"].split(":", 1)[1]), note


_BACKGROUND: set = set()  # 后台收尾任务，留着引用免得被回收


async def _after_turn(cid, model, tts, worker, q, parts, end) -> None:
    if tts:
        if not tts.audio_done:
            await tts.cancel()
        if tts.first_audio_at is not None:
            info = tts.info()
            log.info("speech first_text_to_tts=%.2fs first_audio=%.2fs sent=%.1fs interrupted=%s err=%s",
                     info["first_text_s"] or -1, info["first_audio_s"] or -1, info["seconds"],
                     not tts.audio_done, info["error"])
    if end is not None:
        return
    # 文字还没出完客户端就走了：等模型说完再存，别把半截回答当完整的
    await worker
    while not q.empty():
        kind, value = q.get_nowait()
        if kind == "delta":
            parts.append(value)
        elif kind == "end":
            end = value
    reply = "".join(parts)
    log.info("chat client left mid-text; saved full reply %d chars", len(reply))
    if reply:
        store.append(cid, _reply_record(reply, end or {"served": model, "usage": {}}))


@app.post("/api/conversations/{cid}/messages")
async def send(cid: str, request: Request, body: dict = Body(...)) -> StreamingResponse:
    """存用户消息，再流式返回小新的回答（SSE）。

    事件：user / model / reasoning / search（联网搜索，{queries, state: start|done, ok, sources}） /
          delta（正文） / audio（base64 的 24 kHz s16le PCM，约 100 ms 一块） / audio_end / done / error。
          done 在文字结束时就发（不等音频念完），之后 audio 继续，最后 audio_end。done 里的消息搜过就带 searches。
    body.speak=true 时边出字边合成：攒够一句就送豆包双向流式 TTS，PCM 节流下发，最多领先播放 2 秒。
    客户端断开（用户打断、关页面）就停止下发并取消 TTS；模型那边的回答照样存下来。
    """
    import asyncio

    from server import speech

    text = str(body.get("text") or "").strip()
    files = _attachments(body.get("file_ids") or [])
    if not text and not files:
        raise HTTPException(400, "说点什么，或者发个图片 / 文件")
    try:
        store.get(cid)
    except KeyError:
        raise HTTPException(404, "没有这段对话")
    model = str(body.get("model") or MODELS[0]["id"])
    if model not in {m["id"] for m in MODELS}:
        raise HTTPException(400, f"不支持的模型 {model}")
    if model in NO_VISION and any(f["kind"] == "image" for f in files):
        model = VISION
    _turn_begin(cid)
    user = store.append(cid, {"role": "user", "text": text, "files": files, "via": body.get("via") or "text"})
    history = _llm_messages(store.get(cid))

    voice_note = ""
    tts = None
    if body.get("speak"):
        tts, voice_note = _turn_speech()

    async def events():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        worker = loop.run_in_executor(None, _llm_thread, model, history, q, loop, False, cid)
        if tts:
            tts.start()
        yield _sse("user", user)
        if voice_note:
            yield _sse("note", voice_note)
        parts, end = [], None
        try:
            while end is None or (tts and not tts.audio_done):
                if await request.is_disconnected():
                    log.info("chat client gone; stop streaming (text so far %d chars)", len("".join(parts)))
                    break
                wait = tts.next_wait() if tts else 0.5
                if end is None:
                    try:
                        kind, value = await asyncio.wait_for(q.get(), timeout=wait)
                    except asyncio.TimeoutError:
                        kind = None
                    if kind == "delta":
                        parts.append(value)
                        yield _sse("delta", value)
                        if tts:
                            await tts.feed(value)
                    elif kind in ("model", "reasoning", "search", "tool"):
                        yield _sse(kind, value)
                    elif kind == "end":
                        end = value
                        if tts:
                            if parts:
                                await tts.finish_text()
                            else:
                                await tts.cancel()
                        # 文字一完就存、就发 done，前端可以接着发下一条；音频（如果有）在同一条流里继续下来
                        reply = "".join(parts)
                        if reply:
                            saved = store.append(cid, _reply_record(reply, end))
                            yield _sse("done", {**saved, "ctx": _turn_end(cid, end)})
                        else:
                            yield _sse("error", end["error"] or "没收到回答")
                    elif tts:
                        await tts.tick()
                else:
                    await asyncio.sleep(wait)
                if tts:
                    for chunk in tts.take_audio():
                        yield _sse("audio", chunk)
        finally:
            # 走到这里可能是正常结束，也可能是客户端断开后生成器被取消（取消时不能再 await 太多），
            # 所以收尾放到后台任务里做：取消 TTS、记日志、文字没出完的话等模型说完再存。
            task = asyncio.get_running_loop().create_task(_after_turn(cid, model, tts, worker, q, parts, end))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)
        if tts and tts.first_audio_at is not None:
            yield _sse("audio_end", tts.info())

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 附件

@app.post("/api/files")
async def upload(request: Request) -> dict:
    from urllib.parse import unquote

    name = unquote(request.headers.get("x-filename") or "file")
    mime = request.headers.get("content-type") or "application/octet-stream"
    fid, path = store.allocate()
    size = 0
    try:
        with path.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > store.MAX_FILE:
                    raise HTTPException(413, "文件超过 50 MB")
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "空文件")
        return store.commit(fid, name, mime, size)
    except BaseException:
        store.abort(fid)
        raise


@app.get("/api/files/{fid}")
def download(fid: str, download: int = 0) -> FileResponse:
    info = store.file_info(fid)
    if info is None:
        raise HTTPException(404, "没有这个文件")
    path, meta = info
    inline = meta["kind"] == "image" and not download
    return FileResponse(path, media_type=meta["mime"], filename=meta["name"],
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- 语音

def _asr_engine(override: str = "") -> str:
    """设置里选的 ASR 引擎；请求里显式给了 engine 就用请求的（调试、对比用）。"""
    if override:
        if override not in voices.ASR_IDS:
            raise HTTPException(400, f"没有这个 ASR 引擎：{override}")
        return override
    return voices.engines()["asr"]


@app.post("/api/asr")
async def asr(request: Request, engine: str = "") -> dict:
    """整段识别。body 是 16 kHz 单声道 16 bit wav；Content-Type 给 application/octet-stream 或 audio/L16
    就当裸 PCM（s16le 16 kHz 单声道），端侧录的 PCM 不用再包 wav。引擎按设置，?engine= 可以临时指定。"""
    import asyncio

    data = await request.body()
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype in ("application/octet-stream", "audio/l16", "audio/pcm"):
        pcm = data[:len(data) - len(data) % 2]
    else:
        try:
            pcm = store.wav_pcm16k(data)
        except store.StoreError as e:
            raise HTTPException(400, str(e))
    if len(pcm) < 16000 * 2 * 0.3:
        raise HTTPException(400, "太短了，按住多说一会儿")
    eng = _asr_engine(engine)
    try:
        rec = await asyncio.to_thread(paraformer.transcribe if eng == "paraformer" else doubao.asr, pcm)
    except (doubao.DoubaoError, paraformer.ParaformerError) as e:
        raise HTTPException(502, f"ASR 失败：{e}")
    return {"text": rec.text, "seconds": round(rec.audio_seconds, 2), "latency": round(rec.final, 3), "engine": eng}


def _wav(speech: doubao.Speech) -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(speech.sample_rate)
        w.writeframes(speech.pcm)
    return buf.getvalue()


def _speak(text: str, voice_id: str | None) -> Response:
    from urllib.parse import quote

    try:
        speech, used, note = voices.synth(text, voice_id)
    except voices.VoiceError as e:
        raise HTTPException(400, str(e))
    except doubao.DoubaoError as e:
        raise HTTPException(502, f"TTS 失败：{e}")
    return Response(_wav(speech), media_type="audio/wav",
                    headers={"X-First-Audio": f"{speech.first_audio:.3f}", "X-Voice": used,
                             "X-Voice-Note": quote(note)})


MAX_TALK_SECONDS = 60
# 浏览器带 cookie 连 WebSocket 时校验来源，防别的网站借用户的登录态连进来。APK 用 Bearer、不带 Origin。
ALLOWED_ORIGINS = {o.strip() for o in (doubao.env("XIAOXIN_ORIGINS")
                                       or "https://gpu.cmbian.top:8183,https://47.116.13.227:8183,"
                                          "http://127.0.0.1:8199").split(",") if o.strip()}
# IP 那个是 APK 里的 WebView 和直接用 IP 打开网页时的来源：gpu.cmbian.top 没备案，带域名的 TLS 会被掐掉


def _ws_authed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return False
    try:
        return _authed(ws)  # type: ignore[arg-type]  # 只用到 cookies / headers，WebSocket 也有
    except HTTPException:
        return False


@app.websocket("/api/asr/stream")
async def asr_stream(ws: WebSocket) -> None:
    """按住说话的识别，引擎按设置（?engine=doubao|paraformer 可以临时指定）。

    客户端 → 服务端：二进制帧是 PCM（s16le 16 kHz 单声道，每帧 100 ms 左右）；
                    文本帧 {"type":"end"} 是松手发送，{"type":"cancel"} 是上滑取消。
    服务端 → 客户端：{"type":"ready","engine","partials"} 连上就发；
                    {"type":"partial","text"} 边说边出字（只有豆包有）；{"type":"final","text","seconds","latency","engine"}；
                    {"type":"error","detail"}。
    豆包是边说边传；Paraformer 是服务端攒着，松手整段识别一次。
    取消、断线都直接结束，不发尾包，那段话不会有最终结果，也不会进对话。
    """
    import asyncio
    import time

    if not _ws_authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()

    async def say(**msg) -> None:
        await ws.send_text(json.dumps(msg, ensure_ascii=False))

    try:
        eng = _asr_engine(ws.query_params.get("engine") or "")
    except HTTPException as e:
        await say(type="error", detail=e.detail)
        await ws.close()
        return
    # 一开头告诉端侧用的哪个引擎、有没有中间结果，Paraformer 没有 partial，端侧可以换个提示
    await say(type="ready", engine=eng, partials=eng == "doubao")
    try:
        async with (paraformer.StreamingParaformer() if eng == "paraformer"
                    else doubao.StreamingAsr(hotwords=voices.hotwords())) as rec:
            async def pump() -> None:
                while True:
                    await say(type="partial", text=await rec.partials.get())

            pumping = asyncio.create_task(pump())
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("bytes"):
                        await rec.feed(msg["bytes"])
                        if rec.seconds > MAX_TALK_SECONDS:
                            await say(type="error", detail=f"一次最多说 {MAX_TALK_SECONDS} 秒")
                            return
                        continue
                    cmd = json.loads(msg.get("text") or "{}").get("type")
                    if cmd == "cancel":
                        log.info("asr cancelled after %.1fs", rec.seconds)
                        return
                    if cmd == "end":
                        t0 = time.monotonic()
                        text = await rec.finish()
                        latency = time.monotonic() - t0
                        log.info("asr %s final %.1fs audio, %.0f ms after release", eng, rec.seconds,
                                 latency * 1000)
                        await say(type="final", text=text, seconds=round(rec.seconds, 2), latency=round(latency, 3),
                                  engine=eng)
                        return
            finally:
                pumping.cancel()
    except (doubao.DoubaoError, paraformer.ParaformerError) as e:
        log.warning("asr stream failed: %s", e)
        try:
            await say(type="error", detail=str(e)[:200])
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _stream_asr(engine: str):
    """自由对话一句话的流式识别：豆包有中间结果（够两个字就能打断小新）；Paraformer 没有，返回 None 走整句。"""
    if engine == "paraformer":
        return None
    return doubao.StreamingAsr(hotwords=voices.hotwords())


def _transcribe(engine: str, pcm: bytes) -> str:
    if engine == "paraformer":
        return paraformer.transcribe(pcm).text
    return doubao.asr(pcm, hotwords=voices.hotwords()).text  # 热词每次现读设置，改了下一句就生效


@app.websocket("/api/talk")
async def talk(ws: WebSocket) -> None:
    """自由对话，协议见 server/talk.py。?conversation= 接着已有对话说，?model= 换模型，?asr= 临时换识别引擎。"""
    from server.talk import Talk

    if not _ws_authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = ws.query_params
    model = q.get("model") or MODELS[0]["id"]
    if model not in {m["id"] for m in MODELS}:
        model = MODELS[0]["id"]
    try:
        asr_engine = _asr_engine(q.get("asr") or "")
    except HTTPException as e:
        await ws.send_text(json.dumps({"type": "error", "detail": e.detail}, ensure_ascii=False))
        await ws.close()
        return
    session = Talk(ws, asr_engine=asr_engine, model=model, conversation=q.get("conversation") or None,
                   llm_thread=_llm_thread, make_speech=_turn_speech, transcribe=_transcribe,
                   reply_record=_reply_record, history=_llm_messages, models=[m["id"] for m in MODELS],
                   stream_asr=_stream_asr, before_turn=_turn_begin, after_turn=_turn_end)
    log.info("talk open asr=%s model=%s conversation=%s", asr_engine, model, session.cid)
    try:
        await session.run()
    except KeyError:
        await session.send("error", detail="没有这段对话")
    except WebSocketDisconnect:
        pass
    finally:
        log.info("talk closed conversation=%s turns=%d", session.cid, session.turn_id)
        try:
            await ws.close()
        except Exception:
            pass


# 唤醒 / 主动退出时小新用当前音色说的一个字：pad 提前拉下来存着，唤醒或退出那一刻本机直接放，不等网络
CUES = {"zai": "我在的。", "bye": "拜拜。"}  # zai 是唤醒应答，名字留着不改（pad 按名字缓存）


def _trim_pcm(pcm: bytes, rate: int, lead_ms: int = 30, tail_ms: int = 150, fade_ms: int = 60) -> bytes:
    """去掉首尾静音，让应答一出来就是声音。尾巴多留一点再淡出：之前尾部只留 30 ms，字的余音被一刀切掉，听着发闷发硬。"""
    import array

    s = array.array("h", pcm[:len(pcm) // 2 * 2])
    loud = [i for i in range(0, len(s), 48) if abs(s[i]) > 300]
    if not loud:
        return pcm
    a = max(0, loud[0] - rate * lead_ms // 1000)
    b = min(len(s), loud[-1] + 48 + rate * tail_ms // 1000)
    out = s[a:b]
    n = min(len(out), rate * fade_ms // 1000)
    for k in range(n):
        out[len(out) - n + k] = int(out[len(out) - n + k] * (n - k) / n)
    return out.tobytes()


CUE_MIN_S_PER_CHAR = 0.12  # 正常每字 0.2–0.3 秒；短于这个多半是念了半句就停了


def _cue_synth(text: str, voice_id: str, tries: int = 3) -> bytes:
    """合成一句提示并去掉首尾静音。CosyVoice 偶尔念到逗号就停（「卞依一，5秒倒计时到啦」只剩「卞依一」，
    2026-10-30 实测 12 条里 3 条），太短的重来，最多 tries 次；都短就用最长的那条。缓存后一直用，所以值得多试。"""
    import re

    chars = len(re.sub(r"[\s，。！？、,.!?~～]", "", text))
    best = b""
    for _ in range(tries):
        try:
            speech, _, _ = voices.synth(text, voice_id)
        except (voices.VoiceError, doubao.DoubaoError) as e:
            raise HTTPException(502, f"合成失败：{e}")
        if speech.sample_rate != 24000:
            raise HTTPException(502, f"采样率不对：{speech.sample_rate}")
        pcm = _trim_pcm(speech.pcm, 24000)
        if len(pcm) > len(best):
            best = pcm
        if len(pcm) / 48000 >= CUE_MIN_S_PER_CHAR * chars:
            return pcm
        log.warning("cue %r too short: %.2fs for %d chars, retry", text, len(pcm) / 48000, chars)
    return best


@app.get("/api/cue/{name}")
def cue(name: str, voice: str = "", text: str = "") -> Response:
    """当前音色（或 ?voice= 指定）说「我在的」/「拜拜」/ 响铃提示（ring，?text= 给要念的话，不给用默认提示），
    24 kHz s16le 单声道裸 PCM。按音色和文字缓存在数据目录，第二次直接读。"""
    import hashlib

    if name == "ring":
        try:
            text = clock._check_say(text) or clock.ring_text()
        except clock.ClockError as e:
            raise HTTPException(400, str(e))
    elif name == "say":
        # 描红播报用的短语（笔画名、「第 3 笔」「错了两次」…）：端侧按段缓存、拼起来放，所以只认短的固定话
        text = text.strip()
        if not 1 <= len(text) <= 30:
            raise HTTPException(400, "say 的话 1–30 个字")
    else:
        text = CUES.get(name)
    if text is None:
        raise HTTPException(404, "没有这个提示")
    try:
        v, engine, _ = voices.resolve(voice or None)
    except voices.VoiceError as e:
        raise HTTPException(400, str(e))
    # 内置男声换过参考素材：素材名也算进去，换了就重新合成
    ref = voices.BUILTIN_MALE_SOURCE if v["id"] == voices.BUILTIN else ""
    key = hashlib.sha1(f"{v['id']}|{engine}|{text}|{ref}|v2".encode()).hexdigest()[:16]  # v2：加了念完整的检查
    path = store.VOICE / "cues" / f"{key}.pcm"
    if not path.is_file():
        pcm = _cue_synth(text, v["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pcm)
        tmp.replace(path)
    return Response(path.read_bytes(), media_type="application/octet-stream",
                    headers={"X-Voice": v["id"], "X-Sample-Rate": "24000", "Cache-Control": "no-cache"})


@app.post("/api/tts")
def tts(body: dict = Body(...)) -> Response:
    """读一段话，用当前选中的音色；body 里给 voice 就临时换一个。"""
    text = str(body.get("text") or "").strip()[:600]
    if not text:
        raise HTTPException(400, "没有要读的字")
    return _speak(text, body.get("voice"))


# ---------------------------------------------------------------- 音色

@app.get("/api/voices")
def voice_catalog() -> dict:
    return voices.catalog()


@app.get("/api/engines")
def engine_settings() -> dict:
    """ASR / TTS 引擎。全家共用，存在服务端；平板 APK 也读这个。"""
    return {**voices.engines(), "cosyvoice_ready": voices.clone_ready(), "paraformer_ready": paraformer.ready()}


@app.put("/api/engines")
def engine_select(body: dict = Body(...)) -> dict:
    try:
        voices.set_engines(asr=body.get("asr"), tts=body.get("tts"), vad_end_ms=body.get("vad_end_ms"),
                           wake_word=body.get("wake_word"), wake_idle_s=body.get("wake_idle_s"),
                           wake_words=body.get("wake_words"), exit_words=body.get("exit_words"),
                           hangup_tone=body.get("hangup_tone"), asr_hotwords=body.get("asr_hotwords"))
    except voices.VoiceError as e:
        raise HTTPException(400, str(e))
    return engine_settings()


@app.put("/api/voices/current")
def voice_select(body: dict = Body(...)) -> dict:
    try:
        return voices.select(str(body.get("voice") or ""))
    except voices.VoiceError as e:
        raise HTTPException(400, str(e))


@app.post("/api/voices/preview")
def voice_preview(body: dict = Body(...)) -> Response:
    return _speak(str(body.get("text") or voices.PREVIEW)[:200], str(body.get("voice") or "") or None)


@app.post("/api/voices/custom")
def voice_add(body: dict = Body(...)) -> dict:
    try:
        return voices.add_custom(str(body.get("speaker") or ""), str(body.get("name") or ""))
    except voices.VoiceError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/voices/custom/{speaker}")
def voice_remove(speaker: str) -> dict:
    return voices.remove_custom(speaker)


# ---------------------------------------------------------------- 录的声音（克隆用）

@app.get("/api/voice")
def voice_samples() -> dict:
    return {"samples": store.list_samples()}


@app.post("/api/voice")
async def voice_upload(request: Request) -> dict:
    from urllib.parse import unquote

    data = await request.body()
    try:
        meta = store.save_sample(data, unquote(request.headers.get("x-text") or ""),
                                 unquote(request.headers.get("x-name") or ""))
    except store.StoreError as e:
        raise HTTPException(400, str(e))
    return voices.prepare_clone(meta["id"])


@app.patch("/api/voice/{sid}")
def voice_rename(sid: str, body: dict = Body(...)) -> dict:
    name = str(body.get("name") or "").strip()[:20]
    if not name:
        raise HTTPException(400, "名字不能空")
    try:
        return store.update_sample(sid, name=name)
    except KeyError:
        raise HTTPException(404, "没有这段录音")


@app.get("/api/voice/{sid}.wav")
def voice_file(sid: str, ref: int = 0) -> FileResponse:
    try:
        path = store.sample_path(sid)
    except KeyError:
        raise HTTPException(404, "没有这段录音")
    if ref:
        path = path.with_suffix(".ref.wav")
        if not path.is_file():
            raise HTTPException(404, "还没切参考段")
    return FileResponse(path, media_type="audio/wav")


@app.delete("/api/voice/{sid}")
def voice_delete(sid: str) -> dict:
    try:
        store.delete_sample(sid)
    except KeyError:
        raise HTTPException(404, "没有这段录音")
    if store.settings().get("voice") == "clone:" + sid:
        store.update_settings(voice=voices.DEFAULT)
    return {"ok": True}
