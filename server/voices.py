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

from server import store
from src.voice import cosyvoice, doubao

ASR_ENGINES = [
    ("doubao", "豆包", "云端流式，边说边出字"),
    ("paraformer", "Paraformer", "gpu2 本机 SeACo-Paraformer，松手后整段识别，带热词"),
]
TTS_ENGINES = [
    ("doubao", "豆包", "云端，音色多"),
    ("cosyvoice", "CosyVoice", "gpu2 本机 CosyVoice3，内置男声或自己录的声音"),
]
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

# CosyVoice3 没有内置音色（只能 zero-shot），要给一段男声当参考。2026-10-30 用户在四个候选里试听选了
# MiMo-Audio 的官方中文男声示例 examples/prompt_speech_zh_m.wav（4.5 秒，24 kHz，音高约 125 Hz）。
# CosyVoice 官方那段 cross_lingual_prompt.wav 用户嫌口音重，不用了。
# 切到前后各留 0.1 秒（0.04–4.53 秒）最稳：48 次 0 次无声，首个有声样本中位 0.29 秒、最大 0.60 秒，念出来约 126 Hz。
COSYVOICE_ASSET = Path(os.environ.get("COSYVOICE_ASSET", "/Data/cmbian/codes/mimo_audio/MiMo-Audio/examples"))
BUILTIN_MALE_SOURCE = "prompt_speech_zh_m.wav"
BUILTIN_MALE_SPAN = (0.04, 4.53)
BUILTIN_MALE_TEXT = "养殖场的工作人员说，猪尾巴甩起来，就说明猪高兴。"
BUILTIN = "cosyvoice:male"
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
    return {"asr": asr, "tts": tts,
            "asr_options": [{"id": i, "name": n, "desc": d} for i, n, d in ASR_ENGINES],
            "tts_options": [{"id": i, "name": n, "desc": d} for i, n, d in TTS_ENGINES]}


def set_engines(asr: str | None = None, tts: str | None = None) -> dict:
    fields = {}
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
