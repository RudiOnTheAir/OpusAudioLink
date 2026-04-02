#!/usr/bin/env bash
# build_linux.sh
# Baut OpusAudioLink als Linux-Binary in einem Ubuntu 22.04 Docker-Container.
# Ergebnis: dist/OpusAudioLink
#
# Voraussetzungen:
#   - Docker läuft
#   - Dieses Script liegt im gleichen Verzeichnis wie audio_gui.py
#
# Verwendung:
#   chmod +x build_linux.sh
#   ./build_linux.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="ubuntu:22.04"
BINARY_NAME="OpusAudioLink"
CURRENT_USER="$(id -u):$(id -g)"

echo "================================================"
echo "  OpusAudioLink – Linux Build"
echo "  Basis-Image: $IMAGE"
echo "  Quellverzeichnis: $SCRIPT_DIR"
echo "================================================"

docker run --rm \
  -v "$SCRIPT_DIR":/build \
  -w /build \
  "$IMAGE" \
  bash -c "
    set -e
    echo '── Pakete installieren ──'
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      python3 python3-pip \
      libportaudio2 libopus0 \
      libxcb-cursor0 \
      libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
      libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
      libxcb-shape0 libxcb-xfixes0 libxkbcommon-x11-0 \
      libegl1 libgl1 \
      upx-ucl \
      > /dev/null

    echo '── Python-Pakete installieren ──'
    pip install -q --upgrade pip
    pip install -q pyinstaller sounddevice 'numpy<2.0' PyQt6 opuslib

    echo '── Binary bauen ──'
    pyinstaller audio_gui_linux.spec --noconfirm

    echo '── Fertig ──'
    ls -lh dist/$BINARY_NAME
    echo ''
    echo '✓ Binary: dist/$BINARY_NAME'
  "

echo "── Dateiberechtigungen korrigieren ──"
sudo chown -R "$CURRENT_USER" "$SCRIPT_DIR/dist" "$SCRIPT_DIR/build" 2>/dev/null || true

echo ""
echo "================================================"
echo "  Build abgeschlossen: dist/$BINARY_NAME"
echo "================================================"
