package com.xiaoxin.util;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;

/**
 * pad 待命：麦克风开着，本机断句、本机 Paraformer 识别，听到以唤醒词开头的话才回调 onWake。
 * 没唤醒时一个字节都不上传、不花钱。支持 one-shot：「小新同学现在几点」整句交给 onWake，服务端去掉唤醒词直接回答。
 *
 * 录音用 VOICE_COMMUNICATION（和自由对话同一个通路，系统 AEC / 降噪都开着），唤醒后由 Talk 接管麦克风，
 * 这里先停掉，Talk 挂断后再 start() 回来待命。
 */
public final class WakeListener {
    public interface Listener {
        /** 认出了唤醒词。text 是端侧认出的整句（含唤醒词），交给服务端去掉唤醒词后作答。 */
        void onWake(String text);

        /** 每一句（没唤醒的也算）认出来的字，调试用。millis 识别耗时，audioMs 这句话多长。 */
        void onHeard(String text, long millis, long audioMs);

        void onError(String message);
    }

    private final Context context;
    private final Listener listener;
    private final LocalAsr asr;
    private volatile String[] wakeWords;
    private volatile boolean running;
    private Thread thread;
    /** 唤醒后麦克风不关：接着录，录到的先攒在这里，Talk 接手时一起拿走（takeMic），中间一个字都不丢。 */
    private volatile AudioRecord handoff;
    private final java.util.ArrayDeque<byte[]> carried = new java.util.ArrayDeque<>();
    private final java.util.ArrayDeque<Long> carriedAt = new java.util.ArrayDeque<>();  // 每帧录完的 nanoTime
    private Thread carrier;

    public WakeListener(Context context, LocalAsr asr, String[] wakeWords, Listener listener) {
        this.context = context.getApplicationContext();
        this.asr = asr;
        this.wakeWords = wakeWords;
        this.listener = listener;
    }

    public void setWakeWords(String[] words) {
        wakeWords = words;
    }

    private volatile int endMs = 800;
    private volatile boolean debug;

    /** 停多久算说完，跟主系统设置（下一句起生效）。 */
    public void setEndMs(int ms) {
        if (ms > 0) {
            endMs = ms;
        }
    }

    /**
     * 调试模式：每句都只回调 onHeard，认出唤醒词也不触发唤醒、不松开麦克风。
     * 设置页「端侧模型调试」用，自己说一些词看本机模型认成什么、算不算唤醒 / 退出。
     */
    public void setDebug(boolean on) {
        debug = on;
    }


    public synchronized void start() {
        if (running) {
            return;
        }
        running = true;
        thread = new Thread(this::loop, "wake-listen");
        thread.setDaemon(true);
        thread.start();
    }

    public synchronized void stop() {
        running = false;
        Thread t = thread;
        thread = null;
        if (t != null && handoff == null) {  // 唤醒后线程在 carry 里给 Talk 攒音频，不等它，takeMic 会收尾
            try {
                t.join(1500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
    }

    public boolean isRunning() {
        return running;
    }

    /** 唤醒后接手麦克风：返回还开着的 AudioRecord，以及唤醒词之后到现在录到的音频（按顺序）。没有可接手的返回 null。 */
    public synchronized Mic takeMic() {
        AudioRecord r = handoff;
        if (r == null) {
            return null;
        }
        handoff = null;
        Thread c = carrier;
        carrier = null;
        if (c != null) {
            try {
                c.join(500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        synchronized (carried) {
            Mic m = new Mic(r, new java.util.ArrayList<>(carried), new java.util.ArrayList<>(carriedAt));
            carried.clear();
            carriedAt.clear();
            return m;
        }
    }

    /** 没人接手（比如唤醒后又不开通话了）：把麦克风关掉。 */
    public synchronized void dropMic() {
        Mic m = takeMic();
        if (m != null) {
            m.rec.stop();
            m.rec.release();
        }
    }

    public static final class Mic {
        public final AudioRecord rec;
        public final java.util.List<byte[]> frames;
        public final java.util.List<Long> at;

        Mic(AudioRecord rec, java.util.List<byte[]> frames, java.util.List<Long> at) {
            this.rec = rec;
            this.frames = frames;
            this.at = at;
        }
    }

    /** 唤醒那一刻起：同一个 AudioRecord 接着读，帧攒进 carried，等 Talk 来拿。 */
    private void carry(AudioRecord rec) {
        handoff = rec;
        carrier = Thread.currentThread();
        byte[] frame = new byte[Pcm.FRAME_BYTES];
        while (handoff == rec) {
            int n = rec.read(frame, 0, frame.length);
            if (n > 0 && handoff == rec) {
                synchronized (carried) {
                    if (carried.size() >= 100) {  // 最多 10 秒
                        carried.poll();
                        carriedAt.poll();
                    }
                    carried.add(java.util.Arrays.copyOf(frame, n));
                    carriedAt.add(System.nanoTime());
                }
            }
        }
    }

    private void loop() {
        int min = AudioRecord.getMinBufferSize(Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, Pcm.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, Pcm.FRAME_BYTES * 8));
            rec.startRecording();
        } catch (RuntimeException e) {
            running = false;
            listener.onError("麦克风打不开：" + e.getMessage());
            return;
        }
        int vadMs = endMs;
        Vad vad = new Vad(vadMs);  // 跟主系统设置的「停多久算说完」，设置改了马上换
        byte[] frame = new byte[Pcm.FRAME_BYTES];
        boolean[] woke = {false};
        try {
            while (running) {
                int n = rec.read(frame, 0, frame.length);
                if (n <= 0) {
                    continue;
                }
                if (endMs != vadMs) {
                    vadMs = endMs;
                    vad = new Vad(vadMs);
                }
                vad.feed(frame, n, pcm -> {
                    long t0 = System.nanoTime();
                    String text = asr.decode(pcm, pcm.length);
                    long ms = (System.nanoTime() - t0) / 1_000_000;
                    listener.onHeard(text, ms, pcm.length / 2 * 1000L / Pcm.SAMPLE_RATE);
                    if (!debug && !text.isEmpty() && Keywords.startsWithAny(text, wakeWords)) {
                        running = false;
                        woke[0] = true;
                        listener.onWake(text);
                    }
                });
            }
            if (woke[0]) {
                carry(rec);  // 麦克风不关，接着录，等 Talk 拿走
                return;      // rec 归 Talk 了，这里不 release
            }
        } finally {
            if (woke[0]) {
                return;
            }
            try {
                rec.stop();
            } catch (IllegalStateException ignored) {
                // 没起来
            }
            rec.release();
        }
    }
}
