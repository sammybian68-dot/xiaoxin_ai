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

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import org.json.JSONArray;
import org.json.JSONObject;
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
        takeShare(getIntent());
        openPage(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (takeConfig(intent)) {
            recreate();
            return;
        }
        setIntent(intent);
        if (takeShare(intent)) {
            web.evaluateJavascript("window.xiaoxinShareArrived && window.xiaoxinShareArrived()", null);
            return;
        }
        openPage(intent);
    }

    // ---------------------------------------------------------------- 微信等分享进来的（docs/architecture.md 8.1）
    // 分享来的是 content:// URI，只在这一次 intent 里有读权限：马上拷到私有缓存 share/，再告诉网页「有东西要发」，
    // 网页通过 bridge 一个个取回来变成 File，走和「相册选图」一样的上传（本地压图、原图可选）。
    // 发完或取消，网页调 shareDone() 清掉。缓存只留最近一次分享的。

    private static final long SHARE_MAX = 200L * 1024 * 1024;
    private final JSONArray shared = new JSONArray();
    private String sharedText = "";

    private boolean takeShare(Intent intent) {
        if (intent == null) {
            return false;
        }
        String act = intent.getAction();
        List<Uri> uris = new ArrayList<>();
        if (Intent.ACTION_SEND.equals(act)) {
            Uri u = intent.getParcelableExtra(Intent.EXTRA_STREAM);
            if (u != null) {
                uris.add(u);
            }
        } else if (Intent.ACTION_SEND_MULTIPLE.equals(act)) {
            ArrayList<Uri> list = intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
            if (list != null) {
                uris.addAll(list);
            }
        } else if (Intent.ACTION_VIEW.equals(act) && intent.getData() != null && "content".equals(intent.getData().getScheme())) {
            uris.add(intent.getData());
        } else {
            return false;
        }
        CharSequence text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
        File dir = new File(getCacheDir(), "share");
        deleteAll(dir);
        dir.mkdirs();
        synchronized (shared) {
            while (shared.length() > 0) {
                shared.remove(0);
            }
            sharedText = text == null ? "" : text.toString();
            int i = 0;
            for (Uri u : uris) {
                if (i >= 9) {
                    break;  // 一次最多 9 个，和微信一样
                }
                try {
                    String name = displayName(u);
                    String mime = getContentResolver().getType(u);
                    File out = new File(dir, "f" + i);
                    long n = copy(u, out);
                    if (n <= 0 || n > SHARE_MAX) {
                        out.delete();
                        continue;
                    }
                    JSONObject o = new JSONObject();
                    o.put("id", "f" + i);
                    o.put("name", name);
                    o.put("mime", mime == null ? "application/octet-stream" : mime);
                    o.put("size", n);
                    shared.put(o);
                    i++;
                } catch (Exception ignored) {
                    // 读不了的跳过（对方没给读权限、文件没了）
                }
            }
        }
        intent.setAction(Intent.ACTION_MAIN);  // 转屏、回到前台时别再处理一遍
        return shared.length() > 0 || !sharedText.isEmpty();
    }

    private String displayName(Uri u) {
        try (android.database.Cursor c = getContentResolver().query(u, new String[] {android.provider.OpenableColumns.DISPLAY_NAME},
                null, null, null)) {
            if (c != null && c.moveToFirst() && !c.isNull(0)) {
                return c.getString(0);
            }
        } catch (Exception ignored) {
            // 有的 provider 不给名字
        }
        String last = u.getLastPathSegment();
        return last == null ? "file" : last;
    }

    private long copy(Uri u, File out) throws Exception {
        long n = 0;
        try (InputStream in = getContentResolver().openInputStream(u); OutputStream o = new FileOutputStream(out)) {
            if (in == null) {
                return 0;
            }
            byte[] buf = new byte[64 * 1024];
            int r;
            while ((r = in.read(buf)) > 0) {
                n += r;
                if (n > SHARE_MAX) {
                    return n;
                }
                o.write(buf, 0, r);
            }
        }
        return n;
    }

    private static void deleteAll(File dir) {
        File[] fs = dir.listFiles();
        if (fs != null) {
            for (File f : fs) {
                f.delete();
            }
        }
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

        /** 分享进来的东西：{text, files:[{id,name,mime,size}]}，没有返回 null。 */
        @android.webkit.JavascriptInterface
        public String sharePending() {
            synchronized (shared) {
                if (shared.length() == 0 && sharedText.isEmpty()) {
                    return null;
                }
                try {
                    return new JSONObject().put("text", sharedText).put("files", shared).toString();
                } catch (Exception e) {
                    return null;
                }
            }
        }

        /** 第 off 字节起最多 len 字节，base64（bridge 只能传字符串；网页按 1 MB 一段拼回 File）。 */
        @android.webkit.JavascriptInterface
        public String shareChunk(String id, long off, int len) {
            if (id == null || !id.matches("f[0-8]")) {
                return "";
            }
            File f = new File(new File(getCacheDir(), "share"), id);
            try (java.io.RandomAccessFile r = new java.io.RandomAccessFile(f, "r")) {
                r.seek(off);
                byte[] buf = new byte[(int) Math.max(0, Math.min(len, Math.min(4 * 1024 * 1024, r.length() - off)))];
                r.readFully(buf);
                return android.util.Base64.encodeToString(buf, android.util.Base64.NO_WRAP);
            } catch (Exception e) {
                return "";
            }
        }

        /** 发完或取消：清掉缓存。 */
        @android.webkit.JavascriptInterface
        public void shareDone() {
            synchronized (shared) {
                while (shared.length() > 0) {
                    shared.remove(0);
                }
                sharedText = "";
            }
            deleteAll(new File(getCacheDir(), "share"));
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

    /**
     * 选好的文件。系统相册（Android 13+ 的照片选择器）多选时把结果放在 ClipData 里、getData() 是空的，
     * FileChooserParams.parseResult 只看 getData，网页就拿到 null——选完回来什么都没有（2026-11-01 vivo 实测）。
     */
    private static Uri[] pickedUris(int resultCode, Intent data) {
        if (resultCode != RESULT_OK || data == null) {
            return null;
        }
        android.content.ClipData clip = data.getClipData();
        if (clip != null && clip.getItemCount() > 0) {
            Uri[] out = new Uri[clip.getItemCount()];
            for (int i = 0; i < out.length; i++) {
                out[i] = clip.getItemAt(i).getUri();
            }
            return out;
        }
        return data.getData() != null ? new Uri[] {data.getData()} : null;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == 2 && pendingFiles != null) {
            pendingFiles.onReceiveValue(pickedUris(resultCode, data));
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
