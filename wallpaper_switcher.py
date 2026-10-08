import ctypes
import base64
import json
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
CONFIG_DIR = Path(os.environ.get('APPDATA', Path.home())) / 'WallpaperSwitcher'
CONFIG_FILE = CONFIG_DIR / 'config.json'
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


def _current_app_path() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable)
    return Path(__file__).resolve()


def resource_path(name: str):
    """Locate a bundled asset: PyInstaller _MEIPASS, script dir, then assets/."""
    candidates = []
    base = getattr(sys, '_MEIPASS', None)
    if base:
        candidates.append(Path(base) / name)
    here = Path(__file__).resolve().parent
    candidates.append(here / name)
    candidates.append(here / 'assets' / name)
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


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

# IDesktopWallpaper vtable slots (3..18 after IUnknown)
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


def notify_running_instance():
    """Ask an already-running copy to bring its window forward."""
    user32 = ctypes.windll.user32
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    message = user32.RegisterWindowMessageW(SHOW_REQUEST_NAME)
    if message:
        user32.PostMessageW(HWND_BROADCAST, message, 0, 0)


def enable_high_dpi() -> None:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def set_wallpaper(path: Path) -> bool:
    return bool(ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(path), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE))


def scan_images(folder: Path) -> list:
    try:
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
    """Create or remove the current-user Startup shortcut.

    A shortcut, rather than a registry Run entry, keeps this per-user and does
    not require elevation.  The command is encoded so paths containing spaces,
    apostrophes, or non-ASCII characters do not corrupt the startup entry.
    """
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

    def __init__(self, events: queue.Queue):
        self.events = events
        self.ready = threading.Event()
        self.error = None
        self.hwnd = None
        self.paused_hint = False
        self.thread = threading.Thread(target=self._run, name='wallpaper-tray', daemon=True)
        self.thread.start()
        # Give the tray thread a moment so hide_window can report failures.
        self.ready.wait(0.5)

    def _add_icon(self):
        added = self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self.nid))
        if added:
            self.nid.uVersion = 4
            self.shell32.Shell_NotifyIconW(self.NIM_SETVERSION, ctypes.byref(self.nid))
        return bool(added)

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

    def set_theme(self, period: str) -> None:
        """Swap the tray glyph between the day and night artwork."""
        if not getattr(self, 'icons', None) or not self.tray_added:
            return
        h = self.icons.get('night' if period == 'night' else 'day')
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
            # Registered before CreateWindowExW: WM_CREATE reaches _wnd_proc
            # synchronously inside that call.
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
            # 12 args: exStyle, class, title, style, x, y, w, h, parent, menu, hInst, param
            self.hwnd = user32.CreateWindowExW(0, self.class_name, APP_NAME, 0, 0, 0, 0, 0,
                                                0, 0, self.hinstance, None)
            if not self.hwnd:
                raise ctypes.WinError()

            # SM_CXSMICON (49) is the pixel size the shell actually draws in
            # the tray; loading at that size keeps the artwork crisp on any DPI.
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
            self.events.put('show')
            return 0
        if msg == getattr(self, 'taskbar_created', 0):
            self.tray_added = self._add_icon()
        elif msg == self.WM_TRAY:
            tray_event = int(lparam) & 65535
            if tray_event == self.WM_LBUTTONDBLCLK:
                self.events.put('show')
            elif tray_event == self.WM_RBUTTONUP:
                self._menu()
        elif msg == self.WM_COMMAND:
            command = int(wparam) & 65535
            mapping = {self.ID_SHOW: 'show', self.ID_SWITCH: 'switch', self.ID_PAUSE: 'pause',
                       self.ID_FOLDER: 'folder', self.ID_EXIT: 'exit'}
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


_LIGHT = {'bg': '#f0f0f0', 'fg': '#1a1a1a', 'muted': '#666666', 'field': '#ffffff',
          'button': '#e1e1e1', 'button_hover': '#e8e8e8', 'border': '#adadad',
          'select': '#0078d7', 'sep': '#c8c8c8'}
