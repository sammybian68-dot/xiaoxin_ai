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

import com.xiaoxin.util.Talk;

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
        stopTalk();  // 切到后台就挂断：麦克风和通话模式不该一直开着
        if (web != null) {
            web.evaluateJavascript("window.xiaoxinTalkEvent && window.xiaoxinTalkEvent("
                    + "{type:'native_closed',reason:'切到后台，挂断了'})", null);
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
            String url = pageUrl.replaceFirst("^http", "ws");
            runOnUiThread(() -> {
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 1);
                    emit("{\"type\":\"native_closed\",\"reason\":\"要先允许麦克风\"}");
                    return;
                }
                stopTalk();
                talk = new Talk(TalkActivity.this, new Talk.Listener() {
                    @Override
                    public void onEvent(JSONObject event) {
                        emit(event.toString());
                    }

                    @Override
                    public void onLevel(int level) {
                        emit("{\"type\":\"native_level\",\"level\":" + level + "}");
                    }

                    @Override
                    public void onClosed(String reason) {
                        emit("{\"type\":\"native_closed\",\"reason\":" + JSONObject.quote(reason) + "}");
                    }
                });
                Talk t = talk;
                new Thread(() -> {
                    try {
                        t.start(url, key());
                    } catch (Exception e) {
                        emit("{\"type\":\"native_closed\",\"reason\":"
                                + JSONObject.quote("连不上：" + e.getMessage()) + "}");
                    }
                }, "talk-connect").start();
            });
        }

        @JavascriptInterface
        public void stop() {
            runOnUiThread(TalkActivity.this::stopTalk);
        }
    }

    private void emit(String json) {
        runOnUiThread(() -> {
            if (web != null) {
                web.evaluateJavascript("window.xiaoxinTalkEvent && window.xiaoxinTalkEvent(" + json + ")", null);
            }
        });
    }

    private void stopTalk() {
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
