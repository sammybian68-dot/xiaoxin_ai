package com.xiaoxin.testapp;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;

import com.xiaoxin.testapp.echo.EchoActivity;
import com.xiaoxin.testapp.frontcamera.FrontCameraActivity;
import com.xiaoxin.testapp.mic.MicActivity;

/** Top bar so any test can switch to another without leaving the app. */
public final class TestBar {
    private TestBar() {}

    public static LinearLayout create(Activity activity) {
        LinearLayout bar = new LinearLayout(activity);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(0xFF1A1A1A);
        bar.setPadding(12, 8, 12, 8);
        add(bar, activity, "列表", MainActivity.class);
        add(bar, activity, "前摄", FrontCameraActivity.class);
        add(bar, activity, "麦克风", MicActivity.class);
        add(bar, activity, "回声", EchoActivity.class);
        return bar;
    }

    private static void add(LinearLayout bar, Activity activity, String label, Class<?> target) {
        boolean here = activity.getClass() == target;
        Button button = new Button(activity);
        button.setAllCaps(false);
        button.setText(here ? label : label);
        button.setTextSize(18);
        button.setTextColor(here ? Color.BLACK : Color.WHITE);
        button.setBackgroundColor(here ? 0xFF3DDC84 : 0xFF333333);
        button.setEnabled(!here);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        params.setMargins(8, 0, 8, 0);
        button.setLayoutParams(params);
        button.setOnClickListener(v -> open(activity, target));
        bar.addView(button);
    }

    public static void open(Activity activity, Class<?> target) {
        if (activity.getClass() == target) {
            return;
        }
        Intent intent = new Intent(activity, target);
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP);
        activity.startActivity(intent);
        if (!(activity instanceof MainActivity)) {
            activity.finish();
        }
    }
}
