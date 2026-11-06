package com.xiaoxin.parent;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.function.Supplier;

/**
 * 翻唱「发到微信」的视频：下到 cache/share-video/&lt;服务端的名字&gt;.mp4（先写 .part，下完改名），同一段只下一次。
 * 网页选好一段就 prepare 先下着（手机经公网下载只有 0.2～0.8 MB/s，整首 10 MB 要半分钟）；点「发到微信」时 get 等它下完、
 * 边下边报进度，再 toGallery 复制进相册 Movies/小新（同名同大小的已经在相册里就用那个）。缓存只留最近 KEEP 个。
 * 和 android/xiaoxin 的 com.xiaoxin.app.VideoShare 一样，改一边记得改另一边。
 */
final class VideoShare {
    interface Progress {
        void on(int pct);
    }

    private static final int KEEP = 6;
    private static final String GALLERY = Environment.DIRECTORY_MOVIES + "/小新";

    private static final class Job {
        int pct = -1;
        int waiters;              // 有人在 get 里等着发：不能停
        volatile boolean cancel;  // 换了选段，先下的这个没人要了
        boolean done;
        File file;
        Exception err;
    }

    private final File dir;
    private final Supplier<String> key;
    private final Map<String, Job> jobs = new HashMap<>();

    VideoShare(File dir, Supplier<String> key) {
        this.dir = dir;
        this.key = key;
    }

    /** 先下着；别的还在先下、没人等着发的停掉（带宽小，两个一起下谁都慢）。 */
    void prepare(String url) throws IOException {
        Job mine = start(url);
        synchronized (jobs) {
            for (Job j : jobs.values()) {
                if (j != mine) {
                    synchronized (j) {
                        if (!j.done && j.waiters == 0) {
                            j.cancel = true;
                        }
                    }
                }
            }
        }
    }

    /** 等这段下完（下着的就接着等），每变一个百分点报一次。 */
    File get(String url, Progress p) throws Exception {
        Job j;
        synchronized (jobs) {
            j = start(url);
            synchronized (j) {
                j.waiters++;
                j.cancel = false;
            }
        }
        int last = -2;
        synchronized (j) {
            try {
                while (!j.done) {
                    if (j.pct != last && j.pct >= 0) {
                        last = j.pct;
                        p.on(last);
                    }
                    j.wait(400);
                }
            } finally {
                j.waiters--;
            }
            if (j.err != null) {
                throw j.err;
            }
            return j.file;
        }
    }

    private Job start(String url) throws IOException {
        String path = new URL(url).getPath();
        String name = path.substring(path.lastIndexOf('/') + 1);
        if (!name.matches("[A-Za-z0-9_-]{6,80}\\.mp4")) {
            throw new IOException("不是小新的视频");
        }
        File f = new File(dir, name);
        synchronized (jobs) {
            Job j = jobs.get(name);
            if (j != null) {
                synchronized (j) {
                    if (!j.done || (j.err == null && j.file.isFile())) {
                        return j;
                    }
                }
            }
            j = new Job();
            jobs.put(name, j);
            if (f.isFile()) {
                j.done = true;
                j.file = f;
                f.setLastModified(System.currentTimeMillis());
                return j;
            }
            final Job job = j;
            new Thread(() -> download(url, f, job), "share-video-dl").start();
            return j;
        }
    }

    private void download(String url, File f, Job j) {
        File part = new File(dir, f.getName() + ".part");
        Exception err = null;
        try {
            dir.mkdirs();
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestProperty("Authorization", "Bearer " + key.get());
            c.setConnectTimeout(8000);
            c.setReadTimeout(60000);
            if (c.getResponseCode() != 200) {
                throw new IOException("HTTP " + c.getResponseCode());
            }
            long total = c.getContentLengthLong(), got = 0;
            try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part)) {
                byte[] buf = new byte[65536];
                int n;
                while ((n = in.read(buf)) > 0) {
                    if (j.cancel) {
                        c.disconnect();
                        throw new IOException("换了一段，不下了");
                    }
                    out.write(buf, 0, n);
                    got += n;
                    if (total > 0) {
                        int pct = (int) Math.min(99, got * 100 / total);
                        synchronized (j) {
                            if (pct != j.pct) {
                                j.pct = pct;
                                j.notifyAll();
                            }
                        }
                    }
                }
            }
            if (total > 0 && got != total) {
                throw new IOException("只下了 " + got + "/" + total);
            }
            if (!part.renameTo(f)) {
                throw new IOException("改名失败");
            }
            prune();
        } catch (Exception e) {
            android.util.Log.w("XxShare", "video " + url + ": " + e);
            part.delete();
            err = e;
        }
        synchronized (j) {
            j.done = true;
            j.err = err;
            j.file = err == null ? f : null;
            j.pct = 100;
            j.notifyAll();
        }
    }

    private void prune() {
        File[] all = dir.listFiles((d, n) -> n.endsWith(".mp4"));
        if (all == null || all.length <= KEEP) {
            return;
        }
        Arrays.sort(all, (a, b) -> Long.compare(b.lastModified(), a.lastModified()));
        for (int i = KEEP; i < all.length; i++) {
            all[i].delete();
        }
    }

    /** 放进相册 Movies/小新：同名同大小的已经有了（这一段发过）就用那个，不再存一份。 */
    static Uri toGallery(ContentResolver cr, File f, String displayName) throws IOException {
        String fn = (displayName == null || displayName.isEmpty() ? "xiaoxin.mp4" : displayName.replaceAll("[\\\\/:*?\"<>|]", "_"));
        Uri coll = MediaStore.Video.Media.EXTERNAL_CONTENT_URI;
        try (Cursor q = cr.query(coll, new String[]{MediaStore.Video.Media._ID},
                MediaStore.Video.Media.DISPLAY_NAME + "=? AND " + MediaStore.Video.Media.SIZE + "=? AND "
                        + MediaStore.Video.Media.RELATIVE_PATH + " LIKE ?",
                new String[]{fn, String.valueOf(f.length()), GALLERY + "%"}, null)) {
            if (q != null && q.moveToFirst()) {
                return android.content.ContentUris.withAppendedId(coll, q.getLong(0));
            }
        } catch (RuntimeException e) {
            android.util.Log.i("XxShare", "gallery query: " + e);
        }
        ContentValues v = new ContentValues();
        v.put(MediaStore.Video.Media.DISPLAY_NAME, fn);
        v.put(MediaStore.Video.Media.MIME_TYPE, "video/mp4");
        v.put(MediaStore.Video.Media.RELATIVE_PATH, GALLERY);
        v.put(MediaStore.Video.Media.IS_PENDING, 1);
        Uri dst = cr.insert(coll, v);
        if (dst == null) {
            throw new IOException("相册存不进去");
        }
        try (InputStream in = new FileInputStream(f); OutputStream out = cr.openOutputStream(dst)) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        } catch (IOException | RuntimeException e) {
            cr.delete(dst, null, null);
            throw e;
        }
        v.clear();
        v.put(MediaStore.Video.Media.IS_PENDING, 0);
        cr.update(dst, v, null, null);
        return dst;
    }
}
