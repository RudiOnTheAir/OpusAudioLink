#!/usr/bin/env python3
"""
Bidirectional Audio - GUI
Shared PyQt6 interface for Reporter and Studio.

Dependencies:
    pip install sounddevice numpy PyQt6
    Linux:   pip install opuslib
    Windows: pip install PyOgg

Usage:
    python3 audio_gui.py
"""

import sys
import socket
import configparser
import threading
import time
import struct
import collections
import os
from pathlib import Path

try:
    import sounddevice as sd
    from opus_backend import OpusEncoder, OpusDecoder, backend_info, BACKEND
    import numpy as np
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QComboBox, QSpinBox, QGroupBox,
        QStackedWidget, QLineEdit, QFrame, QSizePolicy
    )
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
    from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont, QPen
except ImportError as e:
    print(f"Import error: {e}")
    print("Please install: pip install sounddevice numpy PyQt6")
    print("  Linux:   pip install opuslib")
    print("  Windows: pip install PyOgg")
    sys.exit(1)


# ─────────────────────────────────────────────
#  Audio Konstanten
# ─────────────────────────────────────────────
SAMPLE_RATE = 48000
FRAME_MS    = 20
FRAME_SIZE  = int(SAMPLE_RATE * FRAME_MS / 1000)  # 960 samples

BITRATE_OPTIONS = [32000, 64000, 96000, 128000, 192000, 256000, 320000]

# VU-Meter Pegelkorrektur in dB (positiv = Anzeige höher)
VU_LEVEL_OFFSET_DB = 12

# Shared state – wird von Audio-Threads beschrieben, GUI liest
shared = {
    "tx_level_l":   0.0,   # Sendepegel L (0.0–1.0)
    "tx_level_r":   0.0,   # Sendepegel R
    "rx_level_l":   0.0,   # Empfangspegel L
    "rx_level_r":   0.0,   # Empfangspegel R
    "latency_ms":   None,  # Letzte gemessene Latenz
    "latency_avg":  None,
    "frames_tx":    0,
    "frames_rx":    0,
    "connected":    False,
    "remote_addr":  "",
    "rx_bitrate":   None,  # Rückkanal-Bitrate: Reporter-Wunsch = Studio-TX
    "tx_bitrate":   None,  # Reporter-Senderate (aus Paketgröße gemessen) = Studio-RX
    "rx_channels":  None,  # Kanalzahl vom Reporter (automatisch erkannt)
    "jitter_buf_ms":        0,
    "jitter_buf_target_ms": 0,
    "jitter_ms":     None,  # Gemessener Jitter in ms
    "underrun":      False, # PLC/Unterlauf aktiv
    "robust_mode":   False,
    "robust_rx":     False,
    "dropout_count":  0,
    "dropout_last":   None,
    "start_time":     None,
    "elapsed_frozen": None,
    "status":       "Stopped",
    "kick_ip":      None,
    "ban_ips":      {},
    "attention":     0,     # TX-Countdown Frames (kurz, für Übertragung)
    "attention_tx":  0,     # Anzeige-Countdown TX (30s)
    "attention_rx":  0,     # Anzeige-Countdown RX (30s)
}

latency_samples = collections.deque(maxlen=100)
stop_event      = threading.Event()
_monitor_stop   = threading.Event()
_engine_threads = []   # Laufende Audio-Threads zum sauberen Stopp
_monitor_stop.set()   # initial: kein Monitor aktiv
_encoder        = None
_decoder        = None
_output_stream  = None
_send_sock      = None
_recv_sock      = None


# ─────────────────────────────────────────────
#  Audio Engine
# ─────────────────────────────────────────────

def _rms(data):
    """RMS-Pegel eines numpy-Arrays (Float32)."""
    if len(data) == 0:
        return 0.0
    return float(np.sqrt(np.mean(data ** 2)))


def get_device_list():
    """Gibt Input- und Output-Geräte als Listen zurück: [(index, name), ...]"""
    devices = sd.query_devices()
    inputs  = [(i, d['name']) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
    outputs = [(i, d['name']) for i, d in enumerate(devices) if d['max_output_channels'] > 0]
    return inputs, outputs


def start_engine(mode, host, send_port, recv_port, channels, bitrate,
                 input_device=None, output_device=None, return_bitrate=64000,
                 jitter_frames=4):
    """Startet Audio-Threads für den gewählten Modus."""
    global _encoder, _decoder, _output_stream, _send_sock, _recv_sock, _engine_threads

    # Warten bis alle alten Threads wirklich beendet sind
    for t in _engine_threads:
        t.join(timeout=3.0)
    _engine_threads.clear()
    stop_event.clear()

    # Sicherstellen dass ALSA den alten Stream vollstaendig freigegeben hat
    if _output_stream is not None:
        time.sleep(0.4)

    _encoder = OpusEncoder(SAMPLE_RATE, channels, bitrate)
    _decoder = OpusDecoder(SAMPLE_RATE, channels)

    shared["status"] = "Running"
    shared["frames_tx"] = 0
    shared["frames_rx"] = 0
    shared["dropout_count"] = 0
    shared["dropout_last"]  = None
    shared["start_time"]    = None   # wird beim ersten Paket gesetzt
    shared["robust_mode"]          = False
    shared["robust_rx"]            = False
    shared["kick_ip"]              = None
    shared["jitter_buf_ms"]        = 0
    shared["jitter_buf_target_ms"] = 0
    shared["jitter_ms"]            = None
    latency_samples.clear()

    _send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF
    _recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF
    _recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _recv_sock.bind(("0.0.0.0", recv_port))
    except OSError as e:
        shared["status"] = f"Port {recv_port} in use: {e}"
        stop_event.set()
        return

    # Sounddevice-Geräte setzen
    if input_device is not None:
        sd.default.device[0] = input_device
    if output_device is not None:
        sd.default.device[1] = output_device

    if mode == "reporter":
        t_send = threading.Thread(
            target=_send_loop,
            args=(host, send_port, recv_port, channels, bitrate, return_bitrate),
            daemon=True
        )
        t_send.start()
        _engine_threads.append(t_send)

    # Empfang in beiden Modi; Studio startet Rückkanal automatisch
    t_recv = threading.Thread(
        target=_recv_loop,
        args=(recv_port, channels, mode, recv_port, jitter_frames),
        daemon=True
    )
    t_recv.start()
    _engine_threads.append(t_recv)


def stop_engine(callback=None):
    """
    Stoppt die Audio-Engine nicht-blockierend.
    callback() wird aufgerufen sobald alles sauber beendet ist.
    """
    global _send_sock, _recv_sock, _output_stream

    stop_event.set()
    shared["status"]    = "Stopped"
    shared["connected"] = False

    def _do_stop():
        global _send_sock, _recv_sock, _output_stream, _engine_threads
        # stop_event ist bereits gesetzt – warten damit Audio-Callbacks es sehen
        time.sleep(0.2)

        # Erst alle Engine-Threads joinen – danach ist sicher kein write() mehr aktiv
        for t in list(_engine_threads):
            t.join(timeout=3.0)
        _engine_threads.clear()

        # Jetzt erst Stream schliessen – kein Thread schreibt mehr rein
        stream = _output_stream
        _output_stream = None          # global auf None setzen bevor close()
        if stream is not None:
            try:
                if stream.active:
                    stream.stop()
                stream.close()
            except Exception:
                pass

        # ALSA vollstaendig freigeben lassen
        time.sleep(0.3)

        try:
            if _send_sock:
                _send_sock.close()
            if _recv_sock:
                _recv_sock.close()
        except Exception:
            pass
        finally:
            _send_sock = None
            _recv_sock = None

        if callback:
            callback()

    threading.Thread(target=_do_stop, daemon=True).start()



# ─────────────────────────────────────────────
#  HELO – Studio-Erreichbarkeit
# ─────────────────────────────────────────────

# Magic-Header für HELO-Protokoll
HELO_PING = b'\xDE\xAD\x48\x45\x4C\x4F'   # DEAD + HELO
HELO_PONG = b'\xDE\xAD\x50\x4F\x4E\x47'   # DEAD + PONG

_helo_stop   = threading.Event()
_helo_active = False

def start_helo(host, send_port, recv_port, on_status):
    """
    Startet den HELO-Ping-Thread (nur Reporter-Modus).
    on_status(True/False) wird im Hintergrund-Thread aufgerufen.
    """
    global _helo_active
    _helo_stop.clear()
    _helo_active = True

    def _run():
        global _helo_active
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        # Auf einem freien Port lauschen für PONG
        sock.bind(("0.0.0.0", 0))
        local_port = sock.getsockname()[1]

        # HELO-Paket: Magic + reply-port (2 Bytes)
        helo_pkt = HELO_PING + local_port.to_bytes(2, 'big')

        last_seen = 0.0
        TIMEOUT = 5.0
        last_status = None  # Hysterese: nur bei Aenderung melden

        while not _helo_stop.is_set():
            try:
                sock.sendto(helo_pkt, (host, send_port))
            except Exception:
                pass

            try:
                data, _ = sock.recvfrom(32)
                if data[:6] == HELO_PONG:
                    last_seen = time.time()
            except socket.timeout:
                pass
            except Exception:
                pass

            reachable = (time.time() - last_seen) < TIMEOUT
            if reachable != last_status:
                last_status = reachable
                on_status(reachable)
            time.sleep(2.0)

        sock.close()
        _helo_active = False

    threading.Thread(target=_run, daemon=True).start()


def stop_helo():
    _helo_stop.set()



def start_monitor(channels, input_device, duration=10):
    """
    Öffnet nur den Eingang für `duration` Sekunden und schreibt
    den Pegel in shared – ohne zu senden oder zu verbinden.
    """
    _monitor_stop.clear()
    shared["tx_level_l"] = 0.0
    shared["tx_level_r"] = 0.0

    def _run():
        def callback(indata, frames, time_info, status):
            if _monitor_stop.is_set():
                return
            shared["tx_level_l"] = _rms(indata[:, 0])
            shared["tx_level_r"] = _rms(indata[:, 1]) if channels > 1 else shared["tx_level_l"]

        try:
            if input_device is not None:
                sd.default.device[0] = input_device
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=channels,
                dtype='float32',
                blocksize=FRAME_SIZE,
                callback=callback
            ):
                deadline = time.time() + duration
                while not _monitor_stop.is_set() and time.time() < deadline:
                    time.sleep(0.05)
        except Exception as e:
            shared["status"] = f"Monitor error: {e}"
        finally:
            shared["tx_level_l"] = 0.0
            shared["tx_level_r"] = 0.0
            _monitor_stop.set()

    threading.Thread(target=_run, daemon=True).start()


def stop_monitor():
    _monitor_stop.set()


