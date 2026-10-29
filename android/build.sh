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

build() {
  app="$1"
  case "$app" in
    xiaoxin) srcs="$ANDROID/xiaoxin/src $ROOT/util"; min=31 ;;
    testapp) srcs="$ROOT/src/com/xiaoxin/testapp $ROOT/util"; min=26 ;;
    *) echo "unknown app $app" >&2; exit 2 ;;
  esac
  OUT="$ANDROID/build/$app"
  rm -rf "$OUT"
  mkdir -p "$OUT/classes" "$OUT/gen"
  "$BT/aapt2" compile --dir "$ANDROID/$app/res" -o "$OUT/res.zip"
  "$BT/aapt2" link -I "$JAR" --manifest "$ANDROID/$app/AndroidManifest.xml" \
    --min-sdk-version "$min" --target-sdk-version 34 --java "$OUT/gen" \
    -o "$OUT/base.apk" "$OUT/res.zip"
  # shellcheck disable=SC2086
  find $srcs "$OUT/gen" -name '*.java' > "$OUT/sources.list"
  javac --release 17 -classpath "$JAR" -d "$OUT/classes" @"$OUT/sources.list"
  find "$OUT/classes" -name '*.class' > "$OUT/classes.list"
  "$BT/d8" --min-api "$min" --lib "$JAR" --output "$OUT" @"$OUT/classes.list"
  cp "$OUT/base.apk" "$OUT/unsigned.apk"
  (cd "$OUT" && zip -q unsigned.apk classes.dex)
  "$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
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
