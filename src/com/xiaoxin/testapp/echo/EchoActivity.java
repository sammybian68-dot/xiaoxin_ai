package com.xiaoxin.testapp.echo;

import android.app.Activity;
import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.xiaoxin.testapp.TestBar;
import com.xiaoxin.util.Pcm;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Locale;

/**
 * 回声测试：边放一段 TTS 边录音，看麦克风收进来多少小新自己的声音。自由对话要靠这个决定回声怎么处理。
 *
 * 每种组合录一段：前 1 秒静音（量底噪）→ 放 clip.wav → 后 1 秒静音。录音和元数据存到
 * getExternalFilesDir("echo")，用 adb pull 回电脑分析。clip.wav 要先 adb push 进同一个目录（16 kHz 单声道 s16）。
 *
 *   A  UNPROCESSED           + 媒体播放   现在测试 APK 的麦克风用法，没有任何处理
 *   B  VOICE_COMMUNICATION   + 媒体播放   只换音源
 *   C  VOICE_COMMUNICATION   + 通话播放   + MODE_IN_COMMUNICATION，系统 AEC 拿得到播放参考
 *   D  C 再显式打开 AcousticEchoCanceler / NoiseSuppressor
 *
 * adb 启动加 --ez auto true 就自动跑完一轮，跑完 finish。
 */
public class EchoActivity extends Activity {
    private static final String[] MODES = {"A", "B", "C", "D"};
    private static final int REPEAT = 2;
    private static final int PAD_MS = 1000;

