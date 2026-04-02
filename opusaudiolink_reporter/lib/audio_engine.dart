import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/services.dart';

// ── Channel-Namen ────────────────────────────────────────────────────────────
const _chControl = 'opusaudiolink/control';
const _chPcmIn   = 'opusaudiolink/pcm_in';
const _chPcmRx   = 'opusaudiolink/pcm_rx';
const _chPcmOut  = 'opusaudiolink/pcm_out';
const _chStats   = 'opusaudiolink/stats';

// ── Status-Snapshot für die UI ───────────────────────────────────────────────
class AudioState {
  bool   connected  = false;
  String remoteAddr = '';
  int?   latencyMs;
  int    framesTx   = 0;
  int    framesRx   = 0;
  int    dropouts   = 0;
  double txLevelL   = 0;
  double txLevelR   = 0;
  double rxLevelL   = 0;
  double rxLevelR   = 0;
  bool   robustMode = false;
  int    rxKbps     = 0;
  int    bufferMs   = 0;
  bool   rejected   = false;

  AudioState copyWith({
    bool?   connected,
    String? remoteAddr,
    int?    latencyMs,
    int?    framesTx,
    int?    framesRx,
    int?    dropouts,
    double? txLevelL,
    double? txLevelR,
    double? rxLevelL,
    double? rxLevelR,
    bool?   robustMode,
    int?    rxKbps,
    int?    bufferMs,
    bool?   rejected,
  }) {
    return AudioState()
      ..connected  = connected  ?? this.connected
      ..remoteAddr = remoteAddr ?? this.remoteAddr
      ..latencyMs  = latencyMs  ?? this.latencyMs
      ..framesTx   = framesTx   ?? this.framesTx
      ..framesRx   = framesRx   ?? this.framesRx
      ..dropouts   = dropouts   ?? this.dropouts
      ..txLevelL   = txLevelL   ?? this.txLevelL
      ..txLevelR   = txLevelR   ?? this.txLevelR
      ..rxLevelL   = rxLevelL   ?? this.rxLevelL
      ..rxLevelR   = rxLevelR   ?? this.rxLevelR
      ..robustMode = robustMode ?? this.robustMode
      ..rxKbps     = rxKbps     ?? this.rxKbps
      ..bufferMs   = bufferMs   ?? this.bufferMs
      ..rejected   = rejected   ?? this.rejected;
  }
}

// ── AudioEngine – Bridge zu Kotlin ──────────────────────────────────────────
class AudioEngine {
  static const _control = MethodChannel(_chControl);
  static const _pcmOut  = MethodChannel(_chPcmOut);
  static const _pcmIn   = EventChannel(_chPcmIn);
  static const _pcmRx   = EventChannel(_chPcmRx);
  static const _stats   = EventChannel(_chStats);

  void Function(AudioState)? onStateUpdate;
  void Function(bool)?        onHeloStatus;
  void Function()?            onAttention;

  // Persistenter State – wird nie komplett ersetzt, nur per copyWith aktualisiert
  AudioState _state = AudioState();

  // Stream-Subscriptions – müssen beim Stop gecancelt werden
  final List<StreamSubscription> _subs = [];

  // ── Berechtigungen ──────────────────────────────────────────────────────

  Future<bool> hasPermission() async =>
      await _control.invokeMethod<bool>('hasPermission') ?? false;

  Future<void> requestPermission() async =>
      await _control.invokeMethod('requestPermission');

  // ── Engine starten ──────────────────────────────────────────────────────

