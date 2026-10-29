# src/voice

豆包（火山 openspeech）的 ASR 和 TTS。协议照 gpu2 `carHer2/server/app/carher/voice/doubao_*.py` 写。key 用 `.env` 里的 `DOUBAO_TTS_API_KEY`，ASR 和 TTS 共用这一个。这里依赖 `websockets`，要用 `.venv/bin/python` 跑（`pip install -r requirements.txt`）。

```bash
.venv/bin/python -m src.voice tts "你好，我是小新" -o tmp/voice/hi.wav
.venv/bin/python -m src.voice asr some_16k.wav --realtime
.venv/bin/python -m src.voice roundtrip "今天数学作业是第二十三页"
```

2026-10-28 实测：TTS 首包 0.5 s；ASR 实时送流，音频结束后约 0.14 s 出最终结果。

## 声音复刻

接口在，但这个账号没开通。`mega_tts/audio/upload` 返回 403 `parameter license not found for appid 9962383035`。开通后，复刻出来的 `S_xxx` 音色用 `--speaker S_xxx --resource-id seed-icl-2.0` 调用。
