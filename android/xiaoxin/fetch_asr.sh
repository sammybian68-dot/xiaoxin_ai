#!/bin/sh
# 下载 pad 端侧唤醒识别要用的东西（不进仓库，放 android/xiaoxin/third_party/，build.sh 打进 APK）：
#   sherpa-onnx v2.0.1 的 Android 库（只要 arm64 的 libsherpa-onnx-c-api.so 和 libonnxruntime.so）和 C 头文件
#   Paraformer 中文小模型 sherpa-onnx-paraformer-zh-small-2024-03-09（82 MB，没有热词，「小新同学」实测 16/16）
#   TEN-VAD（Agora 开源，和 carHer2 同一个）的 Android arm64 库和头文件：断句按人声概率，不按音量
#     许可：Apache-2.0 + 附加条件（不得用来和 Agora 的产品竞争），家用助手不涉及
# 版本和 sha256 都钉死，下载内容对不上就报错退出。
set -eu
HERE="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
OUT="$HERE/third_party"
REL=https://github.com/k2-fsa/sherpa-onnx/releases/download
mkdir -p "$OUT/dl"

fetch() {  # url sha256 file
  if [ -f "$OUT/dl/$3" ] && echo "$2  $OUT/dl/$3" | shasum -a 256 -c - >/dev/null 2>&1; then return; fi
  curl -fL --retry 3 -C - -o "$OUT/dl/$3" "$1"
  echo "$2  $OUT/dl/$3" | shasum -a 256 -c -
}

fetch "$REL/v2.0.1/sherpa-onnx-v2.0.1-android.tar.bz2" \
  39003753ec293425ed56723ea9b972b98841a490af3311670c3060548d654330 android.tar.bz2
fetch "$REL/asr-models/sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2" \
  da92b3db5218c5be53aad53e57d1b6e63e7fc98a0e054fbdd6dbe18e9c6b1450 paraformer-small.tar.bz2
fetch "https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v2.0.1/sherpa-onnx/c-api/c-api.h" \
  5698f38ba09ad2bdf085c56a6cbbd7741acf15e25f4ce5c7097b9702cadf437f c-api.h

TV=https://raw.githubusercontent.com/TEN-framework/ten-vad/d68ab71c8d5fbd8f98e97ae7c9986af76b5d72b1
fetch "$TV/lib/Android/arm64-v8a/libten_vad.so" \
  6bc63b352ff5d0752b7c90ca7eb0b23189d5e9cbea9979f82fac962d2ac3c5a5 libten_vad.so
fetch "$TV/include/ten_vad.h" \
  21409e22b80a104d1eeeb11527812baeaf96f9a59819e2e2fb3b3d462c609f7f ten_vad.h

mkdir -p "$OUT/lib/arm64-v8a" "$OUT/include/sherpa-onnx/c-api" "$OUT/assets/asr"
cp "$OUT/dl/libten_vad.so" "$OUT/lib/arm64-v8a/"
cp "$OUT/dl/ten_vad.h" "$OUT/include/"
tar xjf "$OUT/dl/android.tar.bz2" -C "$OUT/dl"
cp "$OUT/dl/jniLibs/arm64-v8a/libsherpa-onnx-c-api.so" "$OUT/dl/jniLibs/arm64-v8a/libonnxruntime.so" "$OUT/lib/arm64-v8a/"
cp "$OUT/dl/c-api.h" "$OUT/include/sherpa-onnx/c-api/"
tar xjf "$OUT/dl/paraformer-small.tar.bz2" -C "$OUT/dl"
M="$OUT/dl/sherpa-onnx-paraformer-zh-small-2024-03-09"
cp "$M/model.int8.onnx" "$M/tokens.txt" "$OUT/assets/asr/"
echo "ok: $OUT"
