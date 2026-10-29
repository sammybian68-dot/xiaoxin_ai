# 小新 APK

pad 上的「小新」，现在只有自由对话：点「开始说话」后像打电话一样一直说，小新说话时可以直接插话打断。断句、识别、回答、打断都在服务端 `/api/talk`（协议见 `server/talk.py`），APK 只收发 PCM。

「小新测试」是另一个 APK（`com.xiaoxin.testapp`，橙色烧瓶图标），放麦克风、前摄、回声这些测试页，两个可以同时装。

## 打包和安装

```bash
sh android/build.sh xiaoxin        # 或不带参数，两个都打
adb install -r -g android/build/xiaoxin.apk
```

## 配置

地址和 key 存在 APK 的 SharedPreferences 里，不打进安装包。装好后配一次：

```bash
K=$(grep '^XIAOXIN_KEY=' .env | cut -d= -f2)
adb shell am start -n com.xiaoxin.app/.TalkActivity --es key "$K"                    # 走公网 wss://gpu.cmbian.top:8183
adb shell am start -n com.xiaoxin.app/.TalkActivity --es url ws://127.0.0.1:18184/api/talk   # 改走 USB 调试链路
```

USB 调试链路（公网 8183 不通时用）：

```bash
ssh -f -N -L 18184:127.0.0.1:8184 gpu2   # 电脑 → gpu2 的小新
adb reverse tcp:18184 tcp:18184           # pad → 电脑
```

`ws://` 明文只允许 `127.0.0.1`，见 `res/xml/network_security_config.xml`。公网走 wss，只信任 `res/raw/gpu_cert.pem` 这张自签证书（2027-04-15 到期，换证书要同步换这个文件）。

## 音频

- 录音 `VOICE_COMMUNICATION`，播放 `USAGE_VOICE_COMMUNICATION` + `MODE_IN_COMMUNICATION` + 外放，靠 pad 的 MTK 系统 AEC 消回声。2026-10-29 在 TB335FC 实测：小新的声音在麦克风里剩 2 dB 左右，人声 55–65 dB，插话能被识别。
- 通话音量默认偏小（5/15），开始时调到至少 8/15，挂断后恢复。
- 切到后台就挂断。
