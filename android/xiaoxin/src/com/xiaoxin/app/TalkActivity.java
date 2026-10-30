package com.xiaoxin.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.net.Uri;
import android.os.Bundle;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import com.xiaoxin.util.Cues;
import com.xiaoxin.util.Earcon;
import com.xiaoxin.util.Keywords;
import com.xiaoxin.util.LocalAsr;
import com.xiaoxin.util.Talk;
import com.xiaoxin.util.WakeListener;

import org.json.JSONObject;

import java.io.InputStream;
import java.security.MessageDigest;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Arrays;

/**
 * 小新：一个 WebView 套着网页版（和电脑上同一套界面、同一套设置），只有自由对话的音频交给原生。
 *
 * 为什么音频要原生：网页里的 getUserMedia 在 WebView 里走不到 pad 的系统 AEC，小新自己的声音会被当成用户说话。
 * 原生用 VOICE_COMMUNICATION + 通话播放（com.xiaoxin.util.Talk），实测回声剩 2 dB。网页检测到 window.XiaoxinNative
 * 就把自由对话交给它，事件通过 window.xiaoxinTalkEvent 回给网页画界面。
 *
 * 地址和 key 存 SharedPreferences，不打进 APK，用 adb 配一次（见 android/xiaoxin/README.md）。
 * 页面用 IP 打开：gpu.cmbian.top 没备案，带域名的 TLS 会被掐掉。
 */
public class TalkActivity extends Activity {
    private static final String DEFAULT_BASE = "https://47.116.13.227:8183/xiaoxin/";

