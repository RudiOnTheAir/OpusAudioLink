/**
 * opusjni.c – Schlanker JNI-Wrapper für libopus
 * Wird als libopusjni.so geladen (System.loadLibrary("opusjni"))
 *
 * Encoder-Handle = (jlong)(uintptr_t) OpusEncoder*
 * Decoder-Handle = (jlong)(uintptr_t) OpusDecoder*
 */
#include <jni.h>
#include <stdint.h>
#include <stdlib.h>
#include "opus.h"

#define PKG "com/example/opusaudiolink_reporter/OpusJni"

/* ── Encoder ───────────────────────────────────────────────────────────── */

JNIEXPORT jlong JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_encoderCreate(
    JNIEnv *env, jobject obj, jint sampleRate, jint channels, jint application)
{
    int err = 0;
    OpusEncoder *enc = opus_encoder_create(sampleRate, channels, application, &err);
    return (err == OPUS_OK) ? (jlong)(uintptr_t)enc : 0L;
}

JNIEXPORT void JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_encoderSetBitrate(
    JNIEnv *env, jobject obj, jlong handle, jint bitrate)
{
    OpusEncoder *enc = (OpusEncoder *)(uintptr_t)handle;
    if (enc) opus_encoder_ctl(enc, OPUS_SET_BITRATE(bitrate));
}

JNIEXPORT jbyteArray JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_encode(
    JNIEnv *env, jobject obj, jlong handle, jbyteArray pcm, jint frameSize)
{
    OpusEncoder *enc = (OpusEncoder *)(uintptr_t)handle;
    if (!enc) return NULL;

    jsize   pcm_len  = (*env)->GetArrayLength(env, pcm);
    jbyte  *pcm_buf  = (*env)->GetByteArrayElements(env, pcm, NULL);
    uint8_t out[4000];

    opus_int32 len = opus_encode(enc,
        (const opus_int16 *)pcm_buf, frameSize, out, sizeof(out));

    (*env)->ReleaseByteArrayElements(env, pcm, pcm_buf, JNI_ABORT);

    if (len <= 0) return (*env)->NewByteArray(env, 0);
    jbyteArray result = (*env)->NewByteArray(env, len);
    (*env)->SetByteArrayRegion(env, result, 0, len, (jbyte *)out);
    return result;
}

JNIEXPORT void JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_encoderDestroy(
    JNIEnv *env, jobject obj, jlong handle)
{
    OpusEncoder *enc = (OpusEncoder *)(uintptr_t)handle;
    if (enc) opus_encoder_destroy(enc);
}

/* ── Decoder ───────────────────────────────────────────────────────────── */

JNIEXPORT jlong JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_decoderCreate(
    JNIEnv *env, jobject obj, jint sampleRate, jint channels)
{
    int err = 0;
    OpusDecoder *dec = opus_decoder_create(sampleRate, channels, &err);
    return (err == OPUS_OK) ? (jlong)(uintptr_t)dec : 0L;
}

JNIEXPORT jbyteArray JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_decode(
    JNIEnv *env, jobject obj, jlong handle, jbyteArray data, jint frameSize, jint channels)
{
    OpusDecoder *dec = (OpusDecoder *)(uintptr_t)handle;
    if (!dec) return (*env)->NewByteArray(env, 0);

    jsize  data_len = (*env)->GetArrayLength(env, data);
    jbyte *data_buf = (*env)->GetByteArrayElements(env, data, NULL);

    /* PCM16: frameSize * channels * 2 Bytes */
    int    out_samples = frameSize * channels;
    int16_t *pcm_out = (int16_t *)malloc(out_samples * sizeof(int16_t));

    int decoded = opus_decode(dec,
        (const uint8_t *)data_buf, data_len,
        pcm_out, frameSize, 0);

    (*env)->ReleaseByteArrayElements(env, data, data_buf, JNI_ABORT);

    if (decoded <= 0) { free(pcm_out); return (*env)->NewByteArray(env, 0); }

    /* decoded = Anzahl Samples pro Kanal
       byte_len = decoded * channels * 2 Bytes (PCM16) */
    int byte_len = decoded * channels * 2;
    jbyteArray result = (*env)->NewByteArray(env, byte_len);
    (*env)->SetByteArrayRegion(env, result, 0, byte_len, (jbyte *)pcm_out);
    free(pcm_out);
    return result;
}

JNIEXPORT void JNICALL
Java_com_example_opusaudiolink_1reporter_OpusJni_decoderDestroy(
    JNIEnv *env, jobject obj, jlong handle)
{
    OpusDecoder *dec = (OpusDecoder *)(uintptr_t)handle;
    if (dec) opus_decoder_destroy(dec);
}
