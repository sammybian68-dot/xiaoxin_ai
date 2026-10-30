package com.xiaoxin.app;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * 闹钟和倒计时在 pad 上响：前台服务，熄屏也活着。
 *
 * 数据在服务端（/api/clock），这里长轮询拿最新状态，本机存一份（files/clock.json），断网也照着本机副本响。
 * 到点用 AlarmManager.setAlarmClock 唤醒（Doze 里也准），醒来亮屏、盖在锁屏上显示响铃页、放铃声，直到有人点「关掉」
 * 或者说「关掉」（网页里点按钮 → /api/clock/... → 这边收到新状态就停）。
 *
 * 2026-10-30 在 TB335FC 上实测：前台服务 + 部分唤醒锁，熄屏并强制进深度 Doze 4 分钟，录音一帧没丢、时钟零漂移。
 */
public class ClockService extends Service {
    static final String TAG = "XiaoxinClock";
    static final String ACTION_FIRE = "com.xiaoxin.app.CLOCK_FIRE";
    static final String ACTION_STOP = "com.xiaoxin.app.CLOCK_STOP";
    private static final String CHANNEL = "clock";
    private static final String CHANNEL_RING = "clock_ring";
    private static final long RING_MAX_MS = 5 * 60_000;  // 没人管最多响 5 分钟

    /** 网页（TalkActivity）注册进来，响铃 / 停止时通知它画界面。 */
    interface Listener {
        void onRing(JSONObject ev);
    }

    static volatile Listener listener;
    static volatile ClockService running;

    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile boolean alive;
    private volatile JSONObject snap;
    private PowerManager.WakeLock cpu;
    private AudioTrack ring;
    private String ringingKey;            // "alarm:a1:<触发时刻>" / "timer:t2"
    private final java.util.Set<String> fired = new java.util.HashSet<>();

