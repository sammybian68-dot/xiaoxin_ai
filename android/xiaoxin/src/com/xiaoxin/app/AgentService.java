package com.xiaoxin.app;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.hardware.HardwareBuffer;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 抓作业的「手」（docs/qq-homework.md）：小新服务到点自己跑 agent（模型看屏决定下一步），
 * 这里在 pad 上执行它要的动作，执行完把屏幕截图传回去。不需要 Mac、不需要数据线。
 *
 * 用的是无障碍服务（辅助功能）：只有它能在别的 app（QQ）上点、滑、截屏。要在系统设置里手动打开一次：
 * 设置 → 辅助功能 → 已下载的应用 → 小新 · 抓作业 → 开。
 *
 * 协议（都是小新服务的接口，设备 token 鉴权）：
 *   GET  api/agent/next?wait=50      长轮询：有动作就回 {"id","name","args"}，没有回 {}
 *   POST api/agent/done/{id}          执行结果：{"ok","out","w","h","shot"(JPEG base64),"widgets"}
 * 动作：shot（只截屏）、tap、swipe、back、home、open_qq（强制重开 QQ）、wake（亮屏、解锁）。
 * 坐标都是屏幕像素（服务端按截图尺寸换算好再发过来）。
 */
public class AgentService extends AccessibilityService {
    private static final String TAG = "XxAgent";
    private static final String QQ = "com.tencent.mobileqq";
    private volatile boolean alive;
    private final Handler main = new Handler(Looper.getMainLooper());

