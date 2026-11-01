package com.xiaoxin.app;

import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * 家里打印机的最小 IPP 客户端：mDNS 找打印机（_ipp._tcp），Get-Printer-Attributes 看状态，Print-Job 发一页 JPEG。
 * 不走系统打印框（PrintManager 一定会弹预览、要人点），小朋友说一句就出纸。
 *
 * 打印机：HP DeskJet 4800（mDNS 名「HP DeskJet 4800 series [039FA9]」），吃 image/jpeg，不吃 PDF；
 * print-color-mode 支持 monochrome / color，中枢那边黑白页已经转成灰度，这里再告诉打印机用 monochrome（只用黑墨）。
 * IPP 是明文 HTTP 在家里局域网：打印机自带的 ipps 是自签证书，内容只是作业纸，局域网内不必加密。
 */
final class Printer {
    private static final String TAG = "XxPrint";
    private static final String CACHE = "printer_url";

    static final class Status {
        final int state;           // 3 idle, 4 printing, 5 stopped
        final String reasons;      // media-empty、door-open、offline…（none 是正常）
        final String inks;         // 「彩色 80% 黑色 100%」

        Status(int state, String reasons, String inks) {
            this.state = state;
            this.reasons = reasons;
            this.inks = inks;
        }

        /** 不能打的原因，用孩子听得懂的话；能打返回 null。 */
        String problem() {
            if (reasons.contains("media-empty") || reasons.contains("media-needed")) return "打印机没纸了，请放几张 A4 纸";
            if (reasons.contains("media-jam")) return "打印机卡纸了，请爸爸妈妈看看";
            if (reasons.contains("door-open") || reasons.contains("cover-open")) return "打印机的盖子开着，合上再打";
            if (reasons.contains("marker-supply-empty")) return "打印机没墨了";
            if (reasons.contains("offline") || state == 5) return "打印机停住了，看看是不是没开机";
            return null;
        }
    }

    private final Context ctx;
    private String url;  // http://192.168.2.13:631/ipp/print

    Printer(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.url = ctx.getSharedPreferences("xiaoxin", Context.MODE_PRIVATE).getString(CACHE, null);
    }

    /** 找打印机：先试上次的地址，不通再 mDNS 找一遍（打印机换了 IP）。找不到返回 null。 */
    String find() {
        if (url != null && status(url) != null) {
            return url;
        }
        String found = discover(4000);
        if (found != null) {
            url = found;
            ctx.getSharedPreferences("xiaoxin", Context.MODE_PRIVATE).edit().putString(CACHE, found).apply();
        }
        return found;
    }

    private String discover(long timeoutMs) {
        NsdManager nsd = (NsdManager) ctx.getSystemService(Context.NSD_SERVICE);
        WifiManager wifi = (WifiManager) ctx.getSystemService(Context.WIFI_SERVICE);
        WifiManager.MulticastLock lock = wifi.createMulticastLock("xiaoxin-print");
        lock.acquire();
        final String[] hit = {null};
        CountDownLatch done = new CountDownLatch(1);
        NsdManager.DiscoveryListener dl = new NsdManager.DiscoveryListener() {
            @Override public void onStartDiscoveryFailed(String t, int e) { done.countDown(); }
            @Override public void onStopDiscoveryFailed(String t, int e) { }
            @Override public void onDiscoveryStarted(String t) { }
            @Override public void onDiscoveryStopped(String t) { }
            @Override public void onServiceLost(NsdServiceInfo s) { }
            @Override public void onServiceFound(NsdServiceInfo s) {
                nsd.resolveService(s, new NsdManager.ResolveListener() {
                    @Override public void onResolveFailed(NsdServiceInfo s, int e) { }
                    @Override public void onServiceResolved(NsdServiceInfo s) {
                        byte[] rp = s.getAttributes().get("rp");
                        String path = rp == null ? "ipp/print" : new String(rp, StandardCharsets.UTF_8);
                        String u = "http://" + s.getHost().getHostAddress() + ":" + s.getPort() + "/" + path;
                        synchronized (hit) {
                            if (hit[0] == null) {
                                hit[0] = u;
                                done.countDown();
                            }
                        }
                    }
                });
            }
        };
        try {
            nsd.discoverServices("_ipp._tcp", NsdManager.PROTOCOL_DNS_SD, dl);
            done.await(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            Log.w(TAG, "discover: " + e);
        } finally {
            try { nsd.stopServiceDiscovery(dl); } catch (Exception ignored) { }
            lock.release();
        }
        Log.i(TAG, "discovered " + hit[0]);
        return hit[0];
    }

    // ---------------------------------------------------------------- IPP 编码

    private static void attr(DataOutputStream o, int tag, String name, byte[] value) throws IOException {
        byte[] n = name.getBytes(StandardCharsets.UTF_8);
        o.writeByte(tag);
        o.writeShort(n.length);
        o.write(n);
        o.writeShort(value.length);
        o.write(value);
    }

    private static void attr(DataOutputStream o, int tag, String name, String value) throws IOException {
        attr(o, tag, name, value.getBytes(StandardCharsets.UTF_8));
    }

    private static byte[] intBytes(int v) {
        return new byte[] {(byte) (v >> 24), (byte) (v >> 16), (byte) (v >> 8), (byte) v};
    }

    private static byte[] request(int op, String uri, List<Object[]> extra) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        DataOutputStream o = new DataOutputStream(buf);
        o.writeShort(0x0200);   // IPP 2.0
        o.writeShort(op);
        o.writeInt(1);
        o.writeByte(0x01);      // operation-attributes
        attr(o, 0x47, "attributes-charset", "utf-8");
        attr(o, 0x48, "attributes-natural-language", "zh-cn");
        attr(o, 0x45, "printer-uri", uri.replaceFirst("^http", "ipp"));
        attr(o, 0x42, "requesting-user-name", "xiaoxin");
        for (Object[] a : extra) {
            if (a[0] instanceof Integer && (Integer) a[0] == 0x02) {
                o.writeByte(0x02);  // job-attributes 开始
                continue;
            }
            int tag = (Integer) a[0];
            if (a[2] instanceof Integer) {
                attr(o, tag, (String) a[1], intBytes((Integer) a[2]));
            } else {
                attr(o, tag, (String) a[1], (String) a[2]);
            }
        }
        o.writeByte(0x03);      // end
        return buf.toByteArray();
    }

