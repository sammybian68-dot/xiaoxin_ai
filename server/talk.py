"""自由对话：一条 WebSocket 全双工，端侧一直送麦克风，服务端断句、识别、回答、朗读，用户插话就打断。

协议（/api/talk，鉴权同按住说话）：
  上行  二进制帧：s16le 16 kHz 单声道 PCM，多长都行（建议 20–100 ms）
        文本帧 {"type":"hello","conversation":"<id 可空>","model":"<可空>"}  连上后第一条，可省
        文本帧 {"type":"stop"}  挂断
        文本帧 {"type":"model","model":"<id>"}  换模型，下一句起生效
        文本帧 {"type":"wake","text":"<端侧认出的整句>","acked":true}  pad 待命唤醒后发的第一句：端侧 Paraformer 已经认好了，
                 服务端去掉开头的唤醒词，剩下有内容就直接当这一轮的问题（省掉云端识别）；只喊了唤醒词：
                 acked 表示 pad 已经用本机缓存说了「我在的」，这边就不再合成；没 acked 才合成一句
        文本帧 {"type":"vad_start"} / {"type":"vad_end"}  端侧断句（pad 上的 TEN-VAD，和 carHer2 一样按人声概率）。
                 收到过一次就改用端侧断句：vad_start 起收音频，vad_end 时把这段送识别；服务端自己的能量 VAD 不再用。
                 没有端侧断句的（浏览器）照旧用服务端 VAD。事件和音频同一条连接按顺序到，vad_end 时这句音频已经到齐
        文本帧 {"type":"local","text":"<端侧 Paraformer 认出的一句>"}  pad 通话中本机断句识别的结果（排查用），
                 记到下一条存下的用户消息上（local 字段），和云端识别、原声放一起对照
  下行  文本帧 JSON：
        ready      {conversation, asr, tts, voice}           准备好了，可以说话
        listening  {}                                         用户开口了（VAD start）
        model      {model}                                    换模型生效了
        heard      {text, t_asr, id, local, audio}            一句话识别完；t_asr 是说完到识别出来的秒数；id 是存下的用户消息；
                                                              local 端侧认出的（没有是空）；audio 有没有存原声
                                                              （GET /api/conversations/<cid>/audio/<id> 回放）
        thinking   {}                                         送给模型了
        search     {queries, state, ok, sources}              联网搜索，同对话页
        tool       {name, result}                             调了闹钟类工具，result 是执行结果
        delta      {text}                                     回答正文
        reply      {message}                                  回答存进对话了
        speaking   {sample_rate}                              下面开始有音频
        interrupt  {}                                         用户插话，端侧立刻清空播放队列
        idle       {timing}                                   这一轮念完了，等用户说。timing 见下
        exit       {text}                                     用户说了退出词（整句精确匹配；pad 一般本机已经先判了）
        bye        {}                                         端侧挂断：说一声「拜拜」（/api/cue/bye，当前音色）回待命
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

# 语音通话额外拼上 server/prompts/voice.md：不然模型以为在打字聊天，会说「我听不到声音，请打字」（deepseek 实测这么回过）


BARGE_IN_MIN_CHARS = 2  # 小新在想 / 在说时，识别出至少这么多字（不算标点空格）才算插话；退出词不受限
WAKE_ACK = "我在的。"  # 端侧没缓存时（浏览器）服务端合成；pad 用本机缓存的同一句（/api/cue/zai）
_PUNCT = "，。！？、；：,.!?;: 　"


# 「小新」常被认成同音的「小心」（2026-10-30 端侧测「你好小新…」8 句错 2 句）。只在比对唤醒词时把这几个当成「小新」，
# 别的地方不替换，免得「小心点」被改坏。WakeListener.java 里是同一张表。
_XIAOXIN_ALIASES = ("小心", "小欣", "小鑫")
_LEADS = ("", "嘿", "哎", "喂", "嗨", "hi", "hey")


# 读音一样、端侧小模型常互相认错的字，比对时当成同一个（2026-10-30 实测「退一下」被认成「推一下」）。
# 一个字换一个字，位置不变，strip_wake_word 按字数切原文不受影响。Keywords.java 里是同一张表。
_SAME_SOUND = str.maketrans({"推": "退"})


def _bare(text: str) -> str:
    return "".join(c for c in text if c not in _PUNCT).lower().translate(_SAME_SOUND)


WILDCARD = "*"  # 唤醒词 / 退出词里的「*」匹配恰好一个字：「你好小*」「小*同学」


def _match_at(bare: str, i: int, pattern: str) -> bool:
    """bare[i:] 开头是不是 pattern（* 当任意一个字）。"""
    if i + len(pattern) > len(bare):
        return False
    return all(p == WILDCARD or p == bare[i + k] for k, p in enumerate(pattern))


def wake_forms(wake: str) -> list[str]:
    """一个唤醒词能接受的写法：原样，加上把「小新」换成同音字的。"""
    forms = [wake]
    if "小新" in wake:
        forms += [wake.replace("小新", a) for a in _XIAOXIN_ALIASES]
    return forms


def strip_wake_word(text: str, wakes) -> str:
    """去掉开头的唤醒词（前面可以带「嘿 / 哎」这类语气词），返回剩下的话；不是以唤醒词开头就原样返回。

    wakes 是一个或几个唤醒词。比对时不看标点空格：端侧小模型认出来没有标点，豆包认出来有。
    """
    bare = _bare(text)
    for wake in ([wakes] if isinstance(wakes, str) else wakes):
        for form in wake_forms(wake):
            for lead in _LEADS:
                if bare.startswith(lead) and _match_at(bare, len(lead), _bare(form)):
                    # 在原文里找到唤醒词最后一个字的位置，从后面切，保留原文的标点
                    seen, need = 0, len(lead + form)
                    for i, c in enumerate(text):
                        if c not in _PUNCT:
                            seen += 1
                            if seen == need:
                                return text[i + 1:].lstrip(_PUNCT)
    return text.strip()


def is_exit(text: str, exits) -> bool:
    """整句（去掉标点空格）正好是某个退出词，「小新」认成「小心」也算（和唤醒词同一套）。「你先退下吧」不算。
    pad 上是 Keywords.equalsAny 在本机先判；这里给浏览器和本机没判到的兜底。"""
    bare = _bare(text)
    return bool(bare) and any(len(bare) == len(_bare(f)) and _match_at(bare, 0, _bare(f))
                              for e in exits for f in wake_forms(e))


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
        self._local: list[str] = []  # 端侧认出、还没对上用户消息的句子
        self._client_vad = False     # 端侧在断句（收到过 vad_start / vad_end）
        self._utt: bytearray | None = None  # 端侧断句时，这句正在收的音频

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
                elif cmd.get("type") == "vad_start":
                    await self.on_vad_start()
                elif cmd.get("type") == "vad_end":
                    await self.on_vad_end()
                elif cmd.get("type") == "local":
                    text = str(cmd.get("text") or "").strip()[:200]
                    if text:
                        self._local = (self._local + [text])[-4:]
                elif cmd.get("type") == "wake":
                    await self.on_wake(str(cmd.get("text") or ""), quiet=bool(cmd.get("acked")))
        finally:
            self.closed = True
            await self.cancel_turn()

    async def on_vad_start(self) -> None:
        self._client_vad = True
        self._utt = bytearray()
        await self.send("listening")

    async def on_vad_end(self) -> None:
        self._client_vad = True
        utt, self._utt = self._utt, None
        if utt is not None:
            self._hear(bytes(utt))

    def _hear(self, utt: bytes) -> None:
        """一句说完：先识别，认出字了才算这一句。小新正在想 / 在说时，认出字才打断（barge-in）。

        不在开口时就打断：开口可能是咳嗽、回声、唤醒词尾巴，认出来是空的。之前一开口就取消，
        「你好小新给我设一个倒计时」刚送进模型，0.1 秒后一段 0.6 秒的杂音把它取消了，结果什么都不答（2026-10-30）。"""
        task = asyncio.create_task(self._recognize(utt))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)

    async def _recognize(self, utt: bytes) -> None:
        t0 = time.monotonic()
        try:
            text = (await asyncio.to_thread(self._transcribe, self.asr_engine, utt)).strip()
        except Exception as e:
            await self.send("error", detail=f"识别失败：{str(e)[:120]}")
            return
        t_asr = time.monotonic() - t0
        if self.closed:
            return
        if not text:
            log.info("talk heard %.1fs audio in %.2fs: (空，不算)", len(utt) / 32000, t_asr)
            self._local = []
            if not self.busy:
                await self.send("idle")
            return
        if self.busy:
            if len(_bare(text)) < BARGE_IN_MIN_CHARS and not is_exit(text, voices.engines()["exit_words"]):
                # 小新在想 / 在说时只认出一个字（「嗯」「啊」「好」，多半是附和、回声或杂音）：不打断，这句也不答
                log.info("talk ignore short barge-in during turn %d: %s", self.turn_id, text)
                self._local = []
                return
            log.info("talk barge-in during turn %d: %s", self.turn_id, text[:30])
            await self.cancel_turn()
            await self.send("interrupt")
        self.turn_id += 1
        self.turn = asyncio.create_task(self.answer(self.turn_id, utt, text=text, t_asr=t_asr))

    async def on_pcm(self, pcm: bytes) -> None:
        if self._client_vad:
            if self._utt is not None:
                self._utt += pcm
            return
        for kind, utt in self.vad.feed(pcm):
            if kind == "start":
                await self.send("listening")
            else:
                self._hear(utt)

    async def on_wake(self, text: str, quiet: bool = False) -> None:
        """quiet：端侧已经用本机缓存的「在」应答过了（pad），只喊了唤醒词时这边不再合成应答。"""
        rest = strip_wake_word(text, voices.engines()["wake_words"])
        log.info("talk wake: %r -> %r", text, rest)
        await self.cancel_turn()
        self.turn_id += 1
        if rest:
            self._local = [text]  # one-shot：这句本来就是端侧认的
            self.turn = asyncio.create_task(self.answer(self.turn_id, b"", text=rest))
        elif not quiet:
            self.turn = asyncio.create_task(self.answer(self.turn_id, b"", text=WAKE_ACK, ack=True))
        else:
            await self.send("idle")  # pad 已经在本机说了「在」，这边只等用户开口

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
    async def answer(self, turn_id: int, utt: bytes, text: str | None = None, ack: bool = False,
                     t_asr: float = 0.0) -> None:
        """一轮：（已经识别好的）text → 模型 → 朗读。ack=True 时 text 是固定应答，不问模型。
        t_asr 是识别花的时间；首响计时从说完算，所以 t0 往前挪 t_asr。"""
        t0 = time.monotonic() - t_asr
        if ack:
            await self.speak_only(text)
            return
        text = (text or "").strip()
        local, self._local = " / ".join(self._local), []
        log.info("talk turn %d heard %.1fs audio in %.2fs: %s%s", turn_id, len(utt) / 32000, t_asr, text[:60],
                 f" (端侧: {local[:60]})" if local else "")
        if not text:
            await self.send("idle")
            return
        if is_exit(text, voices.engines()["exit_words"]):
            # 退出词（本机没判到时的兜底）：不问模型、不进记录，告诉端侧挂断。「拜拜」由端侧用缓存的音色说
            log.info("talk turn %d exit word: %s", turn_id, text)
            await self.send("exit", text=text)
            await self.send("bye")
            return
        model = self.model
        t_search, search_at, first_text, saved_id = 0.0, None, None, None
        user = store.append(self.cid, {"role": "user", "text": text, "files": [], "via": "talk",
                                       "local": local, "audio": bool(utt)})
        if utt:
            await asyncio.to_thread(store.save_utterance, self.cid, user["id"], utt)
        await self.send("heard", text=text, t_asr=round(t_asr, 2), id=user["id"], local=local, audio=bool(utt))
        history = self._history(store.get(self.cid))
        tts, note = self._make_speech()
        if note:
            await self.send("note", detail=note)
        await self.send("thinking")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        worker = loop.run_in_executor(None, self._llm_thread, model, history, q, loop, True)
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
                    elif kind == "tool":
                        await self.send("tool", **value)
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
            log.info("talk turn %d done: %s audio=%.1fs total=%.1fs tts[open=%s text=%s pcm=%s sent=%s, +%.2f]",
                     turn_id, timing, info["seconds"], time.monotonic() - t0, info.get("opened_s"),
                     info["first_text_s"], info.get("first_pcm_s"), info["first_audio_s"], tts_base)
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

    async def speak_only(self, text: str, idle: bool = True) -> None:
        """念一句固定应答（「我在」「有事再叫我」），不进对话记录、不问模型。"""
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
            if idle:
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
