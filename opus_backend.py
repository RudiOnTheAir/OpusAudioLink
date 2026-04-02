#!/usr/bin/env python3
"""
opus_backend.py – Plattformunabhängiger Opus-Wrapper.

Reihenfolge:
  1. opuslib        (Linux/NixOS)
  2. ctypes direkt  (Windows/macOS – kein PyOgg nötig)

Installation:
    Linux/NixOS:  pip install opuslib
    Windows:      kein extra Paket – libopus.dll liegt im App-Verzeichnis
    macOS:        brew install opus
"""

import sys
import ctypes
import ctypes.util
import os
import struct as _struct

# ── Backend erkennen ──────────────────────────────────────────────────────────

BACKEND = None

try:
    import opuslib as _opuslib
    BACKEND = "opuslib"
except ImportError:
    pass

if BACKEND is None:
    # Direkte ctypes-Anbindung – funktioniert auf Windows und macOS
    # ohne PyOgg. Sucht libopus in dieser Reihenfolge:
    #   1. Neben der .exe (sys._MEIPASS bei PyInstaller)
    #   2. Systemweit via ctypes.util.find_library
    #   3. Bekannte Windows-Namen
    _libopus = None

    def _find_opus():
        candidates = []

        # PyInstaller: DLL liegt im entpackten Temp-Verzeichnis
        if hasattr(sys, '_MEIPASS'):
            for name in ('libopus-0.dll', 'opus.dll', 'libopus.dll'):
                candidates.append(os.path.join(sys._MEIPASS, name))

        # Neben dem Script / der EXE
        base = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, 'frozen', False) else __file__))
        for name in ('libopus-0.dll', 'opus.dll', 'libopus.dll',
                     'libopus.so.0', 'libopus.0.dylib'):
            candidates.append(os.path.join(base, name))

        # Systemweit
        found = ctypes.util.find_library('opus')
        if found:
            candidates.append(found)

        # Windows-Fallback-Namen
        for name in ('libopus-0.dll', 'opus.dll', 'libopus.dll'):
            candidates.append(name)

        for path in candidates:
            try:
                lib = ctypes.cdll.LoadLibrary(path)
                # Kurzer Funktionstest
                lib.opus_get_version_string.restype = ctypes.c_char_p
                lib.opus_get_version_string()
                return lib
            except Exception:
                continue
        return None

    _libopus = _find_opus()

    if _libopus is not None:
        BACKEND = "ctypes"

if BACKEND is None:
    print("FEHLER: Kein Opus-Backend gefunden!")
    print("  Linux:   pip install opuslib")
    print("  Windows: libopus-0.dll neben die .exe legen")
    sys.exit(1)


# ── ctypes Opus API ───────────────────────────────────────────────────────────

if BACKEND == "ctypes":
    OPUS_APPLICATION_VOIP = 2048
    OPUS_SET_BITRATE_REQUEST = 4002
    OPUS_OK = 0

    # Encoder
    _libopus.opus_encoder_get_size.restype  = ctypes.c_int
    _libopus.opus_encoder_get_size.argtypes = [ctypes.c_int]

    _libopus.opus_encoder_init.restype  = ctypes.c_int
    _libopus.opus_encoder_init.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]

    _libopus.opus_encode.restype  = ctypes.c_int
    _libopus.opus_encode.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int32,
    ]

    _libopus.opus_encoder_create.restype  = ctypes.c_void_p
    _libopus.opus_encoder_create.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

    _libopus.opus_encode_float.restype  = ctypes.c_int
    _libopus.opus_encode_float.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int32,
    ]

    _libopus.opus_encoder_ctl.restype  = ctypes.c_int

    # Decoder
    _libopus.opus_decoder_get_size.restype  = ctypes.c_int
    _libopus.opus_decoder_get_size.argtypes = [ctypes.c_int]

    _libopus.opus_decoder_create.restype  = ctypes.c_void_p
    _libopus.opus_decoder_create.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

    _libopus.opus_decoder_init.restype  = ctypes.c_int
    _libopus.opus_decoder_init.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int]

    _libopus.opus_decode_float.restype  = ctypes.c_int
    _libopus.opus_decode_float.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
    ]

    _libopus.opus_decode.restype  = ctypes.c_int
    _libopus.opus_decode.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_int,
    ]


# ── Einheitliche Encoder-Klasse ───────────────────────────────────────────────

