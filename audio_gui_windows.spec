# audio_gui_windows.spec

import sys
import os
from PyInstaller.building.build_main import Analysis, PYZ, EXE

block_cipher = None

def find_libopus():
    try:
        import pyogg
        pyogg_dir = os.path.dirname(pyogg.__file__)
        for name in ('libopus-0.dll', 'opus.dll', 'libopus.dll'):
            path = os.path.join(pyogg_dir, name)
            if os.path.exists(path):
                print(f"  libopus gefunden: {path}")
                return [(path, '.')]
    except Exception as e:
        print(f"  PyOgg nicht gefunden: {e}")
    return []

def find_qt_plugins():
    result = []
    try:
        import PyQt6
        qt_dir = os.path.dirname(PyQt6.__file__)
        plugins_dir = os.path.join(qt_dir, 'Qt6', 'plugins')
        platforms_dir = os.path.join(plugins_dir, 'platforms')
        if os.path.exists(platforms_dir):
            result.append((platforms_dir, 'PyQt6/Qt6/plugins/platforms'))
        for plugin in ('styles', 'imageformats'):
            p = os.path.join(plugins_dir, plugin)
            if os.path.exists(p):
                result.append((p, f'PyQt6/Qt6/plugins/{plugin}'))
    except Exception as e:
        print(f"  Qt plugins nicht gefunden: {e}")
    return result

a = Analysis(
    ['audio_gui.py'],
    pathex=['.'],
    binaries=find_libopus(),
    datas=find_qt_plugins() + [
        ('opus_backend.py',  '.'),
    ],
    hiddenimports=[
        'sounddevice',
        'numpy',
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.sip',
        'collections',
        'struct',
        'socket',
        'threading',
        'ctypes',
        'ctypes.util',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['opuslib', 'pyogg'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='OpusAudioLink',
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    target_arch=None,
    icon=None,
)
