package com.xiaoxin.testapp.screenoff;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import com.xiaoxin.util.Pcm;

/**
 * 熄屏存活测试：前台服务（microphone 类型）+ 部分唤醒锁，熄屏后一直录音，每 5 秒打一行 logcat：
 * 录到多少帧（应该每 5 秒 50 帧）、电平、和墙钟/开机钟的漂移。看 Doze、ZUI 省电会不会把它停掉。
 *   adb logcat -s ScreenOff
 */
public class ScreenOffService extends Service {
    static final String TAG = "ScreenOff";
    private volatile boolean running;
    private PowerManager.WakeLock lock;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (running) {
            return START_STICKY;
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("screenoff", "熄屏测试", NotificationManager.IMPORTANCE_LOW));
        Notification n = new Notification.Builder(this, "screenoff")
                .setContentTitle("熄屏存活测试").setContentText("在录音，每 5 秒记一次")
                .setSmallIcon(android.R.drawable.ic_btn_speak_now).setOngoing(true).build();
        startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        lock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "xiaoxin:screenoff");
        lock.acquire();
        running = true;
        new Thread(this::loop, "screenoff-mic").start();
        return START_STICKY;
    }

    private void loop() {
        int min = AudioRecord.getMinBufferSize(Pcm.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, Pcm.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, Pcm.FRAME_BYTES * 8));
        rec.startRecording();
        byte[] frame = new byte[Pcm.FRAME_BYTES];
        long t0Wall = System.currentTimeMillis(), t0Up = SystemClock.elapsedRealtime();
        long lastLog = t0Up;
        int frames = 0, peak = 0;
        PowerManager pm = getSystemService(PowerManager.class);
        while (running) {
            int n = rec.read(frame, 0, frame.length);
            if (n > 0) {
                frames++;
                peak = Math.max(peak, Pcm.level(frame, n).rms);
            }
            long now = SystemClock.elapsedRealtime();
            if (now - lastLog >= 5000) {
                long drift = (System.currentTimeMillis() - t0Wall) - (now - t0Up);
                Log.i(TAG, "t=" + (now - t0Up) / 1000 + "s frames=" + frames + " peak=" + peak
                        + " screen=" + (pm.isInteractive() ? "on" : "off") + " idle=" + pm.isDeviceIdleMode()
                        + " drift=" + drift + "ms");
                frames = 0;
                peak = 0;
                lastLog = now;
            }
        }
        rec.stop();
        rec.release();
    }

    @Override
    public void onDestroy() {
        running = false;
        if (lock != null && lock.isHeld()) {
            lock.release();
        }
        Log.i(TAG, "service destroyed");
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