def _send_loop_with_stop(host, port, reply_port, channels, bitrate, return_bitrate, local_stop):
    """Wie _send_loop aber mit eigenem stop_event – für Studio-Rückkanal."""
    enc = OpusEncoder(SAMPLE_RATE, channels, bitrate)
    seq = 0

    def callback(indata, frames, time_info, status):
        nonlocal seq
        if stop_event.is_set() or local_stop.is_set():
            return
        try:
            shared["tx_level_l"] = _rms(indata[:, 0])
            shared["tx_level_r"] = _rms(indata[:, 1]) if channels > 1 else shared["tx_level_l"]
            if BACKEND == "ctypes":
                encoded = enc.encode_float(indata.tobytes(), FRAME_SIZE)
            else:
                pcm = (indata * 32767).astype(np.int16).tobytes()
                encoded = enc.encode(pcm, FRAME_SIZE)
            ts = int(time.time() * 1000) & 0xFFFFFFFF
            flags = 0x0001 if shared.get("robust_mode", False) else 0x0000
            flags |= (channels & 0x03) << 1   # Kanalzahl in Bits 1-2
            # Bit 3: Attention
            attn = shared.get("attention", 0)
            if attn > 0:
                flags |= 0x0008
                shared["attention"] = attn - 1
            header = struct.pack("!HIHHH", seq & 0xFFFF, ts, reply_port, return_bitrate // 1000, flags)
            _send_sock.sendto(header + encoded, (host, port))
            seq += 1
            shared["frames_tx"] += 1
        except Exception as e:
            if not stop_event.is_set() and not local_stop.is_set():
                shared["status"] = f"TX error: {e}"

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=channels,
            dtype='float32',
            blocksize=FRAME_SIZE,
            callback=callback
        ):
            while not stop_event.is_set() and not local_stop.is_set():
                time.sleep(0.05)
    except Exception as e:
        if not stop_event.is_set() and not local_stop.is_set():
            shared["status"] = f"Stream error: {e}"
    finally:
        shared["tx_level_l"] = 0.0
        shared["tx_level_r"] = 0.0


def _send_loop(host, port, reply_port, channels, bitrate, return_bitrate=0):
    seq = 0

    def callback(indata, frames, time_info, status):
        nonlocal seq
        if stop_event.is_set():
            return
        try:
            shared["tx_level_l"] = _rms(indata[:, 0])
            shared["tx_level_r"] = _rms(indata[:, 1]) if channels > 1 else shared["tx_level_l"]

            if BACKEND == "ctypes":
                encoded = _encoder.encode_float(indata.tobytes(), FRAME_SIZE)
            else:
                pcm = (indata * 32767).astype(np.int16).tobytes()
                encoded = _encoder.encode(pcm, FRAME_SIZE)

            # Header: [seq:2][timestamp:4][reply-port:2][return-bitrate-kbps:2][flags:2]
            # flags Bit 0: robust_mode  |  Bits 1-2: channels  |  Bit 3: attention
            ts = int(time.time() * 1000) & 0xFFFFFFFF
            flags = 0x0001 if shared.get("robust_mode", False) else 0x0000
            flags |= (channels & 0x03) << 1
            attn = shared.get("attention", 0)
            if attn > 0:
                flags |= 0x0008
                shared["attention"] = attn - 1
            header = struct.pack("!HIHHH", seq & 0xFFFF, ts,
                                 reply_port, return_bitrate // 1000, flags)
            _send_sock.sendto(header + encoded, (host, port))
            seq += 1
            shared["frames_tx"] += 1
        except Exception as e:
            if not stop_event.is_set():
                shared["status"] = f"TX error: {e}"

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=channels,
            dtype='float32',
            blocksize=FRAME_SIZE,
            callback=callback
        ):
            while not stop_event.is_set():
                time.sleep(0.05)
    except Exception as e:
        if not stop_event.is_set():
            shared["status"] = f"Stream error: {e}"


