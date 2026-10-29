#!/bin/sh
# 打包两个 APK，同一个签名（android/debug.keystore）：
#   android/build.sh            两个都打
#   android/build.sh xiaoxin    只打小新          com.xiaoxin.app      源码 android/xiaoxin/src + util
#   android/build.sh testapp    只打小新测试      com.xiaoxin.testapp  源码 src/com/xiaoxin/testapp + util
# 产物在 android/build/<app>.apk。
# 加测试页：src/com/xiaoxin/testapp/<name>/ 下新建 Activity，登记到 testapp/AndroidManifest.xml，再在 MainActivity 加按钮。
# 小新的图标原图是 xiaoxin/icon_source.png；测试的图标是脚本画的橙色烧瓶，故意不一样，桌面上分得开。
set -eu
ANDROID="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$ANDROID/.." && pwd)"
SDK="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"

NDK="${ANDROID_NDK_ROOT:-$SDK/ndk/27.0.12077973}"
TP="$ANDROID/xiaoxin/third_party"

# 小新的端侧唤醒识别：sherpa-onnx 的两个 .so 加我们的 JNI 薄封装，只打 arm64（pad 是 arm64）。
# 库和模型不在仓库里，没有就先跑 android/xiaoxin/fetch_asr.sh。
native_xiaoxin() {
  out="$1"
  if [ ! -f "$TP/lib/arm64-v8a/libsherpa-onnx-c-api.so" ] || [ ! -f "$TP/assets/asr/model.int8.onnx" ]; then
    echo "缺端侧识别的库或模型，先跑 sh android/xiaoxin/fetch_asr.sh" >&2
    exit 1
  fi
  lib="$out/apk/lib/arm64-v8a"
  mkdir -p "$lib"
  cc="$(ls "$NDK"/toolchains/llvm/prebuilt/*/bin/aarch64-linux-android31-clang | head -1)"
  "$cc" -shared -fPIC -O2 -I"$TP/include" "$ANDROID/xiaoxin/jni/wake_jni.c" \
    -L"$TP/lib/arm64-v8a" -lsherpa-onnx-c-api -o "$lib/libxiaoxin-wake.so"
  cp "$TP/lib/arm64-v8a/libsherpa-onnx-c-api.so" "$TP/lib/arm64-v8a/libonnxruntime.so" "$lib/"
}

build() {
  app="$1"
  case "$app" in
    xiaoxin) srcs="$ANDROID/xiaoxin/src $ROOT/util"; min=31 ;;
    testapp) srcs="$ROOT/src/com/xiaoxin/testapp $ROOT/util"; min=26 ;;
    *) echo "unknown app $app" >&2; exit 2 ;;
  esac
  OUT="$ANDROID/build/$app"
  rm -rf "$OUT"
  mkdir -p "$OUT/classes" "$OUT/gen" "$OUT/apk"
  "$BT/aapt2" compile --dir "$ANDROID/$app/res" -o "$OUT/res.zip"
  assets=""
  if [ "$app" = xiaoxin ]; then
    native_xiaoxin "$OUT"
    assets="-A $TP/assets"
  fi
  # shellcheck disable=SC2086
  "$BT/aapt2" link -I "$JAR" --manifest "$ANDROID/$app/AndroidManifest.xml" \
    --min-sdk-version "$min" --target-sdk-version 34 --java "$OUT/gen" $assets \
    -0 onnx -o "$OUT/base.apk" "$OUT/res.zip"
  # shellcheck disable=SC2086
  find $srcs "$OUT/gen" -name '*.java' > "$OUT/sources.list"
  javac --release 17 -classpath "$JAR" -d "$OUT/classes" @"$OUT/sources.list"
  find "$OUT/classes" -name '*.class' > "$OUT/classes.list"
  "$BT/d8" --min-api "$min" --lib "$JAR" --output "$OUT" @"$OUT/classes.list"
  cp "$OUT/base.apk" "$OUT/unsigned.apk"
  (cd "$OUT" && zip -q unsigned.apk classes.dex)
  # .so 不压缩（-0），系统可以直接从 APK 里映射，不用解压一份
  if [ -d "$OUT/apk/lib" ]; then (cd "$OUT/apk" && zip -q -0 -r ../unsigned.apk lib); fi
  # -P 16：不压缩的 .so 按 16 KB 页对齐，系统才能不解压直接从 APK 里加载（manifest 里 extractNativeLibs=false）。
  # 34.0.0 的 zipalign 没这个参数，用 36.1.0 的
  "$SDK/build-tools/36.1.0/zipalign" -f -P 16 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
  "$BT/apksigner" sign --ks "$ANDROID/debug.keystore" --ks-pass pass:android --key-pass pass:android \
    --out "$ANDROID/build/$app.apk" "$OUT/aligned.apk"
  echo "built $ANDROID/build/$app.apk"
}

if [ ! -f "$ANDROID/debug.keystore" ]; then
  keytool -genkeypair -keystore "$ANDROID/debug.keystore" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US"
fi
for app in ${1:-xiaoxin testapp}; do
  build "$app"
done
