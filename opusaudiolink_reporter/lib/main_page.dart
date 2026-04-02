import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'audio_engine.dart';
import 'vu_meter.dart';
import 'settings_page.dart';

class MainPage extends StatefulWidget {
  const MainPage({super.key});

  @override
  State<MainPage> createState() => _MainPageState();
}

class _MainPageState extends State<MainPage> with SingleTickerProviderStateMixin {
  late final Ticker _blinkTicker;
  int _blinkCounter = 0;
  final AudioEngine _engine = AudioEngine();
  AudioState _state = AudioState();

  StudioPreset? _activePreset;
  bool _running  = false;
  bool _studioOk = false;
  bool _busy     = false;
  bool _poorConnection = false;
  int  _audioMode = 0;
  bool _attentionActive = false;
  bool _blinkState = false;  // 0=Hörer, 1=Lautsprecher, 2=Stumm
  int  _lastDropouts = 0;
  DateTime _lastDropoutCheck = DateTime.now();

  @override
  void initState() {
    super.initState();
    // Blink-Ticker ~2Hz
    _blinkTicker = createTicker((_) {
      _blinkCounter++;
      if (_blinkCounter % 30 == 0) {  // ~500ms bei 60fps
        if (mounted) setState(() => _blinkState = !_blinkState);
      }
    })..start();
    _engine.onStateUpdate = (s) {
      if (mounted) setState(() {
        _state = s;
        if (s.rejected && _running) {
          _running = false;
          _busy = true;
          WidgetsBinding.instance.addPostFrameCallback((_) {
            _engine.stop();
            Future.delayed(const Duration(seconds: 5), () {
              if (mounted) setState(() => _busy = false);
              if (_activePreset != null) _startHelo(_activePreset!);
            });
          });
        }
        final now = DateTime.now();
        if (now.difference(_lastDropoutCheck).inSeconds >= 5) {
          final newDropouts = s.dropouts - _lastDropouts;
          _poorConnection = newDropouts > 2;
          _lastDropouts = s.dropouts;
          _lastDropoutCheck = now;
        }
      });
    };
    _engine.onHeloStatus = (ok) {
      if (mounted) setState(() => _studioOk = ok);
    };
    _engine.onAttention = () {
      if (mounted) setState(() {
        _attentionActive = true;
        Future.delayed(const Duration(seconds: 30), () {
          if (mounted) setState(() => _attentionActive = false);
        });
      });
    };
    _loadLastPreset();
  }

  Future<void> _loadLastPreset() async {
    final presets = await PresetStore.load();
    if (presets.isNotEmpty && mounted) {
      setState(() => _activePreset = presets.first);
      _startHelo(presets.first);
    }
  }

  void _startHelo(StudioPreset p) {
    if (p.host.isEmpty) return;
    _engine.startHelo(
      host: p.host,
      port: p.sendPort,
      onStatus: (ok) {
        if (mounted) setState(() => _studioOk = ok);
      },
    );
  }

  Future<void> _onConnect() async {
    if (_running) {
      // Disconnect → HELO wieder starten
      _engine.stop();
      setState(() { _running = false; _studioOk = false; });
      if (_activePreset != null) _startHelo(_activePreset!);
      return;
    }

    if (_activePreset == null || _activePreset!.host.isEmpty) {
      _showSnack('Please configure a studio preset first (⚙)');
      return;
    }

    // Mikrofonberechtigung über nativen Channel
    if (!await _engine.hasPermission()) {
      await _engine.requestPermission();
      if (!await _engine.hasPermission()) {
        _showSnack('Microphone permission required');
        return;
      }
    }

    await _engine.start(
      host:          _activePreset!.host,
      sendPort:      _activePreset!.sendPort,
      recvPort:      _activePreset!.recvPort,
      channels:      _activePreset!.channels,
      bitrate:       _activePreset!.bitrateKbps * 1000,
      returnBitrate: _activePreset!.returnBrKbps * 1000,
      jitterFrames:  _activePreset!.jitterFrames,
    );
    // HELO stoppen – Audio übernimmt jetzt
    _engine.stopHelo();
    setState(() => _running = true);
  }