    static void start(Context c) {
        c.startForegroundService(new Intent(c, ClockService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "小新在后台", NotificationManager.IMPORTANCE_MIN));
        NotificationChannel r = new NotificationChannel(CHANNEL_RING, "闹钟响铃", NotificationManager.IMPORTANCE_HIGH);
        r.setSound(null, null);  // 声音自己放，不用通知的
        nm.createNotificationChannel(r);
        startForeground(1, idleNotice("闹钟和计时在后台"), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        cpu = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "xiaoxin:clock");
        cpu.acquire();  // 一直插着电，换「熄屏也准时」
        loadLocal();
        alive = true;
        running = this;
        new Thread(this::poll, "clock-poll").start();
        main.post(this::tick);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String a = intent == null ? null : intent.getAction();
        if (ACTION_FIRE.equals(a)) {
            main.post(this::tick);
        } else if (ACTION_STOP.equals(a)) {
            stopRinging(true);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        alive = false;
        running = null;
        main.removeCallbacksAndMessages(null);
        stopRinging(false);
        if (cpu.isHeld()) {
            cpu.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ---------------------------------------------------------------- 同步

    private SharedPreferences prefs() {
        return getSharedPreferences("xiaoxin", MODE_PRIVATE);
    }

    private String base() {
        String b = prefs().getString("base", "https://47.116.13.227:8183/xiaoxin/");
        return b.endsWith("/") ? b : b + "/";
    }

    private void poll() {
        long since = -1;
        while (alive) {
            try {
                JSONObject s = get("api/clock?since=" + since + "&wait=" + (since < 0 ? 0 : 50));
                since = s.optLong("version", since);
                s.put("_local_at", System.currentTimeMillis() / 1000.0);
                snap = s;
                saveLocal(s);
                main.post(this::tick);
                prefetchSpeech(s);
            } catch (Exception e) {
                Log.w(TAG, "poll: " + e.getMessage());
                sleep(5000);
            }
        }
    }

    private JSONObject get(String path) throws Exception {
        return http("GET", path);
    }

    static JSONObject http(Context c, String method, String path) throws Exception {
        SharedPreferences p = c.getSharedPreferences("xiaoxin", MODE_PRIVATE);
        String b = p.getString("base", "https://47.116.13.227:8183/xiaoxin/");
        HttpURLConnection conn = (HttpURLConnection) new URL((b.endsWith("/") ? b : b + "/") + path).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(8000);
        conn.setReadTimeout(65000);
        conn.setRequestProperty("Authorization", "Bearer " + p.getString("key", ""));
        try {
            if (conn.getResponseCode() != 200) {
                throw new java.io.IOException("HTTP " + conn.getResponseCode());
            }
            try (InputStream in = conn.getInputStream()) {
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] b2 = new byte[8192];
                for (int n; (n = in.read(b2)) > 0; ) {
                    buf.write(b2, 0, n);
                }
                return new JSONObject(buf.toString("UTF-8"));
            }
        } finally {
            conn.disconnect();
        }
    }

    private JSONObject http(String method, String path) throws Exception {
        return http(this, method, path);
    }

    private File localFile() {
        return new File(getFilesDir(), "clock.json");
    }

    private void loadLocal() {
        try {
            snap = new JSONObject(new String(Files.readAllBytes(localFile().toPath()), StandardCharsets.UTF_8));
        } catch (Exception e) {
            snap = null;
        }
    }

    private void saveLocal(JSONObject s) {
        try (FileOutputStream out = new FileOutputStream(localFile())) {
            out.write(s.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) {
            // 下次再存
        }
    }

    // ---------------------------------------------------------------- 排程和响铃

    /** 看现在有没有该响的；没有就把下一次交给 AlarmManager，并更新常驻通知。 */
    private void tick() {
        main.removeCallbacks(this::tick);
        JSONObject s = snap;
        if (s == null) {
            return;
        }
        double now = System.currentTimeMillis() / 1000.0;
        double next = Double.MAX_VALUE;
        String due = null, title = "", sub = "", kind = "", id = "", say = "";
        JSONArray timers = s.optJSONArray("timers");
        java.util.Set<String> live = new java.util.HashSet<>();
        for (int i = 0; timers != null && i < timers.length(); i++) {
            JSONObject t = timers.optJSONObject(i);
            String key = "timer:" + t.optString("id");
            live.add(key);
            String st = t.optString("state");
            double end = t.optDouble("ends_at", 0);
            if ("done".equals(st) || ("running".equals(st) && end <= now)) {
                if (due == null && !fired.contains(key + ":stopped")) {
                    due = key;
                    kind = "timer";
                    id = t.optString("id");
                    title = "时间到";
                    sub = t.optString("label").isEmpty() ? "倒计时" : t.optString("label");
                    say = t.optString("say_text");
                }
            } else if ("running".equals(st)) {
                next = Math.min(next, end);
            }
        }
        JSONArray alarms = s.optJSONArray("alarms");
        for (int i = 0; alarms != null && i < alarms.length(); i++) {
            JSONObject a = alarms.optJSONObject(i);
            // 服务端标的「正在响」（到点 10 分钟内、没被关掉 / 贪睡）
            if (!a.isNull("ringing")) {
                String key = "alarm:" + a.optString("id") + ":" + (long) a.optDouble("ringing");
                live.add(key);
                if (due == null && !fired.contains(key)) {
                    due = key;
                    kind = "alarm";
                    id = a.optString("id");
                    title = a.optString("time");
                    sub = a.optString("label").isEmpty() ? "闹钟" : a.optString("label");
                    say = a.optString("say_text");
                }
            }
            if (a.isNull("next")) {
                continue;
            }
            double at = a.optDouble("next");
            if (at <= now) {
                // 本机副本里的「下次」到点了：先当在响（断网也响），同时马上去服务端拉一次确认
                String key = "alarm:" + a.optString("id") + ":" + (long) at;
                live.add(key);
                if (now - at < 600 && due == null && !fired.contains(key)) {
                    due = key;
                    kind = "alarm";
                    id = a.optString("id");
                    title = a.optString("time");
                    sub = a.optString("label").isEmpty() ? "闹钟" : a.optString("label");
                    say = a.optString("say_text");
                }
            } else {
                next = Math.min(next, at);
            }
        }
        // 正在响的那个已经不在了（网页、别的设备、模型关掉了 / 贪睡了）：停
        if (ringingKey != null && !live.contains(ringingKey)) {
            stopRinging(false);
        }
        if (due != null && ringingKey == null) {
            startRinging(due, kind, id, title, sub.isEmpty() ? say : sub, say);
        }
        if (next < Double.MAX_VALUE) {
            schedule((long) (next * 1000));
            main.postDelayed(this::tick, (long) Math.max(500, Math.min((next - now) * 1000, 60_000)));
        }
        updateNotice(s, now);
    }

    private void schedule(long atMs) {
        AlarmManager am = getSystemService(AlarmManager.class);
        PendingIntent fire = PendingIntent.getService(this, 1,
                new Intent(this, ClockService.class).setAction(ACTION_FIRE), PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent show = PendingIntent.getActivity(this, 2, new Intent(this, TalkActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        try {
            am.setAlarmClock(new AlarmManager.AlarmClockInfo(atMs, show), fire);
        } catch (SecurityException e) {
            // 没有精确闹钟权限：退回到普通的，Doze 里可能晚几分钟；我们自己的唤醒锁一般也能兜住
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMs, fire);
        }
    }

    private void startRinging(String key, String kind, String id, String title, String sub, String say) {
        ringingKey = key;
        fired.add(key);
        Log.i(TAG, "ring " + key + " " + sub);
        JSONObject ev = new JSONObject();
        try {
            ev.put("type", "ring").put("kind", kind).put("id", id).put("title", title).put("sub", sub).put("say", say);
        } catch (Exception ignored) {
            // 不会
        }
        // 亮屏、盖在锁屏上：响铃页由 TalkActivity 画（全屏通知的 fullScreenIntent 拉起它）
        Intent open = new Intent(this, TalkActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra("ring", ev.toString());
        PendingIntent full = PendingIntent.getActivity(this, 3, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent stop = PendingIntent.getService(this, 4, new Intent(this, ClockService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CHANNEL_RING)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("alarm".equals(kind) ? title + "  " + sub : sub + " 时间到")
                .setCategory(Notification.CATEGORY_ALARM)
                .setFullScreenIntent(full, true)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "关掉", stop).build())
                .build();
        getSystemService(NotificationManager.class).notify(2, n);
        try {
            startActivity(open);
        } catch (Exception e) {
            Log.w(TAG, "open activity: " + e.getMessage());  // 后台起 Activity 被拦时，全屏通知会拉起
        }
        wakeScreen();
        playRing(say);
        Listener l = listener;
        if (l != null) {
            l.onRing(ev);
        }
        main.postDelayed(() -> { if (key.equals(ringingKey)) stopRinging(false); }, RING_MAX_MS);
    }

    /** 停铃。byUser：从通知点的「关掉」，要告诉服务端（网页点的由网页自己调接口）。 */
    void stopRinging(boolean byUser) {
        String key = ringingKey;
        ringingKey = null;
        if (key != null && key.startsWith("timer:")) {
            fired.add(key + ":stopped");
        }
        ringGen++;  // 循环线程看到代数变了就自己停
        AudioTrack t = ring;
        ring = null;
        if (t != null) {
            try {
                t.pause();
                t.flush();
            } catch (IllegalStateException ignored) {
                // 已经停了
            }
        }
        getSystemService(NotificationManager.class).cancel(2);
        if (byUser && key != null) {
            String[] p = key.split(":");
            String path = "alarm".equals(p[0]) ? "api/clock/alarms/" + p[1] + "/dismiss" : "api/clock/timers/" + p[1] + "/cancel";
            new Thread(() -> {
                try {
                    http("POST", path);
                } catch (Exception e) {
                    Log.w(TAG, "dismiss: " + e.getMessage());
                }
            }, "clock-dismiss").start();
        }
        if (key != null) {
            Listener l = listener;
            if (l != null) {
                try {
                    l.onRing(new JSONObject().put("type", "stop"));
                } catch (Exception ignored) {
                    // 不会
                }
            }
        }
    }

    private void wakeScreen() {
        @SuppressWarnings("deprecation")
        PowerManager.WakeLock w = getSystemService(PowerManager.class).newWakeLock(
                PowerManager.SCREEN_BRIGHT_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP, "xiaoxin:ring");
        w.acquire(10_000);
    }

    private volatile int ringGen;
    private static final int RATE = 24000;

    /**
     * 响铃循环：小新用当前音色念一遍 say（「起床了，卞依一」），再响两组铃声，如此往复，直到被关掉。
     * 音量从 40% 渐强到 100%，不吓着人。走闹钟音量（USAGE_ALARM），静音模式下也响。
     * 念的话本机有缓存就直接用；没有（断网）就只响铃。
     */
    private void playRing(String say) {
        int gen = ++ringGen;
        byte[] speech = speech(say);
        short[] bell = bell();
        new Thread(() -> {
            AudioTrack t = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(RATE).build();
            ring = t;
            t.play();
            long t0 = System.currentTimeMillis();
            byte[] gap = new byte[RATE * 2 * 6 / 10];
            byte[] bellBytes = new byte[bell.length * 2];
            java.nio.ByteBuffer.wrap(bellBytes).order(java.nio.ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(bell);
            try {
                while (gen == ringGen) {
                    float vol = Math.min(1f, 0.4f + (System.currentTimeMillis() - t0) / 30_000f);
                    t.setVolume(vol);
                    if (speech != null) {
                        t.write(speech, 0, speech.length);
                        t.write(gap, 0, gap.length);
                    }
                    for (int k = 0; k < 2 && gen == ringGen; k++) {
                        t.write(bellBytes, 0, bellBytes.length);
                    }
                }
            } catch (IllegalStateException ignored) {
                // 被停了
            } finally {
                t.release();
            }
        }, "clock-ring").start();
    }

    /** 两声一组的柔和琴音（C6 + E6），2 秒。 */
    private static short[] bell() {
        int n = RATE * 2;
        short[] pcm = new short[n];
        double[][] notes = {{0.0, 1046.5}, {0.35, 1318.5}};
        for (double[] note : notes) {
            int start = (int) (note[0] * RATE);
            int len = (int) (0.9 * RATE);
            for (int i = 0; i < len && start + i < n; i++) {
                double tt = i / (double) RATE;
                double env = Math.min(1, tt / 0.01) * Math.exp(-tt * 4.5);
                double v = Math.sin(2 * Math.PI * note[1] * tt) + 0.3 * Math.sin(4 * Math.PI * note[1] * tt);
                pcm[start + i] += (short) (v * env * 9000);
            }
        }
        return pcm;
    }

    // ---------------------------------------------------------------- 念的话：按文字缓存，拿到快照就提前下好

    private File speechFile(String text) {
        // v2：服务端加了「念完整」检查，旧缓存里有念了半句的
        return new File(new File(getFilesDir(), "ring"), "v2_" + Integer.toHexString(text.hashCode()) + "_" + text.length() + ".pcm");
    }

    private byte[] speech(String text) {
        if (text == null || text.isEmpty()) {
            return null;
        }
        File f = speechFile(text);
        try {
            return f.isFile() ? Files.readAllBytes(f.toPath()) : null;
        } catch (Exception e) {
            return null;
        }
    }

    /** 每个闹钟 / 倒计时要念的话、默认提示，没缓存的从 /api/cue/ring?text= 下下来（当前音色）。 */
    private void prefetchSpeech(JSONObject s) {
        java.util.Set<String> texts = new java.util.LinkedHashSet<>();
        texts.add(s.optString("ring_text"));
        for (String k : new String[] {"alarms", "timers"}) {
            JSONArray arr = s.optJSONArray(k);
            for (int i = 0; arr != null && i < arr.length(); i++) {
                texts.add(arr.optJSONObject(i).optString("say_text"));
            }
        }
        for (String text : texts) {
            if (text.isEmpty() || speechFile(text).isFile()) {
                continue;
            }
            try {
                byte[] pcm = download("api/cue/ring?text=" + java.net.URLEncoder.encode(text, "UTF-8"));
                File f = speechFile(text);
                f.getParentFile().mkdirs();
                File tmp = new File(f.getPath() + ".tmp");
                try (FileOutputStream out = new FileOutputStream(tmp)) {
                    out.write(pcm);
                }
                if (!tmp.renameTo(f)) {
                    tmp.delete();
                }
            } catch (Exception e) {
                Log.w(TAG, "prefetch " + text + ": " + e.getMessage());
            }
        }
    }

    /** 换了音色：念的话全部作废，下次拿到快照重新下。 */
    static void clearSpeech(Context c) {
        File[] fs = new File(c.getFilesDir(), "ring").listFiles();
        for (File f : fs == null ? new File[0] : fs) {
            f.delete();
        }
        ClockService s = running;
        if (s != null && s.snap != null) {
            JSONObject snap = s.snap;
            new Thread(() -> s.prefetchSpeech(snap), "ring-prefetch").start();
        }
    }

    private byte[] download(String path) throws Exception {
        SharedPreferences p = prefs();
        HttpURLConnection conn = (HttpURLConnection) new URL(base() + path).openConnection();
        conn.setConnectTimeout(8000);
        conn.setReadTimeout(30000);
        conn.setRequestProperty("Authorization", "Bearer " + p.getString("key", ""));
        try {
            if (conn.getResponseCode() != 200) {
                throw new java.io.IOException("HTTP " + conn.getResponseCode());
            }
            try (InputStream in = conn.getInputStream()) {
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] b2 = new byte[8192];
                for (int n; (n = in.read(b2)) > 0; ) {
                    buf.write(b2, 0, n);
                }
                return buf.toByteArray();
            }
        } finally {
            conn.disconnect();
        }
    }

    // ---------------------------------------------------------------- 常驻通知

    private Notification idleNotice(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, TalkActivity.class), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("小新").setContentText(text).setContentIntent(open).setOngoing(true).build();
    }

    private void updateNotice(JSONObject s, double now) {
        String text = "闹钟和计时在后台";
        JSONArray alarms = s.optJSONArray("alarms");
        double best = Double.MAX_VALUE;
        String bestTime = null;
        for (int i = 0; alarms != null && i < alarms.length(); i++) {
            JSONObject a = alarms.optJSONObject(i);
            if (!a.isNull("next") && a.optDouble("next") > now && a.optDouble("next") < best) {
                best = a.optDouble("next");
                bestTime = a.optString("time");
            }
        }
        if (bestTime != null) {
            text = "下一个闹钟 " + bestTime;
        }
        getSystemService(NotificationManager.class).notify(1, idleNotice(text));
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}
