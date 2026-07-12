# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


block_cipher = None

hiddenimports = []
hiddenimports += collect_submodules("accessible_output2")
hiddenimports += collect_submodules("edge_tts")
hiddenimports += collect_submodules("keyring")
hiddenimports += collect_submodules("paho")
hiddenimports += collect_submodules("websockets")
hiddenimports += [
    "google.genai",
    "google.genai.types",
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "wx.html2",
]

datas = [
    ("templates", "templates"),
    ("chimes", "chimes"),
    ("help", "help"),
    ("watch_ha_health.ps1", "."),
    ("watch_home_assistant_vm.ps1", "."),
    ("start_home_assistant_vm_safe.ps1", "."),
    ("viper_heat_pump_alexa.yaml", "."),
    ("run_ha_watchdog_hidden.vbs", "."),
    ("backup_home_assistant_to_d.ps1", "."),
    ("run_ha_backup_hidden.vbs", "."),
    ("harden_ha_virtualbox.ps1", "."),
    ("harden_home_assistant_virtualbox_host.ps1", "."),
]


a = Analysis(
    ["main.pyw"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "pytest",
        "tkinter",
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
    name="ViperVision",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    upx_exclude=[],
    name="ViperVision",
)