    /** 设置页 / 网页要知道这台 pad 开没开辅助功能。 */
    static boolean enabled(Context c) {
        String on = Settings.Secure.getString(c.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        String me = new ComponentName(c, AgentService.class).flattenToString();
        return on != null && on.contains(me);
    }

    static void openSettings(Context c) {
        c.startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    @Override
    protected void onServiceConnected() {
        alive = true;
        bound = true;
        getSharedPreferences("xiaoxin", MODE_PRIVATE).edit().putBoolean("agent_want", true).apply();
        new Thread(this::loop, "agent-poll").start();
        Log.i(TAG, "connected");
    }

    @Override
    public void onDestroy() {
        alive = false;
        bound = false;
        super.onDestroy();
    }

    // ---------------------------------------------------------------- 看门狗（ClockService 每分钟调一次）

    /** 这个进程里服务绑上了没有。进程被杀重启后是 false，直到系统再绑一次。 */
    private static volatile boolean bound;
    private static long lastRebind;

    /**
     * 联想的内存清理（ZuiMemoryCleaner）杀掉小新后，系统把这个服务记成 Crashed、不再自动绑定：
     * 设置里还开着，其实没在跑，pad 不来领动作，抓作业整天落空（2026-11-05 就是这样）。
     * 开关是开着、服务却没绑上的话，自己把开关关了再开一次，系统就会重新绑定。
     * 要 WRITE_SECURE_SETTINGS（adb 授权一次：pm grant com.xiaoxin.app android.permission.WRITE_SECURE_SETTINGS，
     * 覆盖安装不丢）。没授权就什么都不做，只能等人去辅助功能里关开一次。
     */
    static void watchdog(Context c, long upMs) {
        // 开关被清掉也要接上：force-stop（有的清理工具就是这么杀的）会把它从已开列表里删掉，不是家长关的。
        // 只要这台 pad 上开过一次（want），就一直保持开着；真不想抓了在小新设置里关「每天自动抓」
        SharedPreferences p = c.getSharedPreferences("xiaoxin", MODE_PRIVATE);
        if (enabled(c) && !p.getBoolean("agent_want", false)) {
            p.edit().putBoolean("agent_want", true).apply();
        }
        boolean want = p.getBoolean("agent_want", false);
        if (bound || !want || upMs < 30_000) {
            return;  // 绑着；这台从没开过；进程刚起来，先给系统机会自己绑
        }
        long now = System.currentTimeMillis();
        if (now - lastRebind < 5 * 60_000) {
            return;
        }
        lastRebind = now;
        if (c.checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS") != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "watchdog: 服务没绑上，但没有 WRITE_SECURE_SETTINGS，自己接不上");
            return;
        }
        android.content.ContentResolver r = c.getContentResolver();
        String me = new ComponentName(c, AgentService.class).flattenToString();
        String on = Settings.Secure.getString(r, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        StringBuilder rest = new StringBuilder();
        for (String s : (on == null ? "" : on).split(":")) {
            if (!s.isEmpty() && !s.equals(me)) {
                rest.append(rest.length() > 0 ? ":" : "").append(s);
            }
        }
        Log.w(TAG, "watchdog: 服务没绑上（开关" + (enabled(c) ? "开着" : "被清掉了") + "），关开一次辅助功能");
        Settings.Secure.putString(r, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, rest.toString());
        sleep(1500);
        Settings.Secure.putString(r, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
                rest.length() > 0 ? rest + ":" + me : me);
        Settings.Secure.putString(r, Settings.Secure.ACCESSIBILITY_ENABLED, "1");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 不监听事件：只按服务端的指令动
    }

    @Override
    public void onInterrupt() {
    }

    // ---------------------------------------------------------------- 主循环

    private void loop() {
        while (alive) {
            try {
                JSONObject job = http("GET", "api/agent/next?wait=50", null);
                String id = job.optString("id", "");
                if (id.isEmpty()) {
                    continue;
                }
                JSONObject res;
                try {
                    res = run(job.optString("name"), job.optJSONObject("args"));
                } catch (Exception e) {
                    Log.w(TAG, "run " + job, e);
                    res = new JSONObject().put("ok", false).put("out", "执行出错：" + e.getMessage());
                }
                http("POST", "api/agent/done/" + id, res);
            } catch (Exception e) {
                Log.w(TAG, "poll: " + e.getMessage());
                sleep(5000);
            }
        }
    }

    private JSONObject run(String name, JSONObject a) throws Exception {
        if (a == null) {
            a = new JSONObject();
        }
        String out;
        switch (name) {
            case "wake":
                out = wake();
                break;
            case "tap":
                out = gesture(a.getInt("x"), a.getInt("y"), a.getInt("x"), a.getInt("y"), 60) ? "点了" : "点击没执行";
                sleep(1800);
                break;
            case "swipe":
                out = gesture(a.getInt("x1"), a.getInt("y1"), a.getInt("x2"), a.getInt("y2"), a.optInt("ms", 420))
                        ? "滑了" : "滑动没执行";
                sleep(1500);
                break;
            case "back":
                out = performGlobalAction(GLOBAL_ACTION_BACK) ? "按了返回" : "返回没执行";
                sleep(1800);
                break;
            case "home":
                out = performGlobalAction(GLOBAL_ACTION_HOME) ? "回了桌面" : "回桌面没执行";
                sleep(1200);
                break;
            case "open_qq":
                out = openQq();
                break;
            case "shot":
            default:
                out = "看了一眼";
                sleep((long) (a.optDouble("wait_s", 0) * 1000));
                break;
        }
        JSONObject res = new JSONObject().put("ok", true).put("out", out);
        attachShot(res);
        return res;
    }

    // ---------------------------------------------------------------- 动作

    private boolean gesture(int x1, int y1, int x2, int y2, int ms) throws InterruptedException {
        Path p = new Path();
        p.moveTo(x1, y1);
        if (x1 != x2 || y1 != y2) {
            p.lineTo(x2, y2);
        }
        GestureDescription g = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(p, 0, Math.max(1, ms))).build();
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<Boolean> ok = new AtomicReference<>(false);
        main.post(() -> dispatchGesture(g, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                ok.set(true);
                done.countDown();
            }

            @Override
            public void onCancelled(GestureDescription d) {
                done.countDown();
            }
        }, null));
        done.await(ms + 3000L, TimeUnit.MILLISECONDS);
        return ok.get();
    }