def _recv_loop(recv_port, channels, mode, reply_port, jitter_frames=4):
    global _output_stream, _decoder
    import queue as _queue

    # ── Konstanten ────────────────────────────────────────────────────────────
    MIN_FRAMES      = max(1, jitter_frames)   # Untergrenze = manuell eingestellt
    MAX_FRAMES_NORMAL  = 400 // FRAME_MS      # Normalmode Obergrenze 400 ms
    MAX_FRAMES_ROBUST  = 5000 // FRAME_MS     # Robust-Modus Obergrenze 5 s
    MAX_FRAMES         = MAX_FRAMES_ROBUST    # Queue immer maximal vorhalten (kostet kaum RAM)
    ROBUST_MIN_FRAMES  = 2000 // FRAME_MS     # Mindest-Ziel im Robust-Modus (2 s)
    JITTER_WINDOW   = 50                       # Letzte N Abstände für Jitter-Messung
    ADAPT_UP        = 0.15   # Wie schnell Buffer-Ziel steigt  (pro Paket, aggressiv)
    ADAPT_DOWN      = 0.002  # Wie schnell Buffer-Ziel fällt   (pro Paket, langsam)

    # ── State ─────────────────────────────────────────────────────────────────
    jitter_buf      = _queue.Queue(maxsize=MAX_FRAMES * 2)
    silence         = np.zeros((FRAME_SIZE, channels), dtype=np.float32)
    _plc_last       = [silence.copy()]
    _target_frames  = [float(MIN_FRAMES)]     # aktuelles Ziel (float für sanftes Anpassen)
    _arrival_times  = collections.deque(maxlen=JITTER_WINDOW)
    _last_arrival   = [None]
    _rebuffer       = [False]   # Wird auf True gesetzt wenn Buffering-Phase neu gestartet werden soll
    _last_rx_robust = [False]   # Letzter bekannter robust_rx Zustand (Reporter-Modus)

    shared["jitter_buf_target_ms"] = int(_target_frames[0]) * FRAME_MS

    # ── Output Stream ─────────────────────────────────────────────────────────
    _output_stream = sd.OutputStream(
        samplerate=SAMPLE_RATE,
        channels=channels,
        dtype='float32',
        blocksize=FRAME_SIZE
    )
    _output_stream.start()

    def _update_target(jitter_ms):
        robust = shared.get("robust_mode", False) if mode == "studio" else shared.get("robust_rx", False)
        cur_min = ROBUST_MIN_FRAMES if robust else MIN_FRAMES
        cur_max = MAX_FRAMES_ROBUST if robust else MAX_FRAMES_NORMAL

        jitter_frames_needed = (jitter_ms * 2) / FRAME_MS
        if jitter_frames_needed > _target_frames[0]:
            _target_frames[0] += ADAPT_UP * (jitter_frames_needed - _target_frames[0])
        else:
            _target_frames[0] -= ADAPT_DOWN * (_target_frames[0] - jitter_frames_needed)
        _target_frames[0] = max(float(cur_min), min(float(cur_max), _target_frames[0]))
        shared["jitter_buf_target_ms"] = int(_target_frames[0]) * FRAME_MS
        shared["jitter_ms"] = round(jitter_ms, 1)

    def _playback():
        """
        Playback-Thread:
        - Startet im Buffering-Modus (wartet bis Mindestfüllung erreicht)
        - Normalbetrieb: holt Frames aus Queue, gibt sie aus
        - Unterlauf: PLC (gedämpfter letzter Frame), dann Re-Buffering
        - Robust-Moduswechsel: einmalige Buffering-Pause bis neues Ziel erreicht,
          dabei Stille ausgeben. Dauert bei 2s-Ziel ca. 2s – danach stabil.
          Zurückschalten: sofort, Queue wird auf Normalgröße geleert.
        """
        buffering   = True
        silence_out = np.zeros((FRAME_SIZE, channels), dtype=np.float32)

        def _safe_write(pcm_data):
            """Schreibt in den Output-Stream – liest _output_stream jedes Mal neu."""
            if stop_event.is_set():
                return
            s = _output_stream
            if s is None:
                return
            try:
                if s.active:
                    s.write(pcm_data)
            except Exception:
                pass

        while not stop_event.is_set():

            # ── Rebuffer-Anforderung (Moduswechsel) ──────────────────
            if _rebuffer[0]:
                _rebuffer[0] = False
                buffering    = True

            # ── Buffering-Phase ─────────────────────────────────────────────────────
            if buffering:
                target = max(1, int(_target_frames[0]))
                while not stop_event.is_set() and jitter_buf.qsize() < target:
                    shared["jitter_buf_ms"] = jitter_buf.qsize() * FRAME_MS
                    _safe_write(silence_out)
                    time.sleep(0.005)
                buffering = False

            # ── Playback-Phase ────────────────────────────────────────────────────
            try:
                pcm = jitter_buf.get(timeout=FRAME_MS / 1000 * 2)
                _plc_last[0] = pcm
            except _queue.Empty:
                pcm = (_plc_last[0] * 0.5).astype(np.float32)
                shared["rx_level_l"] = 0.0
                shared["rx_level_r"] = 0.0
                if not shared["underrun"]:
                    shared["dropout_count"] += 1
                    shared["dropout_last"] = time.strftime("%H:%M:%S")
                shared["underrun"] = True
                buffering = True

            _safe_write(pcm)


            # Füllstand in ms anzeigen
            shared["jitter_buf_ms"] = jitter_buf.qsize() * FRAME_MS
            # Underrun-Flag zurücksetzen sobald Buffer wieder auf Ziel
            if shared["underrun"] and jitter_buf.qsize() >= int(_target_frames[0]):
                shared["underrun"] = False

    playback_thread = threading.Thread(target=_playback, daemon=True)
    playback_thread.start()
    _engine_threads.append(playback_thread)

    # ── Besetzt-Protokoll ─────────────────────────────────────────────────────
    # Magic-Header für Reject-Pakete: 0xDEAD + "BUSY"
    REJECT_MAGIC = b'\xDE\xAD\x42\x55\x53\x59'   # DEAD + BUSY

    _recv_sock.settimeout(1.0)
    _known_reporter = None
    _send_loop_stop = [None]   # threading.Event für aktuellen Rückkanal-Thread
    _send_loop_thread = [None]
    _timeout_count = [0]       # Aufeinanderfolgende Timeouts – Rückkanal erst nach 3s stoppen

    while not stop_event.is_set():
        try:
            data, addr = _recv_sock.recvfrom(4096)

            # ── HELO-Ping beantworten (Studio-Seite) ─────────────────────────
            if data[:6] == HELO_PING and mode == "studio":
                try:
                    reply_port = int.from_bytes(data[6:8], 'big') if len(data) >= 8 else 0
                    if reply_port > 0:
                        _recv_sock.sendto(HELO_PONG, (addr[0], reply_port))
                except Exception:
                    pass
                continue

            # ── Kick+Ban: GUI hat Ban-Button gedrückt ─────────────────
            if mode == "studio":
                kick_ip = shared.get("kick_ip")
                if kick_ip is not None:
                    shared["kick_ip"] = None
                    shared["ban_ips"][kick_ip] = time.time() + 600  # 10 Minuten
                    if _known_reporter is not None and _known_reporter[0] == kick_ip:
                        try:
                            _recv_sock.sendto(REJECT_MAGIC, (kick_ip, _known_reporter[1]))
                        except Exception:
                            pass
                        _known_reporter = None
                        shared["remote_addr"] = ""
                        shared["connected"]   = False
                        if _send_loop_stop[0] is not None:
                            _send_loop_stop[0].set()
                            _send_loop_stop[0]   = None
                            _send_loop_thread[0] = None

            # ── Gebannte IP ablehnen ────────────────────────────────────────────────
            if mode == "studio" and len(data) >= 8:
                src_ip   = addr[0]
                ban_until = shared["ban_ips"].get(src_ip, 0)
                if time.time() < ban_until:
                    raw_reply_port = int.from_bytes(data[6:8], "big")
                    try:
                        if raw_reply_port > 0:
                            _recv_sock.sendto(REJECT_MAGIC, (src_ip, raw_reply_port))
                        else:
                            _recv_sock.sendto(REJECT_MAGIC, addr)
                    except Exception:
                        pass
                    continue

            # ── Reject-Paket empfangen (Reporter-Seite) ───────────────────────
            if data[:6] == REJECT_MAGIC:
                if mode == "reporter":
                    shared["status"]    = "Busy – remote end occupied"
                    shared["connected"] = False
                    stop_event.set()   # Beendet Send-Loop und diese Schleife sauber
                continue

            # ── Studio: zweiten Reporter ablehnen VOR dem length-check ──────────
            # Reply-Port aus Roh-Bytes lesen (Header: seq:2, ts:4, reply-port:2 -> offset 6)
            if mode == "studio" and _known_reporter is not None:
                raw_reply_port = int.from_bytes(data[6:8], 'big') if len(data) >= 8 else 0
                known_ip = _known_reporter[0]
                if addr[0] != known_ip:
                    try:
                        if raw_reply_port > 0:
                            _recv_sock.sendto(REJECT_MAGIC, (addr[0], raw_reply_port))
                        else:
                            _recv_sock.sendto(REJECT_MAGIC, addr)
                    except Exception:
                        pass
                    continue   # Paket nicht verarbeiten

            if len(data) < 12:
                continue

            recv_ts = int(time.time() * 1000) & 0xFFFFFFFF
            seq, send_ts, pkt_reply_port, pkt_return_kbps, pkt_flags = struct.unpack("!HIHHH", data[:12])
            encoded = data[12:]

            # Kanalzahl aus Flags lesen (Bits 1-2)
            pkt_channels = (pkt_flags >> 1) & 0x03
            if pkt_channels not in (1, 2):
                pkt_channels = channels

            # Attention Bit 3 auswerten
            if pkt_flags & 0x0008:
                shared["attention_rx_time"] = time.time()  # 30s per Wall-Clock

            # Robust-Modus Flag auswerten – nur im Studio relevant
            # (Reporter ignoriert das Flag im Rückkanal)
            if mode == "studio":
                pkt_robust = bool(pkt_flags & 0x0001)
                if shared.get("robust_mode") != pkt_robust:
                    shared["robust_mode"] = pkt_robust
                    if pkt_robust:
                        _target_frames[0] = float(ROBUST_MIN_FRAMES)
                        _rebuffer[0] = True
                    else:
                        cur_max = MAX_FRAMES_NORMAL
                        while jitter_buf.qsize() > cur_max:
                            try:
                                jitter_buf.get_nowait()
                            except Exception:
                                break
                        _target_frames[0] = float(MIN_FRAMES)
                        _rebuffer[0] = True
                    shared["jitter_buf_target_ms"] = int(_target_frames[0]) * FRAME_MS
            else:
                # Reporter-Modus: robust_rx live prüfen
                rx_robust = shared.get("robust_rx", False)
                if rx_robust != _last_rx_robust[0]:
                    _last_rx_robust[0] = rx_robust
                    if rx_robust:
                        _target_frames[0] = float(ROBUST_MIN_FRAMES)
                        _rebuffer[0] = True
                    else:
                        cur_max = MAX_FRAMES_NORMAL
                        while jitter_buf.qsize() > cur_max:
                            try:
                                jitter_buf.get_nowait()
                            except Exception:
                                break
                        _target_frames[0] = float(MIN_FRAMES)
                        _rebuffer[0] = True
                    shared["jitter_buf_target_ms"] = int(_target_frames[0]) * FRAME_MS

            lat = (recv_ts - send_ts) & 0xFFFFFFFF
            if lat < 2000:
                latency_samples.append(lat)
                shared["latency_ms"] = lat
                if latency_samples:
                    shared["latency_avg"] = int(sum(latency_samples) / len(latency_samples))

            # Jitter aus Paket-Ankunftsabständen messen (unabhängig von Uhren)
            now = time.monotonic()
            if _last_arrival[0] is not None:
                interval_ms = (now - _last_arrival[0]) * 1000
                _arrival_times.append(interval_ms)
                if len(_arrival_times) >= 5:
                    avg_interval = sum(_arrival_times) / len(_arrival_times)
                    jitter_ms = sum(abs(x - avg_interval) for x in _arrival_times) / len(_arrival_times)
                    _update_target(jitter_ms)
            _last_arrival[0] = now

            if BACKEND == "ctypes":
                pcm_bytes = _decoder.decode_float(encoded, FRAME_SIZE)
                pcm = np.frombuffer(pcm_bytes, dtype=np.float32).reshape(-1, pkt_channels)
            else:
                pcm_bytes = _decoder.decode(encoded, FRAME_SIZE)
                pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32767.0
                pcm = pcm.reshape(-1, pkt_channels)

            shared["rx_level_l"] = _rms(pcm[:, 0])
            shared["rx_level_r"] = _rms(pcm[:, 1]) if pkt_channels > 1 else shared["rx_level_l"]

            # In Jitter-Buffer einreihen, bei Überlauf ältestes Paket verwerfen
            if jitter_buf.full():
                try:
                    jitter_buf.get_nowait()
                except _queue.Empty:
                    pass
            jitter_buf.put_nowait(pcm)

            shared["frames_rx"] += 1
            shared["connected"] = True
            _timeout_count[0] = 0   # Verbindung aktiv – Zähler zurücksetzen
            if shared["start_time"] is None:
                shared["start_time"] = time.time()  # Reporter-Modus: beim ersten Paket

            # TX-Bitrate aus Paketgröße schätzen (nur Studio – das ist die Reporter-Senderate)
            # Gleitender Durchschnitt über 50 Pakete (= 1s) um VBR-Zappeln zu dämpfen
            if mode == "studio":
                pkt_bits = len(encoded) * 8 * (1000 / FRAME_MS)
                if not hasattr(_recv_loop, '_tx_bitrate_avg'):
                    _recv_loop._tx_bitrate_avg = pkt_bits
                _recv_loop._tx_bitrate_avg = (
                    _recv_loop._tx_bitrate_avg * 0.98 + pkt_bits * 0.02
                )
                # Auf 8 kbps runden damit die Anzeige ruhig bleibt
                shared["tx_bitrate"] = round(_recv_loop._tx_bitrate_avg / 8000) * 8

            # Studio: Rückkanal automatisch aus Paket-Header ableiten
            if mode == "studio" and pkt_reply_port > 0:
                reporter_key = (addr[0], pkt_reply_port, pkt_return_kbps, pkt_channels)
                if _known_reporter != reporter_key:
                    _known_reporter = reporter_key
                    shared["elapsed_frozen"] = None      # Disconnect-Zeit löschen
                    shared["start_time"] = time.time()   # neue Verbindung – Timer neu
                    shared["remote_addr"] = f"{addr[0]}:{pkt_reply_port}"
                    shared["rx_bitrate"]  = max(16, pkt_return_kbps)
                    shared["rx_channels"] = pkt_channels
                    return_bitrate = max(16, pkt_return_kbps) * 1000

                    # Decoder neu aufbauen wenn Kanalzahl sich geändert hat
                    if pkt_channels != channels:
                        channels = pkt_channels
                        _decoder = OpusDecoder(SAMPLE_RATE, channels)
                        # Output-Stream neu aufbauen mit neuer Kanalzahl
                        old_stream = _output_stream
                        _output_stream = None
                        if old_stream is not None:
                            try:
                                if old_stream.active:
                                    old_stream.stop()
                                old_stream.close()
                            except Exception:
                                pass
                        new_stream = sd.OutputStream(
                            samplerate=SAMPLE_RATE,
                            channels=channels,
                            dtype='float32',
                            blocksize=FRAME_SIZE
                        )
                        new_stream.start()
                        _output_stream = new_stream
                        # Jitter-Buffer leeren (alte Frames mit falscher Kanalzahl)
                        while not jitter_buf.empty():
                            try:
                                jitter_buf.get_nowait()
                            except Exception:
                                break
                        _rebuffer[0] = True

                    # Alten Rückkanal-Thread stoppen falls noch aktiv
                    if _send_loop_stop[0] is not None:
                        _send_loop_stop[0].set()
                    if _send_loop_thread[0] is not None:
                        _send_loop_thread[0].join(timeout=1.0)

                    # Neuen Rückkanal mit eigenem stop_event starten
                    _send_loop_stop[0] = threading.Event()
                    t = threading.Thread(
                        target=_send_loop_with_stop,
                        args=(addr[0], pkt_reply_port, 0, channels,
                              return_bitrate, 0, _send_loop_stop[0]),
                        daemon=True
                    )
                    t.start()
                    _send_loop_thread[0] = t
            else:
                shared["remote_addr"] = f"{addr[0]}:{addr[1]}"

        except socket.timeout:
            shared["connected"] = False
            shared["rx_level_l"] = 0.0
            shared["rx_level_r"] = 0.0
            _timeout_count[0] += 1
            # Rückkanal erst nach 3 aufeinanderfolgenden Timeouts (= ~3s) stoppen
            if _known_reporter is not None and _timeout_count[0] >= 3:
                _known_reporter = None
                shared["remote_addr"] = ""
                shared["frames_tx"] = 0
                # Laufzeit einfrieren
                if shared["start_time"] is not None:
                    shared["elapsed_frozen"] = int(time.time() - shared["start_time"])
                if _send_loop_stop[0] is not None:
                    _send_loop_stop[0].set()
                    _send_loop_stop[0] = None
                    _send_loop_thread[0] = None
            continue
        except Exception as e:
            if not stop_event.is_set():
                shared["status"] = f"RX error: {e}"

    # Stream-Cleanup erfolgt ausschliesslich in stop_engine._do_stop()


# ─────────────────────────────────────────────
#  VU Meter Widget
# ─────────────────────────────────────────────

VERSION = "0.7"
BUILD_DATE = "2026-03-27"

# Destinations-Datei im gleichen Verzeichnis wie das Skript
# Konfigurationsverzeichnis nach XDG-Standard
CONFIG_DIR        = Path.home() / ".config" / "opusaudiolink"
CONFIG_FILE       = CONFIG_DIR / "config.ini"
DESTINATIONS_FILE = CONFIG_DIR / "destinations.txt"
MAX_DESTINATIONS  = 10

def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_destinations():
    """Gespeicherte Ziel-IPs laden."""
    try:
        if DESTINATIONS_FILE.exists():
            lines = DESTINATIONS_FILE.read_text().splitlines()
            return [l.strip() for l in lines if l.strip()]
    except Exception:
        pass
    return []


def save_destination(host):
    """IP oben eintragen, Duplikate entfernen, auf MAX_DESTINATIONS begrenzen."""
    _ensure_config_dir()
    if not host or host == "127.0.0.1":
        return
    destinations = load_destinations()
    # Duplikat entfernen falls vorhanden
    destinations = [d for d in destinations if d != host]
    # Neue IP oben eintragen
    destinations.insert(0, host)
    destinations = destinations[:MAX_DESTINATIONS]
    try:
        DESTINATIONS_FILE.write_text("\n".join(destinations) + "\n")
    except Exception:
        pass

def save_config(settings: dict):
    """Einstellungen in ~/.config/opusaudiolink/config.ini speichern."""
    _ensure_config_dir()
    cfg = configparser.ConfigParser()
    cfg["settings"] = {k: str(v) for k, v in settings.items()}
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)


