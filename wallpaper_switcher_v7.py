"""随机壁纸切换器 (WallpaperSwitcherTray) — 单文件 / 零第三方依赖。

基于 V7 重构：日志可观测、WebP 兼容、切换即时生效与自愈、多时段多文件夹、
递归扫描、DPI 字体统一、主题实时跟随、托盘跟随 UI 主题、Notebook 卡片式 UI
与可自定义的软件外观。
"""

import ctypes
import base64
import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import winreg
import tkinter as tk
from datetime import datetime, time as dt_time
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_NAME = '随机壁纸切换器'
VERSION = '7.1'
CONFIG_DIR = Path(os.environ.get('APPDATA', Path.home())) / 'WallpaperSwitcher'
CONFIG_FILE = CONFIG_DIR / 'config.json'
LOG_FILE = CONFIG_DIR / 'app.log'
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
SPI_SETDESKWALLPAPER = 20
SPIF_UPDATEINIFILE = 1
SPIF_SENDCHANGE = 2

LRESULT = ctypes.c_ssize_t
HCURSOR = wintypes.HANDLE
HRESULT = ctypes.c_long
ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_NAME = 'Local\\WallpaperSwitcher.Random.SingleInstance'
SHOW_REQUEST_NAME = 'WallpaperSwitcher.ShowRequest'
HWND_BROADCAST = 0xFFFF
MF_CHECKED = 8

WM_DPICHANGED = 0x02E0
WM_SETTINGCHANGE = 0x001A
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
GWL_WNDPROC = -4

# 强调色预设色板（可辨识度高、在深浅底上都协调）。
PRESET_ACCENTS = ('#3A7AFE', '#8B5CF6', '#06B6D4', '#10B981', '#F59E0B', '#EF4444')

# 扫描锁最长持有时间（秒）；超过即视为卡死并强制复位（自愈）。
SCAN_DEADLINE = 60
# 距上次成功切换超过该时长（取 max(interval*2分钟, 此值)）则主动自愈。
SELF_HEAL_FLOOR = 600

# “毛玻璃（透出壁纸）”模式使用的透明键颜色：窗口中渲染为该颜色的像素会
# 被设为完全透明，从而露出桌面壁纸。
# 关键：键色必须接近主题底色（深色→近黑，浅色→近白）。因为 Tk 的文字抗锯齿
# 是按控件背景色混色的——若用刺眼的品红作键，文字会整体泛品红且难以阅读；
# 用近黑/近白作键，文字混色结果与正常渲染几乎一致。
GLASS_KEY_DARK = '#010101'
GLASS_KEY_LIGHT = '#fefefe'


def glass_key(dark: bool) -> str:
    return GLASS_KEY_DARK if dark else GLASS_KEY_LIGHT


# SetWindowCompositionAttribute（未公开 API，Win10/11 实际生效的亚克力模糊）
ACCENT_DISABLED = 0
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
ACCENT_ENABLE_BLURBEHIND = 3
WCA_ACCENT_POLICY = 19


