"""小新的音色：豆包官方音色 + 自己加的豆包音色 ID + 自己录的克隆音色。

音色 id 的写法：
  doubao:<speaker>   豆包音色，speaker 就是火山控制台里的音色 ID
  clone:<sample id>  自己录的声音，走 CosyVoice zero-shot；克隆服务没起来时先用默认音色顶上
当前选中的音色存在 store.settings()["voice"]，全家共用（平板、手机、电脑听到的是同一个小新）。
"""

from __future__ import annotations

from server import store
from src.voice import doubao

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


def clone_ready() -> bool:
    """CosyVoice 起来之前一直是 False；接好后改成探活。"""
    return False


def catalog() -> dict:
    s = store.settings()
    custom = s.get("custom_voices") or []
    out = [{"id": "doubao:" + sp, "kind": "doubao", "name": name, "desc": desc} for sp, name, desc in PRESETS]
    out += [{"id": "doubao:" + c["speaker"], "kind": "doubao", "name": c["name"], "desc": "自己加的豆包音色",
             "custom": True} for c in custom]
    for m in store.list_samples():
        ref = m.get("ref") or {}
        out.append({"id": "clone:" + m["id"], "kind": "clone", "name": m.get("name") or "我的声音",
                    "desc": f"自己录的 · {m['seconds']} 秒"
                            + (f"，参考段 {ref['seconds']} 秒" if ref.get("seconds") else ""),
                    "sample": m["id"], "ready": clone_ready() and bool(ref)})
    current = s.get("voice") or DEFAULT
    if not any(v["id"] == current for v in out):
        current = DEFAULT
    return {"voices": out, "current": current, "default": DEFAULT, "clone_ready": clone_ready()}


def find(voice_id: str) -> dict:
    for v in catalog()["voices"]:
        if v["id"] == voice_id:
            return v
    raise VoiceError(f"没有这个音色：{voice_id}")


def select(voice_id: str) -> dict:
    find(voice_id)
    store.update_settings(voice=voice_id)
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


def synth(text: str, voice_id: str | None = None) -> tuple[doubao.Speech, str, str]:
    """合成一段话。返回 (音频, 实际用的音色 id, 说明)。克隆音色没就绪时用默认音色，说明里写原因。"""
    voice_id = voice_id or catalog()["current"]
    note = ""
    v = find(voice_id)
    if v["kind"] == "clone":
        if not v["ready"]:
            note = "克隆服务还没接好，先用默认音色"
            voice_id = DEFAULT
        else:  # pragma: no cover - CosyVoice 接好后在这里调用
            raise VoiceError("克隆合成还没实现")
    return doubao.tts(text, speaker=voice_id.split(":", 1)[1]), voice_id, note


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