def load_config() -> dict:
    """Einstellungen aus ~/.config/opusaudiolink/config.ini laden."""
    _ensure_config_dir()
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
        return dict(cfg["settings"]) if "settings" in cfg else {}
    return {}


# Gemeinsamer oberer Abstand für VUMeter und DBScale
VU_TOP_PAD = 0


class VUMeter(QWidget):
    """Vertikaler VU-Meter Balken mit Peakhold und Farbverlauf."""

    def __init__(self, label="L", parent=None):
        super().__init__(parent)
        self.label     = label
        self._level    = 0.0   # 0.0–1.0 RMS
        self._peak     = 0.0
        self._peak_ttl = 0     # Frames bis Peak fällt
        self.setMinimumSize(28, 180)
        self.setMaximumWidth(36)

    def set_level(self, rms: float):
        # Logarithmische Skalierung: -60dB → 0dB
        if rms > 0:
            db = 20 * np.log10(max(rms, 1e-6)) + VU_LEVEL_OFFSET_DB
            level = max(0.0, min(1.0, (db + 60) / 60))
        else:
            level = 0.0

        # Exponentielles Smoothing: schnell hoch, langsam runter
        if level > self._level:
            self._level = self._level * 0.3 + level * 0.7   # schnell hoch
        else:
            self._level = self._level * 0.85 + level * 0.15  # langsam runter

        if self._level >= self._peak:
            self._peak     = self._level
            self._peak_ttl = 60                              # ~1.2s halten bei 50fps
        else:
            if self._peak_ttl > 0:
                self._peak_ttl -= 1
            else:
                self._peak = max(0.0, self._peak - 0.004)   # langsam fallen

        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height() - 20 - VU_TOP_PAD   # Platz für Label unten + oben
        bar_w = w - 6
        bar_x = 3

        # Hintergrund
        p.fillRect(bar_x, VU_TOP_PAD, bar_w, h, QColor(30, 30, 30))

        # Farbverlauf: grün → gelb → rot
        fill_h = int(h * self._level)
        if fill_h > 0:
            grad = QLinearGradient(0, VU_TOP_PAD + h, 0, VU_TOP_PAD)
            grad.setColorAt(0.0,  QColor(0,   200, 50))
            grad.setColorAt(0.65, QColor(180, 220, 0))
            grad.setColorAt(0.85, QColor(255, 165, 0))
            grad.setColorAt(1.0,  QColor(255, 30,  30))
            p.fillRect(bar_x, VU_TOP_PAD + h - fill_h, bar_w, fill_h, grad)

        # Peak-Linie
        if self._peak > 0:
            peak_y = VU_TOP_PAD + int(h * (1 - self._peak))
            color = QColor(255, 30, 30) if self._peak > 0.85 else QColor(255, 255, 100)
            p.setPen(QPen(color, 2))
            p.drawLine(bar_x, peak_y, bar_x + bar_w, peak_y)

        # Rahmen
        p.setPen(QPen(QColor(80, 80, 80), 1))
        p.drawRect(bar_x, VU_TOP_PAD, bar_w - 1, h - 1)

        # Skala-Markierungen (-6, -12, -18, -30 dB)
        p.setPen(QPen(QColor(100, 100, 100), 1))
        p.setFont(QFont("Monospace", 6))
        for db_mark in [-6, -12, -18, -30]:
            y = VU_TOP_PAD + int(h * (1 - (db_mark + 60) / 60))
            p.drawLine(bar_x + bar_w, y, bar_x + bar_w + 3, y)

        # Label
        p.setPen(QColor(200, 200, 200))
        p.setFont(QFont("Monospace", 8, QFont.Weight.Bold))
        p.drawText(0, VU_TOP_PAD + h + 2, w, 18, Qt.AlignmentFlag.AlignCenter, self.label)

        p.end()


class StereoVUMeter(QGroupBox):
    """Stereo VU-Meter (L+R) mit Titel."""

    def __init__(self, title="TX", parent=None):
        super().__init__(title, parent)
        layout = QHBoxLayout()
        layout.setSpacing(4)
        self.meter_l = VUMeter("L")
        self.meter_r = VUMeter("R")
        layout.addWidget(self.meter_l)
        layout.addWidget(self.meter_r)
        self.setLayout(layout)
        self.setMaximumWidth(100)
        self.setStyleSheet("""
            QGroupBox {
                color: #aaa;
                border: 1px solid #444;
                border-radius: 4px;
                margin-top: 8px;
                font-size: 11px;
            }
            QGroupBox::title { subcontrol-position: top center; padding: 0 4px; }
        """)

    def set_levels(self, l: float, r: float):
        self.meter_l.set_level(l)
        self.meter_r.set_level(r)


class DBScale(QWidget):
    """Vertikale dB-Skala passend zu VUMeter (0 bis -60 dB).
    top_offset/bottom_offset müssen mit dem VUMeter-Balken übereinstimmen."""

    DB_MARKS = [0, -3, -6, -9, -12, -18, -24, -30, -40, -50, -60]

    def __init__(self, top_offset=30, bottom_offset=24, parent=None):
        super().__init__(parent)
        self.top_offset    = top_offset
        self.bottom_offset = bottom_offset
        self.setMinimumWidth(34)
        self.setMaximumWidth(40)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self.height() - self.top_offset - self.bottom_offset
        w = self.width()

        p.setFont(QFont("Monospace", 7))

        for db in self.DB_MARKS:
            y = self.top_offset + int(h * (1 - (db + 60) / 60))

            if db >= -6:
                color = QColor(255, 80, 80)
            elif db >= -12:
                color = QColor(255, 165, 0)
            elif db >= -18:
                color = QColor(180, 220, 0)
            else:
                color = QColor(100, 160, 100)

            p.setPen(QPen(color, 1))
            p.drawLine(2, y, 8, y)
            p.drawLine(w - 8, y, w - 2, y)

            label = "0" if db == 0 else str(db)
            p.setPen(color)
            p.drawText(0, y - 7, w, 14, Qt.AlignmentFlag.AlignHCenter, label)

        p.end()


# ─────────────────────────────────────────────
#  Haupt-GUI
# ─────────────────────────────────────────────

DARK = """
QMainWindow, QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
}
QGroupBox {
    border: 1px solid #444;
    border-radius: 5px;
    margin-top: 10px;
    font-size: 11px;
    color: #aaa;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
}
QLabel { color: #e0e0e0; }
QLineEdit, QSpinBox, QComboBox {
    background: #2d2d2d;
    border: 1px solid #555;
    border-radius: 3px;
    color: #e0e0e0;
    padding: 3px 6px;
}
QPushButton {
    background: #3a3a3a;
    border: 1px solid #555;
    border-radius: 4px;
    color: #e0e0e0;
    padding: 6px 18px;
    font-size: 12px;
}
QPushButton:hover  { background: #4a4a4a; }
QPushButton:pressed { background: #222; }
QPushButton#btn_start {
    background: #1a5c2a;
    border-color: #2a8c3a;
    font-weight: bold;
}
QPushButton#btn_start:hover { background: #226b32; }
QPushButton#btn_stop {
    background: #5c1a1a;
    border-color: #8c2a2a;
    font-weight: bold;
}
QPushButton#btn_stop:hover { background: #6b2222; }
QPushButton#btn_ban {
    background: #7a5c00;
    border-color: #c49a00;
    color: #ffe066;
    font-weight: bold;
}
QPushButton#btn_ban:hover { background: #9a7800; }
QPushButton#btn_ban:disabled { background: #2a2a2a; color: #666; border-color: #444; }
QPushButton#btn_attention {
    background: #2a2a2a; color: #aaa; border: 1px solid #555;
    font-size: 18px; padding: 4px 10px; border-radius: 4px; min-width: 40px;
}
QPushButton#btn_attention:hover { background: #3a3a3a; }
QPushButton#btn_attention[active="true"] {
    background: #cc2200; border-color: #ff4400;
}
QPushButton#btn_monitor {
    background: #1a3a5c;
    border-color: #2a5a8c;
    font-weight: bold;
}
QPushButton#btn_monitor:hover { background: #224466; }
QPushButton#btn_monitor:disabled { background: #2a2a2a; color: #666; }
QPushButton#btn_mode_active {
    background: #2a4a7a;
    border-color: #4a7acc;
    border-width: 2px;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#btn_mode_inactive {
    background: #2a2a2a;
    border-color: #444;
    color: #888;
}
"""


