# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — GPS Uygulama
# Kullanim: build.bat  (veya: .venv\Scripts\pyinstaller GPS_App.spec --clean --noconfirm)

import sys
from pathlib import Path

block_cipher = None

VENV = Path('.venv/Lib/site-packages')

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # Folium HTML şablonları (kritik — olmadan harita oluşturulamaz)
        (str(VENV / 'folium'),         'folium'),
        (str(VENV / 'branca'),         'branca'),
        (str(VENV / 'xyzservices'),    'xyzservices'),
        # SSL sertifikaları (internet bağlantısı için)
        (str(VENV / 'certifi'),        'certifi'),
        # Jinja2 şablonları
        (str(VENV / 'jinja2'),         'jinja2'),
        (str(VENV / 'markupsafe'),     'markupsafe'),
    ],
    hiddenimports=[
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineQuick',
        'PyQt6.sip',
        'folium',
        'folium.raster_layers',
        'folium.plugins',
        'branca',
        'branca.colormap',
        'branca.element',
        'jinja2',
        'jinja2.ext',
        'markupsafe',
        'requests',
        'certifi',
        'charset_normalizer',
        'idna',
        'urllib3',
        'numpy',
        'xyzservices',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'tkinterweb',
        'matplotlib',
        'PIL',
        'scipy',
        'pandas',
        'IPython',
        'notebook',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GPS_App',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # Konsol penceresi yok
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        'Qt6WebEngine*.dll',
        'Qt6*.dll',
    ],
    name='GPS_App',
)
