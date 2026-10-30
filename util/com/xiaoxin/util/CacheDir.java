package com.xiaoxin.util;

import java.io.File;

/**
 * 本机语音缓存的老化：每个文件用一次就把修改时间刷成现在（当作 last_hit），
 * 超过 MAX_IDLE_DAYS 天没被用过的删掉。用在描红短语（phrases）、闹钟响铃语（ring）、唤醒/退出提示（cues）。
 * 内容都能随时从服务端重新合成，删了下次用到再下，所以放心删。
 */
public final class CacheDir {
    public static final int MAX_IDLE_DAYS = 30;
    private static final long DAY_MS = 24L * 3600 * 1000;

    private CacheDir() {}

    /** 命中一次：刷新 last_hit。 */
    public static void touch(File f) {
        if (f != null && f.isFile()) {
            f.setLastModified(System.currentTimeMillis());
        }
    }

    /** 删掉 dir 里超过 MAX_IDLE_DAYS 天没被用过的文件（含没下完的 .tmp）。返回删了几个。 */
    public static int prune(File dir) {
        File[] fs = dir == null ? null : dir.listFiles();
        if (fs == null) {
            return 0;
        }
        long cut = System.currentTimeMillis() - MAX_IDLE_DAYS * DAY_MS;
        int n = 0;
        for (File f : fs) {
            if (f.isFile() && f.lastModified() < cut && f.delete()) {
                n++;
            }
        }
        return n;
    }
}