class MainWindow(QMainWindow):
    helo_status_signal  = pyqtSignal(bool)
    _start_after_stop   = pyqtSignal()   # sicherer Cross-Thread Start
    _ping_result        = pyqtSignal(list)  # RTT-Liste aus Ping-Thread

    def __init__(self):
        super().__init__()
        self.helo_status_signal.connect(self._apply_helo_status)
        self._start_after_stop.connect(self._do_start_now)
        self.setWindowTitle(f"OpusAudioLink v{VERSION}")
        self.setMinimumSize(620, 480)
        self.setStyleSheet(DARK)

        self._channels = 2
        self._running  = False
        self._conn_details = ""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # ── Modus-Auswahl ──────────────────────────
        mode_box = QGroupBox("Mode")
        mode_layout = QHBoxLayout(mode_box)
        self.btn_reporter = QPushButton("📡  Reporter (Transmitter)")
        self.btn_studio   = QPushButton("🎛️  Studio (Receiver)")
        self.btn_reporter.setCheckable(True)
        self.btn_studio.setCheckable(True)
        self.btn_reporter.setChecked(True)
        self.btn_reporter.clicked.connect(lambda: self._on_mode_button("reporter"))
        self.btn_studio.clicked.connect(lambda: self._on_mode_button("studio"))
        mode_layout.addWidget(self.btn_reporter)
        mode_layout.addWidget(self.btn_studio)
        root.addWidget(mode_box)

        # ── Netzwerk ───────────────────────────────
        net_box = QGroupBox("Network")
        net_layout = QHBoxLayout(net_box)

        net_layout.addWidget(QLabel("Target IP:"))
        self.edit_host = QComboBox()
        self.edit_host.setEditable(True)
        self.edit_host.setMinimumWidth(160)
        self.edit_host.setMaximumWidth(200)
        self.edit_host.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._reload_destinations()
        net_layout.addWidget(self.edit_host)

        # Bestaetigungs-Button fuer IP-Eingabe
        self.btn_set_host = QPushButton("Set")
        self.btn_set_host.setMinimumWidth(45)
        self.btn_set_host.setToolTip("Apply IP and check reachability")
        self.btn_set_host.clicked.connect(self._on_host_confirm)
        net_layout.addWidget(self.btn_set_host)

        # Studio-Erreichbarkeits-Indikator als deaktivierter Button
        self.lbl_studio_status = QPushButton("Studio ✗")
        self.lbl_studio_status.setEnabled(False)
        self.lbl_studio_status.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lbl_studio_status.setMinimumWidth(80)
        self.lbl_studio_status.setStyleSheet(
            "QPushButton:disabled { background: #2d2d2d; border: 1px solid #555; "
            "border-radius: 4px; color: #c0392b; font-size: 12px; padding: 4px 10px; }")
        net_layout.addWidget(self.lbl_studio_status)

        # Enter oder OK/Set-Button bestaetigen die IP (Dropdown und Tipp-Eingabe)
        self.edit_host.lineEdit().returnPressed.connect(self._on_host_confirm)

        net_layout.addWidget(QLabel("Send Port:"))
        self.spin_send_port = QSpinBox()
        self.spin_send_port.setRange(1024, 65535)
        self.spin_send_port.setValue(5004)
        self.spin_send_port.setMaximumWidth(80)
        net_layout.addWidget(self.spin_send_port)

        net_layout.addWidget(QLabel("Recv Port:"))
        self.spin_recv_port = QSpinBox()
        self.spin_recv_port.setRange(1024, 65535)
        self.spin_recv_port.setValue(5006)
        self.spin_recv_port.setMaximumWidth(80)
        net_layout.addWidget(self.spin_recv_port)

        net_layout.addStretch()
        root.addWidget(net_box)

        # ── Codec ──────────────────────────────────
        self.codec_box = QGroupBox("Codec")
        codec_layout = QHBoxLayout(self.codec_box)

        self.lbl_bitrate_tx = QLabel("Bitrate TX:")
        codec_layout.addWidget(self.lbl_bitrate_tx)
        self.combo_bitrate = QComboBox()
        for br in BITRATE_OPTIONS:
            self.combo_bitrate.addItem(f"{br // 1000} kbps", br)
        self.combo_bitrate.setCurrentIndex(3)  # 128 kbps default
        self.combo_bitrate.setMaximumWidth(100)
        codec_layout.addWidget(self.combo_bitrate)

        codec_layout.addSpacing(8)
        self.lbl_return_bitrate = QLabel("Bitrate RX:")
        self.combo_return_bitrate = QComboBox()
        for br in BITRATE_OPTIONS:
            self.combo_return_bitrate.addItem(f"{br // 1000} kbps", br)
        self.combo_return_bitrate.setCurrentIndex(1)  # 64 kbps default
        self.combo_return_bitrate.setMaximumWidth(100)
        self.combo_return_bitrate.setToolTip("Bitrate used by the studio for the return channel (reporter uplink)")
        codec_layout.addWidget(self.lbl_return_bitrate)
        codec_layout.addWidget(self.combo_return_bitrate)

        codec_layout.addSpacing(16)
        self.lbl_channels = QLabel("Channels:")
        codec_layout.addWidget(self.lbl_channels)
        self.combo_channels = QComboBox()
        self.combo_channels.addItem("Mono (1)", 1)
        self.combo_channels.addItem("Stereo (2)", 2)
        self.combo_channels.setCurrentIndex(1)
        self.combo_channels.setMaximumWidth(100)
        codec_layout.addWidget(self.combo_channels)

        codec_layout.addStretch()
        root.addWidget(self.codec_box)

        # ── Latenz ─────────────────────────────────
        self.latenz_box = QGroupBox("Latency")
        latenz_layout = QHBoxLayout(self.latenz_box)
        latenz_layout.setSpacing(8)

        # ── Reporter-Elemente ─────────────────────────────────────────────────
        self.lbl_tx_studio = QLabel("TX Studio:")
        latenz_layout.addWidget(self.lbl_tx_studio)

        self.btn_robust = QPushButton("Robust  (2 s)")
        self.btn_robust.setCheckable(True)
        self.btn_robust.setObjectName("btn_mode_inactive")
        self.btn_robust.setToolTip(
            "Sets the studio RX buffer to ~2 s.\n"
            "The studio reacts automatically – no on-site action needed.\n"
            "Can be toggled live during a connection.")
        self.btn_robust.toggled.connect(self._on_robust_toggled)
        latenz_layout.addWidget(self.btn_robust)

        self.latenz_vline = QWidget()
        self.latenz_vline.setFixedWidth(1)
        self.latenz_vline.setStyleSheet("background-color: #555;")
        latenz_layout.addWidget(self.latenz_vline)

        self.lbl_rx_reporter = QLabel("RX Reporter:")
        latenz_layout.addWidget(self.lbl_rx_reporter)

        self.combo_jitter = QComboBox()
        self.combo_jitter.addItem("aus",       0)
        self.combo_jitter.addItem("40 ms",     2)
        self.combo_jitter.addItem("80 ms",     4)
        self.combo_jitter.addItem("120 ms",    6)
        self.combo_jitter.addItem("150 ms",    8)
        self.combo_jitter.addItem("200 ms",   10)
        self.combo_jitter.addItem("250 ms",   13)
        self.combo_jitter.addItem("300 ms",   15)
        self.combo_jitter.addItem("400 ms",   20)
        self.combo_jitter.addItem("500 ms",   25)
        self.combo_jitter.setCurrentIndex(4)
        self.combo_jitter.setMaximumWidth(90)
        self.combo_jitter.setToolTip("RX latency of the return channel – only adjustable before start")
        latenz_layout.addWidget(self.combo_jitter)

        self.btn_robust_rx = QPushButton("Robust  (2 s)")
        self.btn_robust_rx.setCheckable(True)
        self.btn_robust_rx.setObjectName("btn_mode_inactive")
        self.btn_robust_rx.setToolTip(
            "Sets the local RX buffer to ~2 s.\n"
            "Can be toggled live – e.g. for relay operation.")
        self.btn_robust_rx.toggled.connect(self._on_robust_rx_toggled)
        latenz_layout.addWidget(self.btn_robust_rx)

        # ── Studio-Element ────────────────────────────────────────────────────
        self.lbl_rx_studio = QLabel("RX Studio:")
        latenz_layout.addWidget(self.lbl_rx_studio)

        self.combo_jitter_studio = QComboBox()
        self.combo_jitter_studio.addItem("aus",       0)
        self.combo_jitter_studio.addItem("40 ms",     2)
        self.combo_jitter_studio.addItem("80 ms",     4)
        self.combo_jitter_studio.addItem("120 ms",    6)
        self.combo_jitter_studio.addItem("150 ms",    8)
        self.combo_jitter_studio.addItem("200 ms",   10)
        self.combo_jitter_studio.addItem("250 ms",   13)
        self.combo_jitter_studio.addItem("300 ms",   15)
        self.combo_jitter_studio.addItem("400 ms",   20)
        self.combo_jitter_studio.addItem("500 ms",   25)
        self.combo_jitter_studio.setCurrentIndex(4)
        self.combo_jitter_studio.setMaximumWidth(90)
        self.combo_jitter_studio.setToolTip("RX latency in the studio – only adjustable before start")
        latenz_layout.addWidget(self.combo_jitter_studio)

        latenz_layout.addStretch()
        root.addWidget(self.latenz_box)

        # ── Geräteauswahl ──────────────────────────
        dev_box = QGroupBox("Audio Devices")
        dev_layout = QHBoxLayout(dev_box)

        dev_layout.addWidget(QLabel("Input:"))
        self.combo_input = QComboBox()
        self.combo_input.setMinimumWidth(200)
        dev_layout.addWidget(self.combo_input)

        dev_layout.addSpacing(16)
        dev_layout.addWidget(QLabel("Output:"))
        self.combo_output = QComboBox()
        self.combo_output.setMinimumWidth(200)
        dev_layout.addWidget(self.combo_output)

        btn_refresh = QPushButton("reload")
        btn_refresh.setMinimumWidth(60)
        btn_refresh.setToolTip("Refresh device list")
        btn_refresh.clicked.connect(self._refresh_devices)
        dev_layout.addWidget(btn_refresh)

        dev_layout.addStretch()
        root.addWidget(dev_box)

        self._refresh_devices()  # initial befüllen (liest Config intern)

        # ── Gespeicherte Einstellungen laden ───────
        self._load_config()

        # ── VU Meter + Status ──────────────────────
        meter_row = QHBoxLayout()

        self.vu_tx = StereoVUMeter("TX (Senden)")
        self.vu_rx = StereoVUMeter("RX (Empfang)")
        # Offset = GroupBox margin-top (10) + Titelzeile (~16px) + GroupBox content margin (~4)
        self.db_scale = DBScale(top_offset=20, bottom_offset=24)

        meter_row.addWidget(self.vu_tx)
        meter_row.addWidget(self.db_scale)
        meter_row.addWidget(self.vu_rx)

        # Status-Panel
        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        status_layout.setSpacing(6)

        self.lbl_status    = QLabel("●  Stopped")
        self.lbl_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #888;")

        self.lbl_connected    = QLabel("Remote: –")
        self.lbl_latency      = QLabel("Jitter: –")
        self.lbl_latency_avg  = QLabel("Buffer target: –")
        self.lbl_frames       = QLabel("TX: 0  RX: 0 Frames")
        self.lbl_dropouts     = QLabel("Dropouts: –")
        self.lbl_conn_details = QLabel("")   # Modus, IP, Ports, Bitraten

        for lbl in [self.lbl_connected, self.lbl_latency,
                    self.lbl_latency_avg, self.lbl_frames, self.lbl_dropouts]:
            lbl.setStyleSheet("font-size: 11px; color: #bbb;")
        self.lbl_conn_details.setStyleSheet(
            "font-size: 10px; color: #777; font-family: monospace;")
        self.lbl_conn_details.setWordWrap(True)

        status_layout.addWidget(self.lbl_status)
        status_layout.addWidget(self.lbl_connected)
        status_layout.addWidget(self.lbl_latency)
        status_layout.addWidget(self.lbl_latency_avg)
        status_layout.addWidget(self.lbl_frames)
        status_layout.addWidget(self.lbl_dropouts)
        status_layout.addWidget(self.lbl_conn_details)
        status_layout.addStretch()

        meter_row.addWidget(status_box, stretch=1)
        root.addLayout(meter_row)

        # ── Start / Stop ───────────────────────────
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setObjectName("btn_start")
        self.btn_stop  = QPushButton("■  Stop")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setEnabled(False)
        self.btn_monitor = QPushButton("🎙  Test Input")
        self.btn_monitor.setObjectName("btn_monitor")
        self.btn_monitor.setToolTip("Show microphone level for 10 seconds – without connection")
        self.btn_ban = QPushButton("BAN!")
        self.btn_ban.setObjectName("btn_ban")
        self.btn_ban.setToolTip("Disconnect and ban this reporter for 10 minutes")
        self.btn_ban.setVisible(False)
        self.btn_ban.setEnabled(False)
        self.btn_ban.clicked.connect(self._on_ban)
        self.btn_attention = QPushButton("⚠  ⚠  ⚠")
        self.btn_attention.setObjectName("btn_attention")
        self.btn_attention.setToolTip("Send attention signal !")
        self.btn_attention.clicked.connect(self._on_attention)
        self.btn_ping = QPushButton("📶  Test Latency")
        self.btn_ping.setObjectName("btn_monitor")
        self.btn_ping.setToolTip("Send UDP pings to the studio and estimate round-trip latency")
        self.btn_ping.clicked.connect(self._on_ping)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_monitor.clicked.connect(self._on_monitor)
        btn_row.addWidget(self.btn_monitor)
        btn_row.addWidget(self.btn_ping)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_ban)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_attention)
        root.addLayout(btn_row)

        # ── Timer für GUI-Update ───────────────────
        self._timer = QTimer()
        self._timer.setInterval(50)   # 20 fps
        self._timer.timeout.connect(self._update_ui)
        self._timer.start()
        self._blink_state = False
        self._blink_timer = QTimer()
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._on_blink)
        self._blink_timer.start()

        # Modus aus Config laden (falls noch nicht gesetzt via _load_config)
        if not hasattr(self, "_mode"):
            self._mode = "reporter"
        # _set_mode nochmal aufrufen – jetzt sind alle Widgets gebaut
        self._set_mode(self._mode)

        # HELO beim Start auslösen – läuft nach _set_mode, damit self._mode korrekt gesetzt ist
        QTimer.singleShot(500, lambda: self._on_host_changed(self.edit_host.currentText()))

        # Countdown-Timer für Monitor-Button
        self._monitor_countdown = 0
        self._monitor_tick = QTimer()
        self._monitor_tick.setInterval(1000)
        self._monitor_tick.timeout.connect(self._monitor_tick_fn)

    # ── Eingang testen ─────────────────────────────

    def _on_ban(self):
        """Ban-Button: aktuell verbundenen Reporter kicken und 10 Minuten sperren."""
        from PyQt6.QtWidgets import QMessageBox
        addr = shared.get("remote_addr", "")
        ip   = addr.split(":")[0] if addr else ""
        if not ip:
            return
        reply = QMessageBox.question(
            self,
            "Ban Reporter",
            f"Ban {ip} for 10 minutes and disconnect?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            shared["kick_ip"] = ip

    def _on_attention(self):
        """Attention-Button: sendet Aufmerksamkeitssignal kurz, leuchtet 30s."""
        shared["attention"]         = 10          # 10 Frames senden (~200ms)
        shared["attention_tx_time"] = time.time() # Startzeitpunkt für 30s

    def _on_blink(self):
        self._blink_state = not self._blink_state

    def _on_robust_toggled(self, checked):
        """Robust TX Studio – sendet Flag ans Studio."""
        shared["robust_mode"] = checked
        self.btn_robust.setObjectName("btn_mode_active" if checked else "btn_mode_inactive")
        self.btn_robust.setStyle(self.btn_robust.style())

    def _on_robust_rx_toggled(self, checked):
        """Robust RX Reporter – eigener Empfangspuffer."""
        shared["robust_rx"] = checked
        self.btn_robust_rx.setObjectName("btn_mode_active" if checked else "btn_mode_inactive")
        self.btn_robust_rx.setStyle(self.btn_robust_rx.style())

    # ── Eingang testen ─────────────────────────────

    def _on_monitor(self):
        if _monitor_stop.is_set() or self._monitor_countdown == 0:
            # Starten
            channels     = self.combo_channels.currentData()
            input_device = self.combo_input.currentData()
            self._monitor_countdown = 10
            self.btn_monitor.setEnabled(False)
            self.btn_start.setEnabled(False)
            self.btn_monitor.setText(f"🎙  Testing… {self._monitor_countdown}s")
            start_monitor(channels, input_device, duration=10)
            self._monitor_tick.start()

    def _monitor_tick_fn(self):
        self._monitor_countdown -= 1
        if self._monitor_countdown <= 0 or _monitor_stop.is_set():
            stop_monitor()
            self._monitor_tick.stop()
            self._monitor_countdown = 0
            self.btn_monitor.setText("🎙  Test Input")
            self.btn_monitor.setEnabled(True)
            if not self._running:
                self.btn_start.setEnabled(True)
        else:
            self.btn_monitor.setText(f"🎙  Testing… {self._monitor_countdown}s")

    # ── Latenz-Test (UDP-Ping via HELO) ────────────

    def _on_ping(self):
        """UDP-Ping zum Studio – nur im Reporter-Modus und ohne aktive Verbindung."""
        if self._running:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Test Latency",
                "Cannot run latency test while a connection is active."
            )
            return
        if self._mode != "reporter":
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Test Latency",
                "Latency test is only available in Reporter mode\n"
                "(a target IP is required)."
            )
            return

        host = self.edit_host.currentText().strip()
        if not host or host == "127.0.0.1":
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Test Latency",
                "Please enter a valid studio IP address first."
            )
            return

        port = self.spin_send_port.value()
        self.btn_ping.setEnabled(False)
        self.btn_ping.setText("📶  Pinging…")

        def _run_ping():
            import socket as _socket
            NUM = 10
            rtt_list = []
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                sock.settimeout(1.0)
                sock.bind(("0.0.0.0", 0))
                local_port = sock.getsockname()[1]
                helo_pkt = HELO_PING + local_port.to_bytes(2, 'big')

                for _ in range(NUM):
                    t0 = time.monotonic()
                    try:
                        sock.sendto(helo_pkt, (host, port))
                        data, _ = sock.recvfrom(32)
                        if data[:6] == HELO_PONG:
                            rtt = (time.monotonic() - t0) * 1000
                            rtt_list.append(rtt)
                    except _socket.timeout:
                        pass
                    time.sleep(0.2)
                sock.close()
            except Exception:
                pass
            self._ping_result.emit(rtt_list)

        self._ping_result.connect(self._show_ping_result, Qt.ConnectionType.SingleShotConnection)
        threading.Thread(target=_run_ping, daemon=True).start()

    def _show_ping_result(self, rtt_list):
        from PyQt6.QtWidgets import QMessageBox
        self.btn_ping.setEnabled(True)
        self.btn_ping.setText("📶  Test Latency")

        if not rtt_list:
            QMessageBox.warning(
                self, "Test Latency",
                "No response from studio.\n"
                "Check IP address, port and firewall."
            )
            return

        avg  = sum(rtt_list) / len(rtt_list)
        mn   = min(rtt_list)
        mx   = max(rtt_list)
        loss = (10 - len(rtt_list)) * 10

        if avg < 30:
            rating = "Excellent"
        elif avg < 80:
            rating = "Good"
        elif avg < 150:
            rating = "Moderate"
        else:
            rating = "High latency"

        msg = (
            f"UDP Round-Trip to {self.edit_host.currentText().strip()}:{self.spin_send_port.value()}\n"
            f"{'─' * 40}\n"
            f"Packets:   {len(rtt_list)}/10  (loss {loss}%)\n"
            f"Min:       {mn:.1f} ms\n"
            f"Avg:       {avg:.1f} ms\n"
            f"Max:       {mx:.1f} ms\n"
            f"{'─' * 40}\n"
            f"Rating:    {rating}"
        )
        QMessageBox.information(self, "Test Latency – Result", msg)

    # ── Destinations ───────────────────────────────

    def _reload_destinations(self):
        """Dropdown mit gespeicherten Zielen befüllen."""
        current = self.edit_host.currentText() if self.edit_host.count() > 0 else ""
        self.edit_host.blockSignals(True)
        self.edit_host.clear()
        destinations = load_destinations()
        if destinations:
            self.edit_host.addItems(destinations)
            # Zuletzt genutzten Wert wiederherstellen oder ersten Eintrag wählen
            idx = self.edit_host.findText(current)
            if idx >= 0:
                self.edit_host.setCurrentIndex(idx)
            else:
                self.edit_host.setCurrentIndex(0)
        else:
            self.edit_host.addItem("127.0.0.1")
            self.edit_host.setCurrentIndex(0)
        self.edit_host.blockSignals(False)

    # ── Geräte ─────────────────────────────────────

    def _refresh_devices(self, saved_input_name=None, saved_output_name=None):
        """Geräteliste neu einlesen und Dropdowns befüllen.
        Wenn saved_input_name/saved_output_name angegeben, wird nach Name gesucht."""
        inputs, outputs = get_device_list()
        default_in, default_out = sd.default.device

        # Gespeicherte Namen aus Config falls nicht direkt übergeben
        if saved_input_name is None and saved_output_name is None:
            cfg = load_config()
            saved_input_name  = cfg.get("input_device_name",  "")
            saved_output_name = cfg.get("output_device_name", "")

        self.combo_input.blockSignals(True)
        self.combo_output.blockSignals(True)
        self.combo_input.clear()
        self.combo_output.clear()

        sel_in = sel_out = 0
        for i, (idx, name) in enumerate(inputs):
            self.combo_input.addItem(f"[{idx}] {name}", idx)
            if saved_input_name and saved_input_name in name:
                sel_in = i
            elif not saved_input_name and idx == default_in:
                sel_in = i
        for i, (idx, name) in enumerate(outputs):
            self.combo_output.addItem(f"[{idx}] {name}", idx)
            if saved_output_name and saved_output_name in name:
                sel_out = i
            elif not saved_output_name and idx == default_out:
                sel_out = i

        self.combo_input.setCurrentIndex(sel_in)
        self.combo_output.setCurrentIndex(sel_out)
        self.combo_input.blockSignals(False)
        self.combo_output.blockSignals(False)

    # ── Modus ──────────────────────────────────────

    def _on_host_changed(self, text):
        """Startet HELO-Ping neu wenn IP geändert wird (nur Reporter, nur wenn nicht verbunden)."""
        if self._mode == "reporter" and not self._running:
            stop_helo()
            host = text.strip()
            if host and host != "127.0.0.1":
                send_port = self.spin_send_port.value()
                recv_port = self.spin_recv_port.value()
                start_helo(host, send_port, recv_port, self._on_helo_status)
            else:
                self.lbl_studio_status.setText("Studio ✗")
                self.lbl_studio_status.setStyleSheet(
                    "QPushButton:disabled { background: #2d2d2d; border: 1px solid #555; "
                    "border-radius: 4px; color: #c0392b; font-size: 12px; padding: 4px 10px; }")
    def _on_host_confirm(self):
        """IP-Eingabe explizit bestaetigen (Button oder Enter)."""
        host = self.edit_host.currentText().strip()
        if host:
            # Sicherstellen dass der eingetippte Wert auch im Combo-Feld steht
            self.edit_host.setCurrentText(host)
        self._on_host_changed(host)

    def _on_helo_status(self, reachable):
        """Callback aus HELO-Thread – GUI-Update via Signal (thread-safe)."""
        self.helo_status_signal.emit(reachable)

    def _apply_helo_status(self, reachable):
        if self._running:
            return  # Waehrend Verbindung nicht aendern – Punkt bleibt gruen
        if reachable:
            self.lbl_studio_status.setText("Studio ✓")
            self.lbl_studio_status.setStyleSheet(
                "QPushButton:disabled { background: #2d2d2d; border: 1px solid #555; "
                "border-radius: 4px; color: #27ae60; font-size: 12px; padding: 4px 10px; }")
        else:
            self.lbl_studio_status.setText("Studio ✗")
            self.lbl_studio_status.setStyleSheet(
                "QPushButton:disabled { background: #2d2d2d; border: 1px solid #555; "
                "border-radius: 4px; color: #c0392b; font-size: 12px; padding: 4px 10px; }")
    def _on_mode_button(self, mode):
        """Mode-Wechsel mit Sperre während laufender Verbindung."""
        if self._running:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(
                self,
                "Mode locked",
                "Cannot switch mode while a connection is active.\n"
                "Please stop the connection first."
            )
            # Button-Zustand optisch zurücksetzen (damit kein falsches Häkchen bleibt)
            self.btn_reporter.setChecked(self._mode == "reporter")
            self.btn_studio.setChecked(self._mode == "studio")
            return
        self._set_mode(mode)

    def _set_mode(self, mode):
        self._mode = mode
        self.btn_reporter.setObjectName(
            "btn_mode_active" if mode == "reporter" else "btn_mode_inactive")
        self.btn_studio.setObjectName(
            "btn_mode_active" if mode == "studio" else "btn_mode_inactive")
        # Style neu anwenden
        self.btn_reporter.setStyle(self.btn_reporter.style())
        self.btn_studio.setStyle(self.btn_studio.style())

        if mode == "reporter":
            self.edit_host.setEnabled(True)
            self.edit_host.lineEdit().setPlaceholderText("Studio IP address")
            self.edit_host.setToolTip("IP address of the studio")
            self.spin_send_port.setValue(5004)
            self.spin_recv_port.setValue(5006)
            self.codec_box.setVisible(True)
            self.lbl_bitrate_tx.setVisible(True)
            self.combo_bitrate.setVisible(True)
            self.lbl_return_bitrate.setVisible(True)
            self.combo_return_bitrate.setVisible(True)
            self.lbl_channels.setVisible(True)
            self.combo_channels.setVisible(True)
            self.lbl_studio_status.setVisible(True)
            self.btn_set_host.setVisible(True)
            self.latenz_box.setVisible(True)
            self.lbl_tx_studio.setVisible(True)
            self.btn_robust.setVisible(True)
            self.latenz_vline.setVisible(True)
            self.lbl_rx_reporter.setVisible(True)
            self.combo_jitter.setVisible(True)
            self.btn_robust_rx.setVisible(True)
            self.lbl_rx_studio.setVisible(False)
            self.combo_jitter_studio.setVisible(False)
            if hasattr(self, "btn_ban"):
                self.btn_ban.setVisible(False)
            if hasattr(self, "btn_ping"):
                self.btn_ping.setVisible(True)
        else:
            self.edit_host.setEnabled(False)
            self.edit_host.setCurrentText("")
            self.edit_host.lineEdit().setPlaceholderText("detected automatically")
            self.edit_host.setToolTip("In Studio mode the reporter IP is read automatically from incoming packets")
            self.lbl_studio_status.setVisible(False)
            self.btn_set_host.setVisible(False)
            stop_helo()
            self.spin_send_port.setValue(5006)
            self.spin_recv_port.setValue(5004)
            self.codec_box.setVisible(False)
            self.latenz_box.setVisible(True)
            self.lbl_tx_studio.setVisible(False)
            self.btn_robust.setVisible(False)
            self.latenz_vline.setVisible(False)
            self.lbl_rx_reporter.setVisible(False)
            self.combo_jitter.setVisible(False)
            self.btn_robust_rx.setVisible(False)
            self.lbl_rx_studio.setVisible(True)
            self.combo_jitter_studio.setVisible(True)
            if hasattr(self, "btn_ban"):
                self.btn_ban.setVisible(True)
            if hasattr(self, "btn_ping"):
                self.btn_ping.setVisible(False)

    # ── Start / Stop ───────────────────────────────

    def _on_start(self):
        # Guard: verhindert Start während Engine noch läuft oder stoppt
        if getattr(self, '_pending_start', False):
            return

        host         = self.edit_host.currentText().strip() or "127.0.0.1"
        send_port    = self.spin_send_port.value()
        recv_port    = self.spin_recv_port.value()
        channels      = self.combo_channels.currentData()
        # Im Studio-Modus wird die TX-Bitrate vom Reporter-Paket diktiert;
        # combo_bitrate ist ausgeblendet – wir nehmen 64 kbps als Platzhalter.
        bitrate       = self.combo_bitrate.currentData() if self._mode == "reporter" else 64000
        return_bitrate = self.combo_return_bitrate.currentData() if self._mode == "reporter" else 64000
        input_device   = self.combo_input.currentData()
        output_device  = self.combo_output.currentData()
        jitter_frames  = self.combo_jitter.currentData() if self._mode == "reporter" else self.combo_jitter_studio.currentData()

        self._channels = channels

        # IP speichern und Dropdown aktualisieren (nur Reporter)
        # host VOR _reload_destinations() merken – Dropdown-Neuaufbau wuerde currentText() veraendern
        if self._mode == "reporter":
            save_destination(host)
            self._reload_destinations()
            # Sicherstellen dass die urspruenglich eingegebene IP weiter genutzt wird
            self.edit_host.setCurrentText(host)

        # Verbindungsdetails merken für Statusanzeige
        if self._mode == "reporter":
            br_tx = bitrate // 1000
            br_rx = return_bitrate // 1000
            self._conn_details = (
                f"Mode: Reporter\n"
                f"Target: {host}:{send_port}\n"
                f"Receive: Port {recv_port}\n"
                f"TX: {br_tx} kbps  RX: {br_rx} kbps\n"
                f"Channels: {channels}  |  {SAMPLE_RATE//1000} kHz"
            )
        else:
            self._conn_details = (
                f"Mode: Studio\n"
                f"Receive: Port {recv_port}\n"
                f"Return: auto\n"
                f"TX: auto  RX: auto\n"
                f"Channels: {channels}  |  {SAMPLE_RATE//1000} kHz"
            )

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_monitor.setEnabled(False)
        if self._mode == "reporter":
            self.edit_host.setEnabled(False)
        self.spin_send_port.setEnabled(False)
        self.spin_recv_port.setEnabled(False)
        self.combo_bitrate.setEnabled(False)
        self.combo_return_bitrate.setEnabled(False)
        self.combo_channels.setEnabled(False)
        self.combo_jitter.setEnabled(False)
        self.combo_jitter_studio.setEnabled(False)
        self.combo_input.setEnabled(False)
        self.combo_output.setEnabled(False)
        # btn_robust und btn_robust_rx bleiben enabled – live schaltbar während Verbindung

        def _do_start():
            self._pending_start = False
            self._running = True
            stop_helo()  # HELO während Verbindung nicht nötig
            start_engine(self._mode, host, send_port, recv_port, channels, bitrate,
                         input_device, output_device, return_bitrate, jitter_frames)

        # Startparameter merken für _do_start_now
        self._pending_start_args = (
            self._mode, host, send_port, recv_port, channels, bitrate,
            input_device, output_device, return_bitrate, jitter_frames
        )

        def _do_start_safe():
            # Läuft im Stop-Thread → Signal emit ist thread-sicher
            self._start_after_stop.emit()

        if self._running or stop_event.is_set():
            self._pending_start = True
            stop_engine(callback=_do_start_safe)
        else:
            _do_start()

    def _on_stop(self):
        from PyQt6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Disconnect",
            "Really disconnect?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.No:
            return
        self._do_stop_ui()

    def _do_start_now(self):
        """Wird via Signal aus Stop-Thread aufgerufen – läuft im GUI-Thread."""
        if not getattr(self, '_pending_start', False):
            return
        self._pending_start = False
        args = getattr(self, '_pending_start_args', None)
        if args is None:
            return
        mode, host, send_port, recv_port, channels, bitrate,             input_device, output_device, return_bitrate, jitter_frames = args
        self._running = True
        stop_helo()
        start_engine(mode, host, send_port, recv_port, channels, bitrate,
                     input_device, output_device, return_bitrate, jitter_frames)
    def _do_stop_ui(self):
        """Stop-Engine nicht-blockierend, GUI wird nach Abschluss freigegeben."""
        # Buttons sofort sperren damit kein zweiter Stop/Start ausgelöst wird
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_monitor.setEnabled(False)
        self.btn_ban.setEnabled(False)
        # Shared-State sofort setzen – _update_ui zeigt sonst kurz "Connected"
        shared["connected"] = False
        shared["status"]    = "Cleaning up"
        self._cleaning_up   = True
        self._cleanup_start = time.time()

        def _after_stop():
            # Mindestanzeigedauer 1s für "Cleaning up…" – dann GUI freigeben
            elapsed_ms = int((time.time() - self._cleanup_start) * 1000)
            delay_ms   = max(0, 1000 - elapsed_ms)
            QTimer.singleShot(delay_ms, self._reset_ui_after_stop)

        stop_engine(callback=_after_stop)

    def _reset_ui_after_stop(self):
        """GUI nach Stop zurücksetzen – läuft im GUI-Thread."""
        self._running     = False
        self._cleaning_up = False
        stop_event.clear()

        for key in ("tx_level_l", "tx_level_r", "rx_level_l", "rx_level_r"):
            shared[key] = 0.0
        shared["latency_ms"]   = None
        shared["latency_avg"]  = None
        shared["remote_addr"]  = ""
        shared["rx_bitrate"]   = None
        shared["tx_bitrate"]   = None
        shared["rx_channels"]  = None
        if hasattr(_recv_loop, '_tx_bitrate_avg'):
            del _recv_loop._tx_bitrate_avg
        shared["frames_tx"]    = 0
        shared["frames_rx"]    = 0
        shared["robust_mode"]  = False
        shared["robust_rx"]    = False

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_monitor.setEnabled(True)
        if self._mode == "reporter":
            self.edit_host.setEnabled(True)
        self.spin_send_port.setEnabled(True)
        self.spin_recv_port.setEnabled(True)
        self.combo_bitrate.setEnabled(True)
        self.combo_return_bitrate.setEnabled(True)
        self.combo_channels.setEnabled(True)
        self.combo_jitter.setEnabled(True)
        self.combo_jitter_studio.setEnabled(True)
        self.combo_input.setEnabled(True)
        self.combo_output.setEnabled(True)
        # Robust-Buttons zurücksetzen
        self.btn_robust.setChecked(False)
        self.btn_robust_rx.setChecked(False)
        # Kurz grau bleiben damit der Reset sichtbar ist, dann HELO neu starten
        if self._mode == "reporter":
            self.lbl_studio_status.setText("Studio ✗")
            self.lbl_studio_status.setStyleSheet(
                "QPushButton:disabled { background: #2d2d2d; border: 1px solid #555; "
                "border-radius: 4px; color: #c0392b; font-size: 12px; padding: 4px 10px; }")
            QTimer.singleShot(800, lambda: self._on_host_changed(self.edit_host.currentText()))

    def _on_busy_reset(self):
        """GUI nach Besetzt-Signal freigeben."""
        self._busy_reset_pending = False
        self._running = False
        self.lbl_status.setText("●  Cleaning up…")
        self.lbl_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #da3;")
        stop_event.clear()
        shared["status"] = "Stopped"
        for key in ("tx_level_l", "tx_level_r", "rx_level_l", "rx_level_r"):
            shared[key] = 0.0
        shared["remote_addr"] = ""
        shared["connected"]   = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_monitor.setEnabled(True)
        self.btn_ban.setEnabled(False)
        if self._mode == "reporter":
            self.edit_host.setEnabled(True)
        self.spin_send_port.setEnabled(True)
        self.spin_recv_port.setEnabled(True)
        self.combo_bitrate.setEnabled(True)
        self.combo_return_bitrate.setEnabled(True)
        self.combo_channels.setEnabled(True)
        self.combo_jitter.setEnabled(True)
        self.combo_input.setEnabled(True)
        self.combo_output.setEnabled(True)
        # HELO wieder starten
        if self._mode == "reporter":
            self._on_host_changed(self.edit_host.currentText())

    # ── GUI Update ─────────────────────────────────

    def _update_ui(self):
        # VU Meter
        self.vu_tx.set_levels(shared["tx_level_l"], shared["tx_level_r"])
        self.vu_rx.set_levels(shared["rx_level_l"], shared["rx_level_r"])

        # ── Besetzt-Erkennung: stop_event gesetzt aber _running noch True ──
        if self._running and stop_event.is_set() and not shared["connected"]:
            status = shared.get("status", "")
            if "Busy" in status:
                self.lbl_status.setText("●  Busy – remote end occupied")
                self.lbl_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #f44;")
                # GUI nach kurzer Verzögerung freigeben
                if not getattr(self, "_busy_reset_pending", False):
                    self._busy_reset_pending = True
                    QTimer.singleShot(2000, self._on_busy_reset)
                return

        # ── Cleaning-up Phase ────────────────────────────────────────────────
        if getattr(self, "_cleaning_up", False):
            self.lbl_status.setText("●  Cleaning up…")
            self.lbl_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #da3;")
            return

        # Status – Farbe nach Verbindungsqualität (mit Hysterese gegen Flackern)
        if self._running and shared["connected"]:
            jbuf        = shared["jitter_buf_ms"]
            jbuf_target = shared["jitter_buf_target_ms"]
            underrun    = shared["underrun"]
            ratio       = (jbuf / jbuf_target) if jbuf_target > 0 else 1.0
            prev = getattr(self, "_link_quality", "green")
            now  = time.time()

            if underrun:
                # Unterläufe innerhalb von 60s zählen – erst ab 3 wird es rot
                underrun_times = getattr(self, "_underrun_times", [])
                underrun_times = [t for t in underrun_times if now - t < 60]
                if not getattr(self, "_underrun_counted", False):
                    underrun_times.append(now)
                    self._underrun_counted = True
                self._underrun_times = underrun_times
                if len(underrun_times) >= 3:
                    quality_state = "red"
                    self._last_underrun_time = now
                else:
                    quality_state = prev  # noch kein Rot – Status halten
            else:
                self._underrun_counted = False  # Unterlauf vorbei – nächsten zählen
                if prev == "red":
                    # Nach Aussetzer: erst nach 5 Minuten ohne Probleme wieder grün
                    last_underrun = getattr(self, "_last_underrun_time", 0)
                    if (now - last_underrun) >= 300:
                        quality_state = "green"
                        self._underrun_times = []  # Zähler zurücksetzen
                    else:
                        quality_state = "red"
                elif ratio < 0.15:
                    quality_state = "yellow"
                elif prev == "yellow" and ratio < 0.30:
                    quality_state = "yellow"
                else:
                    quality_state = "green"
            self._link_quality = quality_state
            if quality_state == "red":
                dot_color = "#f44"
                quality   = "Connected  ▲ Dropouts"
            elif quality_state == "yellow":
                dot_color = "#da3"
                quality   = "Connected  ⚠ Jitter"
            else:
                dot_color = "#3d3"
                quality   = "Connected"
            # Robust-Modus Hinweis (nur im Studio – Reporter sendet Flag)
            if self._mode == "studio" and shared.get("robust_mode", False):
                quality += "  ⚡ Robust"
            self.lbl_status.setText(f"●  {quality}")
            self.lbl_status.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {dot_color};")
        elif self._running:
            self.lbl_status.setText("●  Waiting for remote…")
            self.lbl_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #da3;")
        else:
            self.lbl_status.setText("●  Stopped")
            self.lbl_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #888;")

        addr = shared["remote_addr"]
        self.lbl_connected.setText(f"Remote: {addr if addr else '–'}")

        # Remote-IP groß + Ban-Button (nur Studio)
        if self._mode == "studio":
            connected = shared["connected"]
            ip_only = addr.split(":")[0] if addr else ""
            self.btn_ban.setEnabled(connected and bool(ip_only))

        jitter = shared["jitter_ms"]
        jbuf_t = shared["jitter_buf_target_ms"]
        self.lbl_latency.setText(f"Jitter: {jitter} ms" if jitter is not None else "Jitter: –")
        self.lbl_latency_avg.setText(f"Buffer target: {jbuf_t} ms" if jbuf_t > 0 else "Buffer target: –")

        # Dropouts & Laufzeit
        d_count = shared["dropout_count"]
        d_last  = shared["dropout_last"]
        t_start   = shared["start_time"]
        t_frozen  = shared["elapsed_frozen"]
        if self._running and shared["connected"] and t_start:
            elapsed = int(time.time() - t_start)
            h, r = divmod(elapsed, 3600)
            m, s = divmod(r, 60)
            laufzeit = f"{h}:{m:02}:{s:02}"
        elif self._running and t_frozen is not None:
            h, r = divmod(t_frozen, 3600)
            m, s = divmod(r, 60)
            laufzeit = f"{h}:{m:02}:{s:02} ▪"
        else:
            laufzeit = "–"
        if d_count > 0:
            d_str = f"Dropouts: {d_count}×  (last {d_last})  |  Runtime: {laufzeit}"
            self.lbl_dropouts.setStyleSheet("font-size: 11px; color: #f84;")
        else:
            d_str = f"Dropouts: –  |  Runtime: {laufzeit}"
            self.lbl_dropouts.setStyleSheet("font-size: 11px; color: #bbb;")
        self.lbl_dropouts.setText(d_str)

        tx = shared["frames_tx"]
        rx = shared["frames_rx"]
        jbuf = shared["jitter_buf_ms"]
        jbuf_target = shared["jitter_buf_target_ms"]
        if jbuf_target > 0:
            jbuf_str = f"  |  RX Buffer: {jbuf_target}ms / {jbuf}ms"
        else:
            jbuf_str = ""
        self.lbl_frames.setText(f"TX: {tx}  RX: {rx} Frames{jbuf_str}")

        # Verbindungsdetails live aktualisieren (RX-Bitrate im Studio)
        if self._running and self._mode == "studio":
            rx_br = shared["rx_bitrate"]   # Reporter-Wunsch = Studio-TX... nein: das IST der Rückkanal
            tx_br = shared["tx_bitrate"]   # Gemessene Paketgröße = was vom Reporter ankommt = Studio-RX
            # Aus Studio-Sicht: TX = Rückkanal (pkt_return_kbps), RX = Reporter-Senderate
            tx_str = f"{rx_br} kbps" if rx_br else "auto"   # Studio sendet mit dieser Rate zurück
            rx_str = f"{tx_br} kbps" if tx_br else "auto"   # Studio empfängt mit dieser Rate
            rx_ch = shared.get("rx_channels", None)
            ch_str = ("Mono" if rx_ch == 1 else "Stereo") if rx_ch else "auto"
            recv_port = self.spin_recv_port.value()
            self._conn_details = (
                f"Mode: Studio\n"
                f"Receive: Port {recv_port}\n"
                f"Return: auto\n"
                f"TX: {tx_str}  RX: {rx_str}\n"
                f"Channels: {ch_str}  |  {SAMPLE_RATE//1000} kHz"
            )
        if self._running:
            self.lbl_conn_details.setText(
                getattr(self, "_conn_details", ""))
        else:
            self.lbl_conn_details.setText("")

        # Attention Button – Wall-Clock basiert, blinkt wenn aktiv
        now = time.time()
        attn_tx = (now - shared.get("attention_tx_time", 0)) < 30
        attn_rx = (now - shared.get("attention_rx_time", 0)) < 30
        attn_active = attn_tx or attn_rx
        if attn_active and self._blink_state:
            self.btn_attention.setStyleSheet(
                "background: #1a1a1a; color: #ff4400; border: 2px solid #ff4400; "
                "font-size: 18px; border-radius: 4px;")
        elif attn_active:
            self.btn_attention.setStyleSheet(
                "background: #1a1a1a; color: #661100; border: 2px solid #661100; "
                "font-size: 18px; border-radius: 4px;")
        else:
            self.btn_attention.setStyleSheet(
                "background: #2a2a2a; color: #ffffff; border: 1px solid #ffffff; "
                "font-size: 18px; border-radius: 4px;")

    def _save_config(self):
        """Aktuelle Einstellungen in Config-Datei speichern."""
        # Gerätename aus Dropdown-Text extrahieren (Format: "[idx] Name")
        in_text  = self.combo_input.currentText()
        out_text = self.combo_output.currentText()
        in_name  = in_text.split("] ", 1)[1]  if "] " in in_text  else in_text
        out_name = out_text.split("] ", 1)[1] if "] " in out_text else out_text

        save_config({
            "mode":               self._mode,
            "send_port":          self.spin_send_port.value(),
            "recv_port":          self.spin_recv_port.value(),
            "bitrate":            self.combo_bitrate.currentData(),
            "return_bitrate":     self.combo_return_bitrate.currentData(),
            "channels":           self.combo_channels.currentData(),
            "jitter_index":       self.combo_jitter.currentIndex(),
            "input_device_name":  in_name,
            "output_device_name": out_name,
        })

    def _load_config(self):
        """Gespeicherte Einstellungen wiederherstellen."""
        cfg = load_config()
        if not cfg:
            return

        # Modus
        mode = cfg.get("mode", "reporter")
        self._set_mode(mode)

        # Ports
        try:
            self.spin_send_port.setValue(int(cfg.get("send_port", 5004)))
            self.spin_recv_port.setValue(int(cfg.get("recv_port", 5006)))
        except ValueError:
            pass

        # Bitrate
        try:
            br = int(cfg.get("bitrate", 128000))
            idx = self.combo_bitrate.findData(br)
            if idx >= 0:
                self.combo_bitrate.setCurrentIndex(idx)
        except ValueError:
            pass

        # Rückkanal-Bitrate
        try:
            rbr = int(cfg.get("return_bitrate", 64000))
            idx = self.combo_return_bitrate.findData(rbr)
            if idx >= 0:
                self.combo_return_bitrate.setCurrentIndex(idx)
        except ValueError:
            pass

        # Kanäle
        try:
            ch = int(cfg.get("channels", 2))
            idx = self.combo_channels.findData(ch)
            if idx >= 0:
                self.combo_channels.setCurrentIndex(idx)
        except ValueError:
            pass

        # Jitter-Buffer
        try:
            jidx = int(cfg.get("jitter_index", 4))
            if 0 <= jidx < self.combo_jitter.count():
                self.combo_jitter.setCurrentIndex(jidx)
        except ValueError:
            pass

        # Geräte wurden bereits in _refresh_devices() per Name wiederhergestellt

    def closeEvent(self, event):
        if self._running:
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                "Connection active",
                "There is still an active connection.\nReally quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        self._save_config()
        stop_helo()
        stop_event.set()  # Threads sofort signalisieren
        time.sleep(0.15)  # Kurz warten – closeEvent darf hier kurz blockieren
        event.accept()


# ─────────────────────────────────────────────
#  Einstieg
# ─────────────────────────────────────────────

def _suppress_alsa_errors():
    """ALSA/PortAudio Fehlermeldungen auf stderr unterdrücken."""
    import ctypes
    import ctypes.util
    try:
        asound = ctypes.cdll.LoadLibrary(ctypes.util.find_library('asound'))
        asound.snd_lib_error_set_handler(ctypes.c_void_p(None))
    except Exception:
        pass
    try:
        # stderr kurz umleiten um PortAudio-Startup-Meldungen zu schlucken
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        os.dup2(devnull, 2)
        os.close(devnull)
        import sounddevice  # noqa – triggert PortAudio-Init
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
    except Exception:
        pass


def main():
    _suppress_alsa_errors()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
