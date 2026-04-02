import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'dart:convert';

// ─────────────────────────────────────────────
//  Preset-Modell
// ─────────────────────────────────────────────
class StudioPreset {
  String name;
  String host;
  int    sendPort;
  int    recvPort;
  int    bitrateKbps;
  int    returnBrKbps;
  int    jitterFrames;
  int    channels;

  StudioPreset({
    required this.name,
    required this.host,
    this.sendPort     = 5004,
    this.recvPort     = 5006,
    this.bitrateKbps  = 64,
    this.returnBrKbps = 64,
    this.jitterFrames = 4,
    this.channels     = 1,
  });

  Map<String, dynamic> toJson() => {
    'name':           name,
    'host':           host,
    'sendPort':       sendPort,
    'recvPort':       recvPort,
    'bitrateKbps':    bitrateKbps,
    'returnBrKbps':   returnBrKbps,
    'jitterFrames':   jitterFrames,
    'channels':       channels,
  };

  factory StudioPreset.fromJson(Map<String, dynamic> j) => StudioPreset(
    name:           j['name']          ?? 'Studio',
    host:           j['host']          ?? '',
    sendPort:       j['sendPort']      ?? 5004,
    recvPort:       j['recvPort']      ?? 5006,
    bitrateKbps:    j['bitrateKbps']   ?? 64,
    returnBrKbps:   j['returnBrKbps']  ?? 64,
    jitterFrames:   j['jitterFrames']  ?? 4,
    channels:       j['channels']      ?? 1,
  );
}

// ─────────────────────────────────────────────
//  Preset-Speicher
// ─────────────────────────────────────────────
class PresetStore {
  static const _key = 'studio_presets';

  static Future<List<StudioPreset>> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw   = prefs.getString(_key);
    if (raw == null) return [];
    try {
      final list = jsonDecode(raw) as List;
      return list.map((e) => StudioPreset.fromJson(e)).toList();
    } catch (_) {
      return [];
    }
  }

  static Future<void> save(List<StudioPreset> presets) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, jsonEncode(presets.map((e) => e.toJson()).toList()));
  }
}

// ─────────────────────────────────────────────
//  Settings Page
// ─────────────────────────────────────────────
class SettingsPage extends StatefulWidget {
  final StudioPreset? activePreset;
  final void Function(StudioPreset) onPresetSelected;

  const SettingsPage({
    super.key,
    required this.activePreset,
    required this.onPresetSelected,
  });

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  List<StudioPreset> _presets = [];
  String _version = '';

  @override
  void initState() {
    super.initState();
    _loadPresets();
    _loadVersion();
  }

  Future<void> _loadVersion() async {
    try {
      final info = await PackageInfo.fromPlatform();
      final v = info.version.isNotEmpty ? info.version : '0.7';
      final b = info.buildNumber.isNotEmpty ? info.buildNumber : '–';
      if (mounted) setState(() => _version = 'v$v  (Build $b)');
    } catch (_) {
      if (mounted) setState(() => _version = 'v0.7');
    }
  }

  Future<void> _loadPresets() async {
    final list = await PresetStore.load();
    setState(() => _presets = list);
  }

  Future<void> _savePresets() async {
    await PresetStore.save(_presets);
  }

