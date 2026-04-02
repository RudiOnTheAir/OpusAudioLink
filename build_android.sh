#!/usr/bin/env bash
# build_android.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="ubuntu:22.04"
BUILD_DATE="$(date +%Y%m%d-%H%M)"
VERSION_CODE="$(date +%y%j%H%M)"
APK_NAME="OpusAudioLink-Reporter-${BUILD_DATE}.apk"
CURRENT_USER="$(id -u):$(id -g)"

FLUTTER_VERSION="3.27.4"
FLUTTER_URL="https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz"
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
OPUS_VERSION="1.5.2"
OPUS_URL="https://downloads.xiph.org/releases/opus/opus-${OPUS_VERSION}.tar.gz"

echo "================================================"
echo "  OpusAudioLink – Android Build"
echo "  Flutter: $FLUTTER_VERSION  AGP: 8.3.1"
echo "  Gradle: 8.4  Kotlin: 1.9.22"
echo "  Opus: $OPUS_VERSION (cross-compiled arm64)"
echo "  Build: $BUILD_DATE  VersionCode: $VERSION_CODE"
echo "================================================"

# ── Keystore: Debug-Keystore von Android verwenden ──
KEYSTORE_PATH="$SCRIPT_DIR/oal_release.keystore"
if [ ! -f "$KEYSTORE_PATH" ]; then
  echo "FEHLER: Keystore nicht gefunden: $KEYSTORE_PATH"
  echo "Einmalig erzeugen mit:"
  echo "  nix-shell -p jdk --run \"keytool -genkeypair -keystore $SCRIPT_DIR/oal_release.keystore -alias oal -keyalg RSA -keysize 2048 -validity 10000 -storepass oal_build_2024 -keypass oal_build_2024 -dname 'CN=OpusAudioLink, OU=Dev, O=Schwoon, L=Emden, S=NDS, C=DE' -noprompt\""
  exit 1
fi
echo "  ✓ Keystore: $KEYSTORE_PATH"

INNER_SCRIPT="$(mktemp /tmp/docker_inner_XXXXXX.sh)"
trap "rm -f '$INNER_SCRIPT'" EXIT

cat > "$INNER_SCRIPT" <<'INNEREOF'
#!/usr/bin/env bash
set -e
FLUTTER_URL="$1"
CMDLINE_TOOLS_URL="$2"
APK_NAME="$3"
VERSION_CODE="$4"
OPUS_URL="$5"

echo '── Systempakete installieren ──'
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  curl wget unzip xz-utils git \
  openjdk-17-jdk-headless \
  libglu1-mesa python3 python3-pip \
  autoconf automake libtool pkg-config \
  > /dev/null

export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export PATH=$JAVA_HOME/bin:$PATH

echo '── Flutter herunterladen ──'
wget -q "$FLUTTER_URL" -O /tmp/flutter.tar.xz
tar xf /tmp/flutter.tar.xz -C /tmp
git config --global --add safe.directory '*'
export PATH=/tmp/flutter/bin:$PATH
flutter --version

echo '── Android SDK herunterladen ──'
mkdir -p /tmp/android-sdk/cmdline-tools
wget -q "$CMDLINE_TOOLS_URL" -O /tmp/cmdline-tools.zip
unzip -q /tmp/cmdline-tools.zip -d /tmp/android-sdk/cmdline-tools
mv /tmp/android-sdk/cmdline-tools/cmdline-tools /tmp/android-sdk/cmdline-tools/latest

export ANDROID_HOME=/tmp/android-sdk
export ANDROID_SDK_ROOT=/tmp/android-sdk
export PATH=$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH

echo '── Android SDK Komponenten installieren ──'
yes | sdkmanager --licenses > /dev/null 2>&1 || true
sdkmanager \
  'platform-tools' \
  'platforms;android-34' \
  'platforms;android-35' \
  'build-tools;34.0.0' \
  'ndk;25.1.8937393'