  Future<void> start({
    required String host,
    required int    sendPort,
    required int    recvPort,
    int channels      = 1,
    int bitrate       = 64000,
    int returnBitrate = 64000,
    int jitterFrames  = 4,
  }) async {
    await _control.invokeMethod('start', {
      'sampleRate':    48000,
      'channels':      channels,
      'bufferFrames':  960,
      'host':          host,
      'sendPort':      sendPort,
      'recvPort':      recvPort,
      'bitrate':       bitrate,
      'returnBitrate': returnBitrate,
    });

    // TX PCM → VU-Meter
    _subs.add(_pcmIn.receiveBroadcastStream().map(
      (data) => Uint8List.fromList(List<int>.from(data as List))
    ).listen((pcm) {
      final level = _rms(pcm);
      _emit(_state.copyWith(txLevelL: level, txLevelR: level));
    }));

    // RX PCM → VU-Meter
    _subs.add(_pcmRx.receiveBroadcastStream().map(
      (data) => Uint8List.fromList(List<int>.from(data as List))
    ).listen((pcm) {
      // Stereo: L und R getrennt berechnen
      final samples = pcm.buffer.asInt16List();
      if (samples.length >= 2 && samples.length % 2 == 0) {
        // Stereo – gerade Samples = L, ungerade = R
        double sumL = 0, sumR = 0;
        for (int i = 0; i < samples.length; i += 2) {
          sumL += samples[i] * samples[i];
          sumR += samples[i + 1] * samples[i + 1];
        }
        final count = samples.length / 2;
        final levelL = (sumL / count).clamp(0, 32768.0 * 32768.0) / (32768.0 * 32768.0);
        final levelR = (sumR / count).clamp(0, 32768.0 * 32768.0) / (32768.0 * 32768.0);
        _emit(_state.copyWith(rxLevelL: levelL, rxLevelR: levelR));
      } else {
        final level = _rms(pcm);
        _emit(_state.copyWith(rxLevelL: level, rxLevelR: level));
      }
    }));

    // Stats / HELO-Events
    _subs.add(_stats.receiveBroadcastStream().listen((event) {
      final map = Map<String, dynamic>.from(event as Map);

      if (map.containsKey('attention')) {
        onAttention?.call();
      }

      if (map.containsKey('rejected')) {
        _emit(_state.copyWith(rejected: true, connected: false));
      }

      if (map.containsKey('heloOk')) {
        final ok   = map['heloOk']     as bool;
        final addr = map['remoteAddr'] as String? ?? '';
        _emit(_state.copyWith(connected: ok, remoteAddr: ok ? addr : ''));
        onHeloStatus?.call(ok);
      }

      if (map.containsKey('framesTx')) {
        _emit(_state.copyWith(
          connected: (map['connected'] as bool?) ?? _state.connected,
          remoteAddr: (map['remoteAddr'] as String?) ?? _state.remoteAddr,
          framesTx: map['framesTx'] as int,
          framesRx: map['framesRx'] as int,
          dropouts: map['dropouts'] as int,
          rxKbps:   (map['rxKbps']   as int?) ?? _state.rxKbps,
          bufferMs: (map['bufferMs'] as int?) ?? _state.bufferMs,
        ));
      }
    }));
  }

  // ── PCM für Wiedergabe schreiben (Flutter-seitig, selten nötig) ──────────

  Future<void> writePcm(Uint8List data) async =>
      await _pcmOut.invokeMethod('write', {'data': data});

  void setAudioMode(int mode) {
    _control.invokeMethod('setAudioMode', {'mode': mode});
  }

  void sendAttention() {
    _control.invokeMethod('sendAttention');
  }

  // ── Engine stoppen ───────────────────────────────────────────────────────

  void stop() {
    for (final sub in _subs) { sub.cancel(); }
    _subs.clear();
    _control.invokeMethod('stop');
    _state = AudioState();
    onStateUpdate?.call(_state);
  }

  // ── HELO (Delegiert an Kotlin) ───────────────────────────────────────────

  void startHelo({
    required String host,
    required int    port,
    required void Function(bool) onStatus,
  }) {
    // Stats-Stream abonnieren falls noch nicht aktiv
    if (_subs.isEmpty) {
      _subs.add(_stats.receiveBroadcastStream().listen((event) {
        final map = Map<String, dynamic>.from(event as Map);
        if (map.containsKey('heloOk')) {
          final ok   = map['heloOk']     as bool;
          final addr = map['remoteAddr'] as String? ?? '';
          _emit(_state.copyWith(connected: false, remoteAddr: ok ? addr : ''));
          onHeloStatus?.call(ok);
        }
      }));
    }
    _control.invokeMethod('startHelo', {'host': host, 'port': port});
  }

  void stopHelo() {
    _control.invokeMethod('stopHelo');
  }

  // ── Intern ───────────────────────────────────────────────────────────────

  void _emit(AudioState s) {
    _state = s;
    onStateUpdate?.call(s);
  }

  double _rms(Uint8List pcm) {
    if (pcm.isEmpty) return 0;
    final samples = pcm.buffer.asInt16List();
    double sum = 0;
    for (final s in samples) { sum += s * s; }
    return (sum / samples.length).clamp(0, 32768.0 * 32768.0) /
           (32768.0 * 32768.0);
  }
}
