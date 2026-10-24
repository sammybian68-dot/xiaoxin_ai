# 小新平板 ADB

下次要连这台平板，先读这份。不要重新摸索 USB 模式、无线配对和点按坐标。

仓库：`git@github.com:sammybian68-dot/xiaoxin_ai.git`。开发在 `dev`，不要直接提交到 `main`。截图在 `tmp/screen_shots/`，整个 `tmp/` 不入库。

记录日期：2026-10-24。IP、端口、配对码会变，下面标了「会变」的不要当常量。

## 默认策略

- **平时用有线。** 用户在书房，平板插着 USB 时，只走 USB。
- **没带 USB 再用无线。** 典型场景：平板在女儿房间，人在书房，要远程装/更新 APK、看日志、截图。
- 两条都在时，`adb devices` 会列出 3 行（USB 序列号、`IP:端口`、mDNS 名）。它们不是三台设备。USB 是一条，后两行是同一条无线。**永远加 `-s`，不要裸跑 `adb`。**
- 大文件、大 APK 优先等有线。无线能装，但慢一个数量级。

## 这是哪台

| 项 | 值 |
|---|---|
| 产品 | 小新平板 11（设置里的设备名称；`ro.product.marketname` 为空） |
| 品牌 | LENOVO / Lenovo |
| 型号 | `TB335FC`（`ro.product.model` / `ro.product.device`） |
| 产品名 | `TB335FC_PRC` |
| 序列号 | `HA2KGGAH`（稳定，USB 的 `-s` 就用这个） |
| 系统 | Android 16（SDK 36），ZUI/ZUXOS |
| 版本号 | `TB335FC_CN_OPEN_USER_M1317.3_W_ZUXOS_1.5.10.233_ST_260615` |
| 分辨率 | 物理 1600x2560，横屏截图 2560x1600，dpi 320，旋转 90 |
| Wi-Fi | `cmbian2-5G`（5 GHz，802.11ac）。书房和女儿房间要在同一局域网才能无线调试 |
| adb | `/Users/cmbian/Library/Android/sdk/platform-tools/adb`（本机 PATH 里的 `adb` 就是它） |

不是理想汽车的车机。USB 描述符厂商是 LENOVO，产品名 `TB335FC`。

## 有线

```bash
adb devices -l
adb -s HA2KGGAH shell getprop ro.serialno
```

看到 `HA2KGGAH device usb:...` 就可以直接用。装包：

```bash
adb -s HA2KGGAH install -r path/to/app.apk
```

### 插上了但列表是空的

系统能看到 USB 设备，不代表 adb 能看见。当时的状态：

- `ioreg` 里有 `USB Vendor Name = LENOVO`，`USB Product Name = TB335FC`，`USB Serial Number = HA2KGGAH`
- `idProduct = 32544`，没有 ADB 接口。`UsbDeviceSignature` 里没有 `ff4201`
- `adb devices` 为空

这是「仅充电」或 USB 调试没开。用户在平板上打开开发者选项里的 **USB 调试**，通知栏 USB 用途改成 **文件传输**，并点允许调试之后：

- `idProduct` 变成 `32545`
- 签名里出现 `ff4201`（ADB interface class `0xFF` / subclass `0x42` / protocol `0x01`）
- `adb devices` 出现 `HA2KGGAH device`

不要在列表为空时反复 `adb kill-server`。先确认调试开关和 USB 用途。

有线协商的是 **USB 2.0 High Speed**（`Device Speed = 2`）。Mac 那个口能力是 USB 3（`USBSpeed = 3`），但平板没有枚举成 USB 3。不要按 USB 3 的速度估时间。

## 无线

这台用的是 Android 11+ 的无线调试（TLS 配对），**不是** `adb tcpip 5555`。不要切到 5555，那会动到 adbd，和现在这条配对不是一回事。

2026-10-24 已和这台 Mac 配过对。平板「已配对的设备」里是 `cmbian@biancaomingMacBook-Pro.local`，状态「当前已连接」。配对还在的话，**不用再要配对码**，发现地址后直接 `adb connect`。

当时的地址（会变）：

- 连接：`192.168.2.9:44067`
- mDNS 名：`adb-HA2KGGAH-b9QOwg`（名字里带序列号，用来确认是这台）
- 配对端口当时是 `44603`，配对码 `431416`。码和配对端口一次性，文档里只是记录，不能再拿去 pair

### 没插线时怎么连

```bash
adb devices -l
adb mdns services
```

mDNS 正常时会看到类似：

```text
adb-HA2KGGAH-b9QOwg    _adb-tls-connect._tcp    192.168.2.9:44067
```

然后：

```bash
adb connect 192.168.2.9:44067
adb -s 192.168.2.9:44067 shell getprop ro.serialno
```

`getprop` 必须打出 `HA2KGGAH` 再装包。IP 是 DHCP，端口每次打开无线调试都可能变，以 `adb mdns services` 为准，不要写死 `44067`。

