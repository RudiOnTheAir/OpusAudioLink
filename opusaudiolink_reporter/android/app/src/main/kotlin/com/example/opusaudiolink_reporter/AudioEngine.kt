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
