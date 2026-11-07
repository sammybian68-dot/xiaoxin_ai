package com.xiaoxin.parent;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * 网页「拍照」：系统相机把照片写到这里给的地址（cache/camera/&lt;名&gt;.jpg），拍完网页从同一个地址读去上传。
 * 没有 androidx 的 FileProvider（build.sh 只用 android.jar），自己写个最小的：只认 cache/camera 下的文件名。
 * 和 android/xiaoxin 的 com.xiaoxin.app.CameraFiles 一样，改一边记得改另一边。
 */
public final class CameraFiles extends ContentProvider {
    static final String DIR = "camera";

    static String authority(android.content.Context c) {
        return c.getPackageName() + ".camera";
    }

    /** 新拍一张的地址；顺手清掉以前拍的（网页早就传完了）。 */
    static Uri newShot(android.content.Context c) {
        File d = new File(c.getCacheDir(), DIR);
        d.mkdirs();
        File[] old = d.listFiles();
        if (old != null) {
            for (File f : old) {
                f.delete();
            }
        }
        String name = "拍照_" + new java.text.SimpleDateFormat("MMdd_HHmmss", java.util.Locale.CHINA).format(new java.util.Date()) + ".jpg";
        return new Uri.Builder().scheme("content").authority(authority(c)).appendPath(name).build();
    }

    static File file(android.content.Context c, Uri uri) throws FileNotFoundException {
        String name = uri.getLastPathSegment();
        if (name == null || name.contains("/") || name.startsWith(".")) {
            throw new FileNotFoundException(String.valueOf(uri));
        }
        return new File(new File(c.getCacheDir(), DIR), name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = file(getContext(), uri);
        int m = mode.contains("w")
                ? ParcelFileDescriptor.MODE_READ_WRITE | ParcelFileDescriptor.MODE_CREATE | ParcelFileDescriptor.MODE_TRUNCATE
                : ParcelFileDescriptor.MODE_READ_ONLY;
        return ParcelFileDescriptor.open(f, m);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        File f;
        try {
            f = file(getContext(), uri);
        } catch (FileNotFoundException e) {
            return null;
        }
        String[] cols = projection != null ? projection : new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        MatrixCursor c = new MatrixCursor(cols, 1);
        Object[] row = new Object[cols.length];
        for (int i = 0; i < cols.length; i++) {
            if (OpenableColumns.DISPLAY_NAME.equals(cols[i])) {
                row[i] = f.getName();
            } else if (OpenableColumns.SIZE.equals(cols[i])) {
                row[i] = f.length();
            }
        }
        c.addRow(row);
        return c;
    }

    @Override
    public String getType(Uri uri) {
        return "image/jpeg";
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] args) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] args) {
        return 0;
    }
}