    private SharedPreferences prefs;
    private WebView web;
    private Talk talk;
    // 待命唤醒：本机模型认出唤醒词才连服务端。standbyOn 是用户开没开；woken 表示这通电话是唤醒起来的，挂断后回待命
    private LocalAsr localAsr;
    private WakeListener wake;
    private volatile boolean standbyOn;
    private volatile boolean woken;
    private String[] wakeWords = {"小新同学", "你好小新"};
    private String[] exitWords = {"小新退下", "没事了小新"};
    private volatile int vadEndMs = 800;
    private volatile String hangupTone = "down";
    private Cues cues;
    private volatile boolean wakeAcked;
    private volatile long handoffCueMs;
    private String talkUrl = "";
    private long lastActivity;
    private final android.os.Handler idle = new android.os.Handler(android.os.Looper.getMainLooper());
    private byte[] pinnedSha256;
    private ValueCallback<Uri[]> pendingFiles;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("xiaoxin", MODE_PRIVATE);
        takeConfig(getIntent());
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(0xFF0F1115);
        getWindow().setNavigationBarColor(0xFF0F1115);
        pinnedSha256 = loadPin();
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 1);
        }
        if (TextUtils.isEmpty(key())) {
            TextView hint = new TextView(this);
            hint.setText("还没配置 key。\n在电脑上运行一次 android/xiaoxin/README.md 里的 adb 命令。");
            hint.setTextColor(0xFFE8EAEE);
            hint.setTextSize(22);
            hint.setGravity(Gravity.CENTER);
            hint.setBackgroundColor(0xFF0F1115);
            setContentView(hint);
            return;
        }

        // debug 包允许用 chrome://inspect 调网页（打包签的是 debug.keystore）
        WebView.setWebContentsDebuggingEnabled(
                (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0f1115"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        web.addJavascriptInterface(new Bridge(), "XiaoxinNative");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        setContentView(web);
        // 免密链接：?k= 让服务端种 cookie 再跳回去，key 不留在地址栏
        web.loadUrl(base() + "?k=" + Uri.encode(key()));
        cues = new Cues(this, base(), key());
        // 闹钟和倒计时：前台服务，熄屏也在，到点亮屏响铃
        ClockService.listener = ev -> emitClock(ev.toString());
        ClockService.start(this);
        showRing(getIntent());
    }

    /** 闹钟响了被拉起来（可能在锁屏上）：亮屏、盖住锁屏、退出熄屏，网页画响铃页。 */
    private void showRing(Intent intent) {
        String ring = intent == null ? null : intent.getStringExtra("ring");
        if (ring == null) {
            return;
        }
        intent.removeExtra("ring");
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        leaveNight();
        emitClock(ring);
    }

    private void emitClock(String json) {
        runOnUiThread(() -> {
            if (web != null) {
                web.evaluateJavascript("window.xiaoxinClockEvent && window.xiaoxinClockEvent(" + json + ")", null);
            }
        });
    }

    // ---------------------------------------------------------------- 熄屏
    // 有「设备管理器」权限就真的锁屏关屏（adb shell dpm set-active-admin com.xiaoxin.app/.Admin 开一次）；
    // 没有就退一步：背光调到最低、盖一层全黑（pad 插着电时系统设置了不自动灭屏）。闹钟由 ClockService 照样响。

    private android.view.View nightCover;

    private void goNight() {
        android.app.admin.DevicePolicyManager dpm = getSystemService(android.app.admin.DevicePolicyManager.class);
        android.content.ComponentName admin = new android.content.ComponentName(this, Admin.class);
        if (dpm.isAdminActive(admin)) {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            dpm.lockNow();
            return;
        }
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = 0f;
        getWindow().setAttributes(lp);
        if (nightCover == null) {
            nightCover = new android.view.View(this);
            nightCover.setBackgroundColor(Color.BLACK);
            nightCover.setOnClickListener(v -> leaveNight());
            addContentView(nightCover, new android.view.ViewGroup.LayoutParams(-1, -1));
        }
        nightCover.setVisibility(android.view.View.VISIBLE);
    }

    private void leaveNight() {
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(lp);
        if (nightCover != null) {
            nightCover.setVisibility(android.view.View.GONE);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (takeConfig(intent)) {
            recreate();
            return;
        }
        showRing(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 白天回到前台：亮屏常亮；锁屏上显示只在响铃时需要
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    /** adb 传进来的配置存下来；有变化返回 true。 */
    private boolean takeConfig(Intent intent) {
        SharedPreferences.Editor e = prefs.edit();
        boolean changed = false;
        if (intent.hasExtra("key")) {
            e.putString("key", intent.getStringExtra("key"));
            changed = true;
        }
        if (intent.hasExtra("url")) {
            e.putString("base", intent.getStringExtra("url"));
            changed = true;
        }
        e.apply();
        return changed;
    }

    private String key() {
        return prefs.getString("key", "");
    }

    private String base() {
        String b = prefs.getString("base", DEFAULT_BASE);
        return b.endsWith("/") ? b : b + "/";
    }

    @Override
    protected void onStop() {
        super.onStop();
        // 切到后台就挂断、停待命：麦克风和通话模式不该在后台一直开着（熄屏待命要做成前台服务，还没做）
        boolean wasStandby = standbyOn;
        stopTalk();
        if (debugListener != null) {
            stopDebug();
        }
        WakeListener w = wake;
        wake = null;
        if (w != null) {
            new Thread(w::stop, "wake-stop").start();
        }
        standbyOn = wasStandby;  // 回到前台时网页会重新开待命
        if (web != null) {
            web.evaluateJavascript("window.xiaoxinTalkEvent && window.xiaoxinTalkEvent("
                    + "{type:'native_closed',reason:'切到后台，挂断了'})", null);
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (standbyOn) {
            startStandby();
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        stopTalk();
        standbyOn = false;
        if (wake != null) {
            wake.stop();
        }
        if (localAsr != null) {
            localAsr.release();
        }
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }

    // ---------------------------------------------------------------- 给网页用的原生接口

    private final class Bridge {
        /** 网页点「自由对话」：wsUrl 是 https://…/api/talk?… 的页面地址，这里换成 wss 自己连。 */
        @JavascriptInterface
        public void start(String pageUrl) {
            runOnUiThread(() -> openTalk(pageUrl, null, 0));
        }

        /**
         * 网页的「待命」开关。wakes / exits 是逗号连起来的列表（命中任意一个），idleS 唤醒后多久没动静挂断，
         * endMs 说完判定；talkUrl 是唤醒后要连的 /api/talk 地址（带对话 id、模型）。
         */
        @JavascriptInterface
        public void standby(boolean on, String wakes, String exits, String pageUrl, int idleS, int endMs, String tone) {
            runOnUiThread(() -> {
                applyWords(wakes, exits, idleS, endMs, tone);
                talkUrl = pageUrl;
                standbyOn = on;
                if (on) {
                    startStandby();
                } else {
                    stopStandby();
                }
            });
        }

        /** 设置里改了唤醒词 / 退出词 / 超时，马上生效。 */
        @JavascriptInterface
        public void setWords(String wakes, String exits, int idleS, int endMs, String tone) {
            runOnUiThread(() -> applyWords(wakes, exits, idleS, endMs, tone));
        }

        /** 设置页试听提示音。 */
        @JavascriptInterface
        public void playTone(String id) {
            Earcon.play(id);
        }

        /** 网页告诉原生当前音色：把这个音色的「在」「拜拜」拉下来存好（换音色后再唤醒，就是新音色的声音）。 */
        @JavascriptInterface
        public void setVoice(String voiceId) {
            refreshCues(voiceId);
        }

        /** 设置页「端侧模型调试」：开 / 关。开着时待命先停，结束后恢复。 */
        @JavascriptInterface
        public void debugAsr(boolean on) {
            runOnUiThread(() -> {
                if (on) {
                    startDebug();
                } else {
                    stopDebug();
                }
            });
        }

        /** 设置页试听「在」「拜拜」。 */
        @JavascriptInterface
        public void playCue(String name) {
            if (cues != null) {
                cues.play(name);
            }
        }

        /** 网页发给服务端的控制消息（比如通话中换模型）。 */
        @JavascriptInterface
        public void send(String json) {
            Talk t = talk;
            if (t != null) {
                new Thread(() -> t.sendText(json), "talk-send").start();
            }
        }

        @JavascriptInterface
        public void stop() {
            runOnUiThread(() -> hangUp("挂断了"));
        }

        /** 网页响铃页点了「关掉 / 再睡」：网页已经调了接口，这里先把铃声停了，不等服务端回来。 */
        @JavascriptInterface
        public void clockStop() {
            ClockService s = ClockService.running;
            if (s != null) {
                new android.os.Handler(android.os.Looper.getMainLooper()).post(() -> s.stopRinging(false));
            }
            runOnUiThread(() -> setShowWhenLocked(false));
        }

        /** 闹钟页「熄屏睡觉」。 */
        @JavascriptInterface
        public void screenOff() {
            runOnUiThread(TalkActivity.this::goNight);
        }
    }

    private String cueVoice = "";

    /** 后台拉当前音色的「在」「拜拜」；同一个音色只拉一次，已经存在本机的不重下。 */
    private void refreshCues(String voiceId) {
        String v = voiceId == null ? "" : voiceId;
        if (cues == null) {
            cues = new Cues(this, base(), key());
        }
        if (v.equals(cueVoice)) {
            return;
        }
        if (!cueVoice.isEmpty()) {
            ClockService.clearSpeech(this);  // 换了音色，响铃念的话也换
        }
        cueVoice = v;
        Cues c = cues;
        new Thread(() -> c.refresh(v), "cues-refresh").start();
    }

    private void applyWords(String wakes, String exits, int idleS, int endMs, String tone) {
        if (tone != null && !tone.isEmpty()) {
            hangupTone = tone;
        }
        if (wakes != null && !wakes.isEmpty()) {
            wakeWords = Keywords.split(wakes);
        }
        if (exits != null && !exits.isEmpty()) {
            exitWords = Keywords.split(exits);
            Talk t = talk;
            if (t != null) {
                t.setExitWords(exitWords);
            }
        }
        if (idleS > 0) {
            wakeIdleS = idleS;
        }
        if (endMs > 0) {
            vadEndMs = endMs;
        }
        WakeListener w = wake;
        if (w != null) {
            w.setWakeWords(wakeWords);
            w.setEndMs(vadEndMs);
        }
        WakeListener d = debugListener;
        if (d != null) {
            d.setWakeWords(wakeWords);
            d.setEndMs(vadEndMs);
        }
    }

    /**
     * 挂断，所有路径都走这里（说了退出词、超时、点挂断、服务端说 bye、连接断了）：
     * 同一刻停掉麦克风、播放和连接（不再拾音和不再出声同步发生），放「挂断了」提示音，网页切回待命样子，再回去待命。
     */
    private static final int BY_USER = 0;     // 说了退出词 / 点了挂断：小新说「拜拜」
    private static final int BY_TIMEOUT = 1;  // 太久没说话：超时提示音
    private static final int BY_ERROR = 2;    // 连接断了、连不上：不出声

    private void hangUp(String reason) {
        hangUp(reason, BY_USER);
    }

    private void hangUp(String reason, int why) {
        boolean wasTalking = talk != null;
        idle.removeCallbacks(idleCheck);
        Talk t = talk;
        talk = null;
        woken = false;
        if (t != null) {
            t.stopNow();  // 同步：麦克风和播放器在这一行之后都已经停了
            new Thread(t::stop, "talk-stop").start();  // 关连接可以慢慢来
        }
        long playMs = 0;
        if (wasTalking) {
            if (why == BY_USER) {
                playMs = cues == null ? 0 : cues.play(Cues.BYE);
            } else if (why == BY_TIMEOUT) {
                Earcon.play(hangupTone);
                playMs = 400;
            }
            emit("{\"type\":\"native_closed\",\"reason\":" + JSONObject.quote(reason) + "}");
        }
        // 「拜拜」/ 提示音放完再开待命的麦克风，免得把自己的声音听进去
        idle.postDelayed(this::afterTalk, wasTalking ? playMs + 250 : 0);
    }

    private void emit(String json) {
        runOnUiThread(() -> {
            if (web != null) {
                web.evaluateJavascript("window.xiaoxinTalkEvent && window.xiaoxinTalkEvent(" + json + ")", null);
            }
        });
    }

    /**
     * 开一通电话。wakeText 不空时是唤醒起来的：连上先把这句发过去（one-shot）。
     * cueMs > 0：「我在的」正在放，这么久之后才开通话音频；握手和它同时进行。
     */
    private void openTalk(String pageUrl, String wakeText, long cueMs) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 1);
            emit("{\"type\":\"native_closed\",\"reason\":\"要先允许麦克风\"}");
            return;
        }
        if (localAsr == null) {
            // 手动点「自由对话」也要本机听退出词：模型没加载就先加载（3 秒），加载完再开
            emit("{\"type\":\"standby\",\"state\":\"loading\"}");
            new Thread(() -> {
                try {
                    localAsr = LocalAsr.load(this);
                } catch (Exception e) {
                    emit("{\"type\":\"standby\",\"state\":\"error\",\"detail\":"
                            + JSONObject.quote("本机识别加载失败，退出词只能靠云端：" + e.getMessage()) + "}");
                }
                runOnUiThread(() -> openTalkNow(pageUrl, wakeText, cueMs));
            }, "asr-load").start();
            return;
        }
        openTalkNow(pageUrl, wakeText, cueMs);
    }

    private void openTalkNow(String pageUrl, String wakeText, long cueMs) {
        String url = pageUrl.replaceFirst("^http", "ws");
        stopTalk();
        // 待命在听的话先松开麦克风，不然和这通电话抢同一个麦克风（手动点「自由对话」时会这样）
        WakeListener w = wake;
        wake = null;
        WakeListener.Mic mic = null;
        if (w != null) {
            w.stop();
            mic = wakeText != null ? w.takeMic() : null;
            if (mic == null) {
                w.dropMic();
            }
        }
        woken = wakeText != null;
        xiaoxinBusy = woken;  // 唤醒带着问题来的：小新马上要答，答完才开始计时
        lastShown = -1;
        lastActivity = System.currentTimeMillis();
        Talk[] self = new Talk[1];
        talk = new Talk(TalkActivity.this, new Talk.Listener() {
            @Override
            public void onEvent(JSONObject event) {
                String type = event.optString("type");
                noteActivity(type);
                emit(event.toString());
                String tool = event.optString("name");
                if ("tool".equals(type) && event.optString("result").startsWith("已")
                        && ("alarm_ring".equals(tool) || "timer_control".equals(tool) || "alarm_delete".equals(tool))) {
                    // 通话里用语音关了闹钟 / 倒计时：铃声马上停，别等长轮询，也别和小新的回答叠在一起
                    ClockService s = ClockService.running;
                    if (s != null) {
                        idle.post(() -> s.stopRinging(false));
                    }
                }
                if ("bye".equals(type)) {
                    // 服务端也认出了退出词（浏览器那条路径用；pad 上本机通常已经先挂了）
                    runOnUiThread(() -> { if (talk == self[0]) hangUp("退下了，要用再叫我"); });
                }
            }

            @Override
            public void onLevel(int level) {
                emit("{\"type\":\"native_level\",\"level\":" + level + "}");
            }

            @Override
            public void onClosed(String reason) {
                runOnUiThread(() -> { if (talk == self[0]) hangUp(reason, BY_ERROR); });
            }
        });
        Talk t = talk;
        self[0] = t;
        if (mic != null) {
            t.adoptMic(mic, handoffCueMs);
        }
        // 本机听退出词：和唤醒词同一个模型、同一套比对，命中就在这台 pad 上立刻挂断
        if (localAsr != null) {
            t.watchExit(localAsr, exitWords, vadEndMs,
                    () -> runOnUiThread(() -> { if (talk == t) hangUp("退下了，要用再叫我"); }));
        }
        new Thread(() -> {
            try {
                if (wakeText != null) {
                    t.startWoken(url, key(), wakeText, wakeAcked, cueMs);
                } else {
                    t.start(url, key());
                }
            } catch (Exception e) {
                runOnUiThread(() -> { if (talk == t) hangUp("连不上：" + e.getMessage(), BY_ERROR); });
            }
        }, "talk-connect").start();
        // 唤醒的和手动开的同一套超时、同一套退出（用户要求两边逻辑完全一样）
        idle.removeCallbacks(idleCheck);
        idle.postDelayed(idleCheck, 2000);
    }

    /**
     * 唤醒起来的电话：wakeIdleS 秒没动静（没人开口、小新也不在说）就挂断，回去待命。
     * 「动静」= 用户开口（listening）或小新出声（speaking / 音频还在放）；小新念完（idle）才开始计时。
     * 最后 COUNTDOWN_S 秒每秒发一次 wake_countdown {left}，网页显示倒计时；有人开口就清零。
     */
    private static final int COUNTDOWN_S = 10;
    private volatile int wakeIdleS = 30;
    private volatile boolean xiaoxinBusy;  // 小新在想或在说：这时不算没动静
    private int lastShown = -1;
    private final Runnable idleCheck = new Runnable() {
        @Override
        public void run() {
            if (talk == null) {
                return;
            }
            long quietMs = xiaoxinBusy ? 0 : System.currentTimeMillis() - lastActivity;
            int left = (int) Math.ceil((wakeIdleS * 1000L - quietMs) / 1000.0);
            if (left <= 0) {
                emit("{\"type\":\"wake_countdown\",\"left\":-1}");
                hangUp(wakeIdleS + " 秒没说话，挂断了", BY_TIMEOUT);
                return;
            }
            int show = left <= COUNTDOWN_S ? left : -1;
            if (show != lastShown) {
                lastShown = show;
                emit("{\"type\":\"wake_countdown\",\"left\":" + show + "}");  // -1 = 不显示
            }
            idle.postDelayed(this, 250);
        }
    };

    /** 服务端事件里哪些算「有动静」。 */
    private void noteActivity(String type) {
        switch (type) {
            case "listening":
            case "heard":
            case "interrupt":
                xiaoxinBusy = true;  // 用户开口了，接下来小新要想要说，计时暂停
                lastActivity = System.currentTimeMillis();
                break;
            case "speaking":
                xiaoxinBusy = true;
                break;
            case "idle":
            case "error":
                xiaoxinBusy = false;  // 小新说完了：从现在开始算没动静
                lastActivity = System.currentTimeMillis();
                break;
            default:
                break;
        }
    }

    /** 电话挂了：开着待命就回去待命。 */
    private void afterTalk() {
        if (standbyOn && talk == null) {
            startStandby();
        }
    }

    private void startStandby() {
        if (!standbyOn || talk != null || debugListener != null) {
            return;  // 通话中、调试中都不开待命
        }
        if (wake != null && wake.isRunning()) {
            return;
        }
        emit("{\"type\":\"standby\",\"state\":\"loading\"}");
        new Thread(() -> {
            try {
                if (localAsr == null) {
                    localAsr = LocalAsr.load(this);  // 第一次 3 秒左右
                }
            } catch (Exception e) {
                emit("{\"type\":\"standby\",\"state\":\"error\",\"detail\":"
                        + JSONObject.quote("端侧识别加载失败：" + e.getMessage()) + "}");
                return;
            }
            runOnUiThread(() -> {
                if (!standbyOn || talk != null) {
                    return;
                }
                wake = new WakeListener(this, localAsr, wakeWords, new WakeListener.Listener() {
                    @Override
                    public void onWake(String text) {
                        emit("{\"type\":\"standby\",\"state\":\"woken\",\"text\":" + JSONObject.quote(text) + "}");
                        // 只喊了唤醒词：用本机缓存的当前音色说「我在的」。one-shot（唤醒词后面带着问题）不说，直接答。
                        // 马上开始连服务端，和这句同时进行；放完（再留 80 ms 尾音）才开通话的麦克风和播放器，放完就能接着说
                        boolean oneShot = Keywords.restAfterWake(text, wakeWords) > 0;
                        long ms = oneShot || cues == null ? 0 : cues.play(Cues.ZAI);
                        wakeAcked = ms > 0;
                        // 麦克风不关：待命那条录音直接交给这通电话，唤醒词说完后的每一帧都送上去。
                        // 「我在的」放的时候那几帧当静音送（不然会把小新自己的声音当成用户说话）
                        handoffCueMs = ms > 0 ? ms + 80 : 0;
                        openTalk(talkUrl, text, handoffCueMs);
                    }

                    @Override
                    public void onHeard(String text, long millis, long audioMs) {
                        emit("{\"type\":\"standby\",\"state\":\"heard\",\"text\":" + JSONObject.quote(text)
                                + ",\"ms\":" + millis + "}");
                    }

                    @Override
                    public void onError(String message) {
                        // 麦克风一时被别的 app 占着之类：告诉网页，3 秒后再试，待命不悄悄失效
                        emit("{\"type\":\"standby\",\"state\":\"error\",\"detail\":"
                                + JSONObject.quote(message + "，3 秒后再试") + "}");
                        runOnUiThread(() -> {
                            wake = null;
                            idle.postDelayed(TalkActivity.this::startStandby, 3000);
                        });
                    }
                });
                wake.setEndMs(vadEndMs);
                wake.start();
                emit("{\"type\":\"standby\",\"state\":\"listening\",\"wake_word\":" + JSONObject.quote(wakeWords[0])
                        + ",\"wake_words\":" + new org.json.JSONArray(java.util.Arrays.asList(wakeWords)) + "}");
            });
        }, "wake-load").start();
    }

    // ---------------------------------------------------------------- 端侧模型调试
    // 和待命同一个模型、同一套断句（停多久算说完跟主设置），每句话报出：本机认成什么、算不算唤醒、算不算退出、
    // 识别耗时。只听不连云端、不唤醒。调试期间待命停着，关掉后恢复。

    private WakeListener debugListener;

    private void startDebug() {
        stopTalk();
        WakeListener w = wake;
        wake = null;
        if (w != null) {
            w.stop();
        }
        emit("{\"type\":\"asr_debug\",\"state\":\"loading\"}");
        new Thread(() -> {
            try {
                if (localAsr == null) {
                    localAsr = LocalAsr.load(this);
                }
            } catch (Exception e) {
                emit("{\"type\":\"asr_debug\",\"state\":\"error\",\"detail\":" + JSONObject.quote(e.getMessage()) + "}");
                return;
            }
            runOnUiThread(() -> {
                if (debugListener != null) {
                    debugListener.stop();
                }
                debugListener = new WakeListener(this, localAsr, wakeWords, new WakeListener.Listener() {
                    @Override
                    public void onWake(String text) {}

                    @Override
                    public void onHeard(String text, long millis, long audioMs) {
                        int rest = Keywords.restAfterWake(text, wakeWords);
                        try {
                            JSONObject o = new JSONObject().put("type", "asr_debug").put("state", "heard")
                                    .put("text", text).put("ms", millis).put("audio_ms", audioMs)
                                    .put("wake", rest >= 0).put("one_shot", rest > 0)
                                    .put("exit", Keywords.equalsAny(text, exitWords));
                            emit(o.toString());
                        } catch (Exception ignored) {
                            // 拼 JSON 不会失败
                        }
                    }

                    @Override
                    public void onError(String message) {
                        emit("{\"type\":\"asr_debug\",\"state\":\"error\",\"detail\":" + JSONObject.quote(message) + "}");
                    }
                });
                debugListener.setEndMs(vadEndMs);
                debugListener.setDebug(true);
                debugListener.start();
                emit("{\"type\":\"asr_debug\",\"state\":\"listening\",\"end_ms\":" + vadEndMs + "}");
            });
        }, "asr-debug-load").start();
    }

    private void stopDebug() {
        WakeListener d = debugListener;
        debugListener = null;
        if (d != null) {
            new Thread(() -> {
                d.stop();
                runOnUiThread(this::afterTalk);  // 开着待命的话恢复待命
            }, "asr-debug-stop").start();
        }
        emit("{\"type\":\"asr_debug\",\"state\":\"off\"}");
    }

    private void stopStandby() {
        WakeListener w = wake;
        wake = null;
        if (w != null) {
            new Thread(w::stop, "wake-stop").start();
        }
        emit("{\"type\":\"standby\",\"state\":\"off\"}");
    }

    /** 不放提示音、不回待命地停掉（切后台、开新电话前用）。 */
    private void stopTalk() {
        idle.removeCallbacks(idleCheck);
        Talk t = talk;
        talk = null;
        woken = false;
        if (t != null) {
            t.stopNow();
            new Thread(t::stop, "talk-stop").start();
        }
    }

    // ---------------------------------------------------------------- WebView

    private final class Client extends WebViewClient {
        /** 自签证书：只放行和 APK 里那张 res/raw/gpu_cert.pem 指纹完全一样的，别的一律拒绝。 */
        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            if (matchesPin(error.getCertificate())) {
                handler.proceed();
            } else {
                handler.cancel();
            }
        }

        /** 站外链接（搜索出处）交给系统浏览器，别在小新里打开。 */
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (u.toString().startsWith(base())) {
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (Exception ignored) {
                // 没有浏览器就算了
            }
            return true;
        }
    }

    private final class Chrome extends WebChromeClient {
        /** 网页里的麦克风（按住说话、录声音）：只给自己的页面。 */
        @Override
        public void onPermissionRequest(PermissionRequest request) {
            runOnUiThread(() -> {
                if (request.getOrigin().toString().startsWith(base().replaceAll("/xiaoxin/$", ""))) {
                    request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                } else {
                    request.deny();
                }
            });
        }

        /** 网页里的「＋ 图片 / 文件」。 */
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (pendingFiles != null) {
                pendingFiles.onReceiveValue(null);
            }
            pendingFiles = callback;
            try {
                startActivityForResult(params.createIntent(), 2);
            } catch (Exception e) {
                pendingFiles = null;
                return false;
            }
            return true;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == 2 && pendingFiles != null) {
            pendingFiles.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            pendingFiles = null;
        }
    }

    // ---------------------------------------------------------------- 证书指纹

    private byte[] loadPin() {
        try (InputStream in = getResources().openRawResource(R.raw.gpu_cert)) {
            X509Certificate c = (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(in);
            return MessageDigest.getInstance("SHA-256").digest(c.getEncoded());
        } catch (Exception e) {
            return null;
        }
    }

    private boolean matchesPin(SslCertificate cert) {
        if (pinnedSha256 == null || cert == null) {
            return false;
        }
        X509Certificate x = cert.getX509Certificate();
        if (x == null) {
            return false;
        }
        try {
            return Arrays.equals(pinnedSha256, MessageDigest.getInstance("SHA-256").digest(x.getEncoded()));
        } catch (Exception e) {
            return false;
        }
    }
}
