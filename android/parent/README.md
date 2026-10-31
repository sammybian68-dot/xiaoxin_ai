# 小新家长 APK

爸爸妈妈手机上的「小新家长」（`com.xiaoxin.parent`，图标是小新头像加橙色「家」角标）。现在是最小的壳：WebView 套网页版（同一个家庭线程），自签证书按指纹放行，摄像头和麦克风只给自己的页面、网页真要用时才弹系统框。没有唤醒、闹钟、开机自启。后面按 docs/architecture.md 加扫码配对、微信分享入口、通知。

```bash
sh android/build.sh parent
adb install -r android/build/parent.apk
K=$(grep '^XIAOXIN_KEY=' .env | cut -d= -f2)
adb shell am start -n com.xiaoxin.parent/.MainActivity --es key "$K"     # 以后换成扫码配对
adb shell am start -n com.xiaoxin.parent/.MainActivity --es page "'calltest?room=x'"   # 调试：打开站内某一页（单引号别丢，& 会被 sh 切掉）
```

视频通话端到端（这台 Mac 的无头 Chrome ↔ pad 上的 App，见 tests/e2e/call.py）：

```bash
.venv/bin/python -m tests.e2e.call --pad <序列号> --app parent            # 局域网直连
.venv/bin/python -m tests.e2e.call --pad <序列号> --app parent --relay    # 强制走 ECS 中继
```
