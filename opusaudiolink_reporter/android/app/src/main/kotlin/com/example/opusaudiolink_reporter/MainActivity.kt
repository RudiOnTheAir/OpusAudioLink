package com.example.opusaudiolink_reporter

import android.Manifest
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.PowerManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    companion object {
        private const val CH_CONTROL  = "opusaudiolink/control"
        private const val CH_PCM_IN   = "opusaudiolink/pcm_in"
        private const val CH_PCM_RX   = "opusaudiolink/pcm_rx"
        private const val CH_PCM_OUT  = "opusaudiolink/pcm_out"
        private const val CH_STATS    = "opusaudiolink/stats"
        private const val MIC_PERM_RC = 1001
    }

    private var engine: AudioEngine? = null
    private var pcmInSink:  EventChannel.EventSink? = null
    private var pcmRxSink:  EventChannel.EventSink? = null
    private var statsSink:  EventChannel.EventSink? = null
    private var wakeLock:   PowerManager.WakeLock? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CH_CONTROL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "start" -> {
                        if (!hasMicPermission()) {
                            requestMicPermission()
                            result.error("PERMISSION", "Mikrofonberechtigung fehlt", null)
                            return@setMethodCallHandler
                        }
                        val sampleRate    = call.argument<Int>("sampleRate")    ?: 48000
                        val channels      = call.argument<Int>("channels")      ?: 1
                        val bufferFrames  = call.argument<Int>("bufferFrames")  ?: 960
                        val host          = call.argument<String>("host")       ?: ""
                        val sendPort      = call.argument<Int>("sendPort")      ?: 5004
                        val recvPort      = call.argument<Int>("recvPort")      ?: 5006
                        val bitrate       = call.argument<Int>("bitrate")       ?: 64000
                        val returnBitrate = call.argument<Int>("returnBitrate") ?: 64000

                        engine?.stop()
                        engine = AudioEngine(sampleRate, channels, bufferFrames).apply {
                            onPcmCaptured = { pcm ->
                                runOnUiThread { pcmInSink?.success(pcm) }
                            }
                            onPcmReceived = { pcm ->
                                runOnUiThread { pcmRxSink?.success(pcm) }
                            }
                            onStatusEvent = { map ->
                                runOnUiThread { statsSink?.success(map) }
                            }
                            configure(host, sendPort, recvPort, bitrate, returnBitrate)
                            startCapture()
                            startPlayback()
                            if (host.isNotEmpty()) {
                                startHelo(host, sendPort)
                            }
                        }
                        val audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
                        audioManager.mode = AudioManager.MODE_NORMAL
                        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        if (wakeLock?.isHeld != true) {
                            wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OpusAudioLink::AudioWakeLock")
                            wakeLock?.acquire(3600_000L)
                        }
                        result.success(null)
                    }
                    "stop" -> {
                        engine?.stop(); engine = null
                        wakeLock?.release(); wakeLock = null
                        val audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
                        audioManager.mode = AudioManager.MODE_NORMAL
                        window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        result.success(null)
                    }
                    "isRunning"         -> result.success(engine?.isRunning ?: false)
                    "hasPermission"     -> result.success(hasMicPermission())
                    "requestPermission" -> { requestMicPermission(); result.success(null) }
                    "setAudioMode" -> {
                        val mode = call.argument<Int>("mode") ?: 0
                        engine?.setMuted(mode == 1)
                        result.success(null)
                    }
                    "sendAttention" -> {
                        engine?.sendAttention()
                        result.success(null)
                    }
                    "startHelo" -> {
                        val host = call.argument<String>("host") ?: ""
                        val port = call.argument<Int>("port")    ?: 5004
                        if (host.isNotEmpty()) {
                            if (engine == null) engine = AudioEngine()
                            engine!!.onStatusEvent = { map ->
                                runOnUiThread { statsSink?.success(map) }
                            }
                            engine!!.startHelo(host, port)
                        }
                        result.success(null)
                    }
                    "stopHelo" -> {
                        engine?.stopHelo()
                        result.success(null)
                    }
                    else                -> result.notImplemented()
                }
            }

        // TX PCM (Mic → Flutter, für VU-Meter)
        EventChannel(flutterEngine.dartExecutor.binaryMessenger, CH_PCM_IN)
            .setStreamHandler(object : EventChannel.StreamHandler {
                override fun onListen(a: Any?, sink: EventChannel.EventSink?) { pcmInSink = sink }
                override fun onCancel(a: Any?) { pcmInSink = null }
            })

        // RX PCM (UDP-Eingang → Flutter, für VU-Meter)
        EventChannel(flutterEngine.dartExecutor.binaryMessenger, CH_PCM_RX)
            .setStreamHandler(object : EventChannel.StreamHandler {
                override fun onListen(a: Any?, sink: EventChannel.EventSink?) { pcmRxSink = sink }
                override fun onCancel(a: Any?) { pcmRxSink = null }
            })

        // Stats / HELO-Events
        EventChannel(flutterEngine.dartExecutor.binaryMessenger, CH_STATS)
            .setStreamHandler(object : EventChannel.StreamHandler {
                override fun onListen(a: Any?, sink: EventChannel.EventSink?) { statsSink = sink }
                override fun onCancel(a: Any?) { statsSink = null }
            })

        // PCM-Out (Flutter → Playback, Fallback)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CH_PCM_OUT)
            .setMethodCallHandler { call, result ->
                if (call.method == "write") {
                    val data = call.argument<ByteArray>("data")
                    if (data != null) { engine?.writePcm(data); result.success(null) }
                    else result.error("INVALID", "data ist null", null)
                } else result.notImplemented()
            }
    }

    private fun hasMicPermission() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    private fun requestMicPermission() =
        ActivityCompat.requestPermissions(this,
            arrayOf(Manifest.permission.RECORD_AUDIO), MIC_PERM_RC)

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) { super.onRequestPermissionsResult(requestCode, permissions, grantResults) }
}