  void _toggleAudio() {
    setState(() => _audioMode = (_audioMode + 1) % 2);
    _engine.setAudioMode(_audioMode);
  }

  void _onAttention() {
    if (!_running) return;
    setState(() => _attentionActive = true);
    _engine.sendAttention();
    Future.delayed(const Duration(seconds: 30), () {
      if (mounted) setState(() => _attentionActive = false);
    });
  }

  void _showSnack(String msg) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg,
          style: const TextStyle(color: Colors.white)),
        backgroundColor: const Color(0xFF3A3A3A),
      ),
    );
  }

  void _openSettings() {
    if (_running) {
      _showSnack('Cannot change settings while connected');
      return;
    }
    Navigator.push(
      context,
      MaterialPageRoute(
        builder: (_) => SettingsPage(
          activePreset: _activePreset,
          onPresetSelected: (p) {
            setState(() {
              _activePreset = p;
              _studioOk = false;
            });
            _engine.stopHelo();
            _startHelo(p);
          },
        ),
      ),
    );
  }

  @override
  void dispose() {
    _blinkTicker.dispose();
    _engine.stop();
    _engine.stopHelo();
    super.dispose();
  }

  // ── UI ─────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'OpusAudioLink',
          style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            tooltip: 'Settings / Presets',
            onPressed: _openSettings,
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              // ── Preset-Anzeige ──────────────
              _PresetCard(
                preset:        _activePreset,
                studioOk:      _studioOk,
                running:       _running,
                connected:     _state.connected,
                poorConnection: _poorConnection,
              ),
              const SizedBox(height: 16),

              // ── VU-Meter ────────────────────
              Expanded(
                flex: 3,
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    StereoVUMeter(
                      title: 'TX  (Mic)',
                      levelL: _state.txLevelL,
                      levelR: _state.txLevelR,
                    ),
                    // ── Achtung-Button ──────────
                    GestureDetector(
                      onTap: _onAttention,
                      child: SizedBox(
                        width: 72,
                        height: 72,
                        child: ColorFiltered(
                          colorFilter: _attentionActive && _blinkState
                              ? const ColorFilter.mode(
                                  Colors.transparent, BlendMode.multiply)
                              : const ColorFilter.matrix([
                                  0.2126, 0.7152, 0.0722, 0, 0,
                                  0.2126, 0.7152, 0.0722, 0, 0,
                                  0.2126, 0.7152, 0.0722, 0, 0,
                                  0,      0,      0,      1, 0,
                                ]),
                          child: Image.asset(
                            'assets/achtung.png',
                            fit: BoxFit.contain,
                          ),
                        ),
                      ),
                    ),
                    StereoVUMeterRX(
                      levelL:    _state.rxLevelL,
                      levelR:    _state.rxLevelR,
                      audioMode: _audioMode,
                      onToggle:  _toggleAudio,
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              // ── Status ──────────────────────
              _StatusPanel(
                state:   _state,
                running: _running,
                busy:    _busy,
                txKbps:  _activePreset?.bitrateKbps ?? 0,
                rxKbps:  _activePreset?.returnBrKbps ?? 0,
              ),
              const SizedBox(height: 20),

              // ── Connect / Disconnect ────────
              SizedBox(
                height: 56,
                child: ElevatedButton.icon(
                  onPressed: _onConnect,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _running
                        ? const Color(0xFF5C1A1A)
                        : const Color(0xFF1A5C2A),
                    foregroundColor: Colors.white,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(8),
                      side: BorderSide(
                        color: _running
                            ? const Color(0xFF8C2A2A)
                            : const Color(0xFF2A8C3A),
                        width: 1.5,
                      ),
                    ),
                  ),
                  icon: Icon(_running ? Icons.stop : Icons.play_arrow, size: 24),
                  label: Text(
                    _running ? 'Disconnect' : 'Connect',
                    style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────
//  Preset-Karte
// ─────────────────────────────────────────────
class _PresetCard extends StatelessWidget {
  final StudioPreset? preset;
  final bool studioOk;
  final bool running;
  final bool connected;
  final bool poorConnection;

  const _PresetCard({
    required this.preset,
    required this.studioOk,
    required this.running,
    required this.connected,
    required this.poorConnection,
  });

  @override
  Widget build(BuildContext context) {
    final name = preset?.name ?? '–  (no preset selected)';
    final host = preset?.host ?? '';
    final dot  = connected && poorConnection
        ? '🟠'
        : connected
            ? '🔵'
            : studioOk
                ? '🟢'
                : '⚫';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: const Color(0xFF2D2D2D),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: const Color(0xFF444444)),
      ),
      child: Row(
        children: [
          Text(dot, style: const TextStyle(fontSize: 18)),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name,
                  style: const TextStyle(
                    color: Color(0xFFE0E0E0),
                    fontWeight: FontWeight.bold,
                    fontSize: 14,
                  ),
                ),
                if (host.isNotEmpty)
                  Text(host,
                    style: const TextStyle(
                      color: Color(0xFF888888),
                      fontSize: 11,
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────
//  Status-Panel (kompakt)
// ─────────────────────────────────────────────
class _StatusPanel extends StatelessWidget {
  final AudioState state;
  final bool running;
  final bool busy;
  final int  txKbps;
  final int  rxKbps;

  const _StatusPanel({
    required this.state,
    required this.running,
    required this.busy,
    required this.txKbps,
    required this.rxKbps,
  });

  @override
  Widget build(BuildContext context) {
    // Status-Text + LED-Farbe
    String statusText;
    Color  statusColor;

    if (busy) {
      statusText  = '●  Busy – Studio besetzt';
      statusColor = const Color(0xFFFF4444);
    } else if (state.connected) {
      if (state.dropouts > 5) {
        statusText  = '●  Connected  ⚠ ${state.dropouts}× Dropout';
        statusColor = const Color(0xFFFF4444);
      } else if (state.dropouts > 0) {
        statusText  = '●  Connected  ${state.dropouts}× Dropout';
        statusColor = const Color(0xFFFFAA33);
      } else {
        statusText  = '●  Connected';
        statusColor = const Color(0xFF33DD33);
      }
    } else if (running) {
      statusText  = '●  Waiting for studio…';
      statusColor = const Color(0xFFDDAA33);
    } else {
      statusText  = '●  Stopped';
      statusColor = const Color(0xFF888888);
    }

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF252525),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: const Color(0xFF3A3A3A)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(statusText,
            style: TextStyle(
              color: statusColor,
              fontWeight: FontWeight.bold,
              fontSize: 14,
            ),
          ),
          const SizedBox(height: 6),
          _row('Remote', state.remoteAddr.isEmpty ? '–' : state.remoteAddr),
          _row('TX / RX Frames', '${state.framesTx} / ${state.framesRx}'),
          if (running) ...[
            const SizedBox(height: 4),
            Row(
              children: [
                Text('TX/RX: $txKbps / $rxKbps kbps',
                  style: const TextStyle(color: Color(0xFF888888), fontSize: 11)),
                const Spacer(),
                Text(
                  state.bufferMs > 0 ? 'Buffer: ${state.bufferMs} ms' : '',
                  style: const TextStyle(color: Color(0xFF888888), fontSize: 11),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }

  Widget _row(String label, String value, {Color? color}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: Row(
        children: [
          SizedBox(
            width: 110,
            child: Text('$label:',
              style: const TextStyle(color: Color(0xFF888888), fontSize: 11)),
          ),
          Text(value,
            style: TextStyle(
              color: color ?? const Color(0xFFCCCCCC),
              fontSize: 11,
            ),
          ),
        ],
      ),
    );
  }
}