    private TextView log;
    private volatile boolean running;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF101114);
        root.addView(TestBar.create(this));
        Button start = new Button(this);
        start.setText("开始回声测试（会放声音）");
        start.setOnClickListener(v -> run(false));
        root.addView(start);
        log = new TextView(this);
        log.setTextColor(0xFFE7E8EA);
        log.setTextSize(15);
        log.setPadding(24, 16, 24, 16);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(log);
        root.addView(scroll);
        setContentView(root);
        say("AEC " + AcousticEchoCanceler.isAvailable() + "  NS " + NoiseSuppressor.isAvailable()
                + "  AGC " + AutomaticGainControl.isAvailable());
        if (getIntent().getBooleanExtra("dtalk", false)) {
            doubleTalk();
        } else if (getIntent().getBooleanExtra("auto", false)) {
            run(true);
        }
    }

    // ---------------------------------------------------------------- 双讲：小新说话时人插话，看人声留下多少

    /** 每轮先单讲（只有人说，量人声本来多响），再双讲（小新放音的同时人说）。两句话不一样，识别结果才分得清。 */
    private static final String[][] DTALK = {
            {"帮我把空调开到二十四度", "等一下，我想问另一个问题"},
            {"明天北京会下雨吗", "停一下，先别说了"},
    };
    private static final int CALL_VOLUME = 10;  // 测试期间把通话音量调到接近平时媒体的响度，测完恢复
    private TextView prompt;

    private void show(String text, int color) {
        runOnUiThread(() -> {
            if (prompt == null) {
                prompt = new TextView(this);
                prompt.setTextSize(44);
                prompt.setPadding(48, 48, 48, 48);
                ((LinearLayout) log.getParent().getParent()).addView(prompt, 1);
            }
            prompt.setTextColor(color);
            prompt.setText(text);
        });
    }

    private void doubleTalk() {
        running = true;
        new Thread(() -> {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            int oldVol = am.getStreamVolume(AudioManager.STREAM_VOICE_CALL);
            try {
                File dir = getExternalFilesDir("echo");
                byte[] clip = readWavPcm(new File(dir, "clip.wav"));
                am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                String device = useSpeaker(am);
                am.setStreamVolume(AudioManager.STREAM_VOICE_CALL,
                        Math.min(CALL_VOLUME, am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL)), 0);
                try (FileOutputStream meta = new FileOutputStream(new File(dir, "dtalk.jsonl"))) {
                    for (int r = 0; r < DTALK.length; r++) {
                        String line = dtalkTrial(r, clip, DTALK[r][0], DTALK[r][1], am, device);
                        meta.write((line + "\n").getBytes("UTF-8"));
                        say(line);
                    }
                }
                show("测完了，谢谢", 0xFF3DDC84);
                say("DONE");
            } catch (Exception e) {
                say("ERROR " + e);
                show("出错了：" + e.getMessage(), 0xFFFF6B6B);
            } finally {
                am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, oldVol, 0);
                if (Build.VERSION.SDK_INT >= 31) {
                    am.clearCommunicationDevice();
                }
                am.setMode(AudioManager.MODE_NORMAL);
                running = false;
            }
        }, "echo-dtalk").start();
    }

    private String dtalkTrial(int rep, byte[] clip, String single, String dbl, AudioManager am, String device)
            throws Exception {
        int minRec = AudioRecord.getMinBufferSize(Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, Pcm.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(minRec, Pcm.FRAME_BYTES * 8));
        ByteArrayOutputStream mic = new ByteArrayOutputStream();
        rec.startRecording();
        Thread reader = new Thread(() -> {
            byte[] frame = new byte[Pcm.FRAME_BYTES];
            while (rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                int n = rec.read(frame, 0, frame.length);
                if (n > 0) {
                    synchronized (mic) {
                        mic.write(frame, 0, n);
                    }
                }
            }
        }, "dtalk-rec");
        reader.start();

        String tag = "第 " + (rep + 1) + "/" + DTALK.length + " 轮\n";
        show(tag + "准备，马上请你读：\n「" + single + "」", 0xFF8E929A);
        Thread.sleep(2500);
        long singleStart = size(mic);
        show(tag + "现在读（小新不出声）：\n「" + single + "」", 0xFF3DDC84);
        Thread.sleep(4500);
        long singleEnd = size(mic);
        show(tag + "准备，小新要开始说话了。\n她一出声，你就读：\n「" + dbl + "」", 0xFF8E929A);
        Thread.sleep(3000);

        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(Pcm.SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(Math.max(AudioTrack.getMinBufferSize(Pcm.SAMPLE_RATE,
                        AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT), Pcm.FRAME_BYTES * 4))
                .build();
        long playStart = size(mic);
        show(tag + "现在读（小新在说话）：\n「" + dbl + "」", 0xFFFFB74D);
        track.play();
        track.write(clip, 0, clip.length);
        Thread.sleep(300);
        track.stop();
        track.release();
        long playEnd = size(mic);
        show(tag + "好", 0xFF8E929A);
        Thread.sleep(1000);
        rec.stop();
        reader.join(1000);
        rec.release();

        String name = "dtalk" + rep + ".pcm";
        try (FileOutputStream out = new FileOutputStream(new File(getExternalFilesDir("echo"), name))) {
            out.write(mic.toByteArray());
        }
        return String.format(Locale.US,
                "{\"rep\":%d,\"file\":\"%s\",\"single\":\"%s\",\"double\":\"%s\",\"single_start\":%d,"
                        + "\"single_end\":%d,\"play_start\":%d,\"play_end\":%d,\"bytes\":%d,\"volume\":%d,"
                        + "\"device\":\"%s\"}",
                rep, name, single, dbl, singleStart, singleEnd, playStart, playEnd, mic.size(),
                am.getStreamVolume(AudioManager.STREAM_VOICE_CALL), device);
    }

    private static long size(ByteArrayOutputStream mic) {
        synchronized (mic) {
            return mic.size();
        }
    }

    private void say(String line) {
        runOnUiThread(() -> log.append(line + "\n"));
    }

    private void run(boolean finishAfter) {
        if (running) {
            return;
        }
        running = true;
        new Thread(() -> {
            try {
                File dir = getExternalFilesDir("echo");
                byte[] clip = readWavPcm(new File(dir, "clip.wav"));
                say("clip " + clip.length / 2 / Pcm.SAMPLE_RATE + " s");
                try (FileOutputStream meta = new FileOutputStream(new File(dir, "results.jsonl"))) {
                    for (int r = 0; r < REPEAT; r++) {
                        for (String mode : MODES) {
                            String line = trial(mode, r, clip, dir);
                            meta.write((line + "\n").getBytes("UTF-8"));
                            say(line);
                            Thread.sleep(800);
                        }
                    }
                }
                say("DONE");
            } catch (Exception e) {
                say("ERROR " + e);
            } finally {
                running = false;
                if (finishAfter) {
                    runOnUiThread(this::finish);
                }
            }
        }, "echo-test").start();
    }

    /** 一种组合录一段，返回一行 JSON 元数据。 */
    private String trial(String mode, int rep, byte[] clip, File dir) throws Exception {
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        boolean comm = mode.equals("C") || mode.equals("D");
        int source = mode.equals("A") ? MediaRecorder.AudioSource.UNPROCESSED
                : MediaRecorder.AudioSource.VOICE_COMMUNICATION;
        String device = "default";
        if (comm) {
            am.setMode(AudioManager.MODE_IN_COMMUNICATION);
            device = useSpeaker(am);
        }
        int minRec = AudioRecord.getMinBufferSize(Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec = new AudioRecord(source, Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, Math.max(minRec, Pcm.FRAME_BYTES * 8));
        String fx = "";
        AcousticEchoCanceler aec = null;
        NoiseSuppressor ns = null;
        if (mode.equals("D")) {
            aec = AcousticEchoCanceler.create(rec.getAudioSessionId());
            ns = NoiseSuppressor.create(rec.getAudioSessionId());
            if (aec != null) {
                aec.setEnabled(true);
            }
            if (ns != null) {
                ns.setEnabled(true);
            }
        }
        if (comm || mode.equals("B")) {
            // 系统按音源自动挂上的效果，查一下到底开没开
            AcousticEchoCanceler probe = AcousticEchoCanceler.create(rec.getAudioSessionId());
            fx = "aec_enabled=" + (probe != null && probe.getEnabled());
            if (probe != null && probe != aec) {
                probe.release();
            }
        }
        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(comm ? AudioAttributes.USAGE_VOICE_COMMUNICATION : AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(Pcm.SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(Math.max(AudioTrack.getMinBufferSize(Pcm.SAMPLE_RATE,
                        AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT), Pcm.FRAME_BYTES * 4))
                .build();

        ByteArrayOutputStream mic = new ByteArrayOutputStream();
        long[] playAt = {-1};
        rec.startRecording();
        Thread reader = new Thread(() -> {
            byte[] frame = new byte[Pcm.FRAME_BYTES];
            while (rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                int n = rec.read(frame, 0, frame.length);
                if (n > 0) {
                    synchronized (mic) {
                        mic.write(frame, 0, n);
                    }
                }
            }
        }, "echo-rec");
        reader.start();
        Thread.sleep(PAD_MS);
        synchronized (mic) {
            playAt[0] = mic.size();
        }
        track.play();
        track.write(clip, 0, clip.length);  // 阻塞到全部写进去
        Thread.sleep(300);                  // 让缓冲里的放完
        track.stop();
        track.release();
        long playEnd;
        synchronized (mic) {
            playEnd = mic.size();
        }
        Thread.sleep(PAD_MS);
        rec.stop();
        reader.join(1000);
        rec.release();
        if (aec != null) {
            aec.release();
        }
        if (ns != null) {
            ns.release();
        }
        if (comm) {
            if (Build.VERSION.SDK_INT >= 31) {
                am.clearCommunicationDevice();
            }
            am.setMode(AudioManager.MODE_NORMAL);
        }
        String name = mode + rep + ".pcm";
        try (FileOutputStream out = new FileOutputStream(new File(dir, name))) {
            out.write(mic.toByteArray());
        }
        int vol = am.getStreamVolume(comm ? AudioManager.STREAM_VOICE_CALL : AudioManager.STREAM_MUSIC);
        return String.format(Locale.US,
                "{\"mode\":\"%s\",\"rep\":%d,\"file\":\"%s\",\"play_start\":%d,\"play_end\":%d,\"bytes\":%d,"
                        + "\"volume\":%d,\"device\":\"%s\",\"fx\":\"%s\"}",
                mode, rep, name, playAt[0], playEnd, mic.size(), vol, device, fx);
    }

    private static String useSpeaker(AudioManager am) {
        if (Build.VERSION.SDK_INT < 31) {
            return "legacy";
        }
        for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
            if (d.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) {
                return am.setCommunicationDevice(d) ? "speaker" : "speaker_failed";
            }
        }
        AudioDeviceInfo cur = am.getCommunicationDevice();
        return cur == null ? "none" : "type" + cur.getType();
    }

    /** 读 16 kHz 单声道 s16 wav 的 PCM：找 data 块。 */
    private static byte[] readWavPcm(File f) throws IOException {
        byte[] all = new byte[(int) f.length()];
        try (InputStream in = new FileInputStream(f)) {
            int off = 0;
            while (off < all.length) {
                int n = in.read(all, off, all.length - off);
                if (n < 0) {
                    break;
                }
                off += n;
            }
        }
        int i = 12;
        while (i + 8 <= all.length) {
            int size = (all[i + 4] & 0xff) | (all[i + 5] & 0xff) << 8 | (all[i + 6] & 0xff) << 16
                    | (all[i + 7] & 0xff) << 24;
            if (all[i] == 'd' && all[i + 1] == 'a' && all[i + 2] == 't' && all[i + 3] == 'a') {
                byte[] pcm = new byte[Math.min(size, all.length - i - 8)];
                System.arraycopy(all, i + 8, pcm, 0, pcm.length);
                return pcm;
            }
            i += 8 + size + (size & 1);
        }
        throw new IOException("clip.wav 里没有 data 块");
    }
}
