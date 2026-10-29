"""自由对话：一条 WebSocket 全双工，端侧一直送麦克风，服务端断句、识别、回答、朗读，用户插话就打断。

协议（/api/talk，鉴权同按住说话）：
  上行  二进制帧：s16le 16 kHz 单声道 PCM，多长都行（建议 20–100 ms）
        文本帧 {"type":"hello","conversation":"<id 可空>","model":"<可空>"}  连上后第一条，可省
        文本帧 {"type":"stop"}  挂断
  下行  文本帧 JSON：
        ready      {conversation, asr, tts, voice}           准备好了，可以说话
        listening  {}                                         用户开口了（VAD start）
        heard      {text}                                     一句话识别完
        thinking   {}                                         送给模型了
        search     {queries, state, ok, sources}              联网搜索，同对话页
        delta      {text}                                     回答正文
        reply      {message}                                  回答存进对话了
        speaking   {sample_rate}                              下面开始有音频
        interrupt  {}                                         用户插话，端侧立刻清空播放队列
        idle       {}                                         这一轮念完了，等用户说
        note / error {detail}
        二进制帧：24 kHz s16le PCM，约 100 ms 一块（服务端节流，最多领先播放 2 秒）

一轮的状态：听 → 识别 → 想（LLM，可能搜）→ 说（边出字边 TTS）→ 听。
在想或说的时候 VAD 判到用户开口，就取消这一轮（LLM 和 TTS 都停，端侧清空播放），转去听这句新的。
端侧开了系统 AEC（pad 上 VOICE_COMMUNICATION + 通话播放，实测小新的声音剩 2 dB），所以服务端不再做回声消除。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from server import store, voices
from server.vad import Vad

log = logging.getLogger("xiaoxin.talk")


class Talk:
    """一通电话。run() 跑到挂断。外部依赖都从 main 传进来，方便测试替换。"""

    def __init__(self, ws, *, asr_engine: str, model: str, conversation: str | None,
                 llm_thread, make_speech, transcribe, reply_record, history):
        self.ws = ws
        self.asr_engine = asr_engine
        self.model = model
        self.cid = conversation
        self._llm_thread = llm_thread    # main._llm_thread(model, history, q, loop)
        self._make_speech = make_speech  # () -> (TurnSpeech, note)
        self._transcribe = transcribe    # (engine, pcm) -> text，同步，放线程里跑
        self._reply_record = reply_record
        self._history = history          # conv -> OpenAI messages
        self.vad = Vad()
        self.turn: asyncio.Task | None = None
        self.turn_id = 0
        self._send_lock = asyncio.Lock()
        self.closed = False

    # ---------------------------------------------------------------- 下行
    async def send(self, kind: str, **data) -> None:
        if self.closed:
            return
        async with self._send_lock:
            await self.ws.send_text(json.dumps({"type": kind, **data}, ensure_ascii=False))

    async def send_audio(self, pcm: bytes) -> None:
        if self.closed:
            return
        async with self._send_lock:
            await self.ws.send_bytes(pcm)

    # ---------------------------------------------------------------- 主循环
    async def run(self) -> None:
        if not self.cid:
            self.cid = store.create()["id"]
        else:
            store.get(self.cid)  # 不存在就 KeyError，调用方处理
        cat = voices.catalog()
        await self.send("ready", conversation=self.cid, asr=self.asr_engine, tts=cat["engines"]["tts"],
                        voice=cat["current"])
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes"):
                    await self.on_pcm(msg["bytes"])
                    continue
                cmd = json.loads(msg.get("text") or "{}")
                if cmd.get("type") == "stop":
                    return
        finally:
            self.closed = True
            await self.cancel_turn()

    async def on_pcm(self, pcm: bytes) -> None:
        for kind, utt in self.vad.feed(pcm):
            if kind == "start":
                if self.busy:
                    log.info("talk barge-in during turn %d", self.turn_id)
                    await self.cancel_turn()
                    await self.send("interrupt")
                await self.send("listening")
            else:
                self.turn_id += 1
                self.turn = asyncio.create_task(self.answer(self.turn_id, utt))

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()

    async def cancel_turn(self) -> None:
        t, self.turn = self.turn, None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ---------------------------------------------------------------- 一轮
    async def answer(self, turn_id: int, utt: bytes) -> None:
        t0 = time.monotonic()
        try:
            text = (await asyncio.to_thread(self._transcribe, self.asr_engine, utt)).strip()
        except Exception as e:
            await self.send("error", detail=f"识别失败：{str(e)[:120]}")
            return
        log.info("talk turn %d heard %.1fs audio in %.2fs: %s", turn_id, len(utt) / 32000,
                 time.monotonic() - t0, text[:60])
        if not text:
            await self.send("idle")
            return
        await self.send("heard", text=text)
        store.append(self.cid, {"role": "user", "text": text, "files": [], "via": "talk"})
        history = self._history(store.get(self.cid))
        tts, note = self._make_speech()
        if note:
            await self.send("note", detail=note)
        await self.send("thinking")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        worker = loop.run_in_executor(None, self._llm_thread, self.model, history, q, loop)
        tts.start()
        parts, end, speaking = [], None, False
        try:
            while end is None or not tts.audio_done:
                wait = tts.next_wait()
                if end is None:
                    try:
                        kind, value = await asyncio.wait_for(q.get(), timeout=wait)
                    except asyncio.TimeoutError:
                        kind = None
                    if kind == "delta":
                        parts.append(value)
                        await self.send("delta", text=value)
                        await tts.feed(value)
                    elif kind == "search":
                        await self.send("search", **value)
                    elif kind == "end":
                        end = value
                        reply = "".join(parts)
                        if reply:
                            await tts.finish_text()
                            saved = store.append(self.cid, self._reply_record(reply, end))
                            await self.send("reply", message=saved)
                        else:
                            await tts.cancel()
                            await self.send("error", detail=end["error"] or "没收到回答")
                    elif kind is None:
                        await tts.tick()
                else:
                    await asyncio.sleep(wait)
                for chunk in tts.take_audio_bytes():
                    if not speaking:
                        speaking = True
                        await self.send("speaking", sample_rate=24000)
                    await self.send_audio(chunk)
            info = tts.info()
            if info["error"]:
                await self.send("note", detail="朗读出错：" + info["error"])
            log.info("talk turn %d done: first_audio=%.2fs audio=%.1fs total=%.1fs", turn_id,
                     info["first_audio_s"] or -1, info["seconds"], time.monotonic() - t0)
            await self.send("idle")
        except asyncio.CancelledError:
            # 被插话打断：TTS 停掉；模型还在想的话等它说完把整段存下，免得历史里只有问题没有回答
            await tts.cancel()
            task = asyncio.get_running_loop().create_task(self._save_late(worker, q, parts, end))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)
            raise

    async def _save_late(self, worker, q, parts, end) -> None:
        if end is not None:
            return
        await worker
        while not q.empty():
            kind, value = q.get_nowait()
            if kind == "delta":
                parts.append(value)
            elif kind == "end":
                end = value
        reply = "".join(parts)
        if reply and end:
            store.append(self.cid, {**self._reply_record(reply, end), "interrupted": True})


_BACKGROUND: set = set()
