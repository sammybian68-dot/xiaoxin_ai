#!/bin/sh
# Test APK. Sources: ../src (one package per test) and ../util (shared).
# Add a test: new activity under src/com/xiaoxin/testapp/<name>/,
# register it in AndroidManifest.xml, add a button in MainActivity.
set -eu
ANDROID="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$ANDROID/.." && pwd)"
SDK="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"
OUT="$ANDROID/build"
rm -rf "$OUT"
mkdir -p "$OUT/classes"
# res/ only holds launcher icons; source image is icon_source.png (made with `python -m src.llm image`)
"$BT/aapt2" compile --dir "$ANDROID/res" -o "$OUT/res.zip"
"$BT/aapt2" link -I "$JAR" --manifest "$ANDROID/AndroidManifest.xml" \
  --min-sdk-version 26 --target-sdk-version 34 \
  -o "$OUT/base.apk" "$OUT/res.zip"
find "$ROOT/src" "$ROOT/util" -name '*.java' > "$OUT/sources.list"
javac --release 17 -classpath "$JAR" -d "$OUT/classes" @"$OUT/sources.list"
find "$OUT/classes" -name '*.class' > "$OUT/classes.list"
"$BT/d8" --min-api 26 --lib "$JAR" --output "$OUT" @"$OUT/classes.list"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT" && zip -q unsigned.apk classes.dex)
if [ ! -f "$ANDROID/debug.keystore" ]; then
  keytool -genkeypair -keystore "$ANDROID/debug.keystore" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US"
fi
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
"$BT/apksigner" sign --ks "$ANDROID/debug.keystore" --ks-pass pass:android --key-pass pass:android \
  --out "$OUT/testapp.apk" "$OUT/aligned.apk"
echo "built $OUT/testapp.apk"
