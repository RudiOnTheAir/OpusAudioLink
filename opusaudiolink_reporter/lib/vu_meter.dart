import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

const double kVuLevelOffsetDb   = 32.0;  // TX
const double kVuLevelOffsetDbRX = 32.0;  // RX – höherer Offset für Broadcast-Pegel

// ─────────────────────────────────────────────────────────────────────────────
//  VUMeter – Einzelkanal
// ─────────────────────────────────────────────────────────────────────────────
class VUMeter extends StatefulWidget {
  final String label;
  final double level;
  final double offsetDb;

  const VUMeter({
    super.key,
    required this.label,
    required this.level,
    this.offsetDb = kVuLevelOffsetDb,
  });

  @override
  State<VUMeter> createState() => _VUMeterState();
}

class _VUMeterState extends State<VUMeter> with SingleTickerProviderStateMixin {
  late final Ticker _ticker;

  double _smoothed = 0.0;
  double _peak     = 0.0;
  int    _peakTtl  = 0;

  @override
  void initState() {
    super.initState();
    // Ticker läuft mit ~60 fps und treibt den Peak-Decay an
    _ticker = createTicker((_) {
      setState(() => _tick(widget.level));
    })..start();
  }

  @override
  void dispose() {
    _ticker.dispose();
    super.dispose();
  }

  void _tick(double rms) {
    double level = 0.0;
    if (rms > 0) {
      final db = 20 * math.log(math.max(rms, 1e-6)) / math.ln10 + widget.offsetDb;
      level = ((db + 60.0) / 60.0).clamp(0.0, 1.0);
    }
    // Schnell hoch, langsam runter
    _smoothed = level > _smoothed
        ? _smoothed * 0.3 + level * 0.7
        : _smoothed * 0.85 + level * 0.15;

    // Peak-Hold
    if (_smoothed >= _peak) {
      _peak    = _smoothed;
      _peakTtl = 60; // ~1 s bei 60 fps
    } else if (_peakTtl > 0) {
      _peakTtl--;
    } else {
      _peak = math.max(0.0, _peak - 0.004);
    }
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 18,
      child: Column(
        children: [
          Expanded(
            child: CustomPaint(
              painter: _VUBarPainter(level: _smoothed, peak: _peak),
              child: const SizedBox.expand(),
            ),
          ),
          const SizedBox(height: 2),
          Text(
            widget.label,
            style: const TextStyle(
              fontSize: 9,
              fontWeight: FontWeight.bold,
              color: Color(0xFFC8C8C8),
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  Painter
// ─────────────────────────────────────────────────────────────────────────────
class _VUBarPainter extends CustomPainter {
  final double level;
  final double peak;

  _VUBarPainter({required this.level, required this.peak});

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;

    // Hintergrund
    canvas.drawRect(
      Rect.fromLTWH(0, 0, w, h),
      Paint()..color = const Color(0xFF1E1E1E),
    );

    // Farbverlauf-Balken
    final fillH = (h * level).clamp(0.0, h);
    if (fillH > 0) {
      final paint = Paint()
        ..shader = const LinearGradient(
          begin: Alignment.bottomCenter,
          end: Alignment.topCenter,
          stops: [0.0, 0.65, 0.85, 1.0],
          colors: [
            Color(0xFF00C832),
            Color(0xFFB4DC00),
            Color(0xFFFFA500),
            Color(0xFFFF1E1E),
          ],
        ).createShader(Rect.fromLTWH(0, 0, w, h));
      canvas.drawRect(Rect.fromLTWH(0, h - fillH, w, fillH), paint);
    }

    // Peak-Linie
    if (peak > 0) {
      final peakY = h * (1.0 - peak);
      canvas.drawLine(
        Offset(0, peakY), Offset(w, peakY),
        Paint()
          ..color = peak > 0.85 ? const Color(0xFFFF1E1E) : const Color(0xFFFFFF64)
          ..strokeWidth = 2,
      );
    }

    // Rahmen
    canvas.drawRect(
      Rect.fromLTWH(0, 0, w - 1, h - 1),
      Paint()
        ..color = const Color(0xFF505050)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1,
    );

    // dB-Markierungen
    for (final db in [-6, -12, -18, -30]) {
      final y = h * (1.0 - (db + 60.0) / 60.0);
      canvas.drawLine(
        Offset(w - 4, y), Offset(w, y),
        Paint()
          ..color = const Color(0xFF646464)
          ..strokeWidth = 1,
      );
    }
  }

  @override
  bool shouldRepaint(_VUBarPainter old) =>
      old.level != level || old.peak != peak;
}

// ─────────────────────────────────────────────────────────────────────────────
//  StereoVUMeter – TX (links)
// ─────────────────────────────────────────────────────────────────────────────
class StereoVUMeter extends StatelessWidget {
  final String title;
  final double levelL;
  final double levelR;

  const StereoVUMeter({
    super.key,
    required this.title,
    required this.levelL,
    required this.levelR,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 72,
      decoration: BoxDecoration(
        border: Border.all(color: const Color(0xFF444444)),
        borderRadius: BorderRadius.circular(4),
      ),
      padding: const EdgeInsets.fromLTRB(4, 4, 4, 4),
      child: Column(
        children: [
          Text(title,
            style: const TextStyle(fontSize: 10, color: Color(0xFFAAAAAA))),
          const SizedBox(height: 4),
          Expanded(
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                Expanded(child: VUMeter(label: 'L', level: levelL)),
                const SizedBox(width: 3),
                Expanded(child: VUMeter(label: 'R', level: levelR)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
//  StereoVUMeterRX – Studio (rechts) mit Audio-Toggle
// ─────────────────────────────────────────────────────────────────────────────
class StereoVUMeterRX extends StatelessWidget {
  final double levelL;
  final double levelR;
  final int    audioMode;  // 0=Hörer, 1=Lautsprecher, 2=Stumm
  final VoidCallback onToggle;

  const StereoVUMeterRX({
    super.key,
    required this.levelL,
    required this.levelR,
    required this.audioMode,
    required this.onToggle,
  });

  String get _modeIcon => audioMode == 0 ? '🔊' : '🔇';

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onToggle,
      child: Container(
        width: 72,
        decoration: BoxDecoration(
          border: Border.all(color: const Color(0xFF444444)),
          borderRadius: BorderRadius.circular(4),
        ),
        padding: const EdgeInsets.fromLTRB(4, 4, 4, 4),
        child: Column(
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text('Studio',
                  style: TextStyle(fontSize: 10, color: Color(0xFFAAAAAA))),
                Text(_modeIcon,
                  style: const TextStyle(fontSize: 12)),
              ],
            ),
            const SizedBox(height: 4),
            Expanded(
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  Expanded(child: VUMeter(label: 'L',
                    level: audioMode == 2 ? 0 : levelL,
                    offsetDb: kVuLevelOffsetDbRX)),
                  const SizedBox(width: 3),
                  Expanded(child: VUMeter(label: 'R',
                    level: audioMode == 2 ? 0 : levelR,
                    offsetDb: kVuLevelOffsetDbRX)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
