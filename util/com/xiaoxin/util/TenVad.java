package com.xiaoxin.util;

/**
 * TEN-VAD 断句，和 carHer2 的 TenVadGate（clients/web_vehicle_simulator/static/js/ten-vad.js）逐行一致：
 *   - 10 ms 一帧（hop 160），TEN-VAD 给每帧一个人声概率，≥ threshold（默认 0.5）算这一帧是人声
 *   - 最近 5 帧投票：没在说时 ≥ startVotes（3）帧是人声 → 开口；在说时 ≥ continueVotes（3）帧是人声 → 还在说
 *   - 在说时，离最后一次「还在说」超过 silenceMs（400–1000，默认 800，50 一档）→ 说完
 *   - 一句超过 20 秒强制结束
 * 只按「像不像人声」判断，不看音量：电视、风扇、空调一直响也不会当成人在说、切不断。
 *
 * 句子音频：carHer2 是开口后才开始上传（开口前没有预录）。这里同样从开口那一帧起收，
 * 另外把投票窗口里那几帧（开口前 ≤ 40 ms，正是触发开口的人声）也算进去，免得吃掉字头。
 */
public final class TenVad {
    static {
        System.loadLibrary("ten_vad");
        System.loadLibrary("xiaoxin-tenvad");
    }

    private static native long nativeCreate(int hop, float threshold);

    private static native float nativeProcess(long handle, short[] frame, int hop);

    private static native void nativeDestroy(long handle);

    public static final int HOP = 160;
    public static final int FRAME_MS = 10;
    private static final int VOTE_WINDOW = 5;
    private static final int MAX_SPEECH_MS = 20_000;
    private static final int RATE = 16000;

    public interface Listener {
        /** 一句话说完了。pcm 是整句 s16le 16 kHz。 */
        void onUtterance(byte[] pcm);
    }

    /** 开口 / 说完的回调，给只要事件、不要整句音频的地方用（比如通话里决定什么时候算用户在说） */
    public interface Events {
        void onStart();

        void onEnd();
    }

    private long handle;
    private final float threshold;
    private final int startVotes, continueVotes, silenceMs;
    private final int[] window = new int[VOTE_WINDOW];
    private int cursor;
    private int frameIndex = -1;
    private boolean inSpeech;
    private int lastSpeech = -1, speechStart = -1;
    private final short[] frame = new short[HOP];
    private int filled;
    private final java.io.ByteArrayOutputStream utt = new java.io.ByteArrayOutputStream();
    private final java.util.ArrayDeque<byte[]> recent = new java.util.ArrayDeque<>();  // 投票窗口里的帧
    public volatile float lastProbability;

    public TenVad(int silenceMs) {
        this(0.5f, 3, 3, silenceMs);
    }

    public TenVad(float threshold, int startVotes, int continueVotes, int silenceMs) {
        this.threshold = clampThreshold(threshold);
        this.startVotes = clampVotes(startVotes);
        this.continueVotes = clampVotes(continueVotes);
        this.silenceMs = clampSilence(silenceMs);
        java.util.Arrays.fill(window, -1);
        handle = nativeCreate(HOP, this.threshold);
        if (handle == 0) {
            throw new IllegalStateException("ten_vad_create failed");
        }
    }

    static int clampVotes(int v) {
        return Math.max(1, Math.min(5, v));
    }

    /** 和 carHer2 一样：400–1000 毫秒，按 50 一档取整。 */
    static int clampSilence(int ms) {
        int c = Math.max(400, Math.min(1000, ms));
        return 400 + Math.round((c - 400) / 50f) * 50;
    }

    static float clampThreshold(float v) {
        return Math.max(0.1f, Math.min(0.9f, v));
    }

    public int silenceMs() {
        return silenceMs;
    }

    public boolean inSpeech() {
        return inSpeech;
    }

    /** 喂 s16le PCM（多长都行）。说完一句回调 listener（可空）；events 收开口 / 说完（可空）。 */
    public void feed(byte[] pcm, int length, Listener listener) {
        feed(pcm, length, listener, null);
    }

    public synchronized void feed(byte[] pcm, int length, Listener listener, Events events) {
        if (handle == 0) {
            return;
        }
        for (int i = 0; i + 1 < length; i += 2) {
            frame[filled++] = (short) ((pcm[i] & 0xff) | (pcm[i + 1] << 8));
            if (filled == HOP) {
                filled = 0;
                byte[] bytes = new byte[HOP * 2];
                for (int k = 0; k < HOP; k++) {
                    bytes[2 * k] = (byte) frame[k];
                    bytes[2 * k + 1] = (byte) (frame[k] >> 8);
                }
                float p = nativeProcess(handle, frame, HOP);
                lastProbability = p;
                String ev = accept(p);
                if (inSpeech || "end".equals(ev)) {
                    if ("start".equals(ev)) {
                        utt.reset();
                        for (byte[] b : recent) {
                            utt.write(b, 0, b.length);
                        }
                    }
                    utt.write(bytes, 0, bytes.length);
                }
                recent.addLast(bytes);
                if (recent.size() > VOTE_WINDOW - 1) {
                    recent.removeFirst();
                }
                if ("start".equals(ev) && events != null) {
                    events.onStart();
                } else if ("end".equals(ev)) {
                    byte[] out = utt.toByteArray();
                    utt.reset();
                    if (events != null) {
                        events.onEnd();
                    }
                    if (listener != null) {
                        listener.onUtterance(out);
                    }
                }
            }
        }
    }

    /** carHer2 TenVadGate._accept 原样。 */
    private String accept(float probability) {
        frameIndex += 1;
        boolean speech = probability >= threshold;
        window[cursor] = speech ? frameIndex : -1;
        cursor = (cursor + 1) % VOTE_WINDOW;
        int need = inSpeech ? continueVotes : startVotes;
        int votes = 0;
        for (int x : window) {
            if (x >= 0) {
                votes++;
            }
        }
        boolean confirmed = votes >= need;
        if (!inSpeech) {
            if (!confirmed) {
                return null;
            }
            inSpeech = true;
            speechStart = frameIndex;
            lastSpeech = frameIndex;
            return "start";
        }
        if (confirmed) {
            lastSpeech = frameIndex;
        } else if ((frameIndex - lastSpeech) * FRAME_MS >= silenceMs) {
            inSpeech = false;
            return "end";
        }
        if ((frameIndex - speechStart) * FRAME_MS >= MAX_SPEECH_MS) {
            inSpeech = false;
            return "end";
        }
        return null;
    }

    /** 丢掉正在攒的句子（模型状态保留）。 */
    public synchronized void reset() {
        inSpeech = false;
        utt.reset();
        recent.clear();
        java.util.Arrays.fill(window, -1);
    }

    public synchronized void release() {
        if (handle != 0) {
            nativeDestroy(handle);
            handle = 0;
        }
    }
}
