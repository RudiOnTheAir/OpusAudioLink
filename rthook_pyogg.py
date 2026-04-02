# rthook_pyogg.py
# Runtime-Hook: Stellt sicher dass PyOgg die libopus DLL aus dem
# PyInstaller-Temp-Verzeichnis (_MEIPASS) laden kann.
import os
import sys

if hasattr(sys, '_MEIPASS'):
    # PyOgg sucht libopus über ctypes.util.find_library –
    # wir setzen PATH sodass die mitgelieferte DLL gefunden wird.
    meipass = sys._MEIPASS
    os.environ['PATH'] = meipass + os.pathsep + os.environ.get('PATH', '')

    # PyOgg erlaubt auch direktes Setzen des Pfades
    try:
        import pyogg
        if hasattr(pyogg, 'PYOGG_OPUS_LIBRARY'):
            opus_dll = os.path.join(meipass, 'libopus-0.dll')
            if not os.path.exists(opus_dll):
                opus_dll = os.path.join(meipass, 'opus.dll')
            if os.path.exists(opus_dll):
                pyogg.PYOGG_OPUS_LIBRARY = opus_dll
    except Exception:
        pass