_DARK = {'bg': '#202020', 'fg': '#e6e6e6', 'muted': '#9a9a9a', 'field': '#2d2d2d',
         'button': '#3a3a3a', 'button_hover': '#454545', 'border': '#5a5a5a',
         'select': '#094771', 'sep': '#3f3f3f'}


class App:
    _THEME_LABELS = {'system': '跟随系统', 'light': '浅色', 'dark': '深色'}
    _THEME_MODES = {'跟随系统': 'system', '浅色': 'light', '深色': 'dark'}

    def __init__(self, root, start_hidden=False):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry('640x440')
        self.root.minsize(560, 400)
        self.events = queue.Queue()
        self.running = True
        self.paused = False
        self.last = None
        self.config = self.load_config()
        default_folder = str(Path.home() / 'Pictures' / 'Wallpapers')
        self.day_folder = Path(self.config.get('day_folder', self.config.get('folder', default_folder)))
        self.night_folder = Path(self.config.get('night_folder', self.config.get('folder', default_folder)))
        self.folder = self.day_folder
        self.day_start = self.config.get('day_start', '07:00')
        self.day_end = self.config.get('day_end', '19:00')
        self.night_start = self.config.get('night_start', '19:00')
        self.night_end = self.config.get('night_end', '07:00')
        self.interval = max(1, int(self.config.get('interval', 30)))
        self.startup_var = tk.BooleanVar(value=bool(self.config.get('startup', False)))
        self.slideshow_var = tk.BooleanVar(value=bool(self.config.get('slideshow', False)))
        _mode = self.config.get('theme', 'system')
        self.theme_var = tk.StringVar(value=self._THEME_LABELS.get(_mode, '跟随系统'))
        self.interval_var = tk.StringVar(value=str(self.interval))
        self.day_folder_var = tk.StringVar(value=str(self.day_folder))
        self.night_folder_var = tk.StringVar(value=str(self.night_folder))
        self.day_start_var = tk.StringVar(value=self.day_start)
        self.day_end_var = tk.StringVar(value=self.day_end)
        self.night_start_var = tk.StringVar(value=self.night_start)
        self.night_end_var = tk.StringVar(value=self.night_end)
        self.status_var = tk.StringVar(value='准备就绪')
        self.count_var = tk.StringVar(value='')
        self.next_switch = 0.0
        self.last_period = None
        # Scheduler state is read from saved values only, never from live
        # StringVars, so a half-typed time box cannot break the 1-second tick.
        self._times = self._parse_saved_times()
        self._scan_busy = False
        self._scan_seq = 0
        self._image_count = 0
        self._bag = []
        self._bag_key = None
        # IDesktopWallpaper COM state
        self._dw = None
        self._dw_failed = False
        self._slideshow_active = False
        self._slideshow_folder = None
        self.build_ui()
        self._apply_theme()
        self.tray = TrayIcon(self.events)
        self.root.after(250, self.poll_events)
        self.root.after(300, self.tick)
        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)
        self._repair_startup_link()
        if start_hidden:
            self.root.withdraw()

    # ---------------------------------------------------------------- config

    def _parse_saved_times(self):
        defaults = {'day_start': '07:00', 'day_end': '19:00',
                    'night_start': '19:00', 'night_end': '07:00'}
        result = []
        for key in ('day_start', 'day_end', 'night_start', 'night_end'):
            raw = getattr(self, key, None) or defaults[key]
            try:
                result.append(App.parse_time(str(raw)))
            except (ValueError, TypeError):
                result.append(App.parse_time(defaults[key]))
        return tuple(result)

    def load_config(self):
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                'day_folder': str(self.day_folder), 'night_folder': str(self.night_folder),
                'day_start': self.day_start, 'day_end': self.day_end,
                'night_start': self.night_start, 'night_end': self.night_end,
                'interval': self.interval, 'startup': self.startup_var.get(),
                'slideshow': self.slideshow_var.get(),
                'theme': self._THEME_MODES.get(self.theme_var.get(), 'system'),
                'app_path': str(_current_app_path()),
            },
            ensure_ascii=False, indent=2,
        )
        # Write-then-replace keeps the config readable if the process dies
        # half-way through a save.
        tmp = CONFIG_DIR / 'config.json.tmp'
        tmp.write_text(payload, encoding='utf-8')
        os.replace(tmp, CONFIG_FILE)

    def _repair_startup_link(self):
        """Re-create the Startup shortcut when the exe moved or the link vanished."""
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

    def _apply_theme(self):
        """Recolor the whole window: clam palette + DWM dark title bar."""
        pal = _DARK if self._effective_dark() else _LIGHT
        self._pal = pal
        root = self.root
        root.configure(bg=pal['bg'])
        style = ttk.Style(root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        for el in ('TFrame', 'TLabel', 'TCheckbutton', 'TRadiobutton', 'TLabelframe'):
            style.configure(el, background=pal['bg'], foreground=pal['fg'])
        style.configure('TButton', background=pal['button'], foreground=pal['fg'],
                        bordercolor=pal['border'], lightcolor=pal['button'],
                        darkcolor=pal['button'], focuscolor=pal['button'],
                        padding=(8, 4))
        style.map('TButton', background=[('active', pal['button_hover'])])
        for el in ('TEntry', 'TSpinbox'):
            style.configure(el, fieldbackground=pal['field'], foreground=pal['fg'],
                            bordercolor=pal['border'], lightcolor=pal['border'],
                            darkcolor=pal['border'], arrowcolor=pal['fg'],
                            insertcolor=pal['fg'])
            style.map(el, fieldbackground=[('readonly', pal['field'])],
                      bordercolor=[('focus', pal['select'])])
        style.configure('TCombobox', fieldbackground=pal['field'], foreground=pal['fg'],
                        background=pal['button'], bordercolor=pal['border'],
                        lightcolor=pal['border'], darkcolor=pal['border'],
                        arrowcolor=pal['fg'], selectbackground=pal['select'],
                        selectforeground=pal['fg'])
        style.map('TCombobox', fieldbackground=[('readonly', pal['field'])],
                  selectbackground=[('readonly', pal['select'])],
                  arrowcolor=[('active', pal['fg'])])
        style.configure('TSeparator', background=pal['sep'])
        style.configure('Muted.TLabel', background=pal['bg'], foreground=pal['muted'])
        # Dark title bar (immersive mode, Win10 1809+/Win11).
        try:
            hwnd = ctypes.c_ssize_t(root.winfo_id())
            if ctypes.windll.user32.IsWindow:
                # winfo_id returns the client HWND; climb to the top-level.
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

    def _refresh_title_icons(self):
        """Swap the title-bar icon to match the active theme artwork."""
        ico = resource_path('icon_night.ico' if getattr(self, '_pal', _LIGHT) is _DARK else 'icon_day.ico')
        if ico:
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass

    def on_theme_change(self):
        self._apply_theme()
        self.save_settings()

    # ------------------------------------------------------------------ UI

    def build_ui(self):
        pad = {'padx': 14, 'pady': 8}
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=APP_NAME, font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='使用 Windows 原生壁纸接口，无需安装第三方 Python 库').pack(anchor='w', pady=(2, 14))
        ttk.Label(frame, text='时间段壁纸设置', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 2))
        row = ttk.Frame(frame)
        row.pack({'fill': 'x', **pad})
        ttk.Label(row, text='白天', width=12).pack(side='left')
        ttk.Entry(row, textvariable=self.day_start_var, width=7).pack(side='left')
        ttk.Label(row, text='–').pack(side='left', padx=4)
        ttk.Entry(row, textvariable=self.day_end_var, width=7).pack(side='left')
        ttk.Entry(row, textvariable=self.day_folder_var).pack(side='left', fill='x', expand=True, padx=(10, 0))
        ttk.Button(row, text='选择…', command=lambda: self.choose_folder('day')).pack(side='left', padx=(8, 0))
        row = ttk.Frame(frame)
        row.pack({'fill': 'x', **pad})
        ttk.Label(row, text='夜间', width=12).pack(side='left')
        ttk.Entry(row, textvariable=self.night_start_var, width=7).pack(side='left')
        ttk.Label(row, text='–').pack(side='left', padx=4)
        ttk.Entry(row, textvariable=self.night_end_var, width=7).pack(side='left')
        ttk.Entry(row, textvariable=self.night_folder_var).pack(side='left', fill='x', expand=True, padx=(10, 0))
        ttk.Button(row, text='选择…', command=lambda: self.choose_folder('night')).pack(side='left', padx=(8, 0))
        ttk.Label(frame, text='时间格式 HH:MM；例如白天 07:00–19:00，夜间 19:00–07:00。',
                  style='Muted.TLabel').pack(anchor='w', padx=14)
        row = ttk.Frame(frame)
        row.pack({'fill': 'x', **pad})
        ttk.Label(row, text='切换间隔', width=12).pack(side='left')
        ttk.Spinbox(row, from_=1, to=10080, textvariable=self.interval_var, width=8).pack(side='left')
        ttk.Label(row, text='分钟').pack(side='left', padx=(8, 0))
        ttk.Checkbutton(row, text='幻灯片切换（系统淡入淡出）', variable=self.slideshow_var,
                        command=self.save_settings).pack(side='left', padx=(16, 0))
        row = ttk.Frame(frame)
        row.pack({'fill': 'x', **pad})
        ttk.Checkbutton(row, text='Windows 登录时自动启动', variable=self.startup_var,
                        command=self.save_settings).pack(side='left')
        ttk.Label(row, text='外观', width=8).pack(side='left', padx=(24, 0))
        theme_box = ttk.Combobox(row, textvariable=self.theme_var, width=9,
                                 state='readonly',
                                 values=tuple(self._THEME_LABELS.values()))
        theme_box.pack(side='left')
        theme_box.bind('<<ComboboxSelected>>', lambda event: self.on_theme_change())
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=12)
        ttk.Button(buttons, text='立即切换', command=self.on_switch_click).pack(side='left', padx=4)
        ttk.Button(buttons, text='暂停 / 继续', command=self.toggle_pause).pack(side='left', padx=4)
        ttk.Button(buttons, text='打开文件夹', command=self.open_folder).pack(side='left', padx=4)
        ttk.Button(buttons, text='最小化到托盘', command=self.minimize_window).pack(side='left', padx=4)
        ttk.Button(buttons, text='保存设置', command=self.save_settings).pack(side='left', padx=4)
        ttk.Separator(frame).pack(fill='x', pady=6)
        ttk.Label(frame, textvariable=self.status_var, wraplength=520).pack(anchor='w', pady=5)
        ttk.Label(frame, textvariable=self.count_var).pack(anchor='w')
        ttk.Label(frame, text='“最小化窗口”会保留程序运行；右上角关闭会隐藏到托盘；右键托盘图标的“退出”才会结束程序。',
                  style='Muted.TLabel').pack(anchor='w', pady=(18, 0))

    # ---------------------------------------------------------------- logic

    @staticmethod
    def parse_time(value):
        hour, minute = map(int, value.strip().split(':'))
        if not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ValueError
        return dt_time(hour, minute)

    def save_settings(self):
        try:
            self.interval = max(1, int(self.interval_var.get()))
            for value in (self.day_start_var.get(), self.day_end_var.get(),
                          self.night_start_var.get(), self.night_end_var.get()):
                self.parse_time(value)
            slideshow_was = self._slideshow_active
            self.day_folder = Path(self.day_folder_var.get()).expanduser()
            self.night_folder = Path(self.night_folder_var.get()).expanduser()
            self.folder = self.active_folder()
            self.day_start, self.day_end = self.day_start_var.get(), self.day_end_var.get()
            self.night_start, self.night_end = self.night_start_var.get(), self.night_end_var.get()
            self._times = self._parse_saved_times()
            # Interval/folder changes must rebuild the OS slideshow timer, and
            # unchecking it must hand the desktop back to single-image mode.
            if slideshow_was:
                self._stop_slideshow()
            startup_ok, startup_error = set_startup(self.startup_var.get())
            if not startup_ok:
                self.startup_var.set(startup_is_enabled())
            self.save_config()
            self.next_switch = 0.0
            if startup_ok:
                self.status_var.set('设置已保存')
                return
            messagebox.showwarning(APP_NAME, f'其他设置已保存，但开机自启动设置失败：\n{startup_error}', parent=self.root)
            self.status_var.set('其他设置已保存；开机自启动设置失败')
        except ValueError:
            messagebox.showerror(APP_NAME, '切换间隔必须是正整数，时间必须使用 HH:MM 格式。', parent=self.root)

    def choose_folder(self, period):
        current = self.day_folder if period == 'day' else self.night_folder
        selected = filedialog.askdirectory(initialdir=str(current), title='选择壁纸文件夹')
        if selected:
            (self.day_folder_var if period == 'day' else self.night_folder_var).set(selected)
            self.save_settings()

    def active_period(self):
        now = datetime.now().time()
        day_start, day_end, night_start, night_end = self._times

        def in_range(value, start, end):
            if start == end:
                return True
            if start < end:
                return start <= value < end
            return value >= start or value < end

        if in_range(now, day_start, day_end):
            return 'day'
        if in_range(now, night_start, night_end):
            return 'night'
        return 'night'

    def active_folder(self):
        return self.day_folder if self.active_period() == 'day' else self.night_folder

    def open_folder(self):
        self.folder = self.active_folder()
        self.folder.mkdir(parents=True, exist_ok=True)
        os.startfile(self.folder)

    # ------------------------------------------------------- slideshow (COM)

    def _desktop_wallpaper(self):
        if self._dw is None and not self._dw_failed:
            try:
                ole32 = ctypes.windll.ole32
                obj = ctypes.c_void_p()
                # CLSCTX_ALL: this coclass is served by a local COM server on
                # some Windows builds; INPROC_SERVER alone returns REGDB_E_CLASSNOTREG.
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
        """Hand the current folder to the OS slideshow engine (crossfade)."""
        dw = self._desktop_wallpaper()
        if dw is None or self._slideshow_unsupported():
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
            # Enable legitimately returns S_FALSE (1) when the state was
            # already enabled -- the slideshow is up either way.
            if _com(dw, DW_ENABLE, HRESULT, [wintypes.BOOL], True) not in (0, 1):
                return False
            self._slideshow_active = True
            self._slideshow_folder = str(self.folder)
            return True
        finally:
            if arr:
                _com(arr, IUNKNOWN_RELEASE, HRESULT, [])

    def _stop_slideshow(self):
        if not self._slideshow_active:
            return
        dw = self._dw
        if dw:
            _com(dw, DW_ENABLE, HRESULT, [wintypes.BOOL], False)
            _com(dw, DW_SET_SLIDESHOW, HRESULT, [ctypes.c_void_p], None)
        self._slideshow_active = False
        self._slideshow_folder = None

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
        """'Switch now' in both the window button and the tray menu.

        While the OS slideshow owns the desktop, re-scanning the same folder
        would rebuild the identical collection and look like a dead button --
        ask the slideshow engine to fade to the next image instead.
        """
        if self._slideshow_active and self._advance_slideshow():
            self.status_var.set('幻灯片:已切换到下一张')
            return
        self.switch_now()

    def switch_now(self):
        """Kick off an off-main-thread folder scan; applied via poll_events."""
        if self._scan_busy:
            return
        period = self.active_period()
        folder = self.active_folder()
        self._scan_busy = True
        self._scan_seq += 1
        threading.Thread(target=self._scan_worker, args=(folder, period, self._scan_seq),
                         name='wallpaper-scan', daemon=True).start()

    def _scan_worker(self, folder, period, seq):
        try:
            images = scan_images(folder)
        except Exception:
            images = []
        self.events.put(('scanned', seq, period, folder, images))

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
        return images[self._bag.pop()]

    def _apply_switch(self, seq, period, folder, images):
        if seq != self._scan_seq:
            return  # settings changed while scanning; drop the stale batch
        self._scan_busy = False
        self.folder = folder
        self._image_count = len(images)
        cn = '白天' if period == 'day' else '夜间'
        self.count_var.set(f'当前时段：{cn}　|　当前文件夹找到 {len(images)} 张图片')
        if not images:
            self.status_var.set(f'当前时段没有找到图片，请把 JPG/JPEG/PNG/BMP/WEBP 放入：{folder}')
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
        if set_wallpaper(chosen):
            self.last = chosen
            self.status_var.set(f'当前壁纸：{chosen.name}    |    下次切换约 {self.interval} 分钟后')
        else:
            self.status_var.set(f'设置壁纸失败：{chosen}')

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
        """One-second heartbeat; never allowed to die from a stray exception."""
        if not self.running:
            return
        try:
            if not self.paused:
                current_period = self.active_period()
                period_changed = self.last_period != current_period
                if period_changed:
                    self.last_period = current_period
                    self.next_switch = 0
                if tray := getattr(self, 'tray', None):
                    tray.set_theme(current_period)
                slideshow_up_to_date = (self._slideshow_active
                                         and self._slideshow_folder == str(self.active_folder())
                                         and not period_changed)
                if not slideshow_up_to_date and time.monotonic() >= self.next_switch:
                    self.switch_now()
                    self.next_switch = time.monotonic() + self.interval * 60
            if self.root.winfo_viewable():
                if self.paused:
                    self.count_var.set('已暂停自动切换')
                elif self._slideshow_active:
                    self.count_var.set(
                        f'幻灯片模式运行中　|　{_img_text(self._image_count)}　|　系统自动淡入淡出')
                elif self.next_switch:
                    remaining = max(0, self.next_switch - time.monotonic())
                    if remaining >= 90:
                        label = f'约 {int(remaining // 60)} 分钟后切换'
                    else:
                        label = f'约 {int(remaining)} 秒后切换'
                    cn = '白天' if self.active_period() == 'day' else '夜间'
                    self.count_var.set(
                        f'当前时段：{cn}　|　{_img_text(self._image_count)}　|　{label}')
        except Exception:
            pass
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
                if event == 'show':
                    self.show_window()
                elif event == 'switch':
                    self.on_switch_click()
                elif event == 'pause':
                    self.toggle_pause()
                elif event == 'folder':
                    self.open_folder()
                elif event == 'exit':
                    self.close()
        except queue.Empty:
            pass
        except Exception:
            pass
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
            # Without a tray icon a hidden window would be unrecoverable.
            self.root.after(0, self.show_window)
            detail = str(self.tray.error) if self.tray.error else 'Windows 通知区域暂不可用。'
            messagebox.showerror(APP_NAME, f'系统托盘图标创建失败，窗口将保持打开：\n{detail}', parent=self.root)

    def close(self):
        self.running = False
        try:
            self._stop_slideshow()
            self.save_config()
        finally:
            self.tray.stop()
            self.root.destroy()


def _img_text(count):
    return f'找到 {count} 张图片'


def main():
    if os.name != 'nt':
        raise SystemExit('此程序仅支持 Windows。')
    enable_high_dpi()
    instance_handle = acquire_single_instance()
    if not instance_handle:
        notify_running_instance()
        return
    ole32 = ctypes.windll.ole32
    co_hr = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    co_owned = co_hr in (0, 1)            # S_OK / S_FALSE
    root = tk.Tk()
    try:
        App(root, start_hidden='--background' in sys.argv[1:])
        root.mainloop()
    except Exception as exc:
        messagebox.showerror(APP_NAME, str(exc))
    finally:
        if co_owned:
            ole32.CoUninitialize()
        ctypes.windll.kernel32.CloseHandle(instance_handle)


if __name__ == '__main__':
    main()
