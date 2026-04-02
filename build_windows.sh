#!/usr/bin/env bash
# build_windows.sh
# Baut OpusAudioTool als Windows .exe in einem Docker-Container mit Wine.
# Ergebnis: dist/OpusAudioTool.exe
#
# Verwendung:
#   chmod +x build_windows.sh
#   ./build_windows.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="tobix/pywine:3.11"
EXE_NAME="OpusAudioTool.exe"
CURRENT_USER="$(id -u):$(id -g)"

echo "================================================"
echo "  OpusAudioTool – Windows Build (Wine/Docker)"
echo "  Basis-Image : $IMAGE"
echo "  Quellverz.  : $SCRIPT_DIR"
echo "================================================"
echo ""

if ! docker info > /dev/null 2>&1; then
    echo "FEHLER: Docker ist nicht erreichbar."
    echo "  NixOS:  sudo systemctl start docker"
    exit 1
fi

docker run --rm \
  -v "$SCRIPT_DIR":/build \
  -w /build \
  "$IMAGE" \
  bash -c "
    set -e
    WINEPYTHON='wine /opt/wineprefix/drive_c/Python/python.exe'

    echo '── Python-Pakete für Windows installieren ──'
    \$WINEPYTHON -m pip install -q --upgrade pip
    \$WINEPYTHON -m pip install -q 'numpy<2.0'
    \$WINEPYTHON -m pip install -q pyinstaller sounddevice PyQt6 pyogg

    echo '── .exe bauen ──'
    \$WINEPYTHON -m PyInstaller audio_gui_windows.spec --noconfirm

    echo ''
    echo '── Ergebnis ──'
    ls -lh dist/ || true
    echo '✓  Build abgeschlossen'
  "

# Eigentümer von dist/ und build/ auf den aktuellen User setzen
echo "── Dateiberechtigungen korrigieren ──"
sudo chown -R "$CURRENT_USER" "$SCRIPT_DIR/dist" "$SCRIPT_DIR/build" 2>/dev/null || true

echo ""
echo "================================================"
if [ -f "$SCRIPT_DIR/dist/$EXE_NAME" ]; then
    SIZE=$(du -sh "$SCRIPT_DIR/dist/$EXE_NAME" | cut -f1)
    echo "  ✓  dist/$EXE_NAME  ($SIZE)"
else
    echo "  ⚠  Prüfe dist/ auf die .exe"
fi
echo "================================================"
