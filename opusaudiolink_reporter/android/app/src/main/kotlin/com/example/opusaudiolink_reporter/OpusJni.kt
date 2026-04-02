package com.example.opusaudiolink_reporter

import android.util.Log

/**
 * Schlanker JNI-Wrapper um libopus.so (arm64-v8a).
 * Encoder und Decoder werden als Long-Handle verwaltet (Pointer).
 */
object OpusJni {
    private const val TAG = "OpusJni"

    // Opus-Anwendungstyp
    private const val OPUS_APPLICATION_VOIP  = 2048
    private const val OPUS_APPLICATION_AUDIO = 2049

    init {
        System.loadLibrary("opus")
        System.loadLibrary("opusjni")
    }

    // ── Encoder ─────────────────────────────────────────────────────────────
    external fun encoderCreate(sampleRate: Int, channels: Int, application: Int): Long
    external fun encoderSetBitrate(handle: Long, bitrate: Int)
    external fun encode(handle: Long, pcm: ByteArray, frameSize: Int): ByteArray
    external fun encoderDestroy(handle: Long)

    // ── Decoder ─────────────────────────────────────────────────────────────
    external fun decoderCreate(sampleRate: Int, channels: Int): Long
    external fun decode(handle: Long, data: ByteArray, frameSize: Int, channels: Int): ByteArray
    external fun decoderDestroy(handle: Long)

    // ── High-Level-Wrappers ──────────────────────────────────────────────────

    class Encoder(sampleRate: Int, channels: Int, bitrate: Int) {
        private val handle = encoderCreate(sampleRate, channels, OPUS_APPLICATION_AUDIO)
        init { encoderSetBitrate(handle, bitrate) }
        fun encode(pcm: ByteArray, frameSize: Int): ByteArray = OpusJni.encode(handle, pcm, frameSize)
        fun destroy() = encoderDestroy(handle)
    }

    class Decoder(val sampleRate: Int, val channels: Int) {
        private val handle = decoderCreate(sampleRate, channels)
        fun decode(data: ByteArray, frameSize: Int): ByteArray = OpusJni.decode(handle, data, frameSize, channels)
        fun destroy() = decoderDestroy(handle)
    }
}
