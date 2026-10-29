package com.xiaoxin.util;

import android.graphics.Bitmap;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.media.Image;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;

public final class Yuv {
    private Yuv() {}

    public static byte[] toNv21(Image image) {
        int w = image.getWidth();
        int h = image.getHeight();
        byte[] nv21 = new byte[w * h * 3 / 2];
        Image.Plane yPlane = image.getPlanes()[0];
        Image.Plane uPlane = image.getPlanes()[1];
        Image.Plane vPlane = image.getPlanes()[2];
        ByteBuffer yBuf = yPlane.getBuffer();
        int pos = 0;
        for (int row = 0; row < h; row++) {
            yBuf.position(row * yPlane.getRowStride());
            yBuf.get(nv21, pos, w);
            pos += w;
        }
        ByteBuffer uBuf = uPlane.getBuffer();
        ByteBuffer vBuf = vPlane.getBuffer();
        int chromaH = h / 2;
        int chromaW = w / 2;
        for (int row = 0; row < chromaH; row++) {
            for (int col = 0; col < chromaW; col++) {
                nv21[pos++] = vBuf.get(row * vPlane.getRowStride() + col * vPlane.getPixelStride());
                nv21[pos++] = uBuf.get(row * uPlane.getRowStride() + col * uPlane.getPixelStride());
            }
        }
        return nv21;
    }

    public static byte[] toJpeg(byte[] nv21, int width, int height, int quality) {
        YuvImage yuv = new YuvImage(nv21, ImageFormat.NV21, width, height, null);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        yuv.compressToJpeg(new Rect(0, 0, width, height), quality, out);
        return out.toByteArray();
    }

    public static Bitmap rotate(Bitmap src, int degrees) {
        int turn = ((degrees % 360) + 360) % 360;
        if (turn == 0 || src == null) {
            return src;
        }
        Matrix matrix = new Matrix();
        matrix.postRotate(turn);
        Bitmap out = Bitmap.createBitmap(src, 0, 0, src.getWidth(), src.getHeight(), matrix, true);
        if (out != src) {
            src.recycle();
        }
        return out;
    }
}
