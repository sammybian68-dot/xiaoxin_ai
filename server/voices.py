"""小新的引擎和音色：ASR 豆包 / Paraformer，TTS 豆包 / CosyVoice。

音色 id 的写法：
  doubao:<speaker>   豆包音色，speaker 就是火山控制台里的音色 ID
  clone:<sample id>  自己录的声音，走 gpu2 本机 CosyVoice zero-shot；CosyVoice 没起来时先用豆包音色顶上
设置都存在 store.settings()，全家共用（平板、手机、电脑听到的是同一个小新）：
  asr_engine   doubao | paraformer
  tts_engine   doubao | cosyvoice
  voice         当前音色，哪种都行
  doubao_voice  最近选的豆包音色：引擎切回豆包、手上却是录的声音时用它
引擎和音色分开选：豆包音色两个引擎都能念，CosyVoice 用豆包念的一段参考来模仿它（存在 voice/presets/）；
录的声音只能 CosyVoice，选它时引擎自动切到 CosyVoice。
"""

from __future__ import annotations

import json
import threading
import time

from server import store
from src.voice import cosyvoice, doubao

ASR_ENGINES = [
    ("doubao", "豆包", "云端流式，边说边出字"),
    ("paraformer", "Paraformer", "gpu2 本机 SeACo-Paraformer，松手后整段识别，带热词"),
]
TTS_ENGINES = [
    ("doubao", "豆包", "云端，音色多"),
    ("cosyvoice", "CosyVoice", "gpu2 本机 CosyVoice3，用自己录的声音"),
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
        fields["tts_engine"] = tts
        # 切回豆包时手上是录的声音：豆包念不了，换回豆包音色里上次选的那个
        if tts == "doubao" and str(store.settings().get("voice") or "").startswith("clone:"):
            fields["voice"] = store.settings().get("doubao_voice") or DEFAULT
    store.update_settings(**fields)
    return engines()


def catalog() -> dict:
    s = store.settings()
    custom = s.get("custom_voices") or []
    ready = clone_ready()
    eng = engines()
    # 豆包音色两个引擎都能用（CosyVoice 拿豆包念的一段当参考）；录的声音只能 CosyVoice（豆包复刻没开通）
    both = ["doubao", "cosyvoice"]
    out = [{"id": "doubao:" + sp, "kind": "doubao", "name": name, "desc": desc, "engines": both}
           for sp, name, desc in PRESETS]
    out += [{"id": "doubao:" + c["speaker"], "kind": "doubao", "name": c["name"], "desc": "自己加的豆包音色",
             "custom": True, "engines": both} for c in custom]
    for v in out:
        v["ready"] = eng["tts"] == "doubao" or ready  # 在当前引擎上能不能念
    for m in store.list_samples():
        ref = m.get("ref") or {}
        out.append({"id": "clone:" + m["id"], "kind": "clone", "name": m.get("name") or "我的声音",
                    "desc": f"自己录的 · {m['seconds']} 秒"
                            + (f"，参考段 {ref['seconds']} 秒" if ref.get("seconds") else ""),
                    "sample": m["id"], "engines": ["cosyvoice"], "ready": ready and bool(ref.get("text"))})
    by_id = {v["id"]: v for v in out}
    doubao_ids = {i for i, v in by_id.items() if v["kind"] == "doubao"}
    doubao_voice = next((x for x in (s.get("doubao_voice"), s.get("voice")) if x in doubao_ids), DEFAULT)
    current = s.get("voice") if s.get("voice") in by_id else doubao_voice
    if eng["tts"] == "doubao" and current not in doubao_ids:  # 豆包引擎上不能是录的声音
        current = doubao_voice
    return {"voices": out, "current": current, "default": DEFAULT, "clone_ready": ready,
            "doubao_voice": doubao_voice, "engines": eng}


def find(voice_id: str) -> dict:
    for v in catalog()["voices"]:
        if v["id"] == voice_id:
            return v
    raise VoiceError(f"没有这个音色：{voice_id}")


def select(voice_id: str) -> dict:
    """换音色，引擎不动；只有选了录的声音、而现在是豆包引擎时，才切到 CosyVoice（豆包念不了录的声音）。"""
    v = find(voice_id)
    if v["kind"] == "clone":
        store.update_settings(voice=voice_id, tts_engine="cosyvoice")
    else:
        store.update_settings(voice=voice_id, doubao_voice=voice_id)
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


def preset_text(name: str) -> str:
    """豆包音色当 CosyVoice 参考时念的那段话，6–7 秒。

    带上音色名是为了每个音色的参考文字都不一样：Triton 按参考文字缓存说话人特征，文字一样就会串成同一个声音。
    """
    return f"你好，我是{name}。今天天气不错，放学回家先把作业写完，有不会的题就来问我。"


_preset_lock = threading.Lock()


def preset_reference(speaker: str, name: str) -> tuple:
    """豆包音色给 CosyVoice 用的 (参考音频, 参考文字)。第一次用时让豆包念一段 16 kHz 的参考存下来，之后直接读。"""
    text = preset_text(name)
    path = store.VOICE / "presets" / f"{speaker}.wav"
    meta = path.with_suffix(".json")
    with _preset_lock:
        fresh = path.is_file() and meta.is_file() and json.loads(meta.read_text(encoding="utf-8")).get("text") == text
        if not fresh:
            try:
                speech = doubao.tts(text, speaker=speaker, sample_rate=16000)
            except doubao.DoubaoError as e:
                raise VoiceError(f"豆包没念出参考音：{str(e)[:80]}") from e
            doubao.write_wav(path, speech.pcm, 16000)
            meta.write_text(json.dumps({"speaker": speaker, "text": text}, ensure_ascii=False), encoding="utf-8")
    return cosyvoice.load_reference(path), text


def reference(v: dict) -> tuple:
    """任一音色给 CosyVoice 用的 (参考音频, 参考文字)。"""
    if v["kind"] == "clone":
        return clone_reference(v["sample"])
    return preset_reference(v["id"].split(":", 1)[1], v["name"])


def resolve(voice_id: str | None = None) -> tuple[dict, str, str]:
    """这次用哪个音色、哪个引擎。返回 (音色, 引擎, 说明)。

    引擎按设置；CosyVoice 用不了时换豆包念：豆包音色照用，录的声音换成豆包引擎上次选的音色，说明里写原因。
    """
    cat = catalog()
    v = find(voice_id or cat["current"])
    engine = cat["engines"]["tts"] if "doubao" in v["engines"] else "cosyvoice"
    if engine == "cosyvoice" and not v["ready"]:
        why = "CosyVoice 没起来" if not cat["clone_ready"] else "这段录音的参考段没认出字"
        if v["kind"] == "clone":
            return find(cat["doubao_voice"]), "doubao", f"{why}，先用豆包音色"
        return v, "doubao", f"{why}，这次用豆包念"
    return v, engine, ""


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
