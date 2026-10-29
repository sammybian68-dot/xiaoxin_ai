package com.xiaoxin.util;

/** Carher uplink: 16 kHz, mono, signed 16-bit little-endian, 100 ms frames. */
public final class Pcm {
    public static final int SAMPLE_RATE = 16000;
    public static final int FRAME_SAMPLES = 1600;
    public static final int FRAME_BYTES = FRAME_SAMPLES * 2;

    private Pcm() {}

    public static final class Level {
        public final int rms;
        public final int peak;

        public Level(int rms, int peak) {
            this.rms = rms;
            this.peak = peak;
        }
    }

    public static Level level(byte[] pcm, int length) {
        int samples = length / 2;
        if (samples <= 0) {
            return new Level(0, 0);
        }
        long sum = 0;
        int peak = 0;
        for (int i = 0; i + 1 < length; i += 2) {
            int sample = (short) ((pcm[i] & 0xff) | (pcm[i + 1] << 8));
            int abs = sample < 0 ? -sample : sample;
            if (abs > peak) {
                peak = abs;
            }
            sum += (long) sample * sample;
        }
        return new Level((int) Math.sqrt(sum / (double) samples), peak);
    }

    /** Fixed-size circular window. snapshot() is oldest-to-newest. */
    public static final class Window {
        private final byte[] data;
        private int pos;
        private int filled;

        public Window(int seconds) {
            data = new byte[SAMPLE_RATE * 2 * seconds];
        }

        public synchronized void add(byte[] src, int length) {
            int offset = 0;
            while (offset < length) {
                int n = Math.min(length - offset, data.length - pos);
                System.arraycopy(src, offset, data, pos, n);
                pos = (pos + n) % data.length;
                filled = Math.min(data.length, filled + n);
                offset += n;
            }
        }

        public synchronized byte[] snapshot() {
            byte[] out = new byte[filled];
            if (filled < data.length) {
                System.arraycopy(data, 0, out, 0, filled);
                return out;
            }
            int tail = data.length - pos;
            System.arraycopy(data, pos, out, 0, tail);
            System.arraycopy(data, 0, out, tail, pos);
            return out;
        }

        public synchronized int filled() {
            return filled;
        }
    }
}
