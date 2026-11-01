"""小新的引擎和音色：ASR 豆包 / Paraformer，TTS 豆包 / CosyVoice。

音色 id 的写法：
  doubao:<speaker>   豆包音色，speaker 就是火山控制台里的音色 ID
  clone:<sample id>  自己录的声音，走 gpu2 本机 CosyVoice zero-shot；CosyVoice 没起来时先用豆包音色顶上
设置都存在 store.settings()，全家共用（平板、手机、电脑听到的是同一个小新）：
  asr_engine   doubao | paraformer
  tts_engine   doubao | cosyvoice
  doubao_voice     豆包引擎上选的音色（doubao:…）
  cosyvoice_voice  CosyVoice 引擎上选的音色（cosyvoice:male 或 clone:…），默认内置男声
  voice            最近选的那个（老数据兼容）
每个音色只属于一个引擎：豆包音色走豆包；CosyVoice 用内置男声或自己录的声音。点哪个音色，引擎就切到它那边；
切引擎时用那个引擎上次选的音色。
以前试过拿豆包念的一段当 CosyVoice 的参考，豆包参考 25–40% 整段无声，2026-10-30 去掉了。
  cosyvoice:male     CosyVoice 内置男声（见 BUILTIN_MALE_*）
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from server import store, vad
from src.voice import cosyvoice, doubao

ASR_ENGINES = [
    ("doubao", "豆包", "云端流式，边说边出字"),
    ("paraformer", "Paraformer", "gpu2 本机 SeACo-Paraformer，松手后整段识别，带热词"),
]
TTS_ENGINES = [
    ("doubao", "豆包", "云端，音色多"),
    ("cosyvoice", "CosyVoice", "gpu2 本机 CosyVoice3，内置男声或自己录的声音"),
]
# 挂断提示音：网页（app.js 的 TONES）和 pad（Earcon.java）各自现场合成，这里只记选了哪个
HANGUP_TONES = [("down", "下行两声"), ("drop", "水滴"), ("soft", "轻敲"), ("chime", "三音下行"), ("none", "不响")]
TONE_IDS = {t[0] for t in HANGUP_TONES}
ASR_IDS = {e[0] for e in ASR_ENGINES}
TTS_IDS = {e[0] for e in TTS_ENGINES}

# 2026-10-28 用这个 key 逐个合成过，都能出声。中文名按音色 ID 意译，以试听为准。
PRESETS = [
    ("zh_female_vv_uranus_bigtts", "Vivi", "女 · 亲切自然（carHer2 默认）"),
    ("zh_female_xiaohe_uranus_bigtts", "小何", "女 · 温和"),
    ("zh_female_cancan_uranus_bigtts", "灿灿", "女 · 知性"),
    ("zh_female_kailangjiejie_uranus_bigtts", "开朗姐姐", "女 · 明快"),
    ("zh_female_wenrouxiaoya_uranus_bigtts", "温柔小雅", "女 · 轻柔"),
    ("zh_female_tianmeixiaoyuan_uranus_bigtts", "甜美小源", "女 · 甜"),
    ("zh_female_qingxinnvsheng_uranus_bigtts", "清新女声", "女 · 清亮"),
    ("saturn_zh_female_keainvsheng_tob", "可爱女生", "女 · 童趣"),
    ("saturn_zh_female_tiaopigongzhu_tob", "调皮公主", "女 · 童趣"),
    ("zh_male_m191_uranus_bigtts", "云舟", "男 · 沉稳"),
    ("zh_male_taocheng_uranus_bigtts", "小天", "男 · 年轻"),
    ("zh_male_yangguangqingnian_uranus_bigtts", "阳光青年", "男 · 阳光"),
    ("zh_male_shaonianzixin_uranus_bigtts", "少年梓辛", "男 · 少年"),
    ("saturn_zh_male_tiancaitongzhuo_tob", "天才同桌", "男 · 同龄人"),
    ("saturn_zh_male_shuanglangshaonian_tob", "爽朗少年", "男 · 少年"),
]
DEFAULT = "doubao:" + PRESETS[0][0]

# CosyVoice3 没有内置音色（只能 zero-shot），要给一段男声当参考。
# 2026-10-30 第二轮：用户嫌 MiMo-Audio 那段（prompt_speech_zh_m.wav）口音重、不够阳光，在 13 个候选里试听选了
# 豆包 TTS「少年梓辛」（zh_male_shaonianzixin_uranus_bigtts）生成的一段，3.1 秒、16 kHz、音高约 151 Hz。
# 同一个音色不同段差别很大，这一段 32 次 0 无声 0 跑飞，首个有声样本中位 0.29 秒、最慢 0.34 秒，Paraformer 回认错字 0.3%。
# 候选和全部测量在 gpu2 数据目录 voice/male_candidates_20261030/。素材随代码放在 server/assets/。
COSYVOICE_ASSET = Path(os.environ.get("COSYVOICE_ASSET", str(Path(__file__).parent / "assets")))
BUILTIN_MALE_SOURCE = "male_shaonianzixin.wav"
BUILTIN_MALE_SPAN = (0.0, 3.08)  # 已经切好（前后各留 0.1 秒），整段用
BUILTIN_MALE_TEXT = "这首歌的旋律真好听，我已经跟着哼了好几遍了。"
BUILTIN = "cosyvoice:male"

# pad 待命唤醒：端侧 Paraformer 小模型认出这句话以唤醒词开头，才连服务端。没唤醒时一个字节都不上传。
# 2026-10-30 在 pad 上测：4 种声音念「小新同学…」16/16 认对；只喊「小新」两个字 4 次里认错 1 次（小心 / 小谢），
# 所以唤醒词至少 4 个字。
# 「你好小新」2026-10-30 测：单喊 4/4 对；带问题时「小新」偶尔被认成「小心」（端侧 8 句错 2 句），
# 所以比对时把「小心」也当「小新」（只在唤醒词里替换，见 talk.wake_forms）。
WAKE_WORDS = ["小新同学", "你好小新"]
WAKE_WORD = WAKE_WORDS[0]
# 退出词：唤醒后 pad 端侧和唤醒词同一套判断（本机 Paraformer、「小心」当「小新」），但要整句正好是它才退，
# 「你先退下吧」不算。端侧判、立刻挂断回待命，不等云端。服务端也用同样的规则再兜一次（浏览器里用的时候）。
# 2026-10-30 端侧 4 种声音实测：小新退下 / 没事了小新 / 小新再见 / 小新拜拜 都 4/4；「退下」「退下吧」只有 2/4
# （认成 对的 / 推下 / 退一下吧 / 多一下吧），所以默认用带「小新」的。
EXIT_WORDS = ["小新退下", "没事了小新"]
WAKE_VAD_MS = 300            # 待命时端侧断句：只为认唤醒词，一句短话，300 ms 够了（2026-11-01 用户要求，和云端的分开）
WAKE_VAD_RANGE = (150, 1500)
WAKE_IDLE_S = 30  # 唤醒后这么久没人说话（小新也不在说）就挂断回待命；设置里可调。最后 10 秒 pad 上倒计时
WAKE_IDLE_RANGE = (10, 300)
PREVIEW = "你好，我是小新。今天的作业写完了吗？有不会的题可以来问我。"


class VoiceError(ValueError):
    pass


_probe = {"at": 0.0, "ok": False}


def clone_ready() -> bool:
    """CosyVoice 的 Triton 起没起来。列音色、每轮朗读都要问，探活结果留 10 秒。"""
    if time.monotonic() - _probe["at"] > 10:
        _probe.update(at=time.monotonic(), ok=cosyvoice.ready(timeout=0.5))
    return _probe["ok"]


def engines() -> dict:
    s = store.settings()
    asr = s.get("asr_engine") if s.get("asr_engine") in ASR_IDS else "doubao"
    tts = s.get("tts_engine") if s.get("tts_engine") in TTS_IDS else "doubao"
    end_ms = s.get("vad_end_ms") if isinstance(s.get("vad_end_ms"), int) else vad.END_MS
    words = s.get("wake_words")
    if not (isinstance(words, list) and words and all(isinstance(w, str) for w in words)):
        old = s.get("wake_word")  # 老数据只有一个
        words = [old] if isinstance(old, str) and old else list(WAKE_WORDS)
    exits = s.get("exit_words") if isinstance(s.get("exit_words"), list) and s.get("exit_words") else list(EXIT_WORDS)
    return {"asr": asr, "tts": tts, "vad_end_ms": end_ms, "vad_end_ms_range": list(vad.END_MS_RANGE),
            "wake_words": words, "wake_word": words[0], "exit_words": exits,
            "hangup_tone": s.get("hangup_tone") if s.get("hangup_tone") in TONE_IDS else "down",
            "hangup_tones": [{"id": i, "name": n} for i, n in HANGUP_TONES],
            "wake_idle_s": s.get("wake_idle_s") if isinstance(s.get("wake_idle_s"), int) else WAKE_IDLE_S,
            "wake_vad_ms": s.get("wake_vad_ms") if isinstance(s.get("wake_vad_ms"), int) else WAKE_VAD_MS,
            "wake_vad_ms_range": list(WAKE_VAD_RANGE),
            "wake_idle_s_range": list(WAKE_IDLE_RANGE), "asr_hotwords": hotwords(),
            "asr_options": [{"id": i, "name": n, "desc": d} for i, n, d in ASR_ENGINES],
            "tts_options": [{"id": i, "name": n, "desc": d} for i, n, d in TTS_ENGINES]}


# 豆包 ASR 热词：人名、游戏名、专有名词，识别时优先认成这些。改了下一句起生效（每次识别现读设置）
HOTWORDS = ["小新", "卞依一"]
MAX_HOTWORDS = 100
MAX_HOTWORD_LEN = 20


def hotwords() -> list[str]:
    hw = store.settings().get("asr_hotwords")
    return hw if isinstance(hw, list) and all(isinstance(w, str) for w in hw) else list(HOTWORDS)


def check_hotwords(value) -> list[str]:
    import re

    raw = value if isinstance(value, list) else re.split(r"[\n,，、;；]+", str(value or ""))
    out: list[str] = []
    for w in raw:
        w = str(w).strip()
        if not w:
            continue
        if len(w) > MAX_HOTWORD_LEN:
            raise VoiceError(f"热词每个最多 {MAX_HOTWORD_LEN} 个字：{w[:24]}")
        if w not in out:
            out.append(w)
    if len(out) > MAX_HOTWORDS:
        raise VoiceError(f"热词最多 {MAX_HOTWORDS} 个")
    return out


def _split_words(value) -> list[str]:
    """「小新同学, 你好小新」「小新同学 你好小新」或列表，都拆成列表，去重保序。"""
    import re

    items = value if isinstance(value, list) else re.split(r"[,，、;；\s]+", str(value or ""))
    out = []
    for w in (str(x).strip() for x in items):
        if w and w not in out:
            out.append(w)
    return out


def check_wake_word(word) -> str:
    """唤醒词比退出词多一条：至少 4 个字。唤醒词是开头匹配，短了一句话里随便带到就会误唤醒。"""
    word = _check_word(word, "唤醒词")
    if len(word) < 4:
        raise VoiceError("唤醒词至少 4 个字：两个字的（比如「小新」）容易认成「小心」「小谢」，也容易误唤醒")
    return word


WILDCARD = "*"
MIN_FIXED = 3  # 带 * 的唤醒词，确定的字至少这么多，不然「小**」这种什么都能匹配
# 退出词是整句匹配，「退*吧」只命中三个字、退开头吧结尾的一句话，误判面小，所以 2 个确定的字就够
MIN_FIXED_EXIT = 2


def check_wake_words(value) -> list[str]:
    words = [check_wake_word(w) for w in _split_words(value)]
    if not 1 <= len(words) <= MAX_WORDS:
        raise VoiceError(f"唤醒词 1–{MAX_WORDS} 个")
    return words


MAX_WORDS = 8  # 唤醒词、退出词各最多几个


def _check_word(word, what: str, min_fixed: int = MIN_FIXED) -> str:
    """唤醒词、退出词同一个规则：2–8 个字，不带标点空格；「*」是通配，匹配恰好一个字。"""
    word = str(word or "").strip()
    if not 2 <= len(word) <= 8 or not all(c.isalnum() or c == WILDCARD for c in word):
        raise VoiceError(f"{what}每个 2–8 个字，不带标点和空格（* 代表任意一个字）")
    if WILDCARD in word and len(word) - word.count(WILDCARD) < min_fixed:
        example = "「你好小*」「小*同学」" if min_fixed >= 3 else "「退*吧」"
        raise VoiceError(f"带 * 的{what}至少要有 {min_fixed} 个确定的字，比如{example}")
    return word


def check_exit_words(value) -> list[str]:
    words = [_check_word(w, "退出词", MIN_FIXED_EXIT) for w in _split_words(value)]
    if not 1 <= len(words) <= MAX_WORDS:
        raise VoiceError(f"退出词 1–{MAX_WORDS} 个")
    return words


def _int_in(value, lo: int, hi: int, what: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise VoiceError(f"{what}要填数字") from None
    if not lo <= n <= hi:
        raise VoiceError(f"{what}要在 {lo}–{hi} 之间")
    return n


def set_engines(asr: str | None = None, tts: str | None = None, vad_end_ms=None, wake_word=None,
                wake_idle_s=None, wake_words=None, exit_words=None, hangup_tone=None, asr_hotwords=None,
                wake_vad_ms=None) -> dict:
    fields = {}
    if hangup_tone is not None:
        if hangup_tone not in TONE_IDS:
            raise VoiceError(f"没有这个提示音：{hangup_tone}")
        fields["hangup_tone"] = hangup_tone
    if wake_words is not None or wake_word is not None:
        fields["wake_words"] = check_wake_words(wake_words if wake_words is not None else wake_word)
    if exit_words is not None:
        fields["exit_words"] = check_exit_words(exit_words)
    if asr_hotwords is not None:
        fields["asr_hotwords"] = check_hotwords(asr_hotwords)
    if wake_idle_s is not None:
        fields["wake_idle_s"] = _int_in(wake_idle_s, *WAKE_IDLE_RANGE, "唤醒后多久没说话回待命（秒）")
    if wake_vad_ms is not None:
        fields["wake_vad_ms"] = _int_in(wake_vad_ms, *WAKE_VAD_RANGE, "待命时停多久算一句（毫秒）")
    if vad_end_ms is not None:
        fields["vad_end_ms"] = _int_in(vad_end_ms, *vad.END_MS_RANGE, "说完判定（毫秒）")
    if asr is not None:
        if asr not in ASR_IDS:
            raise VoiceError(f"没有这个 ASR 引擎：{asr}")
        fields["asr_engine"] = asr
    if tts is not None:
        if tts not in TTS_IDS:
            raise VoiceError(f"没有这个 TTS 引擎：{tts}")
        fields["tts_engine"] = tts  # 当前音色由 catalog() 按引擎取那个引擎上次选的
    store.update_settings(**fields)
    return engines()


def catalog() -> dict:
    s = store.settings()
    custom = s.get("custom_voices") or []
    ready = clone_ready()
    eng = engines()
    out = [{"id": "doubao:" + sp, "kind": "doubao", "name": name, "desc": desc, "engine": "doubao", "ready": True}
           for sp, name, desc in PRESETS]
    out += [{"id": "doubao:" + c["speaker"], "kind": "doubao", "name": c["name"], "desc": "自己加的豆包音色",
             "custom": True, "engine": "doubao", "ready": True} for c in custom]
    out.append({"id": BUILTIN, "kind": "builtin", "name": "CosyVoice 男声",
                "desc": "男 · 普通话（本机，最稳）", "engine": "cosyvoice", "ready": ready})
    for m in store.list_samples():
        ref = m.get("ref") or {}
        out.append({"id": "clone:" + m["id"], "kind": "clone", "name": m.get("name") or "我的声音",
                    "desc": f"自己录的 · {m['seconds']} 秒"
                            + (f"，参考段 {ref['seconds']} 秒" if ref.get("seconds") else ""),
                    "sample": m["id"], "engine": "cosyvoice", "ready": ready and bool(ref.get("text"))})
    ids = {e: [v["id"] for v in out if v["engine"] == e] for e in TTS_IDS}
    doubao_voice = next((x for x in (s.get("doubao_voice"), s.get("voice")) if x in ids["doubao"]), DEFAULT)
    cosy_voice = next((x for x in (s.get("cosyvoice_voice"), s.get("voice")) if x in ids["cosyvoice"]), BUILTIN)
    current = cosy_voice if eng["tts"] == "cosyvoice" else doubao_voice
    return {"voices": out, "current": current, "default": DEFAULT, "clone_ready": ready,
            "doubao_voice": doubao_voice, "cosyvoice_voice": cosy_voice, "engines": eng}


def find(voice_id: str) -> dict:
    for v in catalog()["voices"]:
        if v["id"] == voice_id:
            return v
    raise VoiceError(f"没有这个音色：{voice_id}")


def select(voice_id: str) -> dict:
    """选一个音色，引擎跟着切到它所属的那个；两个引擎各记各的音色，切回来还是上次那个。"""
    v = find(voice_id)
    if v["engine"] == "cosyvoice":
        store.update_settings(voice=voice_id, cosyvoice_voice=voice_id, tts_engine="cosyvoice")
    else:
        store.update_settings(voice=voice_id, doubao_voice=voice_id, tts_engine="doubao")
    return catalog()


def add_custom(speaker: str, name: str) -> dict:
    speaker, name = speaker.strip(), name.strip()[:20]
    if not speaker or len(speaker) > 120 or not all(c.isalnum() or c in "_-." for c in speaker):
        raise VoiceError("音色 ID 只能是字母、数字和 _ - .，照火山控制台复制过来")
    if any(sp == speaker for sp, _, _ in PRESETS):
        raise VoiceError("这个音色已经在列表里了")
    try:
        doubao.tts("你好。", speaker=speaker)
    except doubao.DoubaoError as e:
        raise VoiceError(f"豆包不认这个音色 ID（{str(e)[:80]}）") from e
    custom = [c for c in store.settings().get("custom_voices") or [] if c["speaker"] != speaker]
    custom.append({"speaker": speaker, "name": name or speaker})
    store.update_settings(custom_voices=custom)
    return catalog()


def remove_custom(speaker: str) -> dict:
    custom = [c for c in store.settings().get("custom_voices") or [] if c["speaker"] != speaker]
    s = store.update_settings(custom_voices=custom)
    if s.get("voice") == "doubao:" + speaker:
        store.update_settings(voice=DEFAULT)
    return catalog()


def clone_reference(sid: str) -> tuple:
    """录的声音给 CosyVoice 用的 (参考音频, 参考文字)。"""
    ref = store.sample_meta(sid).get("ref") or {}
    path = store.sample_path(sid).with_suffix(".ref.wav")
    if not ref.get("text") or not path.is_file():
        raise VoiceError("这段录音还没切出参考段，或者参考段没认出字，重录一次")
    return cosyvoice.load_reference(path), ref["text"]


_builtin_lock = threading.Lock()


def builtin_reference() -> tuple:
    """内置男声给 CosyVoice 用的 (参考音频, 参考文字)。

    从素材切一次存到数据目录，之后直接读；文件名带上素材名，换了素材会重新切。素材是 s16 wav，这里转 16 kHz。
    """
    import wave

    path = store.VOICE / "builtin" / f"male_{Path(BUILTIN_MALE_SOURCE).stem}.wav"
    with _builtin_lock:
        if not path.is_file():
            src = COSYVOICE_ASSET / BUILTIN_MALE_SOURCE
            try:
                with wave.open(str(src), "rb") as w:
                    rate, raw = w.getframerate(), w.readframes(w.getnframes())
            except (OSError, wave.Error) as e:
                raise VoiceError(f"找不到 CosyVoice 内置男声素材：{src}") from e
            pcm = cosyvoice.resample_s16(raw, rate, 16000)
            a, b = (int(s * 16000) * 2 for s in BUILTIN_MALE_SPAN)
            doubao.write_wav(path, pcm[a:b], 16000)
    return cosyvoice.load_reference(path), BUILTIN_MALE_TEXT


def reference(v: dict) -> tuple:
    """CosyVoice 音色的 (参考音频, 参考文字)。"""
    if v["kind"] == "clone":
        return clone_reference(v["sample"])
    if v["kind"] == "builtin":
        return builtin_reference()
    raise VoiceError(f"{v['name']} 是豆包音色，CosyVoice 念不了")


def resolve(voice_id: str | None = None) -> tuple[dict, str, str]:
    """这次用哪个音色、哪个引擎。返回 (音色, 引擎, 说明)。

    引擎就是音色所属的那个；CosyVoice 用不了时换成豆包引擎上次选的音色，说明里写原因。
    """
    cat = catalog()
    v = find(voice_id or cat["current"])
    if v["engine"] == "cosyvoice" and not v["ready"]:
        why = "CosyVoice 没起来" if not cat["clone_ready"] else "这段录音的参考段没认出字"
        return find(cat["doubao_voice"]), "doubao", f"{why}，先用豆包音色"
    return v, v["engine"], ""


def synth(text: str, voice_id: str | None = None) -> tuple[doubao.Speech, str, str]:
    """合成一段话。返回 (音频, 实际用的音色 id, 说明)。"""
    v, engine, note = resolve(voice_id)
    if engine == "cosyvoice":
        try:
            pcm, first, total = cosyvoice.tts(text, *reference(v))
        except cosyvoice.CosyVoiceError as e:
            raise VoiceError(f"CosyVoice 合成失败：{e}") from e
        return doubao.Speech(pcm, cosyvoice.SAMPLE_RATE, first, total), v["id"], note
    return doubao.tts(text, speaker=v["id"].split(":", 1)[1]), v["id"], note


def prepare_clone(sid: str) -> dict:
    """从整段录音挑 6–10 秒当参考，再用 ASR 认出这一段说的话，写进 meta["ref"]。"""
    meta = store.sample_meta(sid)
    data = store.sample_path(sid).read_bytes()
    pcm = store.wav_pcm16k(data)
    a, b = store.pick_reference(pcm, 16000)
    piece = pcm[a * 2:b * 2]
    store.write_reference(sid, piece, 16000)
    try:
        text = doubao.asr(piece).text
    except doubao.DoubaoError:
        text = ""
    ref = {"start": round(a / 16000, 2), "end": round(b / 16000, 2), "seconds": round((b - a) / 16000, 2),
           "text": text}
    return store.update_sample(sid, ref=ref, **({} if meta.get("name") else {"name": "我的声音"}))
