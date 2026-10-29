# 小新 APK

pad 上的「小新」：一个 WebView 套着网页版，界面、对话、设置和电脑上完全一样，改网页（`server/static/`）部署后 pad 刷新就生效，不用重新打包。

只有「📞 自由对话」的音频走原生：网页检测到 `window.XiaoxinNative` 就把它交给 `com.xiaoxin.util.Talk`，原生连 `/api/talk`、录音、放音，事件通过 `window.xiaoxinTalkEvent` 回给网页画界面。原因是 WebView 里的 getUserMedia 走不到 pad 的系统 AEC。断句、识别、回答、打断都在服务端（协议见 `server/talk.py`）。

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
adb shell am start -n com.xiaoxin.app/.TalkActivity --es key "$K"     # 默认打开 https://47.116.13.227:8183/xiaoxin/
```

**用 IP，不用域名。** gpu.cmbian.top 没备案，TLS 里带这个域名的 SNI 会被运营商掐掉（TCP 能连上，握手收不到任何字节）。证书 SAN 里有这个 IP。

证书是自签的 `res/raw/gpu_cert.pem`（2027-04-15 到期，换证书要同步换这个文件再打包）：WebView 在 `onReceivedSslError` 里按这张证书的指纹放行，原生 wss 由 `res/xml/network_security_config.xml` 信任它，别的证书一律拒绝。

调试时改地址：`--es url http://127.0.0.1:18184/`，配合 `ssh -f -N -L 18184:127.0.0.1:8184 gpu2` 和 `adb reverse tcp:18184 tcp:18184`。明文只允许 127.0.0.1。

## 音频

- 录音 `VOICE_COMMUNICATION`，播放 `USAGE_VOICE_COMMUNICATION` + `MODE_IN_COMMUNICATION` + 外放，靠 pad 的 MTK 系统 AEC 消回声。2026-10-29 在 TB335FC 实测：小新的声音在麦克风里剩 2 dB 左右，人声 55–65 dB，插话能被识别。
- 通话音量默认偏小（5/15），开始时调到至少 8/15，挂断后恢复。
- 切到后台就挂断。
