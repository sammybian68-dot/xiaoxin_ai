"""小新 web：对话（文字 / 图片 / 文件 / 按住说话）和声音录制。

只听 127.0.0.1:8184，对外由 gpu2 上 mainbrain 的 8183（https）在 /xiaoxin/ 下转发过来。
不输密码，用免密链接：打开一次 /xiaoxin/?k=<XIAOXIN_KEY>，服务端种 cookie 并跳回 /xiaoxin/，之后浏览器记住一年。
APK 用 Authorization: Bearer <XIAOXIN_KEY>。换 key 后旧链接、旧 cookie 全部失效。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server import store, voices
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

SYSTEM = ("你是「小新」，一个亲切、靠谱的中文 AI 助手，平时陪一家人聊天、答疑、帮小朋友看作业。"
          "回答先说结论，口语化，不啰嗦；需要分步骤时再用列表。给小朋友讲题时引导思路，不直接替他写答案。"
          "你能联网：新闻、天气、比分、最近的事，或者你拿不准、可能过时的事实，先用 search_web 搜再答，"
          "别说自己不能联网；用了搜索结果就简单说一句信息来源。")
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
    if response.headers.get("content-type", "").startswith("text/html"):
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
    return {"ok": True, "models": MODELS, "default_model": MODELS[0]["id"], "system_prompt": SYSTEM,
            "vision_model": VISION, "fallback_model": FALLBACK}


@app.get("/")
def index(request: Request, k: str = "") -> Response:
    """带 ?k= 是免密链接：对了就种 cookie，再跳回不带 k 的地址，免得 key 留在地址栏和历史记录里。"""
    if k:
        key = _key()
        if not _same(k, key):
            return FileResponse(STATIC / "index.html", headers={"X-Xiaoxin-Link": "bad"})
        resp = RedirectResponse("./", status_code=303)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return _remember(request, resp, key)
    return FileResponse(STATIC / "index.html")


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
    """对话转成 OpenAI 格式。只有最后一条用户消息带图片本体，之前的图片换成一句占位，免得每轮重传。"""
    msgs = conv["messages"][-40:]
    last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
    out = []
    for i, m in enumerate(msgs):
        if m["role"] == "assistant":
            if m.get("text"):
                out.append({"role": "assistant", "content": m["text"]})
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


def _system() -> str:
    """人设加上今天的日期：要不然模型不知道「今天」「明天」是哪天，搜新闻也不知道搜哪天的。"""
    import datetime

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    week = "一二三四五六日"[today.weekday()]
    return f"{SYSTEM}\n今天是 {today:%Y-%m-%d}，星期{week}（北京时间）。"


def _run_tool(name: str, arguments: str) -> web_search.Result:
    if name != web_search.NAME:
        return web_search.Result(False, f"没有这个工具：{name}")
    return web_search.search(arguments)


def _llm_thread(model: str, history: list[dict], q, loop, extra_system: str = "") -> None:
    """在线程里跑同步的流式请求，每个事件丢回事件循环的队列。

    工具循环：模型要搜就搜（发 search 事件），把结果接在后面再问一次，一轮最多搜 MAX_SEARCHES 次。
    到了上限工具照样给，只是不真搜，回一句「到上限了，用已有结果回答」（照 carHer2）。
    不能直接把 tools 拿掉：主脑 27B 看到历史里有工具调用还会接着调，vLLM 没收到 tools 就不解析，
    <tool_call> 被当特殊 token 过滤掉，正文是空的（2026-10-29 复现 6 次空 4 次）。
    重试和兜底：还没吐出正文、也还没搜过就失败（报错或空回答），同一个模型再试一次，再不行换 FALLBACK；
    已经吐了字或搜过了就不换，免得前后两截、重复搜。"""
    def put(*item):
        loop.call_soon_threadsafe(q.put_nowait, item)

    tries = [model, model] + ([FALLBACK] if model != FALLBACK else [])
    got, usage, served, last = False, {}, model, ""
    msgs, searches, searched, rounds = list(history), 0, [], 0
    attempt = 0
    while attempt < len(tries):
        m = tries[attempt]
        if attempt and m != tries[attempt - 1]:
            put("model", m)
        served = m
        calls: list[dict] = []
        text = ""
        tools = [web_search.SPEC]
        try:
            system = _system() + ("\n" + extra_system if extra_system else "")
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
                    got = True
                    text += value
                    put("delta", value)
        except client.ProxyError as e:
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
    put("end", {"served": served, "usage": usage, "error": "" if got else (last or "没收到回答"),
                "searches": searched})


def _reply_record(text: str, end: dict) -> dict:
    """存进对话的那条 assistant 消息。搜过就带上搜了什么、出处，界面上列来源用。"""
    rec = {"role": "assistant", "text": text, "model": end["served"], "usage": end["usage"]}
    if end.get("searches"):
        rec["searches"] = end["searches"]
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
    user = store.append(cid, {"role": "user", "text": text, "files": files, "via": body.get("via") or "text"})
    history = _llm_messages(store.get(cid))

    voice_note = ""
    tts = None
    if body.get("speak"):
        tts, voice_note = _turn_speech()

    async def events():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        worker = loop.run_in_executor(None, _llm_thread, model, history, q, loop)
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
                    elif kind in ("model", "reasoning", "search"):
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
                            yield _sse("done", saved)
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
        async with (paraformer.StreamingParaformer() if eng == "paraformer" else doubao.StreamingAsr()) as rec:
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


def _transcribe(engine: str, pcm: bytes) -> str:
    return (paraformer.transcribe if engine == "paraformer" else doubao.asr)(pcm).text


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
                   reply_record=_reply_record, history=_llm_messages, models=[m["id"] for m in MODELS])
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
                           wake_word=body.get("wake_word"))
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