# NDK-Toolchain für Cross-Compile
NDK_DIR=$ANDROID_HOME/ndk/25.1.8937393
TOOLCHAIN=$NDK_DIR/toolchains/llvm/prebuilt/linux-x86_64
export CC=$TOOLCHAIN/bin/aarch64-linux-android28-clang
export CXX=$TOOLCHAIN/bin/aarch64-linux-android28-clang++
export AR=$TOOLCHAIN/bin/llvm-ar
export RANLIB=$TOOLCHAIN/bin/llvm-ranlib
export STRIP=$TOOLCHAIN/bin/llvm-strip

echo '── libopus cross-kompilieren (arm64-v8a) ──'
wget -q "$OPUS_URL" -O /tmp/opus.tar.gz
tar xf /tmp/opus.tar.gz -C /tmp
cd /tmp/opus-*/

./configure \
  --host=aarch64-linux-android \
  --prefix=/tmp/opus-out \
  --disable-static \
  --enable-shared \
  --disable-doc \
  --disable-extra-programs \
  CFLAGS="-O2 -fPIC" \
  > /dev/null 2>&1

make -j$(nproc) > /dev/null 2>&1
make install > /dev/null 2>&1

# .so ins jniLibs-Verzeichnis
JNI_DIR=/build/android/app/src/main/jniLibs/arm64-v8a
mkdir -p "$JNI_DIR"
cp /tmp/opus-out/lib/libopus.so "$JNI_DIR/"
$STRIP "$JNI_DIR/libopus.so"
echo "  ✓ libopus.so → $JNI_DIR"

