"""Tray creation regression test (needs a real Windows desktop session;
fails under CI services without an explorer/shell)."""
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wallpaper_switcher as ws

fails = []

ev = queue.Queue()
tray = ws.TrayIcon(ev)
tray.ready.wait(2.0)
time.sleep(0.5)
print("tray.error =", repr(tray.error))
print("tray.tray_added =", getattr(tray, "tray_added", None))
if tray.error is not None:
    fails.append(f"tray raised: {tray.error}")
if not getattr(tray, "tray_added", False):
    fails.append("tray_added is False -> icon never appeared")
try:
    tray.set_theme('night')
    tray.set_theme('day')
except Exception as e:
    fails.append(f"set_theme crashed: {e}")
tray.stop()

print("FAILS:", fails if fails else "NONE")
sys.exit(1 if fails else 0)
