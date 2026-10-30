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
import java.nio.file.Files;

/**
 * 小新用当前音色说的「我在的」（唤醒）和「拜拜」（主动退出）。服务端 /api/cue/{zai,bye} 合成，这里按音色存到私有目录，
 * 唤醒 / 退出那一刻本机直接放，不等网络。换了音色先 refresh()，旧音色的文件留着，切回来不用再下。
 * 下载不到（没网、服务端挂了）就不出声，不影响唤醒和挂断本身。
 */
public final class Cues {
    public static final String ZAI = "zai";
    public static final String BYE = "bye";
    private static final int RATE = 24000;

    private final File dir;
    private final String base;  // https://…/xiaoxin/
    private final String key;
    private volatile String voice = "";

    public Cues(Context context, String base, String key) {
        this.dir = new File(context.getFilesDir(), "cues");
        this.base = base.endsWith("/") ? base : base + "/";
        this.key = key;
    }

    /** 服务端改了提示的字或换了内置音色的参考（2：「在」→「我在的」；3：男声换成少年梓辛）就加一，旧缓存不再用 */
    private static final int VERSION = 4;  // 4：服务端加了「念完整」检查

    private File file(String name, String v) {
        return new File(dir, (v.isEmpty() ? "default" : v.replaceAll("[^A-Za-z0-9_.-]", "_")) + "." + name
                + ".v" + VERSION + ".pcm");
    }

    /** 后台线程调：音色变了就把这个音色的两句拉下来（已经有的不重下）。 */
    public void refresh(String voiceId) {
        voice = voiceId == null ? "" : voiceId;
        dir.mkdirs();
        for (String name : new String[] {ZAI, BYE}) {
            File f = file(name, voice);
            if (f.isFile() && f.length() > 0) {
                continue;
            }
            try {
                byte[] pcm = fetch(name, voice);
                File tmp = new File(f.getPath() + ".tmp");
                try (FileOutputStream out = new FileOutputStream(tmp)) {
                    out.write(pcm);
                }
                if (!tmp.renameTo(f)) {
                    tmp.delete();
                }
            } catch (IOException ignored) {
                // 下次再试
            }
        }
    }

    private byte[] fetch(String name, String v) throws IOException {
        String url = base + "api/cue/" + name + (v.isEmpty() ? "" : "?voice=" + java.net.URLEncoder.encode(v, "UTF-8"));
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(5000);
        c.setReadTimeout(20000);  // 第一次要现合成
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

    /**
     * 放一句，返回大概多久放完（毫秒），调用方据此决定什么时候再开麦克风；没有文件返回 0。
     * 走通话音量（USAGE_VOICE_COMMUNICATION），和小新平时说话同一个音量：之前用 USAGE_ASSISTANT，
     * 跟的是「助手」音量（pad 上 3/15），比通话（7/15）小很多（2026-10-30 用户反馈）。
     */
    public long play(String name) {
        File f = file(name, voice);
        if (!f.isFile()) {
            f = file(name, "");  // 当前音色还没拉到：用默认音色的顶一下
        }
        if (!f.isFile()) {
            return 0;
        }
        byte[] pcm;
        try {
            pcm = Files.readAllBytes(f.toPath());
        } catch (IOException e) {
            return 0;
        }
        if (pcm.length < 2) {
            return 0;
        }
        try {
            AudioTrack t = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
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
            return pcm.length / 2 * 1000L / RATE;
        } catch (RuntimeException e) {
            return 0;
        }
    }
}
