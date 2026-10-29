# src/voice

豆包（火山 openspeech）的 ASR 和 TTS。协议照 gpu2 `carHer2/server/app/carher/voice/doubao_*.py` 写。key 用 `.env` 里的 `DOUBAO_TTS_API_KEY`，ASR 和 TTS 共用这一个。这里依赖 `websockets`，要用 `.venv/bin/python` 跑（`pip install -r requirements.txt`）。

```bash
.venv/bin/python -m src.voice tts "你好，我是小新" -o tmp/voice/hi.wav
.venv/bin/python -m src.voice asr some_16k.wav --realtime
.venv/bin/python -m src.voice roundtrip "今天数学作业是第二十三页"
```

2026-10-28 实测：TTS 首包 0.5 s；ASR 实时送流，音频结束后约 0.14 s 出最终结果。

## gpu2 本机引擎

小新设置里可以换引擎（`PUT /api/engines`，全家共用）：ASR 豆包 / Paraformer，TTS 豆包 / CosyVoice。

| 引擎 | 文件 | 服务 | 2026-10-29 实测 |
|:--|:--|:--|:--|
| Paraformer | `paraformer.py` | carHer2 的 ASR worker `127.0.0.1:28182`，没起来时小新自己拉起 | 3.5 秒的话，松手后 0.17 秒出字；没有中间结果 |
| CosyVoice | `cosyvoice.py` | carHer2 的 Triton `127.0.0.1:18001`（健康检查 18100），用自己录的声音 | 单句首块约 0.5 秒；流式朗读首音 1.8–2.3 秒（含 LLM 首字约 1.3 秒） |

音色和引擎分开选。豆包音色在 CosyVoice 下也能用：第一次用某个音色时让豆包念一段 16 kHz 的参考（文字带音色名，见 `server/voices.py` 的 `preset_text`），存到数据目录的 `voice/presets/`，之后 CosyVoice 拿它做 zero-shot。2026-10-29 实测音高跟得上参考（云舟 136 → 132–149 Hz，小何 271 → 255–270 Hz）；第一次用多 1–4 秒，之后单句 1.2–1.5 秒。

CosyVoice 的 Triton 版大约每 10–16 次有一次念完后停不下来，一直出静音到 30 秒（固定 `max_tokens=750`，没有官方实现里按字数的 max_len 和 RAS 采样）。`cosyvoice.stream` 在说过话后连着静音 1 秒就停并取消请求，另外按字数限时长。

## 声音复刻

接口在，但这个账号没开通。`mega_tts/audio/upload` 返回 403 `parameter license not found for appid 9962383035`。开通后，复刻出来的 `S_xxx` 音色用 `--speaker S_xxx --resource-id seed-icl-2.0` 调用。
