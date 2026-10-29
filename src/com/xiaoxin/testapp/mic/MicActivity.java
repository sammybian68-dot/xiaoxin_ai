package com.xiaoxin.testapp.mic;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.xiaoxin.testapp.TestBar;
import com.xiaoxin.util.Pcm;

import java.io.File;
import java.io.FileOutputStream;

public class MicActivity extends Activity {
    private static final String TAG = "MicTest";
    private static final int WINDOW_SECONDS = 8;

    private TextView title;
    private TextView status;
    private Button replay;
    private View barFill;
    private AudioRecord recorder;
    private AudioTrack player;
    private Thread thread;
    private volatile boolean running;
    private volatile boolean playing;
    private final Pcm.Window window = new Pcm.Window(WINDOW_SECONDS);
    private int chunks;
    private int source = MediaRecorder.AudioSource.MIC;
    private String effects = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.addView(TestBar.create(this));

        title = new TextView(this);
        title.setPadding(48, 24, 48, 0);
        title.setText("请说话");
        title.setTextColor(Color.WHITE);
        title.setTextSize(42);
        root.addView(title);

        FrameLayout track = new FrameLayout(this);
        track.setBackgroundColor(0xFF333333);
        LinearLayout.LayoutParams trackParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 72);
        trackParams.leftMargin = 48;
        trackParams.rightMargin = 48;
        trackParams.topMargin = 28;
        trackParams.bottomMargin = 28;
        track.setLayoutParams(trackParams);
        barFill = new View(this);
        barFill.setBackgroundColor(0xFF3DDC84);
        track.addView(barFill, new FrameLayout.LayoutParams(8, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(track);

        status = new TextView(this);
        status.setTextColor(Color.WHITE);
        status.setTextSize(26);
        status.setGravity(Gravity.START);
        status.setPadding(48, 0, 48, 0);
        status.setText("等待录音权限");
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        status.setLayoutParams(statusParams);
        root.addView(status);

        replay = new Button(this);
        replay.setAllCaps(false);
        replay.setText("回听最近 8 秒");
        replay.setTextSize(28);
        replay.setTextColor(Color.BLACK);
        replay.setBackgroundColor(0xFF3DDC84);
        replay.setEnabled(false);
        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 140);
        buttonParams.leftMargin = 48;
        buttonParams.rightMargin = 48;
        buttonParams.bottomMargin = 36;
        buttonParams.topMargin = 24;
        replay.setLayoutParams(buttonParams);
        replay.setOnClickListener(v -> onReplayClicked());
        root.addView(replay);
        setContentView(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
            return;
        }
        startCapture();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startCapture();
        } else {
            status.setText("没有录音权限");
        }
    }

    @Override
    protected void onPause() {
        stopPlayback();
        stopCapture();
        super.onPause();
    }

    private void onReplayClicked() {
        if (playing) {
            stopPlayback();
            startCapture();
            return;
        }
        byte[] pcm = window.snapshot();
        if (pcm.length < Pcm.FRAME_BYTES) {
            status.setText("还没录到一帧，先说一句");
            return;
        }
        stopCapture();
        playing = true;
        title.setText("回听中");
        replay.setText("停止");
        byte[] audible = boostForSpeaker(pcm);
        new Thread(() -> playClip(audible, pcm.length / 2), "mic-play").start();
    }

    private void startCapture() {
        if (thread != null) {
            return;
        }
        effects = "AEC " + yn(AcousticEchoCanceler.isAvailable())
                + "  NS " + yn(NoiseSuppressor.isAvailable())
                + "  AGC " + yn(AutomaticGainControl.isAvailable());
        Log.i(TAG, effects);
        source = MediaRecorder.AudioSource.UNPROCESSED;
        int min = AudioRecord.getMinBufferSize(
                Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (min <= 0) {
            status.setText("这台设备不支持 16kHz 单声道 PCM");
            return;
        }
        recorder = openRecorder(source, Math.max(min, Pcm.FRAME_BYTES * 8));
        if (recorder == null && source != MediaRecorder.AudioSource.MIC) {
            source = MediaRecorder.AudioSource.MIC;
            recorder = openRecorder(source, Math.max(min, Pcm.FRAME_BYTES * 8));
        }
        if (recorder == null) {
            status.setText("AudioRecord 打开失败");
            return;
        }
        running = true;
        recorder.startRecording();
        thread = new Thread(this::readLoop, "mic-pcm");
        thread.start();
        setStatus("打开麦克风  " + sourceName() + "\n16 kHz  mono  s16le  100ms/帧\n" + effects);
    }

    private static String yn(boolean value) {
        return value ? "有" : "无";
    }

    private AudioRecord openRecorder(int audioSource, int bufferBytes) {
        AudioRecord record = new AudioRecord(
                audioSource,
                Pcm.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferBytes);
        if (record.getState() != AudioRecord.STATE_INITIALIZED) {
            record.release();
            return null;
        }
        return record;
    }

    private void readLoop() {
        byte[] frame = new byte[Pcm.FRAME_BYTES];
        int sinceFlush = 0;
        while (running) {
            int n = recorder.read(frame, 0, frame.length);
            if (n <= 0) {
                setStatus("读取失败 " + n);
                continue;
            }
            Pcm.Level level = Pcm.level(frame, n);
            window.add(frame, n);
            chunks++;
            sinceFlush++;
            if (sinceFlush >= 10) {
                sinceFlush = 0;
                flush();
                Log.i(TAG, "chunks=" + chunks + " rms=" + level.rms + " peak=" + level.peak);
            }
            showLevel(level, n);
        }
    }

    private void showLevel(Pcm.Level level, int bytes) {
        runOnUiThread(() -> {
            View parent = (View) barFill.getParent();
            int width = parent.getWidth();
            if (width <= 0) {
                width = 1200;
            }
            float frac = Math.min(1f, level.peak / 12000f);
            ViewGroup.LayoutParams params = barFill.getLayoutParams();
            params.width = Math.max(8, (int) (width * frac));
            barFill.setLayoutParams(params);
            status.setText(sourceName() + "  16 kHz mono s16le  100ms\n"
                    + "第 " + chunks + " 帧    " + bytes + " B\n"
                    + "RMS " + level.rms + "    peak " + level.peak + "\n"
                    + effects + "\n"
                    + "磁盘只留最近 " + WINDOW_SECONDS + " 秒\n"
                    + "说完点下面的回听");
            replay.setEnabled(chunks >= 10);
        });
    }

    private void flush() {
        byte[] pcm = window.snapshot();
        File dir = getFilesDir();
        write(new File(dir, "window.pcm.tmp"), pcm);
        new File(dir, "window.pcm.tmp").renameTo(new File(dir, "window.pcm"));
        String meta = "rate=16000\nchannels=1\nencoding=s16le\nframe_samples=1600\n"
                + "chunks=" + chunks + "\nbytes=" + pcm.length + "\nsource=" + sourceName()
                + "\n" + effects + "\n";
        write(new File(dir, "pcm.txt"), meta.getBytes());
    }

    private static void write(File file, byte[] data) {
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(data);
        } catch (Exception e) {
            Log.e(TAG, "write failed", e);
        }
    }

    private String sourceName() {
        if (source == MediaRecorder.AudioSource.UNPROCESSED) {
            return "UNPROCESSED";
        }
        return "MIC";
    }

    private void playClip(byte[] pcm, int samples) {
        int buffer = Math.max(pcm.length, AudioTrack.getMinBufferSize(
                Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT));
        AudioTrack track = new AudioTrack(
                AudioManager.STREAM_MUSIC,
                Pcm.SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                buffer,
                AudioTrack.MODE_STATIC);
        player = track;
        try {
            track.write(pcm, 0, pcm.length);
            track.play();
            while (playing && track.getPlaybackHeadPosition() < samples) {
                int seconds = track.getPlaybackHeadPosition() / Pcm.SAMPLE_RATE;
                int total = samples / Pcm.SAMPLE_RATE;
                runOnUiThread(() -> status.setText("回听中  " + seconds + " / " + total + " 秒\n"
                        + "喇叭在放，麦克风已关，避免串音"));
                try {
                    Thread.sleep(100);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "playback failed", e);
            setStatus("回听失败: " + e.getMessage());
        } finally {
            releasePlayer(track);
            playing = false;
            runOnUiThread(() -> {
                replay.setText("回听最近 8 秒");
                title.setText("请说话");
                if (!isFinishing()) {
                    startCapture();
                }
            });
        }
    }

    /** Saved PCM stays raw. Speaker playback is boosted so a quiet tablet mic is audible. */
    private static byte[] boostForSpeaker(byte[] pcm) {
        int peak = 0;
        for (int i = 0; i + 1 < pcm.length; i += 2) {
            int sample = (short) ((pcm[i] & 0xff) | (pcm[i + 1] << 8));
            int abs = sample < 0 ? -sample : sample;
            if (abs > peak) {
                peak = abs;
            }
        }
        if (peak < 80) {
            return pcm;
        }
        float gain = Math.min(16f, 20000f / peak);
        byte[] out = new byte[pcm.length];
        for (int i = 0; i + 1 < pcm.length; i += 2) {
            int sample = (short) ((pcm[i] & 0xff) | (pcm[i + 1] << 8));
            int boosted = Math.max(-32768, Math.min(32767, Math.round(sample * gain)));
            out[i] = (byte) boosted;
            out[i + 1] = (byte) (boosted >> 8);
        }
        return out;
    }

    private void stopPlayback() {
        playing = false;
        AudioTrack track = player;
        if (track != null) {
            releasePlayer(track);
        }
    }

    private void releasePlayer(AudioTrack track) {
        try {
            track.pause();
            track.flush();
        } catch (IllegalStateException ignored) {
            // already stopped
        }
        track.release();
        if (player == track) {
            player = null;
        }
    }

    private void stopCapture() {
        running = false;
        Thread worker = thread;
        thread = null;
        if (worker != null) {
            try {
                worker.join(500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        if (recorder != null) {
            try {
                recorder.stop();
            } catch (IllegalStateException ignored) {
                // already stopped
            }
            recorder.release();
            recorder = null;
        }
    }

    private void setStatus(String text) {
        Log.i(TAG, text);
        runOnUiThread(() -> status.setText(text));
    }
}