# Header für JNI-Wrapper
mkdir -p /build/android/app/src/main/cpp
cp /tmp/opus-out/include/opus/*.h /build/android/app/src/main/cpp/

# Reset CC/CXX für den Rest des Builds (Flutter braucht Host-Compiler)
unset CC CXX AR RANLIB STRIP
cd /build

echo '── Flutter konfigurieren ──'
export ANDROID_HOME=/tmp/android-sdk
flutter config --android-sdk $ANDROID_HOME --no-analytics || true
flutter precache --android

echo '── local.properties schreiben ──'
printf 'sdk.dir=/tmp/android-sdk\nflutter.sdk=/tmp/flutter\nflutter.buildMode=release\nflutter.versionName=0.7\nflutter.versionCode=1\n' \
  > /build/android/local.properties

echo '── Abhängigkeiten laden ──'
cd /build
rm -f pubspec.lock
flutter pub get

echo '── Gradle-Dateien schreiben ──'
cat > /build/android/settings.gradle <<'SETTINGSEOF'
pluginManagement {
    def flutterSdkPath = {
        def properties = new Properties()
        file("local.properties").withInputStream { properties.load(it) }
        def flutterSdkPath = properties.getProperty("flutter.sdk")
        assert flutterSdkPath != null, "flutter.sdk not set in local.properties"
        return flutterSdkPath
    }()

    includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")

    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

plugins {
    id "dev.flutter.flutter-plugin-loader" version "1.0.0"
    id "com.android.application" version "8.3.1" apply false
    id "org.jetbrains.kotlin.android" version "1.9.22" apply false
}

include ":app"
SETTINGSEOF

cat > /build/android/app/build.gradle <<APPEOF
plugins {
    id "com.android.application"
    id "org.jetbrains.kotlin.android"
    id "dev.flutter.flutter-gradle-plugin"
}

android {
    namespace "com.example.opusaudiolink_reporter"
    compileSdk 35
    ndkVersion "25.1.8937393"

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    defaultConfig {
        applicationId "com.example.opusaudiolink_reporter"
        minSdk 28
        targetSdk 35
        versionCode ${VERSION_CODE}
        versionName "0.7"

        ndk {
            abiFilters "arm64-v8a"
        }
    }

    signingConfigs {
        release {
            storeFile file("/tmp/oal_release.keystore")
            storePassword "oal_build_2024"
            keyAlias "oal"
            keyPassword "oal_build_2024"
        }
    }

    buildTypes {
        release {
            signingConfig signingConfigs.release
            minifyEnabled false
            shrinkResources false
        }
    }

    // jniLibs wird automatisch von Android eingebunden
    sourceSets {
        main {
            jniLibs.srcDirs = ['src/main/jniLibs']
        }
    }
}

flutter {
    source "../.."
}
APPEOF

mkdir -p /build/android/gradle/wrapper
cat > /build/android/gradle/wrapper/gradle-wrapper.properties <<'WRAPEOF'
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
distributionUrl=https\://services.gradle.org/distributions/gradle-8.4-all.zip
WRAPEOF

echo '── Kotlin-Dateien schreiben ──'
MAIN_KT=$(find /build/android -name 'MainActivity.kt' | head -1)
if [ -z "$MAIN_KT" ]; then
  MAIN_KT=/build/android/app/src/main/kotlin/com/example/opusaudiolink_reporter/MainActivity.kt
  mkdir -p "$(dirname $MAIN_KT)"
fi
KOTLIN_DIR=$(dirname "$MAIN_KT")

cat > "$MAIN_KT" <<'MAINEOF'
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
MAINEOF

cat > "$KOTLIN_DIR/OpusJni.kt" <<'OPUSJNIEOF'
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
OPUSJNIEOF

cat > "$KOTLIN_DIR/AudioEngine.kt" <<'AUDIOEOF'
package com.example.opusaudiolink_reporter

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

class AudioEngine(
    private val sampleRate: Int   = 48000,
    private val channels:   Int   = 1,
    private val bufferFrames: Int = 960   // 20 ms @ 48 kHz
) {
    companion object {
        private const val TAG = "AudioEngine"

        // Protokoll-Magic (identisch audio_gui.py)
        private val HELO_PING = byteArrayOf(0xDE.toByte(), 0xAD.toByte(), 0x48, 0x45, 0x4C, 0x4F)
        private val HELO_PONG = byteArrayOf(0xDE.toByte(), 0xAD.toByte(), 0x50, 0x4F, 0x4E, 0x47)
        private val REJECT_MAGIC = byteArrayOf(0xDE.toByte(), 0xAD.toByte(), 0x42, 0x55, 0x53, 0x59)

        // Header-Layout: seq(2) + timestamp(4) + reply-port(2) + return-bitrate-kbps(2) + flags(2) = 12 Bytes
        private const val HDR_SIZE = 12
        private const val HELO_TIMEOUT_MS = 5_000L
        private const val HELO_INTERVAL_MS = 2_000L
    }

    // ── Audio-Config ─────────────────────────────────────────────────────────
    private val chanIn  = if (channels == 1) AudioFormat.CHANNEL_IN_MONO   else AudioFormat.CHANNEL_IN_STEREO
    private val chanOut = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO  else AudioFormat.CHANNEL_OUT_STEREO
    private val enc     = AudioFormat.ENCODING_PCM_16BIT
    private val bytesPerFrame = channels * 2   // PCM16

    // ── Zustand ───────────────────────────────────────────────────────────────
    private val running     = AtomicBoolean(false)
    private val heloRunning = AtomicBoolean(false)

    // ── UDP-Config ────────────────────────────────────────────────────────────
    private var remoteAddr:      InetAddress? = null
    private var remoteSendPort:  Int = 0
    private var localRecvPort:   Int = 0
    private var txBitrate:       Int = 64_000
    private var returnBitrateKbps: Int = 64

    // ── Opus ──────────────────────────────────────────────────────────────────
    private var encoder: OpusJni.Encoder? = null
    private var decoder: OpusJni.Decoder? = null

    // ── Audio-Objekte ─────────────────────────────────────────────────────────
    private var recorder: AudioRecord? = null
    private var player:   AudioTrack?  = null

    // ── Sockets & Threads ─────────────────────────────────────────────────────
    private var sendSock:      DatagramSocket? = null
    private var recvSock:      DatagramSocket? = null
    private var captureThread: Thread? = null
    private var recvThread:    Thread? = null
    private var heloThread:    Thread? = null

    // ── Playback-Queue ────────────────────────────────────────────────────────
    private val playQueue = LinkedBlockingQueue<ShortArray>(200)

    // ── Statistik ─────────────────────────────────────────────────────────────
    @Volatile private var framesTx = 0
    @Volatile private var framesRx = 0
    @Volatile private var dropouts = 0
    @Volatile private var seqTx:   Int = 0

    // ── Callbacks → MainActivity → Flutter ───────────────────────────────────
    var onPcmCaptured:  ((ByteArray) -> Unit)? = null
    var onPcmReceived:  ((ByteArray) -> Unit)? = null
    var onStatusEvent:  ((Map<String, Any>) -> Unit)? = null

    // ════════════════════════════════════════════════════════════════════════
    //  Konfiguration
    // ════════════════════════════════════════════════════════════════════════

    fun configure(host: String, sendPort: Int, recvPort: Int, bitrate: Int, returnBitrate: Int) {
        remoteAddr         = if (host.isNotEmpty()) InetAddress.getByName(host) else null
        remoteSendPort     = sendPort
        localRecvPort      = recvPort
        txBitrate          = bitrate
        returnBitrateKbps  = returnBitrate / 1000

        encoder = OpusJni.Encoder(sampleRate, channels, bitrate)
        decoder = OpusJni.Decoder(sampleRate, channels)

        sendSock = DatagramSocket().also { it.trafficClass = 0xB8 }  // DSCP EF
        startRecvSocket()
    }

    // ════════════════════════════════════════════════════════════════════════
    //  Capture (Mic → Opus → UDP)
    // ════════════════════════════════════════════════════════════════════════

    fun startCapture() {
        if (running.get()) return
        running.set(true)
        val minBuf  = AudioRecord.getMinBufferSize(sampleRate, chanIn, enc)
        val bufSize = maxOf(minBuf, bufferFrames * bytesPerFrame * 2)
        recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION, sampleRate, chanIn, enc, bufSize
        ).also {
            if (it.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord init failed"); return
            }
            it.startRecording()
        }

        captureThread = Thread {
            val buf = ByteArray(bufferFrames * bytesPerFrame)
            while (running.get()) {
                val read = recorder?.read(buf, 0, buf.size) ?: break
                if (read <= 0) continue
                val pcm = buf.copyOf(read)
                onPcmCaptured?.invoke(pcm)   // VU-Meter TX
                sendOpusPacket(pcm)
                framesTx++
            }
        }.also { it.name = "AudioCapture"; it.isDaemon = true; it.start() }
    }

    // ════════════════════════════════════════════════════════════════════════
    //  Playback (Queue → AudioTrack)
    // ════════════════════════════════════════════════════════════════════════

    fun startPlayback() {
        val minBuf  = AudioTrack.getMinBufferSize(sampleRate, chanOut, enc)
        val bufSize = maxOf(minBuf, bufferFrames * bytesPerFrame * 4)
        player = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_GAME)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(sampleRate).setChannelMask(chanOut).setEncoding(enc).build()
            )
            .setBufferSizeInBytes(bufSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
            .build()
            .also { it.play() }

        Thread {
            val silence = ShortArray(bufferFrames * channels)
            var buffering = true
            val TARGET_FRAMES = 8  // 160ms Startpuffer (~150ms)
            while (running.get()) {
                // Buffering-Phase
                if (buffering) {
                    while (running.get() && playQueue.size < TARGET_FRAMES) {
                        player?.write(silence, 0, silence.size)
                    }
                    buffering = false
                }
                val samples = playQueue.poll(20, java.util.concurrent.TimeUnit.MILLISECONDS)
                if (samples != null) {
                    player?.write(samples, 0, samples.size)
                } else {
                    // Underrun → Stille und neu puffern
                    player?.write(silence, 0, silence.size)
                    dropouts++
                    buffering = true
                }
            }
        }.also { it.name = "AudioPlayback"; it.isDaemon = true; it.start() }
    }

    fun writePcm(data: ByteArray) {
        val shorts = ShortArray(data.size / 2) { i ->
            ((data[i * 2 + 1].toInt() shl 8) or (data[i * 2].toInt() and 0xFF)).toShort()
        }
        if (!playQueue.offer(shorts)) dropouts++
    }

    fun setMuted(muted: Boolean) {
        val vol = if (muted) 0f else 1f
        player?.setVolume(vol)
    }

    @Volatile private var attentionFrames = 0

    fun sendAttention() {
        attentionFrames = 10  // ~200ms senden, andere Seite macht 30s Countdown
    }

    // ════════════════════════════════════════════════════════════════════════
    //  UDP Send – Opus-Paket mit Header (identisch audio_gui.py)
    //  Header: seq(2) + ts(4) + reply-port(2) + return-kbps(2) + flags(2)
    // ════════════════════════════════════════════════════════════════════════

    private fun sendOpusPacket(pcm: ByteArray) {
        val enc  = encoder ?: return
        val sock = sendSock ?: return
        val addr = remoteAddr ?: return

        val opus = try { enc.encode(pcm, bufferFrames) } catch (e: Exception) {
            Log.w(TAG, "Opus encode error: $e"); return
        }

        val ts    = (System.currentTimeMillis() and 0xFFFFFFFFL).toInt()
        var flags = ((channels and 0x03) shl 1)
        if (attentionFrames > 0) {
            flags = flags or 0x0008
            attentionFrames--
        }
        val hdr   = ByteBuffer.allocate(HDR_SIZE).order(ByteOrder.BIG_ENDIAN)
            .putShort((seqTx and 0xFFFF).toShort())
            .putInt(ts)
            .putShort(localRecvPort.toShort())
            .putShort(returnBitrateKbps.toShort())
            .putShort(flags.toShort())
            .array()

        val pkt = hdr + opus
        try {
            sock.send(DatagramPacket(pkt, pkt.size, addr, remoteSendPort))
        } catch (e: Exception) {
            Log.w(TAG, "UDP send: $e")
        }
        seqTx++
    }

    // ════════════════════════════════════════════════════════════════════════
    //  UDP Recv – Opus-Pakete empfangen, dekodieren, in Queue
    // ════════════════════════════════════════════════════════════════════════

    private fun startRecvSocket() {
        recvSock?.close()
        val sock = DatagramSocket(localRecvPort).also {
            it.soTimeout = 500
            it.trafficClass = 0xB8  // DSCP EF
            recvSock = it
        }

        recvThread = Thread {
            val buf = ByteArray(8192)
            val pkt = DatagramPacket(buf, buf.size)
            while (running.get() || recvSock?.isClosed == false) {
                try {
                    sock.receive(pkt)
                    val data = pkt.data.copyOf(pkt.length)

                    // HELO-PONG abfangen (falls Studio antwortet)
                    if (data.size >= 6 && data.take(6).toByteArray().contentEquals(HELO_PONG)) continue
                    // REJECT
                    if (data.size >= 6 && data.take(6).toByteArray().contentEquals(REJECT_MAGIC)) {
                        onStatusEvent?.invoke(mapOf("rejected" to true))
                        continue
                    }

                    if (data.size < HDR_SIZE) continue

                    // Header parsen
                    val bb = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
                    bb.short  // seq – ignoriert
                    bb.int    // timestamp – ignoriert
                    bb.short  // reply-port
                    bb.short  // return-kbps
                    val pktFlags = bb.short.toInt()

                    // Kanalzahl aus Flags (Bits 1-2)
                    val pktChannels = (pktFlags shr 1) and 0x03
                    val rxChannels = if (pktChannels in 1..2) pktChannels else 1

                    // Attention Bit 3
                    if (pktFlags and 0x0008 != 0) {
                        onStatusEvent?.invoke(mapOf("attention" to true))
                    }

                    // Decoder neu aufbauen wenn Kanalzahl sich geändert hat
                    if (decoder == null || decoder!!.channels != rxChannels) {
                        decoder?.destroy()
                        decoder = OpusJni.Decoder(sampleRate, rxChannels)
                        // AudioTrack neu aufbauen mit neuer Kanalzahl
                        val rxChanOut = if (rxChannels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
                        val minBuf = AudioTrack.getMinBufferSize(sampleRate, rxChanOut, AudioFormat.ENCODING_PCM_16BIT)
                        val newPlayer = AudioTrack.Builder()
                            .setAudioAttributes(AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_GAME)
                                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                            .setAudioFormat(AudioFormat.Builder()
                                .setSampleRate(sampleRate).setChannelMask(rxChanOut)
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                            .setBufferSizeInBytes(maxOf(minBuf, bufferFrames * rxChannels * 2 * 4))
                            .setTransferMode(AudioTrack.MODE_STREAM)
                            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                            .build()
                        player?.stop(); player?.release()
                        player = newPlayer
                        player?.play()
                        playQueue.clear()
                        Log.i(TAG, "RX channels changed to $rxChannels")
                    }

                    val opus = data.copyOfRange(HDR_SIZE, data.size)
                    val dec  = decoder ?: continue

                    val pcmBytes = try {
                        dec.decode(opus, bufferFrames)
                    } catch (e: Exception) {
                        Log.w(TAG, "Opus decode: $e"); continue
                    }

                    // PCM16 → ShortArray für Playback
                    val shorts = ShortArray(pcmBytes.size / 2) { i ->
                        ((pcmBytes[i * 2 + 1].toInt() shl 8) or
                         (pcmBytes[i * 2].toInt() and 0xFF)).toShort()
                    }
                    if (!playQueue.offer(shorts)) {
                        // Queue voll → ältestes Frame verwerfen und neues einreihen
                        playQueue.poll()
                        playQueue.offer(shorts)
                    }
                    framesRx++

                    onPcmReceived?.invoke(pcmBytes)   // VU-Meter RX

                    onStatusEvent?.invoke(mapOf(
                        "connected"   to true,
                        "remoteAddr"  to "${pkt.address.hostAddress}:${pkt.port}",
                        "framesTx"    to framesTx,
                        "framesRx"    to framesRx,
                        "dropouts"    to dropouts,
                        "bufferMs"    to (playQueue.size * 20 / 10) * 10  // auf 10ms gerundet
                    ))

                } catch (_: java.net.SocketTimeoutException) {
                    // Timeout → kein Paket → disconnected
                    onStatusEvent?.invoke(mapOf(
                        "connected" to false,
                        "framesTx"  to framesTx,
                        "framesRx"  to framesRx,
                        "dropouts"  to dropouts
                    ))
                } catch (e: Exception) {
                    if (running.get()) Log.w(TAG, "UDP recv: $e")
                }
            }
        }.also { it.name = "UdpRecv"; it.isDaemon = true; it.start() }
    }

    // ════════════════════════════════════════════════════════════════════════
    //  HELO (Reporter → Studio)
    //  Magic: 0xDEAD + HELO  +  reply-port (2 Bytes big-endian)
    //  Antwort: 0xDEAD + PONG
    //  Timeout: 5 s, Intervall: 2 s
    // ════════════════════════════════════════════════════════════════════════

    fun startHelo(host: String, port: Int) {
        stopHelo()
        heloRunning.set(true)

        heloThread = Thread {
            var lastPong = 0L
            var wasReachable = false

            try {
                val sock = DatagramSocket().also { it.soTimeout = 1000 }
                // Auf freiem Port lauschen für PONG
                val replyPort = sock.localPort
                val addr = InetAddress.getByName(host)

                // Ping-Paket: Magic (6) + reply-port (2)
                val ping = HELO_PING + ByteBuffer.allocate(2)
                    .order(ByteOrder.BIG_ENDIAN)
                    .putShort(replyPort.toShort())
                    .array()

                val ackBuf = ByteArray(32)
                val ackPkt = DatagramPacket(ackBuf, ackBuf.size)

                while (heloRunning.get()) {
                    try {
                        sock.send(DatagramPacket(ping, ping.size, addr, port))
                    } catch (e: Exception) { Log.w(TAG, "HELO send: $e") }

                    try {
                        sock.receive(ackPkt)
                        val resp = ackPkt.data.copyOf(ackPkt.length)
                        if (resp.size >= 6 && resp.take(6).toByteArray().contentEquals(HELO_PONG)) {
                            lastPong = System.currentTimeMillis()
                        }
                    } catch (_: java.net.SocketTimeoutException) { }

                    val reachable = (System.currentTimeMillis() - lastPong) < HELO_TIMEOUT_MS
                    if (reachable != wasReachable) {
                        wasReachable = reachable
                        onStatusEvent?.invoke(mapOf(
                            "heloOk"     to reachable,
                            "remoteAddr" to if (reachable) host else ""
                        ))
                    }

                    Thread.sleep(HELO_INTERVAL_MS)
                }
                sock.close()
            } catch (e: Exception) {
                Log.e(TAG, "HELO thread: $e")
                onStatusEvent?.invoke(mapOf("heloOk" to false, "remoteAddr" to ""))
            }
        }.also { it.name = "HeloThread"; it.isDaemon = true; it.start() }
    }

    fun stopHelo() {
        heloRunning.set(false)
        heloThread?.join(500)
        heloThread = null
    }

    // ════════════════════════════════════════════════════════════════════════
    //  Stop
    // ════════════════════════════════════════════════════════════════════════

    fun stop() {
        running.set(false)
        stopHelo()
        captureThread?.join(500); captureThread = null
        recvThread?.join(500);    recvThread    = null
        recorder?.stop(); recorder?.release(); recorder = null
        player?.stop();   player?.release();   player   = null
        sendSock?.close(); sendSock = null
        recvSock?.close(); recvSock = null
        encoder?.destroy(); encoder = null
        decoder?.destroy(); decoder = null
        playQueue.clear()
        framesTx = 0; framesRx = 0; dropouts = 0; seqTx = 0
    }

    val isRunning: Boolean get() = running.get()
}
AUDIOEOF

echo '── opusjni.c schreiben ──'
mkdir -p /build/android/app/src/main/cpp
cat > /build/android/app/src/main/cpp/opusjni.c <<'CEOF'
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
CEOF

# CMakeLists für opusjni.so
cat > /build/android/app/src/main/cpp/CMakeLists.txt <<'CMAKEOF'
cmake_minimum_required(VERSION 3.22)
project(opusjni)

# libopus.so liegt in jniLibs/arm64-v8a – als imported library einbinden
# CMAKE_CURRENT_SOURCE_DIR = .../app/src/main/cpp
set(OPUS_SO "${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}/libopus.so")

message(STATUS "libopus.so path: ${OPUS_SO}")

if(NOT EXISTS "${OPUS_SO}")
    message(FATAL_ERROR "libopus.so not found at: ${OPUS_SO}")
endif()

add_library(opus SHARED IMPORTED)
set_target_properties(opus PROPERTIES IMPORTED_LOCATION "${OPUS_SO}")

add_library(opusjni SHARED opusjni.c)
target_include_directories(opusjni PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(opusjni opus log)
CMAKEOF

# CMake in build.gradle eintragen
sed -i 's|ndkVersion "25.1.8937393"|ndkVersion "25.1.8937393"\n\n    externalNativeBuild {\n        cmake {\n            path "src/main/cpp/CMakeLists.txt"\n            version "3.22.1"\n        }\n    }|' /build/android/app/build.gradle

echo '── App-Icons erzeugen ──'
pip3 install --quiet Pillow
python3 - <<'PYEOF'
from PIL import Image, ImageDraw
import os

def make_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Hintergrund mit abgerundeten Ecken (Rechteck + Kreis-Ecken simuliert)
    r = size // 6
    d.rounded_rectangle([0, 0, size-1, size-1], radius=r, fill=(26, 42, 58, 255))
    # Innerer Bereich
    m = size // 17
    d.rounded_rectangle([m, m, size-m-1, size-m-1], radius=r, fill=(30, 48, 80, 255))
    # Mikrofon-Körper
    cx = size // 2
    mw = size // 7
    mh = size // 4
    my = size // 4
    d.rounded_rectangle([cx-mw, my, cx+mw, my+mh], radius=mw, fill=(74, 122, 204, 255))
    # Mikrofon-Bogen
    bx0 = cx - size//4
    bx1 = cx + size//4
    by0 = my + mh//2
    by1 = my + mh + size//8
    d.arc([bx0, by0, bx1, by1], start=0, end=180, fill=(74, 122, 204, 255), width=max(2, size//24))
    # Stiel
    lw = max(2, size//24)
    d.line([cx, by1, cx, by1+size//10], fill=(74, 122, 204, 255), width=lw)
    d.line([cx-size//10, by1+size//10, cx+size//10, by1+size//10], fill=(74, 122, 204, 255), width=lw)
    # Schallwellen links
    d.arc([cx-size//3, my+size//8, cx-size//8, my+mh], start=120, end=240, fill=(42, 140, 58, 255), width=max(1, size//32))
    # Schallwellen rechts
    d.arc([cx+size//8, my+size//8, cx+size//3, my+mh], start=300, end=60, fill=(42, 140, 58, 255), width=max(1, size//32))
    return img

sizes = {'mdpi':48,'hdpi':72,'xhdpi':96,'xxhdpi':144,'xxxhdpi':192}
for dpi, size in sizes.items():
    path = '/build/android/app/src/main/res/mipmap-' + dpi
    os.makedirs(path, exist_ok=True)
    make_icon(size).save(path + '/ic_launcher.png', 'PNG')
print('Icons OK')
PYEOF

echo '── APK bauen ──'
flutter build apk --release

echo '── APK kopieren ──'
cp build/app/outputs/flutter-apk/app-release.apk /dist/"$APK_NAME"
ls -lh /dist/"$APK_NAME"
echo '✓ Fertig'
INNEREOF

chmod +x "$INNER_SCRIPT"

docker run --rm \
  -v "$SCRIPT_DIR/opusaudiolink_reporter":/build \
  -v "$SCRIPT_DIR/dist":/dist \
  -v "$KEYSTORE_PATH":/tmp/oal_release.keystore:ro \
  -v "$INNER_SCRIPT":/tmp/inner_build.sh \
  -w /build \
  "$IMAGE" \
  bash /tmp/inner_build.sh \
    "$FLUTTER_URL" "$CMDLINE_TOOLS_URL" "$APK_NAME" "$VERSION_CODE" "$OPUS_URL"

sudo chown -R "$CURRENT_USER" "$SCRIPT_DIR/dist" 2>/dev/null || true
sudo chown -R "$CURRENT_USER" "$SCRIPT_DIR/opusaudiolink_reporter" 2>/dev/null || true

echo "================================================"
echo "  Build abgeschlossen: dist/$APK_NAME"
echo "================================================"
