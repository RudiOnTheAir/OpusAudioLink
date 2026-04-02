# rthook_qt_plugin_path.py
# Setzt den Qt-Plugin-Pfad zur Laufzeit, damit xcb gefunden wird.
import os
import sys

# _MEIPASS ist das temporäre Entpackverzeichnis von PyInstaller
if hasattr(sys, '_MEIPASS'):
    plugin_path = os.path.join(sys._MEIPASS, 'PyQt6', 'Qt6', 'plugins')
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path
    # Fallback: auch qt.conf-Mechanismus unterstützen
    os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