def _set_accent(hwnd, state: int, gradient_abgr: int) -> bool:
    """通过 SetWindowCompositionAttribute 设置窗口背景合成效果。"""
    try:
        user32 = ctypes.windll.user32

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [('AccentState', ctypes.c_uint),
                        ('AccentFlags', ctypes.c_uint),
                        ('GradientColor', ctypes.c_uint),
                        ('AnimationId', ctypes.c_uint),
                        ('BlendColor', ctypes.c_uint)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [('Attribute', ctypes.c_uint),
                        ('Data', ctypes.c_void_p),
                        ('SizeOfData', ctypes.c_size_t)]

        user32.SetWindowCompositionAttribute.argtypes = [
            wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
        user32.SetWindowCompositionAttribute.restype = ctypes.c_int
        policy = ACCENT_POLICY(state, 2, gradient_abgr, 0, 0)
        data = WINCOMPATTRDATA(WCA_ACCENT_POLICY,
                               ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
                               ctypes.sizeof(policy))
        return bool(user32.SetWindowCompositionAttribute(int(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def set_window_glass(hwnd, enable: bool, dark: bool) -> bool:
    """在顶层窗口上启用/关闭“毛玻璃透出壁纸”效果；失败则静默降级。

    优先用 Acrylic（真·磨砂+着色，Win10/11 生效，着色的透明度保证文字可读），
    其次 BLURBEHIND accent，最后回退 Vista 时代的 DwmEnableBlurBehindWindow
    （Win11 会忽略它，只剩锐利透出）。
    """
    # 取顶层 HWND（tk 的 winfo_id 是子窗口）
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        root_hwnd = user32.GetAncestor(int(hwnd), 2) or int(hwnd)  # GA_ROOT=2
    except Exception:
        root_hwnd = int(hwnd)

    if not enable:
        _set_accent(root_hwnd, ACCENT_DISABLED, 0)
        dwm_set_blur(root_hwnd, False)
        return True

    # 着色：AABBGGRR。深色主题用半透明深色底保证浅色文字可读；浅色反之。
    if dark:
        tint = 0x501A1A20   # alpha 0x50, #201A1A
    else:
        tint = 0x60F2F3F5   # alpha 0x60, #F5F3F2
    if _set_accent(root_hwnd, ACCENT_ENABLE_ACRYLICBLURBEHIND, tint):
        return True
    if _set_accent(root_hwnd, ACCENT_ENABLE_BLURBEHIND, tint):
        return True
    return dwm_set_blur(root_hwnd, True)


def set_window_glass_blurbehind(hwnd, dark: bool) -> bool:
    """拖动/缩放窗口时临时改用 BLURBEHIND：无 Acrylic 的黑闪，仅作过渡。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        root_hwnd = user32.GetAncestor(int(hwnd), 2) or int(hwnd)  # GA_ROOT=2
    except Exception:
        root_hwnd = int(hwnd)
    tint = 0x501A1A20 if dark else 0x60F2F3F5
    if _set_accent(root_hwnd, ACCENT_ENABLE_BLURBEHIND, tint):
        return True
    return dwm_set_blur(root_hwnd, True)


def dwm_set_blur(hwnd, enable):
    """在窗口上启用/关闭 DWM 模糊（毛玻璃）效果；失败则静默忽略。

    配合 -transparentcolor 把背景区域设为透明键，即可让该区域
    透出被模糊处理后的桌面壁纸，而输入框/按钮等控件仍保持不透明可读。
    """
    try:
        dwmapi = ctypes.windll.dwmapi
    except Exception:
        return False

    class DWM_BLURBEHIND(ctypes.Structure):
        _fields_ = [('dwFlags', ctypes.c_uint),
                    ('fEnable', ctypes.c_int),
                    ('hRgnBlur', ctypes.c_void_p),
                    ('fTransitionOnMaximized', ctypes.c_int)]

    bb = DWM_BLURBEHIND()
    bb.dwFlags = 0x1  # DWM_BB_ENABLE
    bb.fEnable = 1 if enable else 0
    bb.hRgnBlur = 0   # NULL：作用于整个窗口
    bb.fTransitionOnMaximized = 0
    try:
        dwmapi.DwmEnableBlurBehindWindow.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(DWM_BLURBEHIND)]
        dwmapi.DwmEnableBlurBehindWindow.restype = ctypes.c_long
        return dwmapi.DwmEnableBlurBehindWindow(int(hwnd), ctypes.byref(bb)) == 0
    except Exception:
        return False


def _current_app_path() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable)
    return Path(__file__).resolve()


def resource_path(name: str):
    """Locate a bundled asset (PyInstaller _MEIPASS first, then script dir)."""
    base = getattr(sys, '_MEIPASS', None)
    if base:
        p = Path(base) / name
        if p.is_file():
            return str(p)
    p = Path(__file__).resolve().parent / name
    return str(p) if p.is_file() else None


def setup_logging() -> None:
    """Write a size-rotated log to %APPDATA%/WallpaperSwitcher/app.log."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=512 * 1024, backupCount=3, encoding='utf-8')
        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                                '%Y-%m-%d %H:%M:%S')
        handler.setFormatter(fmt)
        root = logging.getLogger('WallpaperSwitcher')
        root.setLevel(logging.INFO)
        if not root.handlers:
            root.addHandler(handler)
    except Exception:
        # 日志初始化失败绝不能影响主程序。
        pass


log = logging.getLogger('WallpaperSwitcher')


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_byte * 8)]

    @classmethod
    def from_parts(cls, d1, d2, d3, d4: bytes) -> 'GUID':
        g = cls()
        g.Data1, g.Data2, g.Data3 = d1, d2, d3
        g.Data4 = (ctypes.c_byte * 8)(*d4)
        return g


# {b92b56a9-8b55-4e14-9a89-0199bbb6f93b} / {c2cf3110-460e-4fc1-b9d0-8a1c0c9cc4bd}
IID_DESKTOP_WALLPAPER = GUID.from_parts(
    0xb92b56a9, 0x8b55, 0x4e14, bytes([0x9a, 0x89, 0x01, 0x99, 0xbb, 0xb6, 0xf9, 0x3b]))
CLSID_DESKTOP_WALLPAPER = GUID.from_parts(
    0xc2cf3110, 0x460e, 0x4fc1, bytes([0xb9, 0xd0, 0x8a, 0x1c, 0x0c, 0x9c, 0xc4, 0xbd]))

# IDesktopWallpaper vtable slots (沿用 V7 经验值；SetWallpaper 取 4)。
DW_SET_WALLPAPER = 4
DW_SET_SLIDESHOW = 12
DW_SET_SLIDESHOW_OPTIONS = 14
DW_ADVANCE_SLIDESHOW = 16
DW_GET_STATUS = 17
DW_ENABLE = 18
DW_GET_MONITOR_COUNT = 6
DW_GET_MONITOR_AT = 5
DW_SET_POSITION = 10
IUNKNOWN_RELEASE = 2
DSO_SHUFFLEIMAGES = 1
DSS_DISABLED_BY_REMOTE_SESSION = 4
DWPOS_FILL = 4


def _com(obj, index, restype, argtypes, *args):
    """Call vtable slot *index* of a COM interface pointer with `this`."""
    vt = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(ctypes.c_void_p(ctypes.cast(
        ctypes.c_void_p(vt), ctypes.POINTER(ctypes.c_void_p))[index]),
        ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes))
    return fn(obj, *args)


def _shell_item_array(paths):
    """Build an IShellItemArray from file paths (caller releases it)."""
    if not paths:
        return None
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.SHCreateShellItemArrayFromIDLists.restype = HRESULT
    shell32.SHCreateShellItemArrayFromIDLists.argtypes = [
        wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pidls = (ctypes.c_void_p * len(paths))()
    n = 0
    for p in paths:
        pidl = shell32.ILCreateFromPathW(str(p))
        if pidl:
            pidls[n] = pidl
            n += 1
    arr = ctypes.c_void_p()
    try:
        if n == 0:
            return None
        hr = shell32.SHCreateShellItemArrayFromIDLists(n, pidls, ctypes.byref(arr))
        return arr if hr == 0 else None
    finally:
        for i in range(n):
            ole32.CoTaskMemFree(pidls[i])


def acquire_single_instance():
    """Return a mutex handle, or ``None`` when another copy is running."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD
    handle = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_NAME)
    if not handle:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def notify_running_instance(background: bool = False):
    """Ask an already-running copy to bring its window forward (or stay hidden)."""
    user32 = ctypes.windll.user32
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    message = user32.RegisterWindowMessageW(SHOW_REQUEST_NAME)
    if message:
        # wParam=1 表示“请求后台运行”，已有实例据此保持隐藏。
        user32.PostMessageW(HWND_BROADCAST, message, 1 if background else 0, 0)


def enable_high_dpi() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def set_wallpaper(path: Path) -> bool:
    return bool(ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(path), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE))


def scan_images(folder: Path, recursive: bool = False, max_depth: int = 4) -> list:
    try:
        if recursive:
            files = []
            stack = [(folder, 0)]
            while stack:
                cur, depth = stack.pop()
                try:
                    for child in cur.iterdir():
                        if child.is_dir() and depth < max_depth:
                            stack.append((child, depth + 1))
                        elif child.is_file() and child.suffix.lower() in IMAGE_EXTENSIONS:
                            files.append(child)
                except (OSError, PermissionError):
                    continue
            return sorted(files, key=lambda p: p.name.lower())
        return sorted(
            (p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda p: p.name.lower(),
        )
    except (OSError, FileNotFoundError):
        return []


def startup_link_path() -> Path:
    return Path(os.environ.get('APPDATA', Path.home())) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup' / 'WallpaperSwitcher.lnk'


def _ps_quote(value: str) -> str:
    """Return a value safe for a single-quoted PowerShell string."""
    return value.replace("'", "''")


def set_startup(enabled: bool) -> tuple[bool, str]:
    """Create or remove the current-user Startup shortcut."""
    link = startup_link_path()
    try:
        if not enabled:
            try:
                link.unlink()
            except FileNotFoundError:
                pass
            return (True, '')

        link.parent.mkdir(parents=True, exist_ok=True)
        frozen = getattr(sys, 'frozen', False)
        target = Path(sys.executable).resolve() if frozen else _current_app_path()
        script_path = Path(__file__).resolve()
        arguments = '--background' if frozen else f'"{script_path}" --background'
        working_dir = target.parent if frozen else script_path.parent
        ps = (
            "$ErrorActionPreference = 'Stop'\n"
            "$shell = New-Object -ComObject WScript.Shell\n"
            f"$shortcut = $shell.CreateShortcut('{_ps_quote(str(link))}')\n"
            f"$shortcut.TargetPath = '{_ps_quote(str(target))}'\n"
            f"$shortcut.Arguments = '{_ps_quote(arguments)}'\n"
            f"$shortcut.WorkingDirectory = '{_ps_quote(str(working_dir))}'\n"
            "$shortcut.Save()\n"
        )
        encoded = base64.b64encode(ps.encode('utf-16le')).decode('ascii')
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-EncodedCommand', encoded],
            check=False,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            return (False, (result.stderr or result.stdout or 'PowerShell 未能创建启动项。').strip())
        return (True, '')
    except OSError as exc:
        return (False, str(exc))


def startup_is_enabled() -> bool:
    return startup_link_path().is_file()


class TrayIcon:
    """Dependency-free Windows notification-area icon and context menu."""

    WM_USER = 1024
    WM_TRAY = WM_USER + 1
    WM_LBUTTONDBLCLK = 515
    WM_RBUTTONUP = 517
    WM_COMMAND = 273
    WM_DESTROY = 2
    WM_CLOSE = 16
    WM_QUIT = 18
    CS_DBLCLKS = 8
    TPM_RIGHTBUTTON = 2
    MF_STRING = 0
    MF_SEPARATOR = 2048
    NIF_MESSAGE = 1
    NIF_ICON = 2
    NIF_TIP = 4
    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_SETVERSION = 4
    NIM_DELETE = 2
    ID_SHOW = 1001
    ID_SWITCH = 1002
    ID_PAUSE = 1003
    ID_FOLDER = 1004
    ID_EXIT = 1005
    ID_LOG = 1006

    def __init__(self, events: queue.Queue):
        self.events = events
        self.ready = threading.Event()
        self.error = None
        self.hwnd = None
        self.paused_hint = False
        self.thread = threading.Thread(target=self._run, name='wallpaper-tray', daemon=True)
        self.thread.start()
        self.ready.wait(0.5)

    def _add_icon(self):
        added = self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self.nid))
        if added:
            self.nid.uVersion = 4
            self.shell32.Shell_NotifyIconW(self.NIM_SETVERSION, ctypes.byref(self.nid))
            return True
        # 任务栏重建瞬间可能失败：延迟重试一次，否则此后永远无图标。
        if not getattr(self, '_add_retried', False):
            self._add_retried = True
            threading.Timer(3.0, self._add_icon).start()
        return False

    def _load_icon(self, name, size):
        """Load an ICO at the exact tray size (crisp, no shell rescaling)."""
        user32 = self.user32
        path = resource_path(name)
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x00000010, 0x00000040
        if path:
            user32.LoadImageW.restype = wintypes.HICON
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                          wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            h = user32.LoadImageW(None, path, IMAGE_ICON, size, size,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return user32.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR))

    def set_dark(self, dark: bool) -> None:
        """Swap the tray glyph to follow the UI theme (light/dark)."""
        if not getattr(self, 'icons', None) or not self.tray_added:
            return
        h = self.icons.get('night' if dark else 'day')
        if h and h != self.nid.hIcon:
            self.nid.hIcon = h
            self.nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            self.shell32.Shell_NotifyIconW(self.NIM_MODIFY, ctypes.byref(self.nid))

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32
            self.user32 = user32
            self.shell32 = shell32
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
            user32.LoadIconW.restype = wintypes.HICON
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
            user32.DestroyIcon.argtypes = [wintypes.HICON]
            user32.DefWindowProcW.restype = LRESULT
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]

            self.hinstance = kernel32.GetModuleHandleW(None)
            self.thread_id = kernel32.GetCurrentThreadId()
            user32.RegisterWindowMessageW.restype = wintypes.UINT
            self.show_request = user32.RegisterWindowMessageW(SHOW_REQUEST_NAME)
            self.wnd_proc = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)(self._wnd_proc)

            class WNDCLASSEXW(ctypes.Structure):
                _fields_ = [
                    ('cbSize', wintypes.UINT), ('style', wintypes.UINT),
                    ('lpfnWndProc', ctypes.c_void_p), ('cbClsExtra', ctypes.c_int),
                    ('cbWndExtra', ctypes.c_int), ('hInstance', wintypes.HINSTANCE),
                    ('hIcon', wintypes.HICON), ('hCursor', HCURSOR),
                    ('hbrBackground', wintypes.HBRUSH), ('lpszMenuName', wintypes.LPCWSTR),
                    ('lpszClassName', wintypes.LPCWSTR), ('hIconSm', wintypes.HICON)]
            self.class_name = 'WallpaperSwitcherTrayClass'
            wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), self.CS_DBLCLKS,
                             ctypes.cast(self.wnd_proc, ctypes.c_void_p),
                             0, 0, self.hinstance, 0, 0, 0, None, self.class_name, 0)
            user32.RegisterClassExW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(0, self.class_name, APP_NAME, 0, 0, 0, 0, 0,
                                                0, 0, self.hinstance, None)
            if not self.hwnd:
                raise ctypes.WinError()

            user32.GetSystemMetrics.restype = ctypes.c_int
            small = user32.GetSystemMetrics(49) or 16
            self.icons = {'day': self._load_icon('icon_day.ico', small),
                          'night': self._load_icon('icon_night.ico', small)}

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND),
                    ('uID', wintypes.UINT), ('uFlags', wintypes.UINT),
                    ('uCallbackMessage', wintypes.UINT), ('hIcon', wintypes.HICON),
                    ('szTip', wintypes.WCHAR * 128), ('dwState', wintypes.DWORD),
                    ('dwStateMask', wintypes.DWORD), ('szInfo', wintypes.WCHAR * 256),
                    ('uVersion', wintypes.UINT), ('szInfoTitle', wintypes.WCHAR * 64),
                    ('dwInfoFlags', wintypes.DWORD), ('guidItem', ctypes.c_byte * 16),
                    ('hBalloonIcon', wintypes.HICON)]
            self.nid = NOTIFYICONDATAW()
            self.nid.cbSize = ctypes.sizeof(self.nid)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            self.nid.uCallbackMessage = self.WM_TRAY
            self.nid.hIcon = self.icons['day']
            self.nid.szTip = APP_NAME
            self.taskbar_created = user32.RegisterWindowMessageW('TaskbarCreated')
            self.tray_added = self._add_icon()
            self.ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.nid))
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if self.show_request and msg == self.show_request:
            # wParam=1：请求后台（保持隐藏）；否则唤醒窗口。
            self.events.put(('show', int(wparam) & 1))
            return 0
        if msg == getattr(self, 'taskbar_created', 0):
            self.tray_added = self._add_icon()
        elif msg == self.WM_TRAY:
            tray_event = int(lparam) & 65535
            if tray_event == self.WM_LBUTTONDBLCLK:
                self.events.put(('show', 0))
            elif tray_event == self.WM_RBUTTONUP:
                self._menu()
        elif msg == self.WM_COMMAND:
            command = int(wparam) & 65535
            mapping = {self.ID_SHOW: ('show', 0), self.ID_SWITCH: 'switch',
                       self.ID_PAUSE: 'pause', self.ID_FOLDER: 'folder',
                       self.ID_LOG: 'log', self.ID_EXIT: 'exit'}
            if command in mapping:
                self.events.put(mapping[command])
        elif msg == self.WM_CLOSE:
            self.user32.DestroyWindow(hwnd)
            return 0
        elif msg == self.WM_DESTROY:
            self.user32.PostQuitMessage(0)
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        user32 = self.user32
        menu = user32.CreatePopupMenu()
        pause_flags = self.MF_STRING | (MF_CHECKED if getattr(self, 'paused_hint', False) else 0)
        for ident, text, flags in (
            (self.ID_SHOW, '打开窗口', self.MF_STRING),
            (self.ID_SWITCH, '立即切换', self.MF_STRING),
            (self.ID_PAUSE, '暂停 / 继续', pause_flags),
            (self.ID_FOLDER, '打开壁纸文件夹', self.MF_STRING),
            (self.ID_LOG, '打开日志', self.MF_STRING),
        ):
            user32.AppendMenuW(menu, flags, ident, text)
        user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT, '退出')
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)

    def stop(self):
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
        if self.thread.is_alive():
            self.thread.join(2.0)
        if self.thread.is_alive() and getattr(self, 'thread_id', None):
            self.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
            self.thread.join(1.0)


def _rand_below(n: int) -> int:
    return int.from_bytes(os.urandom(4), 'little') % n


def system_prefers_dark() -> bool:
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
    except OSError:
        return False


_LIGHT = {'bg': '#f5f6f8', 'fg': '#1a1a1a', 'muted': '#666666', 'field': '#ffffff',
          'button': '#e9eaee', 'button_hover': '#dfe1e6', 'border': '#d0d3d9',
          'select': '#3A7AFE', 'sep': '#e2e4e8', 'card': '#ffffff'}
_DARK = {'bg': '#1e1e20', 'fg': '#e6e6e6', 'muted': '#9a9a9a', 'field': '#2d2d2f',
         'button': '#3a3a3d', 'button_hover': '#454549', 'border': '#4a4a4e',
         'select': '#3A7AFE', 'sep': '#34343a', 'card': '#262628'}


class App:
    _THEME_LABELS = {'system': '跟随系统', 'light': '浅色', 'dark': '深色'}
    _THEME_MODES = {'跟随系统': 'system', '浅色': 'light', '深色': 'dark'}

    def __init__(self, root, start_hidden=False):
        self.root = root
        self.root.title(f'{APP_NAME}  v{VERSION}')
        self.root.geometry('720x560')
        self.root.minsize(640, 500)
        self.events = queue.Queue()
        self.running = True
        self.paused = False
        self.last = None
        self.config = self.load_config()
        self._normalize_config(self.config)
        default_folder = str(Path.home() / 'Pictures' / 'Wallpapers')

        # 时段列表（核心数据结构），保持与配置同步。
        self.schedules = self.config['schedules']
        self._last_used = self.config.get('last_used', {})
        self.interval = max(1, int(self.config.get('interval', 30)))
        self.startup_var = tk.BooleanVar(value=bool(self.config.get('startup', False)))
        self.slideshow_var = tk.BooleanVar(value=bool(self.config.get('slideshow', False)))
        self.recursive_var = tk.BooleanVar(value=bool(self.config.get('recursive', False)))
        _mode = self.config.get('theme', 'system')
        self.theme_var = tk.StringVar(value=self._THEME_LABELS.get(_mode, '跟随系统'))
        self.accent = self.config.get('accent', '#3A7AFE')
        self.app_bg = self.config.get('app_bg', {'mode': 'default', 'value': ''})
        self.interval_var = tk.StringVar(value=str(self.interval))
        self.status_var = tk.StringVar(value='准备就绪')
        self.count_var = tk.StringVar(value='')

        self.next_switch = 0.0
        self.last_period = None
        self._scan_busy = False
        self._scan_seq = 0
        self._scan_started = 0.0
        self._image_count = 0
        self._bag = []
        self._bag_key = None
        self.last_ok_switch = 0.0
        self._dw = None
        self._dw_failed = False
        self._slideshow_active = False
        self._slideshow_folder = None

        # 时段编辑器状态
        self.sel_idx = None
        self._loading_form = False
        self.s_name_var = tk.StringVar()
        self.s_start_var = tk.StringVar()
        self.s_end_var = tk.StringVar()
        self.s_folder_var = tk.StringVar()
        self.s_enabled_var = tk.BooleanVar(value=True)

        self._build_background()
        self.build_ui()
        self._apply_theme()
        self._apply_app_background()
        self.tray = TrayIcon(self.events)
        self._install_window_hooks()
        self.root.after(250, self.poll_events)
        self.root.after(300, self.tick)
        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)
        self._repair_startup_link()
        if start_hidden:
            self.root.withdraw()
        log.info('应用启动完成 (version=%s)', VERSION)

    # ---------------------------------------------------------------- config

    def _normalize_config(self, cfg):
        """向后兼容：旧版 day/night 字段迁移为 schedules 列表。"""
        if not isinstance(cfg.get('schedules'), list) or not cfg['schedules']:
            day = cfg.get('day_folder') or cfg.get('folder') or str(Path.home() / 'Pictures' / 'Wallpapers')
            night = cfg.get('night_folder') or cfg.get('folder') or str(Path.home() / 'Pictures' / 'Wallpapers')
            cfg['schedules'] = [
                {'name': '白天', 'start': cfg.get('day_start', '07:00'),
                 'end': cfg.get('day_end', '19:00'), 'folder': day, 'enabled': True},
                {'name': '夜间', 'start': cfg.get('night_start', '19:00'),
                 'end': cfg.get('night_end', '07:00'), 'folder': night, 'enabled': True},
            ]
        for s in cfg['schedules']:
            s.setdefault('enabled', True)
            s.setdefault('name', '时段')
            for k in ('start', 'end', 'folder'):
                s.setdefault(k, '' if k == 'folder' else '00:00')
        cfg.setdefault('interval', 30)
        cfg.setdefault('theme', 'system')
        cfg.setdefault('recursive', False)
        cfg.setdefault('slideshow', False)
        cfg.setdefault('startup', False)
        cfg.setdefault('accent', '#3A7AFE')
        ab = cfg.setdefault('app_bg', {'mode': 'default', 'value': ''})
        if not isinstance(ab, dict) or ab.get('mode') not in ('default', 'glass'):
            cfg['app_bg'] = {'mode': 'default', 'value': ''}
        cfg.setdefault('version', VERSION)
        cfg.setdefault('app_path', str(_current_app_path()))
        return cfg

    def load_config(self):
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                'version': VERSION,
                'theme': self._THEME_MODES.get(self.theme_var.get(), 'system'),
                'accent': self.accent,
                'app_bg': self.app_bg,
                'interval': self.interval,
                'recursive': self.recursive_var.get(),
                'slideshow': self.slideshow_var.get(),
                'startup': self.startup_var.get(),
                'schedules': self.schedules,
                'app_path': str(_current_app_path()),
                'last_used': self._last_used,
            },
            ensure_ascii=False, indent=2,
        )
        tmp = CONFIG_DIR / 'config.json.tmp'
        tmp.write_text(payload, encoding='utf-8')
        os.replace(tmp, CONFIG_FILE)

    def _persist_last_used(self):
        """把“上次使用图片”落盘，避免重启后立即重复（缺陷14）。"""
        try:
            self.save_config()
        except Exception:
            log.exception('持久化 last_used 失败')

    def _repair_startup_link(self):
        if not self.startup_var.get():
            return
        link = startup_link_path()
        saved_path = str(self.config.get('app_path', ''))
        current_path = str(_current_app_path())
        if not link.is_file() or (saved_path and saved_path != current_path):
            set_startup(True)
            self.save_config()

    # ----------------------------------------------------------------- theme

    def _effective_dark(self) -> bool:
        mode = self._THEME_MODES.get(self.theme_var.get(), 'system')
        if mode == 'dark':
            return True
        if mode == 'light':
            return False
        return system_prefers_dark()

    def _glass_active(self) -> bool:
        return (self.app_bg or {}).get('mode') == 'glass'

    def _apply_theme(self):
        pal = _DARK if self._effective_dark() else _LIGHT
        self._pal = pal
        # 毛玻璃（透出壁纸）模式：背景型控件用“接近主题底色”的透明键，
        # 使文字抗锯齿不泛色；输入/按钮等保持不透明配色。
        bg = glass_key(self._effective_dark()) if self._glass_active() else pal['bg']
        root = self.root
        root.configure(bg=bg)
        style = ttk.Style(root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TFrame', background=bg, foreground=pal['fg'])
        style.configure('TLabel', background=bg, foreground=pal['fg'])
        style.configure('TLabel', background=bg, foreground=pal['fg'])
        style.configure('Muted.TLabel', background=bg, foreground=pal['muted'])
        style.configure('Card.TLabelframe', background=bg, foreground=pal['fg'],
                        bordercolor=pal['border'], lightcolor=bg, darkcolor=bg)
        style.configure('Card.TLabelframe.Label', background=bg, foreground=pal['fg'],
                        font=('Segoe UI', max(9, round(11 * self._dpi_scale())), 'bold'))
        for el in ('TCheckbutton', 'TRadiobutton'):
            style.configure(el, background=bg, foreground=pal['fg'])
        style.configure('TButton', background=pal['button'], foreground=pal['fg'],
                        bordercolor=pal['border'], lightcolor=pal['button'],
                        darkcolor=pal['button'], focuscolor=pal['button'], padding=(10, 5))
        style.map('TButton', background=[('active', pal['button_hover'])])
        style.configure('Accent.TButton', background=self.accent, foreground='#ffffff',
                        bordercolor=self.accent, lightcolor=self.accent,
                        darkcolor=self.accent, focuscolor=self.accent, padding=(12, 6))
        style.map('Accent.TButton', background=[('active', self._shade(self.accent, -15))])
        for el in ('TEntry', 'TSpinbox'):
            style.configure(el, fieldbackground=pal['field'], foreground=pal['fg'],
                            bordercolor=pal['border'], lightcolor=pal['border'],
                            darkcolor=pal['border'], arrowcolor=pal['fg'],
                            insertcolor=pal['fg'])
            style.map(el, fieldbackground=[('readonly', pal['field'])],
                      bordercolor=[('focus', self.accent)])
        style.configure('TCombobox', fieldbackground=pal['field'], foreground=pal['fg'],
                        background=pal['button'], bordercolor=pal['border'],
                        lightcolor=pal['border'], darkcolor=pal['border'],
                        arrowcolor=pal['fg'], selectbackground=self.accent,
                        selectforeground='#ffffff')
        style.map('TCombobox', fieldbackground=[('readonly', pal['field'])],
                  selectbackground=[('readonly', self.accent)],
                  arrowcolor=[('active', pal['fg'])])
        style.configure('TNotebook', background=bg, bordercolor=pal['border'])
        style.configure('TNotebook.Tab', background=pal['button'], foreground=pal['fg'],
                        padding=(14, 6))
        style.map('TNotebook.Tab', background=[('selected', pal['card'])],
                  foreground=[('selected', pal['fg'])])
        style.configure('Treeview', background=pal['field'], foreground=pal['fg'],
                        fieldbackground=pal['field'], bordercolor=pal['border'])
        style.configure('Treeview.Heading', background=pal['button'], foreground=pal['fg'])
        style.configure('TScale', background=bg)
        style.configure('TSeparator', background=pal['sep'])
        try:
            hwnd = ctypes.c_ssize_t(root.winfo_id())
            if ctypes.windll.user32.IsWindow:
                GA_ROOTOWNER = 3
                ctypes.windll.user32.GetAncestor.restype = wintypes.HWND
                ctypes.windll.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                hwnd = ctypes.c_ssize_t(ctypes.windll.user32.GetAncestor(hwnd.value, GA_ROOTOWNER))
            value = ctypes.c_int(1 if pal is _DARK else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass
        self._refresh_title_icons()
        # 标题字号随 DPI 缩放（跨屏拖动到不同 DPI 显示器时保持一致观感）。
        if getattr(self, 'title_label', None) is not None:
            try:
                self.title_label.configure(
                    font=('Segoe UI', max(14, round(18 * self._dpi_scale())), 'bold'))
            except tk.TclError:
                pass

    def _shade(self, hex_color: str, delta: int) -> str:
        try:
            c = hex_color.lstrip('#')
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            r = max(0, min(255, r + delta))
            g = max(0, min(255, g + delta))
            b = max(0, min(255, b + delta))
            return f'#{r:02x}{g:02x}{b:02x}'
        except Exception:
            return hex_color

    def _dpi_scale(self) -> float:
        """当前 DPI 相对 96dpi 的缩放系数，用于让字号随显示器 DPI 变化。"""
        try:
            return max(0.5, min(3.0, float(self.root.tk.call('tk', 'scaling')) / 1.3333))
        except Exception:
            return 1.0

    def _refresh_title_icons(self):
        ico = resource_path('icon_night.ico' if getattr(self, '_pal', _LIGHT) is _DARK else 'icon_day.ico')
        if ico:
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass

    def on_theme_change(self):
        # 仅刷新界面与背景，不落盘、不重建自启、不弹窗（避免系统切深浅色时连带动作）。
        self._apply_theme()
        self._apply_app_background()

    # ----------------------------------------------------- app background

    def _build_background(self):
        self.bg_canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.bg_canvas.bind('<Configure>', self._on_bg_resize)

    def _apply_app_background(self):
        """软件外观背景：default（跟随主题）或 glass（毛玻璃透出壁纸）。"""
        self._photo = None
        mode = (self.app_bg or {}).get('mode', 'default')
        if mode == 'glass':
            self._bg_mode, self._bg_value = 'glass', glass_key(self._effective_dark())
        else:
            mode = 'default'
            self._bg_mode, self._bg_value = 'default', _DARK['bg'] if self._effective_dark() else _LIGHT['bg']
        hwnd = self.root.winfo_id()
        if mode == 'glass':
            try:
                self.root.wm_attributes('-transparentcolor', self._bg_value)
            except tk.TclError:
                pass
            set_window_glass(hwnd, True, self._effective_dark())
        else:
            # 不复位 transparentcolor：default 模式不会绘制该键色，不会真正透明，
            # 且避免向 Tk 传空串导致部分版本抛错、复位失效。
            set_window_glass(hwnd, False, self._effective_dark())
        self._draw_background()

    def _on_bg_resize(self, event):
        self._draw_background()

    def _draw_background(self):
        try:
            # glass：透明键色，露出被磨砂的壁纸；default：主题底色（不透明）。
            self.bg_canvas.configure(bg=self._bg_value)
        except Exception:
            log.exception('绘制软件背景失败')

    # ------------------------------------------------------------------ UI

    def build_ui(self):
        pad = {'padx': 14, 'pady': 8}
        self.notebook = ttk.Notebook(self.root)
        self.notebook.place(relx=0.012, rely=0.012, relwidth=0.976, relheight=0.976)

        self._build_basic_tab(pad)
        self._build_schedule_tab(pad)
        self._build_appearance_tab(pad)
        self._build_about_tab(pad)

    def _card(self, parent, title):
        f = ttk.LabelFrame(parent, text=title, padding=12, style='Card.TLabelframe')
        f.pack(fill='both', expand=True, padx=10, pady=8)
        return f

    def _build_basic_tab(self, pad):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text='基本设置')
        top = ttk.Frame(tab)
        top.pack(fill='x', pady=(0, 6))
        self.title_label = ttk.Label(top, text=APP_NAME,
                                     font=('Segoe UI', 18, 'bold'))
        self.title_label.pack(anchor='w')
        ttk.Label(top, text='使用 Windows 原生壁纸接口，无需安装第三方 Python 库',
                  style='Muted.TLabel').pack(anchor='w')

        card = self._card(tab, '切换与启动')
        row = ttk.Frame(card)
        row.pack(fill='x', **pad)
        ttk.Label(row, text='切换间隔', width=12).pack(side='left')
        ttk.Spinbox(row, from_=1, to=10080, textvariable=self.interval_var, width=8).pack(side='left')
        ttk.Label(row, text='分钟').pack(side='left', padx=(8, 0))
        ttk.Checkbutton(row, text='幻灯片切换（系统淡入淡出）', variable=self.slideshow_var,
                        command=self.save_settings).pack(side='left', padx=(16, 0))
        row = ttk.Frame(card)
        row.pack(fill='x', **pad)
        ttk.Checkbutton(row, text='Windows 登录时自动启动', variable=self.startup_var,
                        command=self.save_settings).pack(side='left')
        ttk.Label(row, text='包含子文件夹', width=12).pack(side='left', padx=(24, 0))
        ttk.Checkbutton(row, text='递归扫描子目录', variable=self.recursive_var,
                        command=self.save_settings).pack(side='left')
        row = ttk.Frame(card)
        row.pack(fill='x', **pad)
        ttk.Label(row, text='外观', width=8).pack(side='left')
        theme_box = ttk.Combobox(row, textvariable=self.theme_var, width=10,
                                 state='readonly', values=tuple(self._THEME_LABELS.values()))
        theme_box.pack(side='left')
        theme_box.bind('<<ComboboxSelected>>', lambda event: self.on_theme_change())

        row = ttk.Frame(tab)
        row.pack(fill='x', pady=12)
        self.switch_btn = ttk.Button(row, text='立即切换', command=self.on_switch_click, style='Accent.TButton')
        self.switch_btn.pack(side='left', padx=4)
        ttk.Button(row, text='暂停 / 继续', command=self.toggle_pause).pack(side='left', padx=4)
        ttk.Button(row, text='打开文件夹', command=self.open_folder).pack(side='left', padx=4)
        ttk.Button(row, text='最小化到托盘', command=self.minimize_window).pack(side='left', padx=4)
        ttk.Button(row, text='保存设置', command=self.save_settings, style='Accent.TButton').pack(side='left', padx=4)
        ttk.Button(row, text='打开日志', command=self.open_log).pack(side='left', padx=4)

        ttk.Separator(tab).pack(fill='x', pady=6)
        ttk.Label(tab, textvariable=self.status_var, wraplength=640).pack(anchor='w', pady=5)
        ttk.Label(tab, textvariable=self.count_var).pack(anchor='w')
        ttk.Label(tab, text='“最小化窗口”会保留程序运行；右上角关闭会隐藏到托盘；右键托盘图标的“退出”才会结束程序。',
                  style='Muted.TLabel').pack(anchor='w', pady=(18, 0))

    def _build_schedule_tab(self, pad):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text='时段与文件夹')
        card = self._card(tab, '按时间切换多个文件夹')
        ttk.Label(card, text='按时段匹配当前时间，使用对应文件夹；无匹配时回退到首个启用时段（兜底）。取消勾选即该时段不切换。',
                  style='Muted.TLabel').pack(anchor='w', pady=(0, 8))

        cols = ('name', 'start', 'end', 'folder', 'enabled')
        self.tree = ttk.Treeview(card, columns=cols, show='headings', height=8,
                                 selectmode='browse')
        self.tree.heading('name', text='名称')
        self.tree.heading('start', text='起始')
        self.tree.heading('end', text='结束')
        self.tree.heading('folder', text='文件夹')
        self.tree.heading('enabled', text='启用')
        self.tree.column('name', width=90)
        self.tree.column('start', width=70)
        self.tree.column('end', width=70)
        self.tree.column('folder', width=300)
        self.tree.column('enabled', width=50)
        self.tree.pack(fill='x', pady=(0, 8))
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)

        btn_row = ttk.Frame(card)
        btn_row.pack(fill='x', pady=(0, 8))
        ttk.Button(btn_row, text='添加', command=self._add_schedule).pack(side='left', padx=3)
        ttk.Button(btn_row, text='删除', command=self._del_schedule).pack(side='left', padx=3)
        ttk.Button(btn_row, text='上移', command=lambda: self._move_schedule(-1)).pack(side='left', padx=3)
        ttk.Button(btn_row, text='下移', command=lambda: self._move_schedule(1)).pack(side='left', padx=3)

        form = ttk.LabelFrame(card, text='编辑所选时段', padding=10, style='Card.TLabelframe')
        form.pack(fill='x')
        r = ttk.Frame(form)
        r.pack(fill='x', pady=3)
        ttk.Label(r, text='名称', width=8).pack(side='left')
        ttk.Entry(r, textvariable=self.s_name_var, width=14).pack(side='left', padx=(4, 12))
        ttk.Label(r, text='起始', width=6).pack(side='left')
        ttk.Entry(r, textvariable=self.s_start_var, width=7).pack(side='left', padx=4)
        ttk.Label(r, text='结束', width=6).pack(side='left')
        ttk.Entry(r, textvariable=self.s_end_var, width=7).pack(side='left', padx=4)
        ttk.Checkbutton(r, text='启用', variable=self.s_enabled_var,
                        command=self._on_form_enabled).pack(side='left', padx=(12, 0))
        r = ttk.Frame(form)
        r.pack(fill='x', pady=3)
        ttk.Label(r, text='文件夹', width=8).pack(side='left')
        ttk.Entry(r, textvariable=self.s_folder_var).pack(side='left', fill='x', expand=True, padx=(4, 8))
        ttk.Button(r, text='选择…', command=self._choose_schedule_folder).pack(side='left')
        for v in (self.s_name_var, self.s_start_var, self.s_end_var, self.s_folder_var):
            v.trace_add('write', self._on_form_change)

        self._refresh_tree()

    def _build_appearance_tab(self, pad):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text='软件外观')
        card = self._card(tab, '窗口外观')

        # 强调色：固定预设色板，点击即换并持久化。
        row = ttk.Frame(card)
        row.pack(fill='x', **pad)
        ttk.Label(row, text='强调色', width=12).pack(side='left')
        sw = tk.Frame(row)
        sw.pack(side='left', padx=6)
        self._swatch_buttons = []
        for color in PRESET_ACCENTS:
            b = tk.Button(sw, width=3, height=1, bg=color, relief='sunken' if color == self.accent else 'ridge',
                          bd=2 if color == self.accent else 1, activebackground=color,
                          command=lambda c=color: self._set_accent(c))
            b.pack(side='left', padx=2)
            self._swatch_buttons.append(b)
        ttk.Button(row, text='重置默认', command=lambda: self._set_accent('#3A7AFE')).pack(side='left', padx=(8, 0))

        # 软件背景：仅 default（跟随主题）/ glass（毛玻璃透出壁纸）。
        row = ttk.Frame(card)
        row.pack(fill='x', **pad)
        ttk.Label(row, text='软件背景', width=12).pack(side='left')
        self.bg_mode_var = tk.StringVar(value=self.app_bg.get('mode', 'default'))
        ttk.Combobox(row, textvariable=self.bg_mode_var, width=12, state='readonly',
                     values=('default', 'glass')).pack(side='left', padx=6)
        ttk.Button(row, text='应用背景', command=self._apply_bg).pack(side='left', padx=4)
        ttk.Label(card, text='default=跟随主题配色；glass=毛玻璃（窗口背景磨砂半透明、透出桌面壁纸，Win10/11 生效）。',
                  style='Muted.TLabel').pack(anchor='w', pady=(4, 0))

    def _build_about_tab(self, pad):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text='关于')
        card = self._card(tab, f'关于 {APP_NAME}')
        lines = [
            f'版本：{VERSION}',
            '功能：Windows 系统托盘常驻，按多时段从指定文件夹随机切换桌面壁纸。',
            '特性：零第三方依赖；原生幻灯片（淡入淡出、跨屏）；单例互斥；登录自启。',
            f'配置：{CONFIG_FILE}',
            f'日志：{LOG_FILE}',
            '提示：右键托盘图标可进行切换、暂停、打开日志、退出等操作。',
        ]
        for line in lines:
            ttk.Label(card, text=line, wraplength=640, justify='left').pack(anchor='w', pady=3)
        ttk.Button(card, text='打开日志文件', command=self.open_log).pack(anchor='w', pady=(10, 0))

    # ------------------------------------------------------------ schedules

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.schedules):
            self.tree.insert('', 'end', iid=str(i), values=(
                s.get('name', ''), s.get('start', ''), s.get('end', ''),
                s.get('folder', ''), '✓' if s.get('enabled', True) else ''))
        if self.sel_idx is not None and 0 <= self.sel_idx < len(self.schedules):
            self.tree.selection_set(str(self.sel_idx))

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            self.sel_idx = None
            return
        self.sel_idx = int(sel[0])
        s = self.schedules[self.sel_idx]
        self._loading_form = True
        self.s_name_var.set(s.get('name', ''))
        self.s_start_var.set(s.get('start', ''))
        self.s_end_var.set(s.get('end', ''))
        self.s_folder_var.set(s.get('folder', ''))
        self.s_enabled_var.set(s.get('enabled', True))
        self._loading_form = False

    def _on_form_change(self, *args):
        if self._loading_form or self.sel_idx is None:
            return
        s = self.schedules[self.sel_idx]
        s['name'] = self.s_name_var.get()
        s['start'] = self.s_start_var.get()
        s['end'] = self.s_end_var.get()
        s['folder'] = self.s_folder_var.get()
        self._refresh_tree()

    def _on_form_enabled(self):
        if self.sel_idx is None:
            return
        self.schedules[self.sel_idx]['enabled'] = self.s_enabled_var.get()
        self._refresh_tree()

    def _add_schedule(self):
        self.schedules.append({'name': f'时段{len(self.schedules) + 1}',
                               'start': '00:00', 'end': '23:59',
                               'folder': str(Path.home() / 'Pictures' / 'Wallpapers'),
                               'enabled': True})
        self._refresh_tree()
        self.tree.selection_set(str(len(self.schedules) - 1))
        self._on_tree_select()

    def _del_schedule(self):
        if self.sel_idx is None or not self.schedules:
            return
        del self.schedules[self.sel_idx]
        self.sel_idx = None
        self._refresh_tree()

    def _move_schedule(self, delta):
        if self.sel_idx is None:
            return
        j = self.sel_idx + delta
        if 0 <= j < len(self.schedules):
            self.schedules[self.sel_idx], self.schedules[j] = \
                self.schedules[j], self.schedules[self.sel_idx]
            self.sel_idx = j
            self._refresh_tree()
            self.tree.selection_set(str(j))

    def _choose_schedule_folder(self):
        if self.sel_idx is None:
            messagebox.showinfo(APP_NAME, '请先在上方选择一个时段。', parent=self.root)
            return
        current = self.schedules[self.sel_idx].get('folder', '')
        selected = filedialog.askdirectory(initialdir=current or str(Path.home()),
                                           title='选择壁纸文件夹')
        if selected:
            self.s_folder_var.set(selected)
            self._on_form_change()

    def _set_accent(self, color):
        """从预设色板选择强调色：立即换色并持久化。"""
        self.accent = color
        for b in getattr(self, '_swatch_buttons', []):
            match = b.cget('bg').lower() == color.lower()
            b.config(relief='sunken' if match else 'ridge', bd=2 if match else 1)
        self._apply_theme()
        self.save_config()

    def _apply_bg(self):
        self.app_bg = {'mode': self.bg_mode_var.get(), 'value': ''}
        self._apply_theme()           # 切换模式时同步更新样式（离开毛玻璃需复原配色）
        self._apply_app_background()
        self.save_settings()

    # ---------------------------------------------------------------- logic

    @staticmethod
    def parse_time(value):
        hour, minute = map(int, value.strip().split(':'))
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError
        return dt_time(hour, minute)

    def _in_range(self, value, start, end):
        if start == end:
            return True
        if start < end:
            return start <= value < end
        return value >= start or value < end

    def _match_schedule(self):
        now = datetime.now().time()
        for s in self.schedules:
            if not s.get('enabled', True):
                continue
            try:
                if self._in_range(now, self.parse_time(s['start']), self.parse_time(s['end'])):
                    return s
            except (ValueError, TypeError, KeyError):
                continue
        return None

    def _fallback_schedule(self):
        for s in self.schedules:
            if s.get('enabled', True):
                return s
        return None

    def active_schedule(self):
        """返回当前匹配时段；无匹配则回退到首个启用时段（兜底）。"""
        return self._match_schedule() or self._fallback_schedule()

    def active_folder(self) -> Path:
        s = self.active_schedule()
        if s and s.get('folder'):
            return Path(s['folder']).expanduser()
        return Path.home()

    def _validate_schedules(self):
        for i, s in enumerate(self.schedules):
            try:
                self.parse_time(s['start'])
                self.parse_time(s['end'])
            except (ValueError, TypeError, KeyError):
                return False, f'第 {i + 1} 个时段“{s.get("name", "")}”的时间格式错误，请使用 HH:MM。'
        return True, ''

    def save_settings(self):
        try:
            self.interval = max(1, int(self.interval_var.get()))
            ok, msg = self._validate_schedules()
            if not ok:
                messagebox.showerror(APP_NAME, msg, parent=self.root)
                return
            slideshow_was = self._slideshow_active
            if slideshow_was:
                self._stop_slideshow()
            # 仅在自启勾选状态真正变化时重建启动项，避免每次保存都重跑 PowerShell。
            if self.startup_var.get() != startup_is_enabled():
                startup_ok, startup_error = set_startup(self.startup_var.get())
            else:
                startup_ok, startup_error = True, ''
            if not startup_ok:
                self.startup_var.set(startup_is_enabled())
            self.save_config()
            # 改动即时生效：下一拍 tick 立即用新配置重新扫描并切换。
            self.next_switch = 0.0
            # 校验当前文件夹有效性，给出明确提示而非静默忽略。
            folder = self.active_folder()
            try:
                count = len(scan_images(folder, self.recursive_var.get()))
            except Exception:
                count = 0
            if count == 0:
                self.status_var.set(f'提示：当前时段文件夹为空或不存在：{folder}')
            else:
                self.status_var.set('设置已保存，将在下次切换时即时生效')
            if not startup_ok:
                messagebox.showwarning(APP_NAME, f'其他设置已保存，但开机自启动设置失败：\n{startup_error}', parent=self.root)
        except ValueError:
            messagebox.showerror(APP_NAME, '切换间隔必须是正整数。', parent=self.root)

    def open_folder(self):
        folder = self.active_folder()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)
        except Exception as exc:
            log.warning('打开文件夹失败: %s', exc)

    def open_log(self):
        try:
            if not LOG_FILE.exists():
                LOG_FILE.write_text('', encoding='utf-8')
            os.startfile(LOG_FILE)
        except Exception as exc:
            log.warning('打开日志失败: %s', exc)
            messagebox.showerror(APP_NAME, f'无法打开日志文件：{exc}', parent=self.root)

    # ------------------------------------------------------- slideshow (COM)

    def _desktop_wallpaper(self):
        if self._dw is None and not self._dw_failed:
            try:
                ole32 = ctypes.windll.ole32
                obj = ctypes.c_void_p()
                hr = ole32.CoCreateInstance(ctypes.byref(CLSID_DESKTOP_WALLPAPER), None, 23,
                                            ctypes.byref(IID_DESKTOP_WALLPAPER), ctypes.byref(obj))
                if hr != 0 or not obj:
                    self._dw_failed = True
                    return None
                self._dw = obj
            except Exception:
                self._dw_failed = True
                return None
        return self._dw

    def _slideshow_unsupported(self):
        dw = self._desktop_wallpaper()
        if dw is None:
            return True
        state = wintypes.UINT(0)
        hr = _com(dw, DW_GET_STATUS, HRESULT, [ctypes.POINTER(wintypes.UINT)], ctypes.byref(state))
        if hr != 0:
            return True
        return bool(state.value & DSS_DISABLED_BY_REMOTE_SESSION)

    def _apply_slideshow(self, images) -> bool:
        dw = self._desktop_wallpaper()
        if dw is None or self._slideshow_unsupported():
            log.warning('系统幻灯片不可用（远程会话或不支持）')
            return False
        arr = _shell_item_array(images)
        if not arr:
            return False
        try:
            if _com(dw, DW_SET_SLIDESHOW, HRESULT, [ctypes.c_void_p], arr) != 0:
                return False
            _com(arr, IUNKNOWN_RELEASE, HRESULT, [])
            arr = None
            tick_ms = max(1, self.interval) * 60000
            if _com(dw, DW_SET_SLIDESHOW_OPTIONS, HRESULT, [wintypes.UINT, wintypes.UINT],
                    DSO_SHUFFLEIMAGES, tick_ms) != 0:
                return False
            _com(dw, DW_SET_POSITION, HRESULT, [wintypes.UINT], DWPOS_FILL)
            if _com(dw, DW_ENABLE, HRESULT, [wintypes.BOOL], True) not in (0, 1):
                return False
            self._slideshow_active = True
            self._slideshow_folder = str(self.active_folder())
            self.last_ok_switch = time.monotonic()
            return True
        finally:
            if arr:
                _com(arr, IUNKNOWN_RELEASE, HRESULT, [])
            if not self._slideshow_active:
                log.warning('应用系统幻灯片失败')

    def _stop_slideshow(self):
        if not self._slideshow_active:
            return
        dw = self._dw
        if dw:
            _com(dw, DW_ENABLE, HRESULT, [wintypes.BOOL], False)
            _com(dw, DW_SET_SLIDESHOW, HRESULT, [ctypes.c_void_p], None)
        self._slideshow_active = False
        self._slideshow_folder = None

    def _set_wallpaper_com(self, path) -> bool:
        """用 IDesktopWallpaper.SetWallpaper(NULL, path) 设单屏壁纸（兼容 WebP）。"""
        dw = self._desktop_wallpaper()
        if dw is None:
            return False
        try:
            hr = _com(dw, DW_SET_WALLPAPER, HRESULT, [wintypes.LPCWSTR], str(path))
            if hr != 0:
                log.warning('COM SetWallpaper 失败：HRESULT=0x%x，将回退 SPI', hr)
            return hr == 0
        except Exception:
            log.exception('COM SetWallpaper 调用失败')
            return False

    def _advance_slideshow(self) -> bool:
        dw = self._desktop_wallpaper()
        if not dw:
            return False
        ole32 = ctypes.windll.ole32
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        count = wintypes.UINT(0)
        advanced = False
        if _com(dw, DW_GET_MONITOR_COUNT, HRESULT, [ctypes.POINTER(wintypes.UINT)],
                ctypes.byref(count)) == 0 and count.value:
            for i in range(count.value):
                mon = ctypes.c_void_p()
                if _com(dw, DW_GET_MONITOR_AT, HRESULT, [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)],
                        i, ctypes.byref(mon)) == 0 and mon:
                    try:
                        name = ctypes.wstring_at(mon)
                        if _com(dw, DW_ADVANCE_SLIDESHOW, HRESULT,
                                [wintypes.LPCWSTR, ctypes.c_int], name, 0) == 0:
                            advanced = True
                    finally:
                        ole32.CoTaskMemFree(mon)
        if not advanced:
            advanced = _com(dw, DW_ADVANCE_SLIDESHOW, HRESULT,
                            [wintypes.LPCWSTR, ctypes.c_int], None, 0) == 0
        return advanced

    # -------------------------------------------------------------- switching

    def on_switch_click(self):
        if self._slideshow_active and self._advance_slideshow():
            self.status_var.set('幻灯片:已切换到下一张')
            return
        self.switch_now()

    def switch_now(self):
        if self._scan_busy:
            return
        sched = self.active_schedule()
        if sched is None:
            self.status_var.set('当前没有启用的时段，未切换壁纸')
            return
        folder = Path(sched['folder']).expanduser()
        name = sched.get('name', '')
        self._scan_busy = True
        self._set_busy(True)
        self._scan_started = time.monotonic()
        self._scan_seq += 1
        threading.Thread(target=self._scan_worker, args=(folder, name, self._scan_seq),
                         name='wallpaper-scan', daemon=True).start()

    def _set_busy(self, busy):
        btn = getattr(self, 'switch_btn', None)
        if btn:
            try:
                btn.state(['disabled'] if busy else ['!disabled'])
            except Exception:
                pass

    def _scan_worker(self, folder, name, seq):
        try:
            images = scan_images(folder, self.recursive_var.get())
        except Exception:
            images = []
            log.exception('扫描文件夹失败: %s', folder)
        self.events.put(('scanned', seq, name, folder, images))

    def _pick(self, folder, images):
        key = str(folder)
        if self._bag_key != key:
            self._bag = list(range(len(images)))
            for i in range(len(self._bag) - 1, 0, -1):
                j = _rand_below(i + 1)
                self._bag[i], self._bag[j] = self._bag[j], self._bag[i]
            self._bag_key = key
        limit = len(images)
        while self._bag and self._bag[-1] >= limit:
            self._bag.pop()
        if not self._bag:
            self._bag = list(range(limit))
            for i in range(limit - 1, 0, -1):
                j = _rand_below(i + 1)
                self._bag[i], self._bag[j] = self._bag[j], self._bag[i]
        cand = images[self._bag.pop()]
        # 缺陷14：避免重启后立即重复上一张——若抽到上次用过的，换一张。
        if len(images) > 1 and cand.name == self._last_used.get(key):
            if self._bag:
                self._bag.insert(0, images.index(cand))
                cand = images[self._bag.pop()]
            else:
                # 袋空且唯一候选就是上一张：重洗至多 5 次取非重复项，仍失败才接受重复。
                for _ in range(5):
                    bag = list(range(len(images)))
                    for i in range(len(bag) - 1, 0, -1):
                        j = _rand_below(i + 1)
                        bag[i], bag[j] = bag[j], bag[i]
                    pick = images[bag.pop()]
                    if pick.name != self._last_used.get(key):
                        self._bag = bag          # 续用剩余袋，保持一轮不重复
                        cand = pick
                        break
        return cand

    def _apply_switch(self, seq, name, folder, images):
        try:
            if seq != self._scan_seq:
                return  # settings changed while scanning; drop the stale batch
            self._scan_busy = False
            self.last_period = name
            self.folder = folder
            self._image_count = len(images)
            self.count_var.set(f'当前时段：{name}　|　当前文件夹找到 {len(images)} 张图片')
            if not images:
                self.status_var.set(f'当前时段没有找到图片，请把图片放入：{folder}')
                return
            if self.slideshow_var.get() and len(images) >= 2:
                if self._apply_slideshow(images):
                    self.status_var.set(
                        f'幻灯片模式：{len(images)} 张，每 {self.interval} 分钟淡入淡出切换')
                    return
                self.status_var.set('系统幻灯片不可用（远程会话？），已改用单张切换')
            if self._slideshow_active:
                self._stop_slideshow()
            chosen = self._pick(folder, images)
            ext = chosen.suffix.lower()
            if ext == '.webp' and not self.slideshow_var.get():
                ok = self._set_wallpaper_com(chosen) or set_wallpaper(chosen)
            else:
                ok = set_wallpaper(chosen)
            if ok:
                self.last = chosen
                self.last_ok_switch = time.monotonic()
                self._last_used[str(folder)] = chosen.name
                self._persist_last_used()
                self.status_var.set(f'当前壁纸：{chosen.name}    |    下次切换约 {self.interval} 分钟后')
            else:
                if ext == '.webp':
                    self.status_var.set('WebP 在“单张模式”下系统可能不支持，建议开启“幻灯片切换”（原生支持 WebP）。')
                else:
                    self.status_var.set(f'设置壁纸失败：{chosen}')
        except Exception:
            log.exception('应用壁纸失败')
        finally:
            # 无论成功失败都复位扫描锁，杜绝永久忙锁（需求20）。
            self._scan_busy = False
            self._set_busy(False)

    def toggle_pause(self):
        self.paused = not self.paused
        self.status_var.set('已暂停自动切换' if self.paused else '已继续自动切换')
        tray = getattr(self, 'tray', None)
        if tray is not None:
            tray.paused_hint = self.paused
        if self._slideshow_active:
            dw = self._dw
            if dw:
                _com(dw, DW_ENABLE, HRESULT, [wintypes.BOOL], not self.paused)
        if not self.paused:
            self.next_switch = 0.0

    def tick(self):
        if not self.running:
            return
        try:
            if not self.paused:
                # 看门狗：扫描锁超时强制复位。
                if self._scan_busy and (time.monotonic() - self._scan_started) > SCAN_DEADLINE:
                    log.warning('扫描锁超时，强制复位')
                    self._scan_busy = False
                sched = self.active_schedule()
                current_name = sched['name'] if sched else None
                period_changed = self.last_period != current_name
                if period_changed:
                    self.last_period = current_name
                    self.next_switch = 0
                if tray := getattr(self, 'tray', None):
                    tray.set_dark(self._effective_dark())
                slideshow_up_to_date = (self._slideshow_active
                                         and self._slideshow_folder == str(self.active_folder())
                                         and not period_changed)
                if sched and not slideshow_up_to_date and time.monotonic() >= self.next_switch:
                    self.switch_now()
                    self.next_switch = time.monotonic() + self.interval * 60
                # 自愈：距上次成功切换过久则主动切换并清锁。
                gap = time.monotonic() - self.last_ok_switch
                if sched and gap > max(self.interval * 120, SELF_HEAL_FLOOR):
                    log.warning('检测到长时间未成功切换(%.0fs)，触发自愈', gap)
                    self._scan_busy = False
                    self.next_switch = 0
            if self.root.winfo_viewable():
                if self.paused:
                    self.count_var.set('已暂停自动切换')
                elif self._slideshow_active:
                    self.count_var.set(
                        f'幻灯片模式运行中　|　{_img_text(self._image_count)}　|　系统自动淡入淡出')
                elif self.next_switch:
                    remaining = max(0, self.next_switch - time.monotonic())
                    label = f'约 {int(remaining // 60)} 分钟后切换' if remaining >= 90 else f'约 {int(remaining)} 秒后切换'
                    cn = self.last_period or '—'
                    self.count_var.set(
                        f'当前时段：{cn}　|　{_img_text(self._image_count)}　|　{label}')
        except Exception:
            log.exception('心跳 tick 异常')
        finally:
            if self.running:
                try:
                    self.root.after(1000, self.tick)
                except tk.TclError:
                    pass

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if isinstance(event, tuple) and event[0] == 'scanned':
                    self._apply_switch(*event[1:])
                    continue
                if isinstance(event, tuple) and event[0] == 'show':
                    if event[1]:
                        self.root.withdraw()
                    else:
                        self.show_window()
                    continue
                if event == 'switch':
                    self.on_switch_click()
                elif event == 'pause':
                    self.toggle_pause()
                elif event == 'folder':
                    self.open_folder()
                elif event == 'log':
                    self.open_log()
                elif event == 'exit':
                    self.close()
        except queue.Empty:
            pass
        except Exception:
            log.exception('事件循环异常')
        finally:
            if self.running:
                try:
                    self.root.after(250, self.poll_events)
                except tk.TclError:
                    pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def minimize_window(self):
        self.root.deiconify()
        self.root.iconify()

    def hide_window(self):
        self.root.withdraw()
        if not getattr(self.tray, 'tray_added', False):
            self.root.after(0, self.show_window)
            detail = str(self.tray.error) if self.tray.error else 'Windows 通知区域暂不可用。'
            messagebox.showerror(APP_NAME, f'系统托盘图标创建失败，窗口将保持打开：\n{detail}', parent=self.root)

    def close(self):
        self.running = False
        try:
            self._stop_slideshow()
            self.save_config()
        except Exception:
            log.exception('关闭前保存失败')
        finally:
            tray = getattr(self, 'tray', None)
            if tray is not None:
                try:
                    tray.stop()
                except Exception:
                    pass
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    # -------------------------------------------- window hooks (DPI / theme)

    def _set_glass_drag(self, dragging):
        """拖动/缩放期间临时降级磨砂效果，避免 Win11 Acrylic 黑闪。"""
        if not self._glass_active():
            return
        hwnd = self.root.winfo_id()
        if dragging:
            set_window_glass_blurbehind(hwnd, self._effective_dark())
        else:
            set_window_glass(hwnd, True, self._effective_dark())

    def _install_window_hooks(self):
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = LRESULT
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                              wintypes.WPARAM, wintypes.LPARAM]
            hwnd = ctypes.c_ssize_t(self.root.winfo_id())
            pre = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
            self._wndproc_cb = ctypes.WINFUNCTYPE(
                LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)(
                self._wnd_proc_hook)
            new = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, self._wndproc_cb)
            if not new:
                # 极端失败：恢复原窗口过程，放弃钩子，保证主程序可用。
                user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, pre)
                log.warning('SetWindowLongPtrW 返回 0，已恢复原窗口过程（DPI/主题实时跟随不可用）')
                return
            self._orig_wndproc = new
        except Exception:
            log.exception('安装窗口钩子失败（DPI/主题实时跟随将不可用）')

    def _wnd_proc_hook(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_DPICHANGED:
                self._on_dpi_changed(wparam)
            elif msg == WM_ENTERSIZEMOVE:
                # 开始拖动/缩放：切到无黑闪的 BLURBEHIND，规避 Win11 Acrylic 黑闪。
                self.root.after(0, self._set_glass_drag, True)
            elif msg == WM_EXITSIZEMOVE:
                # 结束拖动/缩放：恢复 Acrylic 磨砂。
                self.root.after(0, self._set_glass_drag, False)
            elif msg == WM_SETTINGCHANGE:
                try:
                    s = ctypes.wstring_at(lparam)
                    if s and 'ImmersiveColorSet' in s and \
                            self._THEME_MODES.get(self.theme_var.get()) == 'system':
                        self.root.after(0, self.on_theme_change)
                except Exception:
                    pass
        except Exception:
            log.exception('窗口钩子处理异常')
        if getattr(self, '_orig_wndproc', None):
            return ctypes.windll.user32.CallWindowProcW(self._orig_wndproc, hwnd, msg, wparam, lparam)
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_dpi_changed(self, wparam):
        """跨屏拖动到不同 DPI 显示器时重算 tk scaling，统一字号（需求18）。"""
        try:
            new_dpi = wparam & 0xFFFF
            if new_dpi > 0:
                self.root.tk.call('tk', 'scaling', new_dpi / 72.0)
            self.root.update_idletasks()
            self._apply_theme()  # 重新按新 DPI 计算字号
            log.info('DPI 变更为 %d，已重算缩放', new_dpi)
        except Exception:
            log.exception('DPI 变更处理失败')


def _img_text(count):
    return f'找到 {count} 张图片'


def main():
    if os.name != 'nt':
        raise SystemExit('此程序仅支持 Windows。')
    enable_high_dpi()
    setup_logging()
    instance_handle = acquire_single_instance()
    if not instance_handle:
        notify_running_instance('--background' in sys.argv[1:])
        log.info('已有实例运行，已转发请求')
        return
    ole32 = ctypes.windll.ole32
    co_hr = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    co_owned = co_hr in (0, 1)            # S_OK / S_FALSE
    root = tk.Tk()
    try:
        App(root, start_hidden='--background' in sys.argv[1:])
        root.mainloop()
    except Exception as exc:
        log.exception('主循环异常: %s', exc)
        messagebox.showerror(APP_NAME, str(exc))
    finally:
        if co_owned:
            ole32.CoUninitialize()
        ctypes.windll.kernel32.CloseHandle(instance_handle)


if __name__ == '__main__':
    main()
