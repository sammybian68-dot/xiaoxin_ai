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
    private String wakeWord = "小新同学";
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
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (takeConfig(intent)) {
            recreate();
        }
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
            runOnUiThread(() -> openTalk(pageUrl, null));
        }

        /** 网页的「待命」开关。wakeWord 从设置里来；talkUrl 是唤醒后要连的 /api/talk 地址（带对话 id、模型）。 */
        @JavascriptInterface
        public void standby(boolean on, String word, String pageUrl) {
            runOnUiThread(() -> {
                wakeWord = word == null || word.isEmpty() ? wakeWord : word;
                talkUrl = pageUrl;
                standbyOn = on;
                if (on) {
                    startStandby();
                } else {
                    stopStandby();
                }
            });
        }

        @JavascriptInterface
        public void setWakeWord(String word) {
            wakeWord = word;
            WakeListener w = wake;
            if (w != null) {
                w.setWakeWord(word);
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
            runOnUiThread(() -> {
                stopTalk();
                afterTalk();  // 开着待命就回去待命
            });
        }
    }

    private void emit(String json) {
        runOnUiThread(() -> {
            if (web != null) {
                web.evaluateJavascript("window.xiaoxinTalkEvent && window.xiaoxinTalkEvent(" + json + ")", null);
            }
        });
    }

    /** 开一通电话。wakeText 不空时是唤醒起来的：连上先把这句发过去（one-shot）。 */
    private void openTalk(String pageUrl, String wakeText) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 1);
            emit("{\"type\":\"native_closed\",\"reason\":\"要先允许麦克风\"}");
            return;
        }
        String url = pageUrl.replaceFirst("^http", "ws");
        stopTalk();
        woken = wakeText != null;
        lastActivity = System.currentTimeMillis();
        talk = new Talk(TalkActivity.this, new Talk.Listener() {
            @Override
            public void onEvent(JSONObject event) {
                String type = event.optString("type");
                if (!"native_play".equals(type)) {
                    lastActivity = System.currentTimeMillis();
                }
                emit(event.toString());
            }

            @Override
            public void onLevel(int level) {
                emit("{\"type\":\"native_level\",\"level\":" + level + "}");
            }

            @Override
            public void onClosed(String reason) {
                emit("{\"type\":\"native_closed\",\"reason\":" + JSONObject.quote(reason) + "}");
                runOnUiThread(TalkActivity.this::afterTalk);
            }
        });
        Talk t = talk;
        new Thread(() -> {
            try {
                if (wakeText != null) {
                    t.startWoken(url, key(), wakeText);
                } else {
                    t.start(url, key());
                }
            } catch (Exception e) {
                emit("{\"type\":\"native_closed\",\"reason\":" + JSONObject.quote("连不上：" + e.getMessage()) + "}");
                runOnUiThread(TalkActivity.this::afterTalk);
            }
        }, "talk-connect").start();
        if (woken) {
            idle.removeCallbacks(idleCheck);
            idle.postDelayed(idleCheck, 2000);
        }
    }

    /** 唤醒起来的电话：WAKE_IDLE_S 秒没动静（没人说、小新也不在说）就挂断，回去待命。 */
    private static final long WAKE_IDLE_MS = 20_000;
    private final Runnable idleCheck = new Runnable() {
        @Override
        public void run() {
            if (talk == null || !woken) {
                return;
            }
            if (System.currentTimeMillis() - lastActivity >= WAKE_IDLE_MS) {
                emit("{\"type\":\"native_closed\",\"reason\":\"一会儿没说话，回去待命了\"}");
                stopTalk();
                afterTalk();
            } else {
                idle.postDelayed(this, 1000);
            }
        }
    };

    /** 电话挂了：开着待命就回去待命。 */
    private void afterTalk() {
        woken = false;
        if (standbyOn && talk == null) {
            startStandby();
        }
    }

    private void startStandby() {
        if (!standbyOn || talk != null) {
            return;
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
                wake = new WakeListener(this, localAsr, wakeWord, new WakeListener.Listener() {
                    @Override
                    public void onWake(String text) {
                        emit("{\"type\":\"standby\",\"state\":\"woken\",\"text\":" + JSONObject.quote(text) + "}");
                        runOnUiThread(() -> openTalk(talkUrl, text));
                    }

                    @Override
                    public void onHeard(String text, long millis) {
                        emit("{\"type\":\"standby\",\"state\":\"heard\",\"text\":" + JSONObject.quote(text)
                                + ",\"ms\":" + millis + "}");
                    }

                    @Override
                    public void onError(String message) {
                        emit("{\"type\":\"standby\",\"state\":\"error\",\"detail\":" + JSONObject.quote(message) + "}");
                    }
                });
                wake.start();
                emit("{\"type\":\"standby\",\"state\":\"listening\",\"wake_word\":" + JSONObject.quote(wakeWord) + "}");
            });
        }, "wake-load").start();
    }

    private void stopStandby() {
        WakeListener w = wake;
        wake = null;
        if (w != null) {
            new Thread(w::stop, "wake-stop").start();
        }
        emit("{\"type\":\"standby\",\"state\":\"off\"}");
    }

    private void stopTalk() {
        idle.removeCallbacks(idleCheck);
        Talk t = talk;
        talk = null;
        if (t != null) {
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