    /** 亮屏、过掉锁屏（这台没设密码）。孩子房间里 pad 平时熄屏，抓之前要先点亮。 */
    private String wake() throws InterruptedException {
        PowerManager pm = getSystemService(PowerManager.class);
        if (!pm.isInteractive()) {
            @SuppressWarnings("deprecation")
            PowerManager.WakeLock w = pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK
                    | PowerManager.ACQUIRE_CAUSES_WAKEUP, "xiaoxin:agent");
            w.acquire(60_000);
            sleep(800);
        }
        android.app.KeyguardManager km = getSystemService(android.app.KeyguardManager.class);
        if (km.isKeyguardLocked()) {
            DisplayMetrics m = getResources().getDisplayMetrics();
            gesture(m.widthPixels / 2, m.heightPixels * 9 / 10, m.widthPixels / 2, m.heightPixels / 4, 250);
            sleep(1500);
        }
        return km.isKeyguardLocked() ? "屏幕还锁着（是不是设了密码？）" : "屏幕亮了";
    }

    private String openQq() throws InterruptedException {
        // 先回桌面再启动，相当于从头进（不用 force-stop：普通 app 没这个权限；QQ 在后台也会回到它自己的首页）
        performGlobalAction(GLOBAL_ACTION_HOME);
        sleep(800);
        Intent i = getPackageManager().getLaunchIntentForPackage(QQ);
        if (i == null) {
            return "这台 pad 上没装 QQ";
        }
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        startActivity(i);
        sleep(7000);
        return "打开了 QQ";
    }

    // ---------------------------------------------------------------- 截屏

    private void attachShot(JSONObject res) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<Bitmap> got = new AtomicReference<>();
        takeScreenshot(Display.DEFAULT_DISPLAY, getMainExecutor(), new TakeScreenshotCallback() {
            @Override
            public void onSuccess(ScreenshotResult r) {
                HardwareBuffer hb = r.getHardwareBuffer();
                Bitmap b = Bitmap.wrapHardwareBuffer(hb, r.getColorSpace());
                if (b != null) {
                    got.set(b.copy(Bitmap.Config.ARGB_8888, false));
                }
                hb.close();
                done.countDown();
            }

            @Override
            public void onFailure(int code) {
                Log.w(TAG, "screenshot failed " + code);
                done.countDown();
            }
        });
        done.await(5, TimeUnit.SECONDS);
        Bitmap b = got.get();
        if (b == null) {
            res.put("out", res.optString("out") + "（截屏失败）");
            return;
        }
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.JPEG, 85, buf);
        res.put("w", b.getWidth()).put("h", b.getHeight())
                .put("shot", Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP))
                .put("widgets", widgets());
        b.recycle();
    }

    /** 能读到文字的控件和中心坐标（QQ 工具栏按钮能读到；自绘的列表读不到，那些靠模型看图）。 */
    private JSONArray widgets() {
        JSONArray out = new JSONArray();
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            walk(root, out, 0);
        }
        return out;
    }

    private void walk(AccessibilityNodeInfo n, JSONArray out, int depth) {
        if (n == null || out.length() >= 60 || depth > 40) {
            return;
        }
        CharSequence t = n.getText() != null ? n.getText() : n.getContentDescription();
        if (t != null && t.length() > 0 && t.length() <= 16 && n.isVisibleToUser()) {
            android.graphics.Rect r = new android.graphics.Rect();
            n.getBoundsInScreen(r);
            try {
                out.put(new JSONObject().put("t", t.toString().trim()).put("x", r.centerX()).put("y", r.centerY()));
            } catch (Exception ignored) {
            }
        }
        for (int i = 0; i < n.getChildCount(); i++) {
            walk(n.getChild(i), out, depth + 1);
        }
    }

    // ---------------------------------------------------------------- HTTP

    private JSONObject http(String method, String path, JSONObject body) throws Exception {
        SharedPreferences p = getSharedPreferences("xiaoxin", MODE_PRIVATE);
        String b = p.getString("base", "https://47.116.13.227:8183/xiaoxin/");
        HttpURLConnection c = (HttpURLConnection) new URL((b.endsWith("/") ? b : b + "/") + path).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(8000);
        c.setReadTimeout(65000);
        c.setRequestProperty("Authorization", "Bearer " + p.getString("key", ""));
        if (body != null) {
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream o = c.getOutputStream()) {
                o.write(body.toString().getBytes("UTF-8"));
            }
        }
        try {
            if (c.getResponseCode() != 200) {
                throw new java.io.IOException("HTTP " + c.getResponseCode());
            }
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] b2 = new byte[8192];
                for (int n; (n = in.read(b2)) > 0; ) {
                    buf.write(b2, 0, n);
                }
                String s = buf.toString("UTF-8").trim();
                return s.isEmpty() ? new JSONObject() : new JSONObject(s);
            }
        } finally {
            c.disconnect();
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }
}
