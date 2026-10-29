"""引擎设置和音色选择：选卡片联动引擎、CosyVoice 不可用时的降级、synth 走哪个引擎、自定义豆包音色。"""

from __future__ import annotations

import pytest

from server import store, voices
from tests.audio import speech_with_pauses, wav_bytes


def add_clone(name="爸爸", ref_text="参考段文字"):
    """存一段录音并切好参考段（参考文字直接写，不调 ASR）。"""
    meta = store.save_sample(wav_bytes(speech_with_pauses([2.5] * 4)), "朗读", name)
    pcm = store.wav_pcm16k(store.sample_path(meta["id"]).read_bytes())
    store.write_reference(meta["id"], pcm[:16000 * 2 * 8], 16000)
    store.update_sample(meta["id"], ref={"seconds": 8.0, "text": ref_text})
    return "clone:" + meta["id"]


# ---------------------------------------------------------------- 引擎

def test_engines_default_to_doubao(fakes):
    e = voices.engines()
    assert (e["asr"], e["tts"]) == ("doubao", "doubao")
    assert {o["id"] for o in e["asr_options"]} == {"doubao", "paraformer"}
    assert {o["id"] for o in e["tts_options"]} == {"doubao", "cosyvoice"}


def test_set_engines_partial_update(fakes):
    voices.set_engines(asr="paraformer")
    assert voices.engines()["asr"] == "paraformer" and voices.engines()["tts"] == "doubao"
    voices.set_engines(tts="cosyvoice")
    assert voices.engines()["asr"] == "paraformer"


@pytest.mark.parametrize("kw", [{"asr": "whisper"}, {"tts": "edge"}])
def test_set_engines_rejects_unknown(fakes, kw):
    with pytest.raises(voices.VoiceError):
        voices.set_engines(**kw)


def test_unknown_engine_in_settings_file_falls_back(fakes):
    store.update_settings(asr_engine="gone", tts_engine="gone")
    assert (voices.engines()["asr"], voices.engines()["tts"]) == ("doubao", "doubao")


# ---------------------------------------------------------------- 引擎和音色分开选

XIAOHE = "doubao:" + voices.PRESETS[1][0]


def test_doubao_voices_work_on_both_engines(fakes):
    cat = voices.catalog()
    preset = next(v for v in cat["voices"] if v["id"] == XIAOHE)
    assert preset["engines"] == ["doubao", "cosyvoice"]
    clone = voices.find(add_clone())
    assert clone["engines"] == ["cosyvoice"]


def test_selecting_doubao_voice_keeps_cosyvoice_engine(fakes):
    voices.set_engines(tts="cosyvoice")
    cat = voices.select(XIAOHE)
    assert cat["engines"]["tts"] == "cosyvoice" and cat["current"] == XIAOHE
    v, engine, note = voices.resolve()
    assert (v["id"], engine, note) == (XIAOHE, "cosyvoice", "")


def test_selecting_clone_switches_doubao_engine_to_cosyvoice(fakes):
    clone = add_clone()
    cat = voices.select(clone)
    assert cat["engines"]["tts"] == "cosyvoice" and cat["current"] == clone


def test_switching_to_doubao_with_clone_selected_restores_last_doubao_voice(fakes):
    clone = add_clone()
    voices.select(XIAOHE)
    voices.select(clone)
    voices.set_engines(tts="doubao")
    cat = voices.catalog()
    assert cat["current"] == XIAOHE  # 豆包念不了录的声音，换回上次的豆包音色
    assert store.settings()["voice"] == XIAOHE


def test_switching_engine_keeps_doubao_voice(fakes):
    voices.select(XIAOHE)
    voices.set_engines(tts="cosyvoice")
    assert voices.catalog()["current"] == XIAOHE
    voices.set_engines(tts="doubao")
    assert voices.catalog()["current"] == XIAOHE


def test_deleted_clone_voice_falls_back(fakes):
    clone = add_clone()
    voices.select(XIAOHE)
    voices.select(clone)
    store.delete_sample(clone.split(":")[1])
    assert voices.catalog()["current"] == XIAOHE


def test_unknown_voice_rejected(fakes):
    with pytest.raises(voices.VoiceError):
        voices.select("doubao:nope")


# ---------------------------------------------------------------- 降级

def test_clone_ready_requires_triton_and_ref_text(fakes):
    clone = add_clone(ref_text="")
    assert voices.find(clone)["ready"] is False
    fakes.cosyvoice_ready = False
    clone2 = add_clone(name="妈妈")
    voices._probe.update(at=0.0)
    assert voices.find(clone2)["ready"] is False


def test_resolve_clone_falls_back_to_last_doubao_voice_when_triton_down(fakes):
    clone = add_clone()
    voices.select(XIAOHE)
    voices.select(clone)
    fakes.cosyvoice_ready = False
    voices._probe.update(at=0.0)
    v, engine, note = voices.resolve()
    assert (v["id"], engine) == (XIAOHE, "doubao")  # 回到上次选的豆包音色，不是 DEFAULT
    assert "没起来" in note


