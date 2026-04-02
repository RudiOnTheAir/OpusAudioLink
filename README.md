# OpusAudioLink

Bidirectional broadcast audio over UDP/Opus – studio software (Linux/Windows/Python) and reporter app (Android).

Designed for use at sports events: reporter on location, studio in the background. Connection via WireGuard VPN.

---

## Overview

```
Reporter (Android)  ←──UDP/Opus──→  Studio (Linux/Windows/Python)
```

- **Codec:** Opus, 32–128 kbps, mono or stereo
- **Transport:** UDP, bidirectional
- **Latency:** ~150ms jitter buffer (configurable)
- **VPN:** WireGuard recommended
- **QoS:** DSCP EF (0xB8) set automatically

---

## Studio Software (audio_gui.py)

### Requirements (running directly from Python)

```bash
sudo apt install python3 python3-pip portaudio19-dev libopus0
pip3 install pyqt6 numpy sounddevice opuslib
```

### Start

```bash
python3 audio_gui.py
```

### Usage

1. Select **Studio** or **Reporter** at the top
2. Select audio devices (input/output)
3. Set ports (default: receive 5004, return 5006)
4. Press **▶ Start**
5. Wait for connection from reporter

**Buttons:**
- `▶ Start` / `■ Stop` – start/stop connection
- `🎙 Test Input` – test microphone for 10 seconds
- `BAN!` – disconnect reporter and block for 10 minutes
- `⚠ ⚠ ⚠` – send attention signal (30 seconds)

---

## Studio Software – Build as Binary

All build scripts require Docker.

### Linux binary

```bash
chmod +x build_linux.sh
./build_linux.sh
```

Result: `dist/OpusAudioLink`

### Windows .exe

```bash
chmod +x build_windows.sh
./build_windows.sh
```

Result: `dist/OpusAudioLink.exe`

**Note:** The following files must be in the same directory:
- `audio_gui.py`
- `opus_backend.py`
- `audio_gui_linux.spec` / `audio_gui_windows.spec`
- `rthook_qt_plugin_path.py` (Linux only)

---

## Reporter App (Android)

### Installation

1. Download the APK from the [Releases page](../../releases)
2. On your Android device: **Settings → Apps → Unknown sources** – allow installation
3. Install the APK

### Usage

1. Open ⚙ Settings → create a preset
   - Studio IP (WireGuard address)
   - Ports (default: send 5004, receive 5006)
   - TX/RX bitrate
   - TX channels (mono/stereo)
2. Tap preset → return to main screen
3. Press **Connect**
4. LED indicator in preset card:
   - ⚫ Studio not reachable
   - 🟢 Studio reachable
   - 🔵 Connected, audio running
5. Tap RX VU meter → toggle audio (🔊 / 🔇)
6. Tap attention triangle in the center → send attention signal

---

## Build Android App

### Requirements

- Linux (tested on NixOS and Ubuntu)
- Docker

### Create keystore (once)

```bash
# Ubuntu/Debian
sudo apt install default-jdk-headless

# NixOS
nix-shell -p jdk
```

```bash
keytool -genkeypair \
  -keystore oal_release.keystore \
  -alias oal -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass oal_build_2024 -keypass oal_build_2024 \
  -dname "CN=OpusAudioLink, OU=Dev, O=YourName, L=City, S=State, C=DE" \
  -noprompt
```

Place the keystore in the project directory (next to `build_android.sh`).

### Prepare assets

```bash
mkdir -p opusaudiolink_reporter/assets
cp achtung.png opusaudiolink_reporter/assets/achtung.png
```

### Build

```bash
chmod +x build_android.sh
./build_android.sh
```

The finished APK is located in `dist/` and can be uploaded as a GitHub Release.

---

## Network / Ports

| Port | Protocol | Direction | Description |
|------|----------|-----------|-------------|
| 5004 | UDP | Reporter → Studio | Audio TX |
| 5006 | UDP | Studio → Reporter | Audio RX |

Ports are configurable in the preset settings.

**WireGuard:** Prioritize UDP port 51820 on your router for best quality.

---

## Packet Format

```
[seq:2][timestamp:4][reply-port:2][return-bitrate-kbps:2][flags:2][Opus data]
```

**Flags:**
- Bit 0: Robust mode (reserved)
- Bit 1-2: Channel count (1=mono, 2=stereo)
- Bit 3: Attention signal

---

## License

MIT License – free to use, including commercially.

---

## Versions

| Component | Version |
|-----------|---------|
| Studio (audio_gui.py) | 0.7 |
| Reporter App | 0.7 |
| Opus | 1.5.2 |
| Flutter | 3.27.4 |
