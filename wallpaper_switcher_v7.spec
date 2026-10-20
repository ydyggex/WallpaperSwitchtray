# -*- -*- mode: python ; coding: utf-8 -*-
# V7 build (WallpaperSwitcherTray v7.1). Slimming strategy (verified by smoke-testing each step):
#   1. The source no longer imports `random`/`hashlib` (it uses os.urandom), so
#      PyInstaller's dependency graph does not pull libcrypto / _hashlib at all
#      -- no excludes needed for that win (~5.6MB).
#   2. Bundled Tcl/Tk locale data (.msg, tzdata, exotic encodings) is filtered
#      below (~3.5MB).
# Aggressive module excludes are NOT used: stdlib modules import each other in
# surprising ways (pathlib -> urllib.parse -> ipaddress), and breakage only
# shows up when the frozen exe runs.

# Conservative excludes: only the standalone crypto/network/mail subsystems.
# Dropping random+hashlib+_hashlib severs the libcrypto-3-x64.dll dependency
# (~5.6MB). Anything that could plausibly be touched by the PyInstaller
# bootstrap or lazy stdlib imports (inspect, tempfile, zipfile, pprint...) is
# deliberately kept; tempfile's random usage is a lazy function-level import
# that this app never executes.
a = Analysis(
    ['wallpaper_switcher_v7.py'],
    pathex=[],
    binaries=[],
    datas=[('icon_day.ico', '.'), ('icon_night.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'random', 'hashlib', '_hashlib', 'secrets', 'statistics',
        'ssl', 'http', 'email', 'xml', 'uuid',
        'urllib.request', 'urllib.response', 'urllib.error', 'urllib.robotparser',
        'asyncio', 'multiprocessing', 'concurrent',
        'tkinter.dnd', 'tkinter.tix', 'turtle', 'ctypes.macholib',
    ],
    noarchive=False,
)

# Trim locale/timezone/encoding data from bundled Tcl/Tk; the app only needs
# the default system encoding (kept) and never queries Tcl's timezone DB.
KEEP_ENC = {'utf8.enc','cp936.enc','gb2312.enc','gbk.enc','big5.enc','cp1252.enc',
            'ascii.enc','latin1.enc','iso8859-1.enc','shiftjis.enc','euc-jp.enc',
            'euc-kr.enc','koi8-r.enc'}
def _keep(d):
    name = d[0].replace('\\', '/')
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
    name='WallpaperSwitcherTrayV7',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon='icon_day.ico',
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='WallpaperSwitcherTrayV7',
)
