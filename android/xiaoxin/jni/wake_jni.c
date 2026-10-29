// 小新 pad 端侧识别：把 sherpa-onnx 的 C API 包成两个 JNI 方法给 com.xiaoxin.util.LocalAsr 用。
// 官方的 JNI 封装是 Kotlin 写的，我们的打包脚本只有 javac，所以自己包一层，只用到离线 Paraformer。
#include <jni.h>
#include <stdlib.h>
#include <string.h>

#include "sherpa-onnx/c-api/c-api.h"

JNIEXPORT jlong JNICALL Java_com_xiaoxin_util_LocalAsr_nativeCreate(
    JNIEnv *env, jclass cls, jstring jmodel, jstring jtokens, jint threads) {
  const char *model = (*env)->GetStringUTFChars(env, jmodel, NULL);
  const char *tokens = (*env)->GetStringUTFChars(env, jtokens, NULL);
  SherpaOnnxOfflineRecognizerConfig c;
  memset(&c, 0, sizeof(c));
  c.feat_config.sample_rate = 16000;
  c.feat_config.feature_dim = 80;
  c.model_config.paraformer.model = model;
  c.model_config.tokens = tokens;
  c.model_config.num_threads = threads;
  c.model_config.provider = "cpu";
  c.decoding_method = "greedy_search";
  const SherpaOnnxOfflineRecognizer *r = SherpaOnnxCreateOfflineRecognizer(&c);
  (*env)->ReleaseStringUTFChars(env, jmodel, model);
  (*env)->ReleaseStringUTFChars(env, jtokens, tokens);
  return (jlong)(intptr_t)r;
}

// pcm: s16le 16 kHz 单声道。返回识别出的文字（UTF-8），失败返回 null。
JNIEXPORT jstring JNICALL Java_com_xiaoxin_util_LocalAsr_nativeDecode(
    JNIEnv *env, jclass cls, jlong handle, jbyteArray jpcm, jint length) {
  const SherpaOnnxOfflineRecognizer *r = (const SherpaOnnxOfflineRecognizer *)(intptr_t)handle;
  if (!r || length < 2) return NULL;
  int n = length / 2;
  float *samples = (float *)malloc(sizeof(float) * n);
  if (!samples) return NULL;
  jbyte *pcm = (*env)->GetByteArrayElements(env, jpcm, NULL);
  for (int i = 0; i < n; i++) {
    int16_t s = (int16_t)((uint8_t)pcm[2 * i] | ((uint8_t)pcm[2 * i + 1] << 8));
    samples[i] = s / 32768.0f;
  }
  (*env)->ReleaseByteArrayElements(env, jpcm, pcm, JNI_ABORT);
  const SherpaOnnxOfflineStream *s = SherpaOnnxCreateOfflineStream(r);
  SherpaOnnxAcceptWaveformOffline(s, 16000, samples, n);
  SherpaOnnxDecodeOfflineStream(r, s);
  const SherpaOnnxOfflineRecognizerResult *res = SherpaOnnxGetOfflineStreamResult(s);
  jstring out = (res && res->text) ? (*env)->NewStringUTF(env, res->text) : NULL;
  if (res) SherpaOnnxDestroyOfflineRecognizerResult(res);
  SherpaOnnxDestroyOfflineStream(s);
  free(samples);
  return out;
}

JNIEXPORT void JNICALL Java_com_xiaoxin_util_LocalAsr_nativeDestroy(JNIEnv *env, jclass cls, jlong handle) {
  if (handle) SherpaOnnxDestroyOfflineRecognizer((const SherpaOnnxOfflineRecognizer *)(intptr_t)handle);
}
