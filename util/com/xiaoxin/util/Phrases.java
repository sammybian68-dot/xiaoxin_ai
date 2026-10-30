package com.xiaoxin.util;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.file.Files;

/**
 * 描红播报：一句话拆成几段固定短语（「点」「第」「3」「笔」「错了」…），每段用当前音色合成一次、存在本机，
 * 播的时候按顺序拼起来放，不现场 TTS，几十毫秒就出声。还能插一声错误提示音（"#wrong"，本机合成）。
 * 新的一句一来就把正在放的掐掉（写得快时只听最新的那句）。走通话音量，和小新说话一样大。
 * 缓存老化见 CacheDir：30 天没用到的删掉（短语是共用的：笔画名 26 种 + 数字 + 固定话，一个音色总共也就几 MB）。
 */
public final class Phrases {
    private static final int RATE = 24000;
    private static final int GAP_MS = 260;  // 段与段之间：每段都是完整的一句，句与句之间自然停一下
    private final File dir;
    private final String base, key;
    private volatile String voice = "";
    private AudioTrack track;
    private int gen;

    public Phrases(Context context, String base, String key) {
        this.dir = new File(context.getFilesDir(), "phrases");
        this.base = base.endsWith("/") ? base : base + "/";
        this.key = key;
        new Thread(() -> CacheDir.prune(dir), "phrases-prune").start();
    }

    public void setVoice(String v) {
        voice = v == null ? "" : v;
    }

    private File file(String text) {
        String v = voice.isEmpty() ? "default" : voice.replaceAll("[^A-Za-z0-9_.-]", "_");
        return new File(dir, v + "." + Integer.toHexString(text.hashCode()) + "_" + text.length() + ".pcm");
    }

    /** 后台线程调：把这些短语都下好（已经有的跳过）。 */
    public void prefetch(String[] texts) {
        dir.mkdirs();
        for (String t : texts) {
            if (t == null || t.isEmpty() || t.startsWith("#")) {
                continue;
            }
            if (file(t).isFile()) {
                CacheDir.touch(file(t));  // 这个字要用到：也算一次命中，免得常用短语刚好被清
                continue;
            }
            try {
                byte[] pcm = fetch(t);
                File f = file(t), tmp = new File(f.getPath() + ".tmp");
                try (FileOutputStream out = new FileOutputStream(tmp)) {
                    out.write(pcm);
                }
                if (!tmp.renameTo(f)) {
                    tmp.delete();
                }
            } catch (IOException ignored) {
                // 下不到就播的时候跳过这段
            }
        }
    }

    private byte[] fetch(String text) throws IOException {
        String url = base + "api/cue/say?text=" + URLEncoder.encode(text, "UTF-8")
                + (voice.isEmpty() ? "" : "&voice=" + URLEncoder.encode(voice, "UTF-8"));
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(20000);
        c.setRequestProperty("Authorization", "Bearer " + key);
        try {
            if (c.getResponseCode() != 200) {
                throw new IOException("HTTP " + c.getResponseCode());
            }
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] b = new byte[8192];
                for (int n; (n = in.read(b)) > 0; ) {
                    buf.write(b, 0, n);
                }
                return buf.toByteArray();
            }
        } finally {
            c.disconnect();
        }
    }

    /** 错误提示音：两声很短的低音「嘟嘟」，不吓人但一听就知道不对。 */
    private static byte[] wrongTone() {
        int n1 = RATE * 9 / 100, gap = RATE * 5 / 100;
        byte[] out = new byte[(n1 * 2 + gap) * 2];
        int pos = 0;
        for (int k = 0; k < 2; k++) {
            double f = k == 0 ? 330 : 262;
            for (int i = 0; i < n1; i++) {
                double env = Math.min(1, Math.min(i / (RATE * 0.006), (n1 - i) / (RATE * 0.03)));
                double v = 0.32 * env * (0.75 * Math.sin(2 * Math.PI * f * i / RATE) + 0.25 * Math.sin(4 * Math.PI * f * i / RATE));
                short s = (short) Math.round(v * 32767);
                out[2 * pos] = (byte) s;
                out[2 * pos + 1] = (byte) (s >> 8);
                pos++;
            }
            pos += k == 0 ? gap : 0;
        }
        return out;
    }

    /** 按顺序放这几段，掐掉之前正在放的。缺缓存的段跳过（第一次用、没网时）。 */
    public synchronized void play(String[] parts) {
        stop();
        ByteArrayOutputStream all = new ByteArrayOutputStream();
        byte[] gap = new byte[RATE * 2 * GAP_MS / 1000];
        for (String p : parts) {
            byte[] pcm = null;
            if ("#wrong".equals(p)) {
                pcm = wrongTone();
            } else if (p != null && !p.isEmpty()) {
                try {
                    File f = file(p);
                    pcm = f.isFile() ? Files.readAllBytes(f.toPath()) : null;
                    CacheDir.touch(f);
                } catch (IOException ignored) {
                    pcm = null;
                }
            }
            if (pcm != null && pcm.length > 1) {
                if (all.size() > 0) {
                    all.write(gap, 0, gap.length);
                }
                all.write(pcm, 0, pcm.length);
            }
        }
        byte[] pcm = all.toByteArray();
        if (pcm.length < 2) {
            return;
        }
        AudioTrack t = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STATIC).setBufferSizeInBytes(pcm.length).build();
        t.write(pcm, 0, pcm.length);
        t.play();
        track = t;
        int my = ++gen;
        long ms = pcm.length / 2 * 1000L / RATE + 200;
        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
            synchronized (Phrases.this) {
                if (my == gen && track == t) {
                    t.release();
                    track = null;
                }
            }
        }, ms);
    }

    public synchronized void stop() {
        AudioTrack t = track;
        track = null;
        gen++;
        if (t != null) {
            try {
                t.stop();
            } catch (IllegalStateException ignored) {
                // 已经停了
            }
            t.release();
        }
    }
}
