"""引擎设置和音色选择：选卡片联动引擎、CosyVoice 不可用时的降级、synth 走哪个引擎、自定义豆包音色。"""

from __future__ import annotations

from pathlib import Path

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


def test_vad_end_ms_default_800_and_adjustable(fakes):
    assert voices.engines()["vad_end_ms"] == 800
    assert voices.set_engines(vad_end_ms=500)["vad_end_ms"] == 500
    assert voices.set_engines(vad_end_ms="650")["vad_end_ms"] == 650  # 网页传字符串也行


@pytest.mark.parametrize("bad", [100, 5000, "abc"])
def test_vad_end_ms_rejects_out_of_range(fakes, bad):
    with pytest.raises(voices.VoiceError):
        voices.set_engines(vad_end_ms=bad)
    assert voices.engines()["vad_end_ms"] == 800


def test_wake_words_default_and_change(fakes):
    e = voices.engines()
    assert e["wake_words"] == ["小新同学", "你好小新"] and e["wake_word"] == "小新同学"
    assert voices.set_engines(wake_words="小新同学，你好小新、小新小新")["wake_words"] == ["小新同学", "你好小新", "小新小新"]
    assert voices.set_engines(wake_word=" 小新小新 ")["wake_words"] == ["小新小新"]  # 老接口：单个


def test_old_single_wake_word_setting_still_read(fakes):
    store.update_settings(wake_word="小新小新")
    assert voices.engines()["wake_words"] == ["小新小新"]


@pytest.mark.parametrize("bad,msg", [("小新", "至少 4 个字"), ("小新同学，小新", "至少 4 个字"), ("", "1–4"),
                                     ("小新同学你好呀朋友们", "2–8"), ("一二三四,五六七八,甲乙丙丁,子丑寅卯,东南西北", "1–4")])
def test_wake_words_validation(fakes, bad, msg):
    with pytest.raises(voices.VoiceError, match=msg):
        voices.set_engines(wake_words=bad)
    assert voices.engines()["wake_words"] == ["小新同学", "你好小新"]


def test_exit_words_default_and_change(fakes):
    assert voices.engines()["exit_words"] == ["退下", "退下吧"]
    assert voices.set_engines(exit_words="再见小新, 拜拜")["exit_words"] == ["再见小新", "拜拜"]
    with pytest.raises(voices.VoiceError):
        voices.set_engines(exit_words="退")


def test_wake_idle_default_30_and_adjustable(fakes):
    assert voices.engines()["wake_idle_s"] == 30
    assert voices.set_engines(wake_idle_s="45")["wake_idle_s"] == 45


@pytest.mark.parametrize("bad", [5, 301, "x"])
def test_wake_idle_rejects_out_of_range(fakes, bad):
    with pytest.raises(voices.VoiceError, match="回待命"):
        voices.set_engines(wake_idle_s=bad)
    assert voices.engines()["wake_idle_s"] == 30


def test_unknown_engine_in_settings_file_falls_back(fakes):
    store.update_settings(asr_engine="gone", tts_engine="gone")
    assert (voices.engines()["asr"], voices.engines()["tts"]) == ("doubao", "doubao")


# ---------------------------------------------------------------- 引擎和音色分开选

XIAOHE = "doubao:" + voices.PRESETS[1][0]


def test_each_voice_belongs_to_one_engine(fakes):
    cat = voices.catalog()
    by_id = {v["id"]: v for v in cat["voices"]}
    assert by_id[XIAOHE]["engine"] == "doubao"
    assert by_id[voices.BUILTIN]["engine"] == "cosyvoice" and by_id[voices.BUILTIN]["kind"] == "builtin"
    assert voices.find(add_clone())["engine"] == "cosyvoice"


def test_cosyvoice_defaults_to_builtin_male(fakes):
    voices.set_engines(tts="cosyvoice")
    assert voices.catalog()["current"] == voices.BUILTIN
    v, engine, note = voices.resolve()
    assert (v["id"], engine, note) == (voices.BUILTIN, "cosyvoice", "")


def test_selecting_switches_engine_to_the_voices_engine(fakes):
    voices.set_engines(tts="cosyvoice")
    cat = voices.select(XIAOHE)
    assert cat["engines"]["tts"] == "doubao" and cat["current"] == XIAOHE
    cat = voices.select(voices.BUILTIN)
    assert cat["engines"]["tts"] == "cosyvoice" and cat["current"] == voices.BUILTIN
    clone = add_clone()
    cat = voices.select(clone)
    assert cat["engines"]["tts"] == "cosyvoice" and cat["current"] == clone


def test_each_engine_remembers_its_own_voice(fakes):
    clone = add_clone()
    voices.select(XIAOHE)
    voices.select(clone)
    voices.set_engines(tts="doubao")
    assert voices.catalog()["current"] == XIAOHE
    voices.set_engines(tts="cosyvoice")
    assert voices.catalog()["current"] == clone


def test_old_settings_with_doubao_voice_on_cosyvoice_use_builtin(fakes):
    """老数据：以前允许 CosyVoice + 豆包音色，现在 CosyVoice 下就落到内置男声。"""
    store.update_settings(tts_engine="cosyvoice", voice=XIAOHE)
    cat = voices.catalog()
    assert cat["current"] == voices.BUILTIN and cat["doubao_voice"] == XIAOHE


def test_deleted_clone_voice_falls_back_to_builtin(fakes):
    clone = add_clone()
    voices.select(clone)
    store.delete_sample(clone.split(":")[1])
    assert voices.catalog()["current"] == voices.BUILTIN


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


def test_resolve_builtin_falls_back_to_doubao_when_triton_down(fakes):
    voices.select(XIAOHE)
    voices.select(voices.BUILTIN)
    fakes.cosyvoice_ready = False
    voices._probe.update(at=0.0)
    v, engine, note = voices.resolve()
    assert (v["id"], engine) == (XIAOHE, "doubao") and "没起来" in note


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


def test_synth_builtin_male_uses_official_reference(fakes):
    speech, used, note = voices.synth("你好", voices.BUILTIN)
    assert used == voices.BUILTIN and speech.sample_rate == 24000 and note == ""
    assert fakes.calls[-1] == ("cosyvoice", "你好", voices.BUILTIN_MALE_TEXT)
    assert "doubao.tts" not in fakes.names()  # 不再拿豆包念参考


def test_builtin_reference_cut_from_official_asset_at_16k(fakes):
    ref, text = voices.builtin_reference()
    a, b = voices.BUILTIN_MALE_SPAN
    assert text == voices.BUILTIN_MALE_TEXT
    assert ref.size == pytest.approx((b - a) * 16000, abs=2)
    assert (store.VOICE / "builtin" / f"male_{Path(voices.BUILTIN_MALE_SOURCE).stem}.wav").is_file()


def test_builtin_reference_cached(fakes, monkeypatch):
    voices.builtin_reference()
    monkeypatch.setattr(voices, "COSYVOICE_ASSET", voices.COSYVOICE_ASSET / "gone")
    voices.builtin_reference()  # 切好的在数据目录里，第二次不再读素材


def test_builtin_reference_missing_asset(fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(voices, "COSYVOICE_ASSET", tmp_path / "nope")
    with pytest.raises(voices.VoiceError, match="内置男声素材"):
        voices.builtin_reference()


def test_doubao_voice_cannot_be_read_by_cosyvoice(fakes):
    with pytest.raises(voices.VoiceError, match="豆包音色"):
        voices.reference(voices.find(XIAOHE))


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
