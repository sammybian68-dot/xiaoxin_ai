package com.xiaoxin.parent;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.os.Bundle;
import android.text.TextUtils;
import android.view.Gravity;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import java.io.InputStream;
import java.security.MessageDigest;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 小新家长：WebView 套网页版（同一个家庭线程），竖屏手机布局由网页按宽度自己排。
 * 和 pad 上的 TalkActivity 一样：自签证书按指纹放行、麦克风和摄像头只给自己的页面；没有原生音频和闹钟。
 * 配置和 pad 一样用 adb 传一次：--es key "$K"（以后换成扫码配对，见 docs/architecture.md 6.4）。
 * 调试：--es page "calltest?room=x" 登录后打开站内某一页。
 */
public class MainActivity extends Activity {
    private static final String DEFAULT_BASE = "https://47.116.13.227:8183/xiaoxin/";

    private SharedPreferences prefs;
    private WebView web;
    private byte[] pinnedSha256;
    private ValueCallback<Uri[]> pendingFiles;
    private PermissionRequest pendingMedia;  // 网页要摄像头/麦克风时系统权限还没给：先弹系统框，回来再答复网页

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("xiaoxin-parent", MODE_PRIVATE);
        takeConfig(getIntent());
        getWindow().setStatusBarColor(0xFF0F1115);
        getWindow().setNavigationBarColor(0xFF0F1115);
        pinnedSha256 = loadPin();
        // 摄像头、麦克风不在启动时要：网页真要用（视频、语音留言）时在 onPermissionRequest 里按需要。
        // 启动时先弹一个，网页紧接着再要会把前一个顶掉，网页拿到的是 denied（2026-10-31 实测）
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
        // 网页按这个认出自己在家长 App 里（以后按角色排界面）
        s.setUserAgentString(s.getUserAgentString() + " XiaoxinParent/0.1");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.addJavascriptInterface(new Bridge(), "XiaoxinParent");  // 不叫 XiaoxinNative：网页看到那个就当成 pad，走原生自由对话
        setContentView(web);
        // 有 token 就用它登（服务端种 cookie 再跳回去）；没有就直接打开，网页显示配对码输入框
        web.loadUrl(TextUtils.isEmpty(key()) ? base() : base() + "?k=" + Uri.encode(key()));
        openPage(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (takeConfig(intent)) {
            recreate();
            return;
        }
        openPage(intent);
    }

    /** 返回键先问网页：开着大图就关、在群里就回聊天列表（window.xiaoxinBack 返回 true 表示处理了）；网页不管才退出。 */
    @Override
    public void onBackPressed() {
        if (web == null) {
            super.onBackPressed();
            return;
        }
        web.evaluateJavascript("window.xiaoxinBack ? String(window.xiaoxinBack()) : 'false'", r -> {
            if ("\"true\"".equals(r)) {
                return;
            }
            if (web.canGoBack()) {
                web.goBack();
            } else {
                finish();
            }
        });
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();  // 关掉摄像头和麦克风
        }
        super.onDestroy();
    }

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

    /** --es page "calltest?room=x"：只认站内相对路径，等 ?k= 那一跳种好 cookie 再开。 */
    private void openPage(Intent intent) {
        String page = intent == null ? null : intent.getStringExtra("page");
        if (web == null || TextUtils.isEmpty(page) || page.contains("://") || page.startsWith("/") || page.contains("..")) {
            return;
        }
        intent.removeExtra("page");
        web.postDelayed(() -> web.loadUrl(base() + page), 1500);
    }

    private String key() {
        return prefs.getString("key", "");
    }

    private String base() {
        String b = prefs.getString("base", DEFAULT_BASE);
        return b.endsWith("/") ? b : b + "/";
    }

    private boolean ours(Uri u) {
        return u.toString().startsWith(base());
    }

    /** 网页登录页配对成功后把设备 token 交过来存着；以后打开直接用它登。 */
    private final class Bridge {
        @android.webkit.JavascriptInterface
        public void setDeviceToken(String token) {
            if (token != null && token.startsWith("xd_")) {
                prefs.edit().putString("key", token).apply();
            }
        }
    }

    // ---------------------------------------------------------------- WebView

    private final class Client extends WebViewClient {
        /** 自签证书：只放行和 res/raw/gpu_cert.pem 指纹一样的。 */
        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            if (matchesPin(error.getCertificate())) {
                handler.proceed();
            } else {
                handler.cancel();
            }
        }

        /** 站外链接交给系统浏览器。 */
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            if (ours(request.getUrl())) {
                return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
            } catch (Exception ignored) {
                // 没有浏览器就算了
            }
            return true;
        }
    }

    private final class Chrome extends WebChromeClient {
        /** 麦克风、摄像头：只给自己的页面，只给这两样。 */
        @Override
        public void onPermissionRequest(PermissionRequest request) {
            runOnUiThread(() -> {
                if (!request.getOrigin().toString().startsWith(base().replaceAll("/xiaoxin/$", ""))) {
                    request.deny();
                    return;
                }
                List<String> ok = new ArrayList<>();
                for (String r : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r) || PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                        ok.add(r);
                    }
                }
                if (ok.isEmpty()) {
                    request.deny();
                    return;
                }
                List<String> need = new ArrayList<>();
                if (ok.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)
                        && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                    need.add(Manifest.permission.CAMERA);
                }
                if (ok.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                        && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    need.add(Manifest.permission.RECORD_AUDIO);
                }
                if (need.isEmpty()) {
                    request.grant(ok.toArray(new String[0]));
                    return;
                }
                // 不先要系统权限就 grant，网页的 getUserMedia 会一直卡着不回（2026-10-31 实测停在「准备中」）
                if (pendingMedia != null) {
                    pendingMedia.deny();
                }
                pendingMedia = request;
                requestPermissions(need.toArray(new String[0]), 3);
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
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != 3 || pendingMedia == null) {
            return;
        }
        PermissionRequest r = pendingMedia;
        pendingMedia = null;
        List<String> ok = new ArrayList<>();
        for (String res : r.getResources()) {
            if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(res)
                    && checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                ok.add(res);
            } else if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(res)
                    && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                ok.add(res);
            }
        }
        if (ok.isEmpty()) {
            r.deny();  // 网页那边 getUserMedia 会报 NotAllowedError，测试页显示「拿不到摄像头/麦克风」
        } else {
            r.grant(ok.toArray(new String[0]));
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
