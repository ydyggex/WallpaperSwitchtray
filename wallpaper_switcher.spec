# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build for WallpaperSwitcher.
#   pyinstaller wallpaper_switcher.spec --noconfirm
#
# The app imports only the standard library, so the dependency graph is small.
# The excludes below cut the crypto/network/mail subsystems that nothing in the
# app touches -- notably `random` -> `hashlib` -> libcrypto-*.dll (~5.6MB).
# Do NOT exclude inspect/tempfile/zipfile/urllib.parse: stdlib modules import
# each other in surprising ways and breakage only shows up when the frozen exe
# runs. Anything removed here must be re-verified with a smoke test.

a = Analysis(
    ['wallpaper_switcher.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/icon_day.ico', '.'), ('assets/icon_night.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'random', 'hashlib', '_hashlib', 'secrets', 'statistics',
        'socket', 'ssl', 'http', 'email', 'xml', 'uuid',
        'urllib.request', 'urllib.response', 'urllib.error', 'urllib.robotparser',
        'asyncio', 'multiprocessing', 'concurrent',
        'tkinter.dnd', 'tkinter.tix', 'turtle', 'ctypes.macholib',
    ],
    noarchive=False,
)

# Trim locale/timezone/encoding data from bundled Tcl/Tk. The app needs only the
# default system encoding and never queries Tcl's timezone database.
KEEP_ENC = {'utf8.enc', 'cp936.enc', 'gb2312.enc', 'gbk.enc', 'big5.enc',
            'cp1252.enc', 'ascii.enc', 'latin1.enc', 'iso8859-1.enc',
            'shiftjis.enc', 'euc-jp.enc', 'euc-kr.enc', 'koi8-r.enc'}

def _keep(entry):
    name = entry[0].replace('\\', '/')
    if not (('_tcl_data' in name) or ('_tk_data' in name)):
        return True
    base = name.rsplit('/', 1)[-1]
    if base.endswith('.msg'):
        return False
    if '/encoding/' in name and base not in KEEP_ENC:
        return False
    if '/tzdata/' in name:
        return False
    return True

a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='WallpaperSwitcherTray',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon='assets/icon_day.ico',
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='WallpaperSwitcherTray',
)
