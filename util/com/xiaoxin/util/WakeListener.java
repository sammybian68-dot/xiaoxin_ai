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
        if (t != null) {
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
                        running = false;  // 交给 Talk，先松开麦克风
                        listener.onWake(text);
                    }
                });
            }
        } finally {
            try {
                rec.stop();
            } catch (IllegalStateException ignored) {
                // 没起来
            }
            rec.release();
        }
    }
}
