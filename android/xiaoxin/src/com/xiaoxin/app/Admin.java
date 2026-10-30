package com.xiaoxin.app;

import android.app.admin.DeviceAdminReceiver;

/** 只为一件事：「熄屏睡觉」时 DevicePolicyManager.lockNow() 真的把屏幕关掉。不做任何别的设备管理。 */
public class Admin extends DeviceAdminReceiver {
}