class OpusEncoder:
    def __init__(self, sample_rate: int, channels: int, bitrate: int):
        self.sample_rate = sample_rate
        self.channels    = channels
        self.bitrate     = bitrate

        if BACKEND == "opuslib":
            self._enc = _opuslib.Encoder(
                sample_rate, channels, _opuslib.APPLICATION_VOIP)
            self._enc.bitrate = bitrate

        elif BACKEND == "ctypes":
            err = ctypes.c_int(OPUS_OK)
            raw = _libopus.opus_encoder_create(
                sample_rate, channels, OPUS_APPLICATION_VOIP, ctypes.byref(err))
            if err.value != OPUS_OK or not raw:
                raise RuntimeError(f"opus_encoder_create failed: {err.value}")
            self._ptr = ctypes.c_void_p(raw)
            self._set_bitrate(bitrate)

    def _set_bitrate(self, bitrate: int):
        if BACKEND == "ctypes":
            _libopus.opus_encoder_ctl(
                self._ptr,
                OPUS_SET_BITRATE_REQUEST,
                ctypes.c_int(bitrate),
            )

    def set_bitrate(self, bitrate: int):
        self.bitrate = bitrate
        if BACKEND == "opuslib":
            self._enc.bitrate = bitrate
        elif BACKEND == "ctypes":
            self._set_bitrate(bitrate)

    def encode(self, pcm_bytes: bytes, frame_size: int) -> bytes:
        if BACKEND == "opuslib":
            return self._enc.encode(pcm_bytes, frame_size)

        elif BACKEND == "ctypes":
            pcm_arr = (ctypes.c_int16 * (len(pcm_bytes) // 2)).from_buffer_copy(pcm_bytes)
            out = ctypes.create_string_buffer(4096)
            n = _libopus.opus_encode(self._ptr, pcm_arr, frame_size, out, 4096)
            if n < 0:
                raise RuntimeError(f"opus_encode failed: {n}")
            return out.raw[:n]

    def encode_float(self, pcm_float: bytes, frame_size: int) -> bytes:
        """Float32-PCM direkt enkodieren – keine Int16-Konvertierung."""
        if BACKEND == "opuslib":
            return self._enc.encode(pcm_float, frame_size)
        elif BACKEND == "ctypes":
            pcm_arr = (ctypes.c_float * (len(pcm_float) // 4)).from_buffer_copy(pcm_float)
            out = ctypes.create_string_buffer(4096)
            n = _libopus.opus_encode_float(self._ptr, pcm_arr, frame_size, out, 4096)
            if n < 0:
                raise RuntimeError(f"opus_encode_float failed: {n}")
            return out.raw[:n]


# ── Einheitliche Decoder-Klasse ───────────────────────────────────────────────

class OpusDecoder:
    def __init__(self, sample_rate: int, channels: int):
        self.sample_rate = sample_rate
        self.channels    = channels

        if BACKEND == "opuslib":
            self._dec = _opuslib.Decoder(sample_rate, channels)

        elif BACKEND == "ctypes":
            err = ctypes.c_int(OPUS_OK)
            raw = _libopus.opus_decoder_create(
                sample_rate, channels, ctypes.byref(err))
            if err.value != OPUS_OK or not raw:
                raise RuntimeError(f"opus_decoder_create failed: {err.value}")
            self._ptr = ctypes.c_void_p(raw)

    def decode(self, encoded_bytes: bytes, frame_size: int) -> bytes:
        if BACKEND == "opuslib":
            return self._dec.decode(encoded_bytes, frame_size)

        elif BACKEND == "ctypes":
            out = (ctypes.c_int16 * (frame_size * self.channels))()
            n = _libopus.opus_decode(
                self._ptr,
                encoded_bytes,
                len(encoded_bytes),
                out,
                frame_size,
                0,
            )
            if n < 0:
                raise RuntimeError(f"opus_decode failed: {n}")
            return bytes(out)

    def decode_float(self, encoded_bytes: bytes, frame_size: int) -> bytes:
        """Direkt zu Float32 dekodieren – keine Int16-Konvertierung."""
        if BACKEND == "opuslib":
            import numpy as _np
            pcm_int = self._dec.decode(encoded_bytes, frame_size)
            return _np.frombuffer(pcm_int, dtype=_np.int16).astype(_np.float32) / 32767.0
        elif BACKEND == "ctypes":
            out = (ctypes.c_float * (frame_size * self.channels))()
            n = _libopus.opus_decode_float(
                self._ptr,
                encoded_bytes,
                len(encoded_bytes),
                out,
                frame_size,
                0,
            )
            if n < 0:
                raise RuntimeError(f"opus_decode_float failed: {n}")
            return bytes(out)


# ── Info ──────────────────────────────────────────────────────────────────────

def backend_info() -> str:
    if BACKEND == "ctypes":
        try:
            _libopus.opus_get_version_string.restype = ctypes.c_char_p
            ver = _libopus.opus_get_version_string().decode()
            return f"Opus-Backend: ctypes ({ver})"
        except Exception:
            pass
    return f"Opus-Backend: {BACKEND}"


if __name__ == "__main__":
    print(backend_info())
    print("Teste Encoder/Decoder...")
    import numpy as np
    SR = 48000
    CH = 2
    FS = 960
    enc = OpusEncoder(SR, CH, 128000)
    dec = OpusDecoder(SR, CH)
    pcm = (np.zeros((FS, CH), dtype=np.float32) * 32767).astype(np.int16).tobytes()
    encoded = enc.encode(pcm, FS)
    decoded = dec.decode(encoded, FS)
    print(f"  Encode: {len(pcm)} bytes PCM → {len(encoded)} bytes Opus")
    print(f"  Decode: {len(encoded)} bytes Opus → {len(decoded)} bytes PCM")
    print("OK.")
