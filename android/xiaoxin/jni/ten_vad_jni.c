// TEN-VAD（Agora 开源，和 carHer2 同一个库）的 JNI 薄封装，给 com.xiaoxin.util.TenVad 用。
// 官方 Java 封装走 JNA，打包脚本没有 JNA，所以自己包三个方法。
#include <jni.h>
#include <stdint.h>

#include "ten_vad.h"

JNIEXPORT jlong JNICALL Java_com_xiaoxin_util_TenVad_nativeCreate(JNIEnv *env, jclass cls, jint hop, jfloat threshold) {
  ten_vad_handle_t h = NULL;
  if (ten_vad_create(&h, (size_t)hop, threshold) != 0) return 0;
  return (jlong)(intptr_t)h;
}

// 一帧 hop 个样本（s16），返回人声概率 0–1；出错返回 -1。
JNIEXPORT jfloat JNICALL Java_com_xiaoxin_util_TenVad_nativeProcess(JNIEnv *env, jclass cls, jlong handle,
                                                                    jshortArray jframe, jint hop) {
  ten_vad_handle_t h = (ten_vad_handle_t)(intptr_t)handle;
  if (!h) return -1.0f;
  jshort *frame = (*env)->GetShortArrayElements(env, jframe, NULL);
  float prob = 0.0f;
  int flag = 0;
  int rc = ten_vad_process(h, (const int16_t *)frame, (size_t)hop, &prob, &flag);
  (*env)->ReleaseShortArrayElements(env, jframe, frame, JNI_ABORT);
  return rc == 0 ? prob : -1.0f;
}

JNIEXPORT void JNICALL Java_com_xiaoxin_util_TenVad_nativeDestroy(JNIEnv *env, jclass cls, jlong handle) {
  ten_vad_handle_t h = (ten_vad_handle_t)(intptr_t)handle;
  if (h) ten_vad_destroy(&h);
}