    /** 发请求，返回 [status-code, 属性（名字 → 值，多值用逗号连）]。 */
    private static Object[] post(String url, byte[] head, byte[] doc) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(4000);
        c.setReadTimeout(60000);
        c.setDoOutput(true);
        c.setRequestMethod("POST");
        c.setRequestProperty("Content-Type", "application/ipp");
        c.setFixedLengthStreamingMode(head.length + (doc == null ? 0 : doc.length));
        try (OutputStream out = c.getOutputStream()) {
            out.write(head);
            if (doc != null) {
                out.write(doc);
            }
        }
        byte[] r;
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] t = new byte[8192];
            int n;
            while ((n = in.read(t)) > 0) {
                b.write(t, 0, n);
            }
            r = b.toByteArray();
        } finally {
            c.disconnect();
        }
        int status = ((r[2] & 0xff) << 8) | (r[3] & 0xff);
        java.util.Map<String, String> attrs = new java.util.HashMap<>();
        int i = 8;
        String cur = null;
        while (i < r.length) {
            int tag = r[i++] & 0xff;
            if (tag < 0x10) {
                if (tag == 0x03) break;
                continue;
            }
            int nl = ((r[i] & 0xff) << 8) | (r[i + 1] & 0xff);
            i += 2;
            String name = new String(r, i, nl, StandardCharsets.UTF_8);
            i += nl;
            int vl = ((r[i] & 0xff) << 8) | (r[i + 1] & 0xff);
            i += 2;
            String val;
            if ((tag == 0x21 || tag == 0x23) && vl == 4) {
                val = String.valueOf(((r[i] & 0xff) << 24) | ((r[i + 1] & 0xff) << 16) | ((r[i + 2] & 0xff) << 8) | (r[i + 3] & 0xff));
            } else {
                val = new String(r, i, vl, StandardCharsets.UTF_8);
            }
            i += vl;
            if (!name.isEmpty()) {
                cur = name;
                attrs.put(cur, val);
            } else if (cur != null) {
                attrs.put(cur, attrs.get(cur) + "," + val);
            }
        }
        return new Object[] {status, attrs};
    }

    // ---------------------------------------------------------------- 操作

    Status status(String u) {
        try {
            List<Object[]> ex = new ArrayList<>();
            ex.add(new Object[] {0x44, "requested-attributes", "printer-state"});
            ex.add(new Object[] {0x44, "", "printer-state-reasons"});
            ex.add(new Object[] {0x44, "", "marker-names"});
            ex.add(new Object[] {0x44, "", "marker-levels"});
            Object[] r = post(u, request(0x000B, u, ex), null);
            @SuppressWarnings("unchecked") java.util.Map<String, String> a = (java.util.Map<String, String>) r[1];
            int st = Integer.parseInt(a.getOrDefault("printer-state", "3"));
            String inks = a.getOrDefault("marker-names", "") + " " + a.getOrDefault("marker-levels", "");
            return new Status(st, a.getOrDefault("printer-state-reasons", "none"), inks.trim());
        } catch (Exception e) {
            Log.w(TAG, "status " + u + ": " + e);
            return null;
        }
    }

    /** 发一页（JPEG）。返回 null 表示收下了；否则是出错原因（给孩子听的）。 */
    String printJpeg(byte[] jpeg, boolean color, int copies, String jobName) {
        String u = find();
        if (u == null) {
            return "找不到家里的打印机，看看打印机开机了没有，pad 是不是连着家里的 Wi‑Fi";
        }
        Status s = status(u);
        if (s == null) {
            return "连不上打印机";
        }
        String why = s.problem();
        if (why != null) {
            return why;
        }
        try {
            List<Object[]> ex = new ArrayList<>();
            ex.add(new Object[] {0x42, "job-name", jobName.length() > 60 ? jobName.substring(0, 60) : jobName});
            ex.add(new Object[] {0x49, "document-format", "image/jpeg"});
            ex.add(new Object[] {0x02});
            ex.add(new Object[] {0x21, "copies", Math.max(1, Math.min(copies, 5))});
            ex.add(new Object[] {0x44, "print-color-mode", color ? "color" : "monochrome"});
            ex.add(new Object[] {0x44, "media", "iso_a4_210x297mm"});
            ex.add(new Object[] {0x44, "print-scaling", "fit"});
            Object[] r = post(u, request(0x0002, u, ex), jpeg);
            int st = (Integer) r[0];
            if (st > 0x00FF) {  // 0x0000–0x00FF 成功
                Log.w(TAG, "print-job status 0x" + Integer.toHexString(st) + " " + r[1]);
                return "打印机没收下（0x" + Integer.toHexString(st) + "）";
            }
            return null;
        } catch (Exception e) {
            Log.w(TAG, "print-job: " + e);
            url = null;  // 下次重新找
            return "发给打印机的时候断了：" + e.getMessage();
        }
    }
}
