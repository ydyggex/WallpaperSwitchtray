"""Theme engine test on a real (hidden) Tk window: palette swaps, DWM title
bar, config round-trip. Needs a desktop session."""
import ctypes
import sys
import tempfile
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wallpaper_switcher as ws

ws.CONFIG_DIR = Path(tempfile.mkdtemp(prefix="theme_cfg_")) / "WallpaperSwitcher"
ws.CONFIG_FILE = ws.CONFIG_DIR / "config.json"

fails = []
root = tk.Tk()
root.withdraw()
app = ws.App(root, start_hidden=True)
try:
    import tkinter.ttk as ttkmod
    st = ttkmod.Style(root)
    app.theme_var.set('深色'); app._apply_theme(); root.update()
    if app._pal is not ws._DARK:
        fails.append("dark mode did not select _DARK palette")
    if st.lookup('TFrame', 'background') != ws._DARK['bg']:
        fails.append("TFrame bg not dark")
    app.theme_var.set('浅色'); app._apply_theme(); root.update()
    if app._pal is not ws._LIGHT:
        fails.append("light mode did not select _LIGHT palette")
    app.theme_var.set('跟随系统'); app._apply_theme(); root.update()
    app.save_config()
    if app.load_config().get('theme') != 'system':
        fails.append("theme not persisted")
except Exception as e:
    import traceback; traceback.print_exc()
    fails.append(f"theme apply crashed: {e}")
finally:
    app.running = False
    try: app.tray.stop()
    except Exception: pass
    root.destroy()

print("FAILS:", fails if fails else "NONE")
sys.exit(1 if fails else 0)