def test_resolve_doubao_voice_on_cosyvoice_falls_back_to_doubao_engine(fakes):
    voices.set_engines(tts="cosyvoice")
    voices.select(XIAOHE)
    fakes.cosyvoice_ready = False
    voices._probe.update(at=0.0)
    v, engine, note = voices.resolve()
    assert (v["id"], engine) == (XIAOHE, "doubao") and "豆包念" in note


def test_probe_is_cached_for_10_seconds(fakes, monkeypatch):
    n = []
    monkeypatch.setattr(voices.cosyvoice, "ready", lambda timeout=1.0: n.append(1) or True)
    now = [1000.0]
    monkeypatch.setattr(voices.time, "monotonic", lambda: now[0])
    voices._probe.update(at=0.0)
    voices.clone_ready()
    voices.clone_ready()
    now[0] += 11
    voices.clone_ready()
    assert len(n) == 2


# ---------------------------------------------------------------- synth

def test_synth_doubao(fakes):
    speech, used, note = voices.synth("你好", "doubao:" + voices.PRESETS[0][0])
    assert used == voices.DEFAULT and note == ""
    assert fakes.calls[-1] == ("doubao.tts", voices.PRESETS[0][0], "你好")


def test_synth_cosyvoice_passes_reference_text(fakes):
    clone = add_clone(ref_text="放学回家先写作业")
    speech, used, note = voices.synth("你好", clone)
    assert used == clone and speech.sample_rate == 24000 and speech.pcm
    assert fakes.calls[-1] == ("cosyvoice", "你好", "放学回家先写作业")


def test_synth_doubao_voice_on_cosyvoice_uses_doubao_reference(fakes):
    voices.set_engines(tts="cosyvoice")
    speaker = XIAOHE.split(":")[1]
    speech, used, note = voices.synth("你好", XIAOHE)
    ref_text = voices.preset_text("小何")
    # 先让豆包念参考（16 kHz），再用 CosyVoice 合成目标文字
    assert fakes.calls[-2] == ("doubao.tts", speaker, ref_text)
    assert fakes.calls[-1] == ("cosyvoice", "你好", ref_text)
    assert used == XIAOHE and speech.sample_rate == 24000
    assert (store.VOICE / "presets" / f"{speaker}.wav").is_file()


def test_preset_reference_is_cached_on_disk(fakes):
    voices.preset_reference("spk_a", "甲")
    voices.preset_reference("spk_a", "甲")
    assert [c[0] for c in fakes.calls].count("doubao.tts") == 1


def test_preset_reference_texts_differ_per_voice(fakes):
    """Triton 按参考文字缓存说话人，两个音色参考文字一样就会串成同一个声音。"""
    texts = {voices.preset_text(name) for _, name, _ in voices.PRESETS}
    assert len(texts) == len(voices.PRESETS)


def test_preset_reference_regenerated_when_text_changes(fakes, monkeypatch):
    voices.preset_reference("spk_a", "甲")
    monkeypatch.setattr(voices, "preset_text", lambda name: "换了一段参考文字。")
    _, text = voices.preset_reference("spk_a", "甲")
    assert text == "换了一段参考文字。" and [c[0] for c in fakes.calls].count("doubao.tts") == 2


def test_preset_reference_doubao_failure(fakes):
    fakes.doubao_tts_error = "quota"
    with pytest.raises(voices.VoiceError, match="参考音"):
        voices.preset_reference("spk_a", "甲")


def test_synth_cosyvoice_failure_is_voice_error(fakes):
    clone = add_clone()
    fakes.cosyvoice_error = "Triton 500"
    with pytest.raises(voices.VoiceError, match="CosyVoice 合成失败"):
        voices.synth("你好", clone)


def test_clone_reference_requires_ref_wav(fakes):
    clone = add_clone()
    sid = clone.split(":")[1]
    store.sample_path(sid).with_suffix(".ref.wav").unlink()
    with pytest.raises(voices.VoiceError, match="参考段"):
        voices.clone_reference(sid)


# ---------------------------------------------------------------- 自定义豆包音色

def test_add_custom_validates_and_test_synthesizes(fakes):
    with pytest.raises(voices.VoiceError, match="字母"):
        voices.add_custom("bad id!", "x")
    with pytest.raises(voices.VoiceError, match="已经在列表"):
        voices.add_custom(voices.PRESETS[0][0], "x")
    fakes.doubao_tts_error = "speaker not found"
    with pytest.raises(voices.VoiceError, match="不认"):
        voices.add_custom("zh_female_new", "新")
    fakes.doubao_tts_error = ""
    cat = voices.add_custom("zh_female_new", "新")
    assert any(v["id"] == "doubao:zh_female_new" and v.get("custom") for v in cat["voices"])


def test_remove_custom_resets_current_voice(fakes):
    voices.add_custom("zh_female_new", "新")
    voices.select("doubao:zh_female_new")
    cat = voices.remove_custom("zh_female_new")
    assert cat["current"] == voices.DEFAULT


def test_prepare_clone_picks_reference_and_recognizes_it(fakes):
    meta = store.save_sample(wav_bytes(speech_with_pauses([2.5] * 5)), "朗读")
    out = voices.prepare_clone(meta["id"])
    assert 6.0 <= out["ref"]["seconds"] <= 10.5
    assert out["ref"]["text"] == fakes.doubao_text
    assert store.sample_path(meta["id"]).with_suffix(".ref.wav").is_file()