  void _editPreset(StudioPreset preset, {bool isNew = false}) {
    final nameCtrl = TextEditingController(text: preset.name);
    final hostCtrl = TextEditingController(text: preset.host);
    final sendCtrl = TextEditingController(text: preset.sendPort.toString());
    final recvCtrl = TextEditingController(text: preset.recvPort.toString());
    int bitrateKbps   = preset.bitrateKbps;
    int returnBrKbps  = preset.returnBrKbps;
    int jitterFrames  = preset.jitterFrames;
    int channels      = preset.channels;

    showDialog(
      context: context,
      builder: (_) => StatefulBuilder(
        builder: (ctx, setDialogState) => AlertDialog(
          backgroundColor: const Color(0xFF2D2D2D),
          title: Text(
            isNew ? 'New Preset' : 'Edit Preset',
            style: const TextStyle(color: Color(0xFFE0E0E0)),
          ),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                _field('Name',      nameCtrl),
                _field('Studio IP', hostCtrl, hint: '192.168.1.100'),
                Row(
                  children: [
                    Expanded(child: _field('Send Port', sendCtrl,
                      keyboardType: TextInputType.number)),
                    const SizedBox(width: 8),
                    Expanded(child: _field('Recv Port', recvCtrl,
                      keyboardType: TextInputType.number)),
                  ],
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text('TX kbps',
                            style: TextStyle(color: Color(0xFFAAAAAA), fontSize: 12)),
                          DropdownButton<int>(
                            value: bitrateKbps,
                            isExpanded: true,
                            dropdownColor: const Color(0xFF2D2D2D),
                            style: const TextStyle(color: Color(0xFFE0E0E0)),
                            items: [32, 64, 96, 128].map((o) => DropdownMenuItem(
                              value: o, child: Text('$o'))).toList(),
                            onChanged: (v) => setDialogState(() => bitrateKbps = v!),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text('RX kbps',
                            style: TextStyle(color: Color(0xFFAAAAAA), fontSize: 12)),
                          DropdownButton<int>(
                            value: returnBrKbps,
                            isExpanded: true,
                            dropdownColor: const Color(0xFF2D2D2D),
                            style: const TextStyle(color: Color(0xFFE0E0E0)),
                            items: [32, 64, 96, 128].map((o) => DropdownMenuItem(
                              value: o, child: Text('$o'))).toList(),
                            onChanged: (v) => setDialogState(() => returnBrKbps = v!),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
                _dropdownRow(
                  label: 'Jitter Buffer',
                  value: jitterFrames,
                  options: const [0, 2, 4, 6, 8, 10, 13, 15, 20, 25],
                  labelMap: const {
                    0: 'off', 2: '40 ms', 4: '80 ms', 6: '120 ms',
                    8: '150 ms', 10: '200 ms', 13: '250 ms',
                    15: '300 ms', 20: '400 ms', 25: '500 ms',
                  },
                  onChanged: (v) => setDialogState(() => jitterFrames = v!),
                ),
                _dropdownRow(
                  label: 'TX Kanäle',
                  value: channels,
                  options: const [1, 2],
                  labelMap: const {1: 'Mono', 2: 'Stereo'},
                  onChanged: (v) => setDialogState(() => channels = v!),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: const Text('Cancel'),
            ),
            ElevatedButton(
              onPressed: () {
                preset.name          = nameCtrl.text.trim();
                preset.host          = hostCtrl.text.trim();
                preset.sendPort      = int.tryParse(sendCtrl.text) ?? 5004;
                preset.recvPort      = int.tryParse(recvCtrl.text) ?? 5006;
                preset.bitrateKbps   = bitrateKbps;
                preset.returnBrKbps  = returnBrKbps;
                preset.jitterFrames  = jitterFrames;
                preset.channels      = channels;
                if (isNew) _presets.add(preset);
                _savePresets();
                setState(() {});
                Navigator.pop(ctx);
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _field(
    String label,
    TextEditingController ctrl, {
    String? hint,
    TextInputType keyboardType = TextInputType.text,
  }) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: TextField(
        controller: ctrl,
        keyboardType: keyboardType,
        style: const TextStyle(color: Color(0xFFE0E0E0)),
        decoration: InputDecoration(
          labelText: label,
          hintText: hint,
          labelStyle: const TextStyle(color: Color(0xFFAAAAAA)),
          hintStyle: const TextStyle(color: Color(0xFF666666)),
          filled: true,
          fillColor: const Color(0xFF1E1E1E),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(4),
            borderSide: const BorderSide(color: Color(0xFF555555)),
          ),
        ),
      ),
    );
  }

  Widget _dropdownRow<T>({
    required String label,
    required T value,
    required List<T> options,
    Map<T, String>? labelMap,
    required void Function(T?) onChanged,
  }) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        children: [
          SizedBox(
            width: 100,
            child: Text(label,
              style: const TextStyle(color: Color(0xFFAAAAAA), fontSize: 13)),
          ),
          Expanded(
            child: DropdownButton<T>(
              value: value,
              isExpanded: true,
              dropdownColor: const Color(0xFF2D2D2D),
              style: const TextStyle(color: Color(0xFFE0E0E0)),
              items: options.map((o) => DropdownMenuItem(
                value: o,
                child: Text(labelMap?[o] ?? o.toString()),
              )).toList(),
              onChanged: onChanged,
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: 'New preset',
            onPressed: () => _editPreset(
              StudioPreset(name: 'New Studio', host: ''),
              isNew: true,
            ),
          ),
        ],
      ),
      body: _presets.isEmpty
          ? Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const Icon(Icons.radio, size: 64, color: Color(0xFF555555)),
                  const SizedBox(height: 16),
                  const Text(
                    'No presets yet',
                    style: TextStyle(color: Color(0xFF888888), fontSize: 16),
                  ),
                  const SizedBox(height: 8),
                  ElevatedButton.icon(
                    onPressed: () => _editPreset(
                      StudioPreset(name: 'Studio 1', host: ''),
                      isNew: true,
                    ),
                    icon: const Icon(Icons.add),
                    label: const Text('Add preset'),
                  ),
                ],
              ),
            )
          : ListView.builder(
              padding: const EdgeInsets.all(12),
              itemCount: _presets.length + 1,
              itemBuilder: (_, i) {
                if (i == _presets.length) {
                  return Padding(
                    padding: const EdgeInsets.only(top: 24, bottom: 8),
                    child: Text(
                      'OpusAudioLink Reporter  $_version',
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: Color(0xFF555555), fontSize: 11),
                    ),
                  );
                }
                final p = _presets[i];
                final isActive = widget.activePreset?.name == p.name &&
                                 widget.activePreset?.host == p.host;
                return Card(
                  color: isActive
                      ? const Color(0xFF1A3A5C)
                      : const Color(0xFF2D2D2D),
                  margin: const EdgeInsets.only(bottom: 8),
                  child: ListTile(
                    leading: Icon(
                      Icons.cell_tower,
                      color: isActive
                          ? const Color(0xFF4A7ACC)
                          : const Color(0xFF888888),
                    ),
                    title: Text(p.name,
                      style: const TextStyle(
                        color: Color(0xFFE0E0E0),
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    subtitle: Text(
                      '${p.host}  •  TX ${p.bitrateKbps} kbps  •  Ports ${p.sendPort}/${p.recvPort}',
                      style: const TextStyle(color: Color(0xFF888888), fontSize: 11),
                    ),
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        IconButton(
                          icon: const Icon(Icons.edit, size: 18, color: Color(0xFF888888)),
                          onPressed: () => _editPreset(p),
                        ),
                        IconButton(
                          icon: const Icon(Icons.delete, size: 18, color: Color(0xFF884444)),
                          onPressed: () {
                            setState(() => _presets.removeAt(i));
                            _savePresets();
                          },
                        ),
                      ],
                    ),
                    onTap: () {
                      widget.onPresetSelected(p);
                      Navigator.pop(context);
                    },
                  ),
                );
              },
            ),
    );
  }
}