已经配对且平板醒着、连着 `cmbian2-5G` 时，`adb devices` 里可能已经有 `adb-HA2KGGAH-b9QOwg._adb-tls-connect._tcp`。那一行就是无线，可以直接 `-s` 它。

远程更新 APK：

```bash
adb -s 192.168.2.9:44067 install -r path/to/app.apk
```

把 `192.168.2.9:44067` 换成当时 mdns 给出的地址。

### 无线连不上时

按这个顺序看，不要跳步：

1. `adb mdns services` 没有 `_adb-tls-connect._tcp`：平板不在同一局域网、睡着了、Wi-Fi 断了，或「无线调试」被关掉。叫用户唤醒平板，看设置里的开关。agent 在没 USB 时点不了屏幕。
2. mdns 有连接端口，但 `adb connect` 只回 `failed to connect to ...`，没有更多原因：这台电脑还没配对，或配对已被撤销。仅打开「无线调试」开关不够。
3. 配对服务 `_adb-tls-pairing._tcp` 只有用户点了「使用配对码配对设备」、对话框还开着时才出现。对话框一关，配对端口就作废。
4. 配对命令是 `adb pair <配对IP:配对端口> <6位码>`。配对端口不是连接端口。配完再 `adb connect` 连接端口。
5. 有 USB 时可以自己打开那个对话框，不必让用户报码。点「无线调试」标题，不要点右侧开关（点开关会把它关掉）。流程和坐标见下面「点按」。

## 速度

2026-10-24 用同一个 100MB 随机文件测的 `adb push` / `adb pull`，传完对过 MD5。测试文件已删。

| 方向 | 有线 USB 2.0 | 无线（`cmbian2-5G`，-65 dBm，协商 Tx 234 / Rx 292 Mbps） |
|---|---:|---:|
| 电脑 → 平板 | 2.8 秒，35.1 MB/s | 18.3 秒，5.5 MB/s |
| 平板 → 电脑 | 2.7 秒，36.7 MB/s | 24.1 秒，4.1 MB/s |

100MB 无线比有线慢大约 6 到 9 倍。无线实际速度远低于协商速率，因为还要走 TLS，而且信号只有 -65 dBm。装一个几十 MB 的 APK 可以接受；上百 MB 的包或反复推文件，等有线。

## 截图

截图放 `tmp/screen_shots/`。整个 `tmp/` 已在 `.gitignore` 里，不要把截图提交进仓库，也不要散落到系统 `/tmp`。

```bash
mkdir -p tmp/screen_shots
adb -s HA2KGGAH exec-out screencap -p > tmp/screen_shots/name.png
```

没插线就把 `-s` 换成无线那行。PNG 是 2560x1600。

## 点按

只在需要帮用户打开「无线调试」详情、配对码时才点屏幕。平时不要在设置里乱点。

`adb shell input tap/swipe` 用的是**截图像素**，也就是 2560x1600。不要用 `dumpsys input` 里那些 `1600x2560` 的 window frame，那个坐标系转了 90 度，事件会打空。

曾经打空的写法：`input swipe 1700 1300 1700 350`（太快，而且起点靠近底部手势条）。能滚动右栏的写法：

```bash
# 右栏向下滚（手指上移）
adb -s HA2KGGAH shell input swipe 1600 1100 1600 400 500
# 右栏向上滚
adb -s HA2KGGAH shell input swipe 1600 450 1600 1150 450
```

其他坑：

- 设置是双栏。左边是 ZUI 首页，右边才是开发者选项。`uiautomator dump` 只看到过左栏，而且还会触发 `UiAutomationConnection#restoreRotationStateLocked`，把右栏滚动位置打乱。不要用它来定位。
- `am start -a android.settings.WIRELESS_DEBUGGING_SETTINGS` 在这台 ZUI 上解析不了。
- `am start -n com.android.settings/.SubSettings` 未导出，shell 会 `SecurityException`。
- 开发者选项里「启用开发者选项」那一行会粘在卡片顶部，不能拿它判断滚到哪了。
- 「无线调试」在「调试」分组里，紧挨着「USB 调试」。从 WLAN / 蓝牙那一段往上滚才能回到它，往下会进到绘图和动画。
- 点「无线调试」要打标题文字（大约 `1250 370`，以当次截图为准），不要打右边开关。
- 详情页标题是「无线调试」。上面有设备名「小新平板11」、连接用的「IP 地址和端口」。配对码在「使用配对码配对设备」里，对话框标题「与设备配对」。码出来后马上 `adb pair`，不要先干别的。

## 不要做的事

- 不要 `adb tcpip 5555`，不要 `adb disconnect` 掉用户正在用的有线，除非他明确要拔。
- 不要在无线上推大文件还假装和有线一样快。
- 不要把某一次的端口、配对码写进脚本。
- 不要对别的序列号装包。装之前确认是 `HA2KGGAH`。
