"""自由对话的断句：按能量判断用户在不在说话。

端侧（pad）开了系统 AEC，小新自己的声音在麦克风里只剩 2 dB 左右，人声 55–65 dB（2026-10-29 双讲实测），
所以简单的能量阈值就分得开，不用上模型。阈值跟着底噪走：底噪取最近安静帧的较低分位，人声要比它高 SPEECH_OVER_NOISE。

事件：
  start  连续 START_MS 有声：用户开口了（小新正在说就打断）
  end    开口后连续 END_MS 没声：一句话说完，可以送识别
说太久（MAX_UTTERANCE_S）也强制 end，免得一直不停。

底噪怎么学：
  - 刚连上的 WARMUP_MS 只学底噪、不判开口（空调本来就开着时，不会把它当成人一直在说）
  - 之后只拿没在说话的帧更新
  - 被强制 end 的那句多半是中途冒出来的持续噪声，把它的电平也学进底噪，下一句就不会再被它卡住
pad 的 VOICE_COMMUNICATION 通路自带降噪，实测底噪 0–2 dB，这些主要是给电脑网页和别的设备兜底。
"""

from __future__ import annotations

import math
from collections import deque

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000  # 640
START_MS = 200          # 连续这么久有声才算开口，咳嗽、敲桌子大多短于这个
END_MS = 700            # 说完后停这么久算一句结束
PRE_ROLL_MS = 300       # 开口判定前的这段也算进句子，免得吃掉字头
MAX_UTTERANCE_S = 30
WARMUP_MS = 400
MIN_SPEECH_DB = 35      # 不管底噪多低，人声至少这么响（int16 RMS 的 dB）
SPEECH_OVER_NOISE = 15  # 比底噪高这么多 dB 算有声


def frame_db(frame: bytes) -> float:
    n = len(frame) // 2
    if n == 0:
        return 0.0
    total = 0
    for i in range(0, n * 2, 2):
        s = int.from_bytes(frame[i:i + 2], "little", signed=True)
        total += s * s
    return 20 * math.log10(max(math.sqrt(total / n), 1.0))


class Vad:
    """feed(pcm) 返回这块里发生的事件列表：[("start", None)] / [("end", 一整句 PCM)]。"""

    def __init__(self):
        self._buf = b""
        self._noise: deque[float] = deque(maxlen=150)  # 最近 3 秒安静帧
        self._pre: deque[bytes] = deque(maxlen=PRE_ROLL_MS // FRAME_MS)
        self._loud_run = 0
        self._quiet_run = 0
        self.speaking = False
        self._utt = bytearray()
        self._frames = 0

    @property
    def threshold(self) -> float:
        if len(self._noise) < 10:
            return MIN_SPEECH_DB
        floor = sorted(self._noise)[len(self._noise) // 5]
        return max(MIN_SPEECH_DB, floor + SPEECH_OVER_NOISE)

    def reset(self) -> None:
        """丢掉正在攒的句子（比如小新说话时用户的声音被判成噪声）。底噪保留。"""
        self._loud_run = self._quiet_run = 0
        self.speaking = False
        self._utt = bytearray()
        self._pre.clear()

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        self._buf += pcm
        events = []
        while len(self._buf) >= FRAME_BYTES:
            frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
            ev = self._frame(frame)
            if ev:
                events.append(ev)
        return events

    def _frame(self, frame: bytes):
        level = frame_db(frame)
        self._frames += 1
        if self._frames * FRAME_MS <= WARMUP_MS:
            self._noise.append(level)
            self._pre.append(frame)
            return None
        loud = level >= self.threshold
        if not self.speaking:
            self._pre.append(frame)
            if loud:
                self._loud_run += 1
                if self._loud_run * FRAME_MS >= START_MS:
                    self.speaking = True
                    self._quiet_run = 0
                    self._utt = bytearray(b"".join(self._pre))
                    self._pre.clear()
                    return ("start", None)
            else:
                self._loud_run = 0
                self._noise.append(level)
            return None
        self._utt += frame
        self._quiet_run = 0 if loud else self._quiet_run + 1
        too_long = len(self._utt) >= MAX_UTTERANCE_S * RATE * 2
        if self._quiet_run * FRAME_MS >= END_MS or too_long:
            utt = bytes(self._utt[:len(self._utt) - (0 if too_long else (self._quiet_run - 5) * FRAME_BYTES)])
            if too_long:  # 多半是持续噪声：把最近 3 秒的电平学进底噪
                tail = utt[-FRAME_BYTES * self._noise.maxlen:]
                for i in range(0, len(tail) - FRAME_BYTES + 1, FRAME_BYTES):
                    self._noise.append(frame_db(tail[i:i + FRAME_BYTES]))
            self.reset()
            return ("end", utt)
        return None
