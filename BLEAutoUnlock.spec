# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台窗口的 GUI exe。

用法：pyinstaller --noconfirm --clean BLEAutoUnlock.spec
输出：dist/BLEAutoUnlock.exe
"""

from PyInstaller.utils.hooks import collect_submodules

# bleak 在 Windows 上通过 winrt 动态加载后端，静态分析抓不到，
# 需要显式收集相关模块；同时补上 pywin32 的常用模块。
hiddenimports = []
for _pkg in (
    "bleak.backends.winrt",
    "winrt.system",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.radios",
    "winrt.windows.foundation",
    "win32crypt",
    "win32timezone",
):
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        hiddenimports.append(_pkg)

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BLEAutoUnlock",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
