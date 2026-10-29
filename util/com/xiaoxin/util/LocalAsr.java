package com.xiaoxin.util;

import android.content.Context;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * pad 本机的 Paraformer 中文小模型（sherpa-onnx，CPU 2 线程），只用来听唤醒词，不上网。
 *
 * 2026-10-30 在 TB335FC 上实测：加载 2.9 秒，常驻内存约 185 MB；2 秒一句话识别 0.05 秒。
 * 模型打在 APK 的 assets/asr/ 里，第一次用时拷到 app 私有目录（onnxruntime 要文件路径），之后直接读。
 */
public final class LocalAsr {
    static {
        System.loadLibrary("onnxruntime");
        System.loadLibrary("sherpa-onnx-c-api");
        System.loadLibrary("xiaoxin-wake");
    }

    private static native long nativeCreate(String model, String tokens, int threads);

    private static native String nativeDecode(long handle, byte[] pcm, int length);

    private static native void nativeDestroy(long handle);

    private long handle;

    /** 很慢（3 秒左右），在后台线程调。 */
    public static LocalAsr load(Context context) throws IOException {
        File dir = new File(context.getFilesDir(), "asr");
        File model = copyAsset(context, "asr/model.int8.onnx", new File(dir, "model.int8.onnx"));
        File tokens = copyAsset(context, "asr/tokens.txt", new File(dir, "tokens.txt"));
        LocalAsr asr = new LocalAsr();
        asr.handle = nativeCreate(model.getAbsolutePath(), tokens.getAbsolutePath(), 2);
        if (asr.handle == 0) {
            throw new IOException("端侧识别模型加载失败");
        }
        return asr;
    }

    /** s16le 16 kHz 单声道。没认出东西返回空串。 */
    public synchronized String decode(byte[] pcm, int length) {
        if (handle == 0) {
            return "";
        }
        String text = nativeDecode(handle, pcm, length);
        return text == null ? "" : text.trim();
    }

    public synchronized void release() {
        if (handle != 0) {
            nativeDestroy(handle);
            handle = 0;
        }
    }

    /** assets 里的文件拷到私有目录；大小一样就当已经拷过（换模型时文件大小会变）。 */
    private static File copyAsset(Context context, String name, File dst) throws IOException {
        try (InputStream in = context.getAssets().open(name)) {
            long size = context.getAssets().openFd(name).getLength();
            if (dst.isFile() && dst.length() == size) {
                return dst;
            }
        } catch (IOException e) {
            // 压缩存的 asset 拿不到长度，下面照常拷
        }
        dst.getParentFile().mkdirs();
        File tmp = new File(dst.getPath() + ".tmp");
        try (InputStream in = context.getAssets().open(name); OutputStream out = new FileOutputStream(tmp)) {
            byte[] buf = new byte[1 << 16];
            for (int n; (n = in.read(buf)) > 0; ) {
                out.write(buf, 0, n);
            }
        }
        if (!tmp.renameTo(dst)) {
            throw new IOException("拷不了 " + dst);
        }
        return dst;
    }
}
