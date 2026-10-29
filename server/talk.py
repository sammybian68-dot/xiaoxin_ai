"""自由对话：一条 WebSocket 全双工，端侧一直送麦克风，服务端断句、识别、回答、朗读，用户插话就打断。

协议（/api/talk，鉴权同按住说话）：
  上行  二进制帧：s16le 16 kHz 单声道 PCM，多长都行（建议 20–100 ms）
        文本帧 {"type":"hello","conversation":"<id 可空>","model":"<可空>"}  连上后第一条，可省
        文本帧 {"type":"stop"}  挂断
        文本帧 {"type":"model","model":"<id>"}  换模型，下一句起生效
        文本帧 {"type":"wake","text":"<端侧认出的整句>"}  pad 待命唤醒后发的第一句：端侧 Paraformer 已经认好了，
                 服务端去掉开头的唤醒词，剩下有内容就直接当这一轮的问题（省掉云端识别）；只喊了唤醒词就回一句「我在」
  下行  文本帧 JSON：
        ready      {conversation, asr, tts, voice}           准备好了，可以说话
        listening  {}                                         用户开口了（VAD start）
        model      {model}                                    换模型生效了
        heard      {text, t_asr}                              一句话识别完；t_asr 是说完到识别出来的秒数
        thinking   {}                                         送给模型了
        search     {queries, state, ok, sources}              联网搜索，同对话页
        delta      {text}                                     回答正文
        reply      {message}                                  回答存进对话了
        speaking   {sample_rate}                              下面开始有音频
        interrupt  {}                                         用户插话，端侧立刻清空播放队列
        idle       {timing}                                   这一轮念完了，等用户说。timing 见下
        note / error {detail}
        二进制帧：24 kHz s16le PCM，约 100 ms 一块（服务端节流，最多领先播放 2 秒）

首响计时（服务端视角，从 VAD 判定说完算起，秒）：
  timing = {asr, search, first_text, first_audio, model}
  asr 说完到识别出来；search 搜索花的时间（没搜是 0）；first_text 说完到第一个字；first_audio 说完到第一块音频发出。
  端侧再加上自己的「收到第一块到真正出声」，就是用户听到的首响。

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

# 加在人设后面：不然模型以为自己在打字聊天，会说「我听不到声音，请打字」（2026-10-29 实测 deepseek 这么回过）
VOICE_HINT = ("现在是语音通话：用户的话是语音识别出来的文字（可能有错字，按意思理解），你的回答会被念出来。"
              "所以要短，像打电话说话一样，一般一两句、不超过 80 个字；别用列表、表格、markdown、表情符号；"
              "别说你听不到声音或者让用户打字。")


WAKE_ACK = "我在。"
_PUNCT = "，。！？、；：,.!?;: 　"


def strip_wake_word(text: str, wake: str) -> str:
    """去掉开头的唤醒词（前面可以带「嘿 / 哎」这类语气词），返回剩下的话；不是以唤醒词开头就原样返回。

    比对时不看标点空格：端侧小模型认出来没有标点，豆包认出来有。
    """
    bare = "".join(c for c in text if c not in _PUNCT)
    for lead in ("", "嘿", "哎", "喂", "嗨", "hi", "hey"):
        if bare.lower().startswith(lead + wake):
            # 在原文里找到唤醒词最后一个字的位置，从后面切，保留原文的标点
            seen, need = 0, len(lead + wake)
            for i, c in enumerate(text):
                if c not in _PUNCT:
                    seen += 1
                    if seen == need:
                        return text[i + 1:].lstrip(_PUNCT)
    return text.strip()


class Talk:
    """一通电话。run() 跑到挂断。外部依赖都从 main 传进来，方便测试替换。"""

    def __init__(self, ws, *, asr_engine: str, model: str, conversation: str | None,
                 llm_thread, make_speech, transcribe, reply_record, history, models=()):
        self.ws = ws
        self.asr_engine = asr_engine
        self.model = model
        self._models = set(models) or {model}
        self.cid = conversation
        self._llm_thread = llm_thread    # main._llm_thread(model, history, q, loop)
        self._make_speech = make_speech  # () -> (TurnSpeech, note)
        self._transcribe = transcribe    # (engine, pcm) -> text，同步，放线程里跑
        self._reply_record = reply_record
        self._history = history          # conv -> OpenAI messages
        self.vad = Vad(end_ms=voices.engines()["vad_end_ms"])
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
                if cmd.get("type") == "model" and cmd.get("model") in self._models:
                    self.model = cmd["model"]  # 下一轮起生效，正在答的这一轮不换
                    await self.send("model", model=self.model)
                elif cmd.get("type") == "wake":
                    await self.on_wake(str(cmd.get("text") or ""))
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

    async def on_wake(self, text: str) -> None:
        rest = strip_wake_word(text, voices.engines()["wake_word"])
        log.info("talk wake: %r -> %r", text, rest)
        await self.cancel_turn()
        self.turn_id += 1
        if rest:
            self.turn = asyncio.create_task(self.answer(self.turn_id, b"", text=rest))
        else:
            self.turn = asyncio.create_task(self.answer(self.turn_id, b"", text=WAKE_ACK, ack=True))

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
    async def answer(self, turn_id: int, utt: bytes, text: str | None = None, ack: bool = False) -> None:
        """一轮：识别（端侧已经认好就跳过）→ 模型 → 朗读。ack=True 时 text 是固定应答，不问模型。"""
        t0 = time.monotonic()
        if ack:
            await self.speak_only(text)
            return
        if text is None:
            try:
                text = (await asyncio.to_thread(self._transcribe, self.asr_engine, utt)).strip()
            except Exception as e:
                await self.send("error", detail=f"识别失败：{str(e)[:120]}")
                return
        t_asr = time.monotonic() - t0
        log.info("talk turn %d heard %.1fs audio in %.2fs: %s", turn_id, len(utt) / 32000, t_asr, text[:60])
        if not text:
            await self.send("idle")
            return
        await self.send("heard", text=text, t_asr=round(t_asr, 2))
        model = self.model
        t_search, search_at, first_text, saved_id = 0.0, None, None, None
        store.append(self.cid, {"role": "user", "text": text, "files": [], "via": "talk"})
        history = self._history(store.get(self.cid))
        tts, note = self._make_speech()
        if note:
            await self.send("note", detail=note)
        await self.send("thinking")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        worker = loop.run_in_executor(None, self._llm_thread, model, history, q, loop, VOICE_HINT)
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
                        if first_text is None:
                            first_text = time.monotonic() - t0
                        parts.append(value)
                        await self.send("delta", text=value)
                        await tts.feed(value)
                    elif kind == "search":
                        if value.get("state") == "start":
                            search_at = time.monotonic()
                        elif search_at is not None:
                            t_search += time.monotonic() - search_at
                            search_at = None
                        await self.send("search", **value)
                    elif kind == "end":
                        end = value
                        reply = "".join(parts)
                        if reply:
                            await tts.finish_text()
                            saved = store.append(self.cid, self._reply_record(reply, end))
                            saved_id = saved["id"]
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
            # TurnSpeech 的时间从它建起来算，换成从说完算
            tts_base = tts.t0 - t0
            timing = {"asr": round(t_asr, 2), "search": round(t_search, 2),
                      "first_text": first_text and round(first_text, 2),
                      "first_audio": info["first_audio_s"] and round(info["first_audio_s"] + tts_base, 2),
                      "model": (end or {}).get("served") or model}
            log.info("talk turn %d done: %s audio=%.1fs total=%.1fs", turn_id, timing, info["seconds"],
                     time.monotonic() - t0)
            if saved_id:
                store.update_message(self.cid, saved_id, timing=timing)
            await self.send("idle", timing=timing)
        except asyncio.CancelledError:
            # 被插话打断：TTS 停掉；模型还在想的话等它说完把整段存下，免得历史里只有问题没有回答
            await tts.cancel()
            task = asyncio.get_running_loop().create_task(self._save_late(worker, q, parts, end))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)
            raise

    async def speak_only(self, text: str) -> None:
        """只喊了唤醒词：念一句固定应答，不进对话记录、不问模型。"""
        tts, _ = self._make_speech()
        tts.start()
        try:
            await tts.feed(text)
            await tts.finish_text()
            speaking = False
            while not tts.audio_done:
                for chunk in tts.take_audio_bytes():
                    if not speaking:
                        speaking = True
                        await self.send("speaking", sample_rate=24000)
                    await self.send_audio(chunk)
                await asyncio.sleep(tts.next_wait())
            for chunk in tts.take_audio_bytes():
                await self.send_audio(chunk)
            await self.send("idle")
        except asyncio.CancelledError:
            await tts.cancel()
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
