# audio_gui_linux.spec
# PyInstaller Spec-Datei für Linux (Ubuntu 22.04+, Debian 11+/Bookworm+)
#
# Verwendung:
#   Direkt:  pyinstaller audio_gui_linux.spec
#   Docker:  ./build_linux.sh
#
# Voraussetzungen auf dem Build-System:
#   apt install libopus0 libportaudio2 libxcb-cursor0 libxcb1 libxcb-icccm4
#               libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
#               libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0
#
# Das fertige Binary liegt danach in dist/OpusAudioLink

from PyInstaller.building.build_main import Analysis, PYZ, EXE
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files
import sys
import os
import glob

# Pfad zu den PyQt6 Qt-Plattform-Plugins ermitteln
import PyQt6
qt_plugin_path = os.path.join(os.path.dirname(PyQt6.__file__), 'Qt6', 'plugins')

def find_lib(name):
    """Systemlibrary suchen – gibt (pfad, '.') zurück oder None wenn nicht gefunden."""
    search_paths = [
        f'/usr/lib/x86_64-linux-gnu/{name}',
        f'/usr/lib/{name}',
        f'/lib/x86_64-linux-gnu/{name}',
    ]
    for p in search_paths:
        matches = glob.glob(p)
        if matches:
            return (matches[0], '.')
    return None

# Systemlibraries die auf Zielsystemen oft fehlen
system_libs = [
    'libopus.so.0',
    'libportaudio.so.2',
    # xcb-cursor: Pflicht ab Qt 6.5 – ohne diese Library startet xcb-Plugin nicht
    'libxcb-cursor.so.0',
    # Weitere xcb-Libraries die auf älteren/minimalen Systemen fehlen können
    'libxcb-icccm.so.4',
    'libxcb-image.so.0',
    'libxcb-keysyms.so.1',
    'libxcb-randr.so.0',
    'libxcb-render-util.so.0',
    'libxcb-shape.so.0',
    'libxcb-xinerama.so.0',
    'libxcb-xkb.so.1',
    'libxkbcommon-x11.so.0',
    'libxkbcommon.so.0',
]

extra_binaries = []
for lib in system_libs:
    result = find_lib(lib)
    if result:
        extra_binaries.append(result)
    else:
        print(f'WARNUNG: {lib} nicht gefunden – wird nicht gebündelt')

a = Analysis(
    ['audio_gui.py'],
    pathex=['.'],
    binaries=[
        *extra_binaries,
        # PyQt6-Bibliotheken vollständig mitbündeln (inkl. xcb-Plugin)
        *collect_dynamic_libs('PyQt6'),
    ],
    datas=[
        ('opus_backend.py',  '.'),
        # Qt6-Plattform-Plugins (xcb, wayland, ...) explizit einbetten
        (os.path.join(qt_plugin_path, 'platforms'), 'PyQt6/Qt6/plugins/platforms'),
        # xcb-Hilfs-Plugins (Pflicht auf Debian/Ubuntu für xcb)
        (os.path.join(qt_plugin_path, 'xcbglintegrations'), 'PyQt6/Qt6/plugins/xcbglintegrations'),
    ],
    hiddenimports=[
        'sounddevice',
        'numpy',
        'opuslib',
        'opuslib.api',
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.sip',
        'collections',
        'struct',
        'socket',
        'threading',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthook_qt_plugin_path.py'],
    excludes=['pyogg'],   # pyogg nicht einbetten – opuslib wird genutzt
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='OpusAudioLink',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[
        # Qt-Plugins nicht mit UPX komprimieren – führt oft zu Laufzeitfehlern
        'libQt6*.so*',
        'libqxcb*.so*',
        # xcb-Systemlibraries ebenfalls ausnehmen
        'libxcb*.so*',
        'libxkb*.so*',
    ],
    runtime_tmpdir=None,
    console=False,      # Kein Terminalfenster
    target_arch=None,
    icon=None,          # Icon hier eintragen: icon='icon.png'
)
