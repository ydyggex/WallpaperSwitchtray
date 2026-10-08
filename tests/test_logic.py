"""Pure-logic tests: shuffle bag, time parsing/fallback, midnight ranges,
atomic config save. No GUI, no tray, no registry."""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wallpaper_switcher as ws

fails = []

class Fake: pass

# 1) shuffle bag: never None, no repeats per cycle, survives shrinking list,
#    rebags on folder change
f = Fake(); f._bag = []; f._bag_key = None
imgs = [ws.Path(f"p{i}.jpg") for i in range(5)]
seen = []
for _ in range(12):
    c = ws.App._pick(f, ws.Path("d"), imgs)
    if c is None:
        fails.append("None returned for non-empty list"); break
    seen.append(c)
for i in range(0, 10, 5):
    if len(set(seen[i:i+5])) != 5:
        fails.append(f"repeat within cycle: {seen[i:i+5]}")
for _ in range(6):
    c = ws.App._pick(f, ws.Path("d"), imgs[:2])
    if c not in imgs[:2]:
        fails.append(f"stale index leak: {c}")
if ws.App._pick(f, ws.Path("other"), imgs) is None:
    fails.append("rebag failed")

# 2) corrupted time strings fall back instead of raising
g = Fake(); g.day_start = '7:0'; g.day_end = None
g.night_start = '25:99'; g.night_end = '19:00'
t = ws.App._parse_saved_times(g)
assert len(t) == 4

# 3) parse_time accept/reject
for ok in ("07:00", "19:00", "0:5", "23:59"):
    ws.App.parse_time(ok)
for bad in ("07", "7:60", "abc", "24:00", ""):
    try:
        ws.App.parse_time(bad); fails.append(f"bad time accepted: {bad!r}")
    except (ValueError, TypeError):
        pass

# 4) in_range crossing midnight (mirrors App.active_period closure)
from datetime import time as T
def in_range(v, s, e):
    if s == e: return True
    if s < e: return s <= v < e
    return v >= s or v < e
assert in_range(T(23, 0), T(19, 0), T(7, 0))
assert in_range(T(3, 0), T(19, 0), T(7, 0))
assert not in_range(T(12, 0), T(19, 0), T(7, 0))
assert in_range(T(10, 0), T(7, 0), T(19, 0))

# 5) atomic config write incl. slideshow/theme fields
ws.CONFIG_DIR = Path(tempfile.mkdtemp()) / "WS"
ws.CONFIG_FILE = ws.CONFIG_DIR / "config.json"
h = Fake(); h.day_folder = ws.Path("A"); h.night_folder = ws.Path("B")
h.day_start = '07:00'; h.day_end = '19:00'
h.night_start = '19:00'; h.night_end = '07:00'
h.interval = 30
h.startup_var = types.SimpleNamespace(get=lambda: False)
h.slideshow_var = types.SimpleNamespace(get=lambda: True)
h.theme_var = types.SimpleNamespace(get=lambda: '深色')
h._THEME_MODES = ws.App._THEME_MODES
ws.App.save_config(h)
cfg = json.loads(ws.CONFIG_FILE.read_text(encoding='utf-8'))
assert cfg['day_folder'] == 'A' and cfg['night_folder'] == 'B'
assert cfg['slideshow'] is True and cfg['theme'] == 'dark'
assert not (ws.CONFIG_DIR / 'config.json.tmp').exists()

# 6) no random/hashlib import in the app source (keeps libcrypto out of builds)
src = Path(__file__).resolve().parents[1].joinpath('wallpaper_switcher.py').read_text(encoding='utf-8')
if 'import random' in src or 'import hashlib' in src:
    fails.append("app must not import random/hashlib (uses os.urandom)")

print("FAILS:", fails if fails else "NONE")
sys.exit(1 if fails else 0)
