package com.xiaoxin.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 开机就把闹钟服务拉起来：pad 夜里重启过（系统更新、没电），不开小新闹钟也要照响。
 * ClockService 先读本机副本 files/clock.json 排好下一次，再连服务端同步；AlarmManager 的定时重启后会清掉，靠它重排。
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String a = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(a) || Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(a)
                || Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            ClockService.start(context);
        }
    }
}
