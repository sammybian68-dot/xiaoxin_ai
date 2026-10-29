package com.xiaoxin.util;

import java.io.ByteArrayOutputStream;
import java.util.ArrayDeque;

/**
 * 端侧断句，和服务端 server/vad.py 同一套规则（能量阈值、底噪自适应），这里只用来给唤醒切句子。
 * 20 ms 一帧；连续 200 ms 有声算开口，开口后连续 endMs 没声算说完。开口前 300 ms 也算进句子，免得吃掉字头。
 */
public final class Vad {
    private static final int RATE = 16000;
    private static final int FRAME_MS = 20;
    private static final int FRAME_BYTES = RATE * 2 * FRAME_MS / 1000;
    private static final int START_MS = 200;
    private static final int PRE_ROLL_MS = 300;
    private static final int MAX_UTTERANCE_S = 8;  // 唤醒词加一句话，8 秒够了；更长的多半是电视
    private static final double MIN_SPEECH_DB = 35;
    private static final double SPEECH_OVER_NOISE = 15;

    private final int endMs;
    private final byte[] carry = new byte[FRAME_BYTES];
    private int carryLen;
    private final ArrayDeque<Double> noise = new ArrayDeque<>();
    private final ArrayDeque<byte[]> pre = new ArrayDeque<>();
    private final ByteArrayOutputStream utt = new ByteArrayOutputStream();
    private boolean speaking;
    private int loudRun;
    private int quietRun;

    public interface Listener {
        /** 一句话说完了。pcm 是整句 s16le 16 kHz。 */
        void onUtterance(byte[] pcm);
    }

    public Vad(int endMs) {
        this.endMs = endMs;
    }

    public void feed(byte[] pcm, int length, Listener listener) {
        int off = 0;
        while (off < length) {
            int n = Math.min(FRAME_BYTES - carryLen, length - off);
            System.arraycopy(pcm, off, carry, carryLen, n);
            carryLen += n;
            off += n;
            if (carryLen == FRAME_BYTES) {
                byte[] frame = carry.clone();
                carryLen = 0;
                byte[] done = frame(frame);
                if (done != null) {
                    listener.onUtterance(done);
                }
            }
        }
    }

    public void reset() {
        speaking = false;
        loudRun = quietRun = 0;
        utt.reset();
        pre.clear();
    }

    private double threshold() {
        if (noise.size() < 10) {
            return MIN_SPEECH_DB;
        }
        double[] v = new double[noise.size()];
        int i = 0;
        for (double d : noise) {
            v[i++] = d;
        }
        java.util.Arrays.sort(v);
        return Math.max(MIN_SPEECH_DB, v[v.length / 5] + SPEECH_OVER_NOISE);
    }

    static double db(byte[] frame) {
        long sum = 0;
        int n = frame.length / 2;
        for (int i = 0; i + 1 < frame.length; i += 2) {
            int s = (short) ((frame[i] & 0xff) | (frame[i + 1] << 8));
            sum += (long) s * s;
        }
        return 20 * Math.log10(Math.max(Math.sqrt(sum / (double) n), 1.0));
    }

    private byte[] frame(byte[] frame) {
        double level = db(frame);
        boolean loud = level >= threshold();
        if (!speaking) {
            pre.addLast(frame);
            if (pre.size() > PRE_ROLL_MS / FRAME_MS) {
                pre.removeFirst();
            }
            if (loud) {
                if (++loudRun * FRAME_MS >= START_MS) {
                    speaking = true;
                    quietRun = 0;
                    utt.reset();
                    for (byte[] f : pre) {
                        utt.write(f, 0, f.length);
                    }
                    pre.clear();
                }
            } else {
                loudRun = 0;
                noise.addLast(level);
                if (noise.size() > 150) {
                    noise.removeFirst();
                }
            }
            return null;
        }
        utt.write(frame, 0, frame.length);
        quietRun = loud ? 0 : quietRun + 1;
        boolean tooLong = utt.size() >= MAX_UTTERANCE_S * RATE * 2;
        if (quietRun * FRAME_MS >= endMs || tooLong) {
            byte[] out = utt.toByteArray();
            reset();
            return out;
        }
        return null;
    }
}
