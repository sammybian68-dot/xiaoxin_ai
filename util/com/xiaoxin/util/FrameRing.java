package com.xiaoxin.util;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/** Keeps at most {@link #CAPACITY} JPEGs. Older slots are overwritten, never accumulated. */
public final class FrameRing {
    public static final int CAPACITY = 30;

    private final File dir;
    private int next;

    public FrameRing(File dir) {
        this.dir = dir;
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IllegalStateException("cannot create " + dir);
        }
        next = readNext();
        deleteExtras();
    }

    public int stored() {
        int n = 0;
        for (int i = 0; i < CAPACITY; i++) {
            if (file(i).exists()) {
                n++;
            }
        }
        return n;
    }

    public File addJpeg(byte[] jpeg) throws IOException {
        File dest = file(next);
        write(dest, jpeg);
        next = (next + 1) % CAPACITY;
        write(new File(dir, "ring.txt"), Integer.toString(next).getBytes(StandardCharsets.US_ASCII));
        deleteExtras();
        return dest;
    }

    /** One rolling NV21, not part of the 30-image ring. */
    public void writeLatestNv21(byte[] nv21) throws IOException {
        write(new File(dir, "latest.nv21"), nv21);
    }

    private File file(int index) {
        return new File(dir, String.format(Locale.US, "frame_%02d.jpg", index));
    }

    private int readNext() {
        File marker = new File(dir, "ring.txt");
        if (!marker.exists()) {
            return 0;
        }
        try {
            byte[] raw = java.nio.file.Files.readAllBytes(marker.toPath());
            int value = Integer.parseInt(new String(raw, StandardCharsets.US_ASCII).trim());
            if (value < 0 || value >= CAPACITY) {
                return 0;
            }
            return value;
        } catch (Exception ignored) {
            return 0;
        }
    }

    private void deleteExtras() {
        File[] files = dir.listFiles();
        if (files == null) {
            return;
        }
        for (File file : files) {
            String name = file.getName();
            if (name.startsWith("frame_") && name.endsWith(".jpg")) {
                String mid = name.substring("frame_".length(), name.length() - ".jpg".length());
                try {
                    int index = Integer.parseInt(mid);
                    if (index < 0 || index >= CAPACITY) {
                        file.delete();
                    }
                } catch (NumberFormatException e) {
                    file.delete();
                }
            }
        }
    }

    private static void write(File file, byte[] data) throws IOException {
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(data);
        }
    }
}
