package com.xiaoxin.util;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;

/**
 * 小新挂断的提示音，不说话，一听就知道「挂了，要再叫才行」。现场合成，不带音频文件。
 * 网页里是同一组音（server/static/app.js 的 TONES），id 和参数要两边一致；选哪个存在服务端设置 hangup_tone。
 *   down   下行两声 E5 → A4（默认）
 *   drop   水滴：一声从 1200 Hz 滑到 500 Hz
 *   soft   轻敲：一声 A4，短
 *   chime  三音下行 G5 → E5 → C5
 *   none   不响
 */
public final class Earcon {
    private static final int RATE = 24000;
    private static final double VOLUME = 0.35;

    private Earcon() {}

    /** 一个音：起始频率、结束频率（滑音用，不滑就一样）、时长秒、之后停顿秒。 */
    private static double[][] notes(String id) {
        switch (id) {
            case "drop":
                return new double[][] {{1200, 500, 0.18, 0}};
            case "soft":
                return new double[][] {{440, 440, 0.12, 0}};
            case "chime":
                return new double[][] {{783.99, 783.99, 0.11, 0.02}, {659.25, 659.25, 0.11, 0.02}, {523.25, 523.25, 0.22, 0}};
            case "none":
                return new double[0][];
            case "down":
            default:
                return new double[][] {{659.25, 659.25, 0.14, 0.03}, {440, 440, 0.20, 0}};
        }
    }

    static byte[] pcm(String id) {
        double[][] ns = notes(id);
        int total = 0;
        for (double[] n : ns) {
            total += (int) (n[2] * RATE) + (int) (n[3] * RATE);
        }
        byte[] out = new byte[total * 2];
        int pos = 0;
        for (double[] n : ns) {
            int len = (int) (n[2] * RATE);
            int fade = (int) (0.012 * RATE);
            double phase = 0;
            for (int i = 0; i < len; i++) {
                double f = n[0] + (n[1] - n[0]) * i / len;  // 线性滑音
                phase += 2 * Math.PI * f / RATE;
                double env = Math.min(1.0, Math.min(i / (double) fade, (len - i) / (double) (fade * 4)));
                double v = VOLUME * env * (0.8 * Math.sin(phase) + 0.2 * Math.sin(2 * phase));
                short s = (short) Math.round(v * 32767);
                out[2 * pos] = (byte) s;
                out[2 * pos + 1] = (byte) (s >> 8);
                pos++;
            }
            pos += (int) (n[3] * RATE);
        }
        return out;
    }

    /** 放一次，不阻塞。走「助手提示音」，音量跟媒体音量。 */
    public static void play(String id) {
        byte[] pcm = pcm(id == null ? "down" : id);
        if (pcm.length == 0) {
            return;
        }
        try {
            AudioTrack t = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .setBufferSizeInBytes(pcm.length)
                    .build();
            t.write(pcm, 0, pcm.length);
            t.setNotificationMarkerPosition(pcm.length / 2);
            t.setPlaybackPositionUpdateListener(new AudioTrack.OnPlaybackPositionUpdateListener() {
                @Override
                public void onMarkerReached(AudioTrack track) {
                    track.release();
                }

                @Override
                public void onPeriodicNotification(AudioTrack track) {}
            });
            t.play();
        } catch (RuntimeException ignored) {
            // 提示音放不出来不影响挂断
        }
    }
}
