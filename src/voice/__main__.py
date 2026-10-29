"""python -m src.voice tts "你好，我是小新" -o tmp/voice/hi.wav
python -m src.voice tts "你好" --speaker S_xxxxxx -o out.wav      # 声音复刻的音色
python -m src.voice asr tmp/voice/hi_16k.wav --realtime            # 16 kHz 单声道 wav 或裸 pcm
python -m src.voice roundtrip "今天数学作业是第二十三页"           # TTS 出声再 ASR 认回来，测两边延迟
"""

from __future__ import annotations

import argparse
import json
import sys

from src.voice import doubao


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.voice", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tts")
    t.add_argument("text")
    t.add_argument("-o", "--out", required=True)
    t.add_argument("--speaker")
    t.add_argument("--resource-id")
    t.add_argument("--rate", type=int, default=24000)
    a = sub.add_parser("asr")
    a.add_argument("audio")
    a.add_argument("--realtime", action="store_true")
    r = sub.add_parser("roundtrip")
    r.add_argument("text")
    r.add_argument("--speaker")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "tts":
            s = doubao.tts(args.text, speaker=args.speaker, resource_id=args.resource_id, sample_rate=args.rate)
            doubao.write_wav(args.out, s.pcm, s.sample_rate)
            print(json.dumps({"out": args.out, "first_audio_s": round(s.first_audio, 3), "total_s": round(s.total, 3),
                              "audio_s": round(len(s.pcm) / 2 / s.sample_rate, 2)}, ensure_ascii=False))
        elif args.cmd == "asr":
            rec = doubao.asr(doubao.read_pcm16k(args.audio), realtime=args.realtime)
            print(json.dumps(rec.__dict__, ensure_ascii=False))
        else:
            s = doubao.tts(args.text, speaker=args.speaker, sample_rate=16000)
            rec = doubao.asr(s.pcm, realtime=True)
            print(json.dumps({"tts_first_audio_s": round(s.first_audio, 3), "tts_total_s": round(s.total, 3),
                              "asr_text": rec.text, "asr_first_text_s": rec.first_text and round(rec.first_text, 3),
                              "asr_final_after_audio_end_s": round(rec.final - rec.audio_seconds, 3)},
                             ensure_ascii=False))
    except doubao.DoubaoError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
