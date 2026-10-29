package com.xiaoxin.testapp;

import android.app.Activity;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.os.Bundle;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.xiaoxin.testapp.echo.EchoActivity;
import com.xiaoxin.testapp.frontcamera.FrontCameraActivity;
import com.xiaoxin.testapp.mic.MicActivity;

public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.HORIZONTAL);
        root.setGravity(Gravity.CENTER_VERTICAL);
        root.setBackgroundColor(0xFF101114);
        root.setPadding(dp(72), dp(48), dp(72), dp(48));

        LinearLayout intro = new LinearLayout(this);
        intro.setOrientation(LinearLayout.VERTICAL);
        intro.setPadding(0, 0, dp(56), 0);
        TextView title = label("小新测试", 40, Color.WHITE);
        TextView hint = label("选一个开始", 16, 0xFF8E929A);
        hint.setPadding(0, dp(10), 0, 0);
        intro.addView(title);
        intro.addView(hint);
        root.addView(intro, new LinearLayout.LayoutParams(dp(280), LinearLayout.LayoutParams.WRAP_CONTENT));

        LinearLayout cards = new LinearLayout(this);
        cards.setOrientation(LinearLayout.HORIZONTAL);
        cards.setGravity(Gravity.CENTER_VERTICAL);
        cards.addView(card("前摄", "每秒一帧", "画面只在中间一小块", 0xFF7EB6FF, FrontCameraActivity.class));
        cards.addView(card("麦克风", "16 kHz 原始 PCM", "说完可以回听", 0xFF3DDC84, MicActivity.class));
        cards.addView(card("回声", "边放边录", "看系统 AEC 消掉多少", 0xFFFFB74D, EchoActivity.class));
        root.addView(cards);
        setContentView(root);
    }

    private View card(String name, String detail, String note, int accent, Class<?> activity) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(28), dp(26), dp(28), dp(26));
        card.setBackground(ripple(0xFF1C1E24));
        card.setClickable(true);
        card.setOnClickListener(v -> TestBar.open(this, activity));

        View mark = new View(this);
        mark.setBackgroundColor(accent);
        LinearLayout.LayoutParams markParams = new LinearLayout.LayoutParams(dp(36), dp(4));
        markParams.bottomMargin = dp(22);
        card.addView(mark, markParams);

        card.addView(label(name, 26, Color.WHITE));
        TextView detailView = label(detail, 15, 0xFFE7E8EA);
        detailView.setPadding(0, dp(14), 0, 0);
        card.addView(detailView);
        TextView noteView = label(note, 14, 0xFF8E929A);
        noteView.setPadding(0, dp(6), 0, 0);
        card.addView(noteView);

        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(dp(280), dp(196));
        params.rightMargin = dp(20);
        card.setLayoutParams(params);
        return card;
    }

    private TextView label(String text, int sp, int color) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(color);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        return view;
    }

    private RippleDrawable ripple(int color) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color);
        shape.setCornerRadius(dp(22));
        return new RippleDrawable(ColorStateList.valueOf(0x33FFFFFF), shape, null);
    }

    private int dp(int value) {
        return Math.round(TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics()));
    }
}
