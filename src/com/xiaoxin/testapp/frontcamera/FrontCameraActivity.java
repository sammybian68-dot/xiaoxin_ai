package com.xiaoxin.testapp.frontcamera;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.Gravity;
import android.view.Surface;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.xiaoxin.testapp.TestBar;
import com.xiaoxin.util.FrameRing;
import com.xiaoxin.util.Yuv;

import java.io.File;
import java.util.Collections;

public class FrontCameraActivity extends Activity {
    private static final String TAG = "FrontCam";
    private static final long INTERVAL_MS = 1000;

    private ImageView preview;
    private TextView status;
    private HandlerThread thread;
    private Handler handler;
    private ImageReader reader;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private FrameRing ring;
    private Bitmap shown;
    private String cameraId = "";
    private int width;
    private int height;
    private int jpegOrientation;
    private int taken;
    private boolean running;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.BLACK);
        root.addView(TestBar.create(this));
        status = new TextView(this);
        status.setTextColor(Color.WHITE);
        status.setTextSize(22);
        status.setPadding(32, 16, 32, 8);
        status.setText("等待相机权限");
        root.addView(status);
        FrameLayout stage = new FrameLayout(this);
        preview = new ImageView(this);
        preview.setScaleType(ImageView.ScaleType.FIT_CENTER);
        preview.setBackgroundColor(0xFF222222);
        FrameLayout.LayoutParams previewParams = new FrameLayout.LayoutParams(800, 450);
        previewParams.gravity = Gravity.CENTER;
        stage.addView(preview, previewParams);
        root.addView(stage, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);
        ring = new FrameRing(getFilesDir());
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, 1);
            return;
        }
        startCamera();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            startCamera();
        } else {
            status.setText("没有相机权限");
        }
    }

    @Override
    protected void onPause() {
        running = false;
        closeCamera();
        super.onPause();
    }

    private void startCamera() {
        if (thread != null) {
            return;
        }
        thread = new HandlerThread("front-cam");
        thread.start();
        handler = new Handler(thread.getLooper());
        handler.post(this::openFrontCamera);
    }

    private void openFrontCamera() {
        CameraManager manager = (CameraManager) getSystemService(CAMERA_SERVICE);
        try {
            cameraId = findFrontCamera(manager);
            CameraCharacteristics chars = manager.getCameraCharacteristics(cameraId);
            Size size = chooseSize(chars);
            width = size.getWidth();
            height = size.getHeight();
            jpegOrientation = jpegOrientation(chars);
            setStatus("打开前摄 " + cameraId + "  " + width + "x" + height);
            reader = ImageReader.newInstance(width, height, ImageFormat.YUV_420_888, 2);
            reader.setOnImageAvailableListener(this::onImage, handler);
            manager.openCamera(cameraId, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice cam) {
                    camera = cam;
                    createSession();
                }

                @Override
                public void onDisconnected(CameraDevice cam) {
                    setStatus("相机断开");
                    cam.close();
                    camera = null;
                }

                @Override
                public void onError(CameraDevice cam, int error) {
                    setStatus("相机错误 " + error);
                    Log.e(TAG, "camera error " + error);
                    cam.close();
                    camera = null;
                }
            }, handler);
        } catch (Exception e) {
            Log.e(TAG, "open failed", e);
            setStatus("打开失败: " + e.getMessage());
        }
    }

    private String findFrontCamera(CameraManager manager) throws CameraAccessException {
        for (String id : manager.getCameraIdList()) {
            Integer facing = manager.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT) {
                return id;
            }
        }
        throw new IllegalStateException("没有前摄");
    }

    private Size chooseSize(CameraCharacteristics chars) {
        StreamConfigurationMap map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size[] sizes = map == null ? null : map.getOutputSizes(ImageFormat.YUV_420_888);
        if (sizes == null || sizes.length == 0) {
            return new Size(640, 480);
        }
        for (Size size : sizes) {
            if (size.getWidth() == 1280 && size.getHeight() == 720) {
                return size;
            }
        }
        return sizes[0];
    }

    private int jpegOrientation(CameraCharacteristics chars) {
        Integer sensor = chars.get(CameraCharacteristics.SENSOR_ORIENTATION);
        Integer facing = chars.get(CameraCharacteristics.LENS_FACING);
        int device = rotationDegrees(getWindowManager().getDefaultDisplay().getRotation());
        int sensorDegrees = sensor == null ? 0 : sensor;
        if (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT) {
            return (sensorDegrees + device) % 360;
        }
        return (sensorDegrees - device + 360) % 360;
    }

    private static int rotationDegrees(int rotation) {
        switch (rotation) {
            case Surface.ROTATION_90:
                return 90;
            case Surface.ROTATION_180:
                return 180;
            case Surface.ROTATION_270:
                return 270;
            default:
                return 0;
        }
    }

    private void createSession() {
        try {
            camera.createCaptureSession(Collections.singletonList(reader.getSurface()),
                    new CameraCaptureSession.StateCallback() {
                        @Override
                        public void onConfigured(CameraCaptureSession camSession) {
                            session = camSession;
                            running = true;
                            scheduleCapture();
                        }

                        @Override
                        public void onConfigureFailed(CameraCaptureSession camSession) {
                            setStatus("会话配置失败");
                        }
                    }, handler);
        } catch (Exception e) {
            Log.e(TAG, "session failed", e);
            setStatus("会话失败: " + e.getMessage());
        }
    }

    private void scheduleCapture() {
        if (!running || session == null || camera == null) {
            return;
        }
        try {
            CaptureRequest.Builder builder = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            builder.addTarget(reader.getSurface());
            builder.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
            session.capture(builder.build(), null, handler);
        } catch (Exception e) {
            Log.e(TAG, "capture failed", e);
            setStatus("采集失败: " + e.getMessage());
            return;
        }
        handler.postDelayed(this::scheduleCapture, INTERVAL_MS);
    }

    private void onImage(ImageReader imageReader) {
        Image image = imageReader.acquireLatestImage();
        if (image == null) {
            return;
        }
        try {
            byte[] nv21 = Yuv.toNv21(image);
            byte[] jpeg = Yuv.toJpeg(nv21, image.getWidth(), image.getHeight(), 85);
            Bitmap bitmap = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
            bitmap = Yuv.rotate(bitmap, jpegOrientation);
            taken++;
            ring.writeLatestNv21(nv21);
            File saved = ring.addJpeg(jpeg);
            int stored = ring.stored();
            String text = "前摄 " + cameraId + "  YUV " + image.getWidth() + "x" + image.getHeight()
                    + "\n第 " + taken + " 帧    磁盘 " + stored + "/" + FrameRing.CAPACITY
                    + "\n" + saved.getName();
            Bitmap frame = bitmap;
            runOnUiThread(() -> {
                preview.setImageBitmap(frame);
                if (shown != null && shown != frame) {
                    shown.recycle();
                }
                shown = frame;
                status.setText(text);
            });
            Log.i(TAG, text.replace('\n', ' '));
        } catch (Exception e) {
            Log.e(TAG, "save failed", e);
            setStatus("保存失败: " + e.getMessage());
        } finally {
            image.close();
        }
    }

    private void closeCamera() {
        running = false;
        if (session != null) {
            session.close();
            session = null;
        }
        if (camera != null) {
            camera.close();
            camera = null;
        }
        if (reader != null) {
            reader.close();
            reader = null;
        }
        if (thread != null) {
            thread.quitSafely();
            thread = null;
            handler = null;
        }
    }

    private void setStatus(String text) {
        Log.i(TAG, text);
        runOnUiThread(() -> status.setText(text));
    }
}
