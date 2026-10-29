package com.xiaoxin.util;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.net.URI;
import java.util.HashMap;
import java.util.Map;

/**
 * 自由对话的端侧：麦克风一直往服务端送，服务端的音频收到就放，服务端说 interrupt 就清空。
 * 断句、识别、回答、打断判断都在服务端（/api/talk，协议见 server/talk.py），这里只管收发 PCM。
 *
 * 回声靠系统 AEC：录音用 VOICE_COMMUNICATION，播放用 USAGE_VOICE_COMMUNICATION，并切到 MODE_IN_COMMUNICATION、
 * 外放。2026-10-29 在 TB335FC 上实测，这样小新的声音在麦克风里只剩 2 dB 左右，人声照样 55–65 dB。
 */
public final class Talk {
    public interface Listener {
        /** 服务端发来的 JSON 事件（ready / listening / heard / delta / reply / interrupt / idle / error …）。 */
        void onEvent(JSONObject event);

        /** 麦克风电平，每 100 ms 一次，0–100，给界面画波形。 */
        void onLevel(int level);

        /** 连接断了或打不开。 */
        void onClosed(String reason);
    }

    public static final int PLAY_RATE = 24000;
    private static final int CALL_VOLUME_FLOOR = 8;  // 通话音量默认偏小（实测 5/15），开始时至少调到这么大

    private final Context context;
    private final Listener listener;
    private AudioManager am;
    private AudioRecord rec;
    private AudioTrack track;
    private WebSocket ws;
    private volatile boolean running;
    private int oldMode = AudioManager.MODE_NORMAL;
    private int oldVolume = -1;
    private volatile boolean firstAudio;

    /**
     * 这一轮第一块音频写进播放器后，盯着播放头，真正开始出声时报一次 native_play {delay_ms}：
     * 从收到第一块到喇叭出声的时间（AudioTrack 缓冲 + 通话通路的延迟）。
     */
    private void notePlayStart(AudioTrack t) {
        long recvAt = System.nanoTime();
        int head0 = t.getPlaybackHeadPosition();
        Thread w = new Thread(() -> {
            for (int i = 0; i < 300 && running; i++) {
                try {
                    if (t.getPlaybackHeadPosition() != head0) {
                        long ms = (System.nanoTime() - recvAt) / 1_000_000;
                        listener.onEvent(new JSONObject().put("type", "native_play").put("delay_ms", ms));
                        return;
                    }
                    Thread.sleep(5);
                } catch (Exception e) {
                    return;
                }
            }
        }, "talk-play-watch");
        w.setDaemon(true);
        w.start();
    }

    public Talk(Context context, Listener listener) {
        this.context = context.getApplicationContext();
        this.listener = listener;
    }

    /** 在后台线程调：连上服务端、开音频。url 形如 wss://host:8183/xiaoxin/api/talk?model=… */
    public void start(String url, String key) throws IOException {
        Map<String, String> headers = new HashMap<>();
        headers.put("Authorization", "Bearer " + key);
        ws = WebSocket.connect(URI.create(url), headers, 8000, new WebSocket.Listener() {
            @Override
            public void onText(String text) {
                try {
                    JSONObject ev = new JSONObject(text);
                    String type = ev.optString("type");
                    if ("interrupt".equals(type)) {
                        flushPlayback();
                    }
                    if ("speaking".equals(type)) {
                        firstAudio = true;
                    }
                    listener.onEvent(ev);
                } catch (JSONException e) {
                    // 不是 JSON 就忽略
                }
            }

            @Override
            public void onBinary(byte[] data) {
                AudioTrack t = track;
                if (t != null && running) {
                    if (firstAudio) {
                        firstAudio = false;
                        notePlayStart(t);
                    }
                    t.write(data, 0, data.length);
                }
            }

            @Override
            public void onClosed(int code, String reason) {
                boolean was = running;
                stopAudio();
                if (was) {
                    listener.onClosed(code == 4401 ? "登录失效，key 不对" : (reason.isEmpty() ? "连接断了" : reason));
                }
            }
        });
        startAudio();
    }

    private void startAudio() {
        am = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        oldMode = am.getMode();
        am.setMode(AudioManager.MODE_IN_COMMUNICATION);
        for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
            if (d.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) {
                am.setCommunicationDevice(d);
                break;
            }
        }
        int vol = am.getStreamVolume(AudioManager.STREAM_VOICE_CALL);
        int max = am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL);
        if (vol < CALL_VOLUME_FLOOR) {
            oldVolume = vol;
            am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, Math.min(CALL_VOLUME_FLOOR, max), 0);
        }

        track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(PLAY_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STREAM)
                // 服务端最多领先 2 秒，缓冲要放得下，不然 write 会卡住收消息的线程
                .setBufferSizeInBytes(PLAY_RATE * 2 * 3)
                .build();
        track.play();

        int min = AudioRecord.getMinBufferSize(Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, Pcm.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, Pcm.FRAME_BYTES * 8));
        running = true;
        rec.startRecording();
        Thread up = new Thread(this::uplink, "talk-mic");
        up.setDaemon(true);
        up.start();
    }

    private void uplink() {
        byte[] frame = new byte[Pcm.FRAME_BYTES];  // 100 ms
        while (running) {
            int n = rec.read(frame, 0, frame.length);
            if (n <= 0) {
                continue;
            }
            Pcm.Level level = Pcm.level(frame, n);
            listener.onLevel(Math.min(100, level.rms * 100 / 3000));
            try {
                ws.sendBinary(frame, n);
            } catch (IOException e) {
                break;  // 断线由 onClosed 处理
            }
        }
    }

    /** 发一条控制消息给服务端（JSON 文本帧）。断了就算了。 */
    public void sendText(String json) {
        WebSocket w = ws;
        if (w != null) {
            try {
                w.sendText(json);
            } catch (IOException ignored) {
                // 断线由 onClosed 处理
            }
        }
    }

    /** 用户插话：已经排进播放器的音频立刻丢掉。 */
    private synchronized void flushPlayback() {
        AudioTrack t = track;
        if (t != null) {
            t.pause();
            t.flush();
            t.play();
        }
    }

    public void stop() {
        WebSocket w = ws;
        if (w != null) {
            try {
                w.sendText("{\"type\":\"stop\"}");
            } catch (IOException ignored) {
                // 已经断了
            }
        }
        stopAudio();
        if (w != null) {
            w.close();
        }
    }

    private synchronized void stopAudio() {
        if (!running && rec == null) {
            return;
        }
        running = false;
        if (rec != null) {
            try {
                rec.stop();
            } catch (IllegalStateException ignored) {
                // 没起来
            }
            rec.release();
            rec = null;
        }
        if (track != null) {
            track.pause();
            track.flush();
            track.release();
            track = null;
        }
        if (am != null) {
            if (oldVolume >= 0) {
                am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, oldVolume, 0);
                oldVolume = -1;
            }
            am.clearCommunicationDevice();
            am.setMode(oldMode);
        }
    }
}
