"""v7.1 pure-logic tests: config normalization/migration, time parsing and
schedule matching, recursive scan, shuffle-bag picking with last-used
de-duplication, atomic config save, zero-dependency invariants.

No GUI, no tray, no registry. Run: python tests/test_logic_v7.py
"""
import json
import sys
import tempfile
import types
from datetime import time as T
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wallpaper_switcher_v7 as ws

fails = []


class Fake:
    pass


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except Exception as e:
        fails.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  FAIL {name}: {e}")


# 1) parse_time -------------------------------------------------------------
def t_parse_time():
    assert ws.App.parse_time('07:00') == T(7, 0)
    assert ws.App.parse_time(' 23:59 ') == T(23, 59)
    for bad in ('24:00', '12:60', 'abc', '12', '', '7:0:1'):
        try:
            ws.App.parse_time(bad)
        except ValueError:
            continue
        except Exception as e:
            fails.append(f"parse_time({bad!r}) raised {type(e).__name__}, want ValueError")
            continue
        fails.append(f"parse_time accepted invalid {bad!r}")


# 2) _in_range incl. midnight cross ---------------------------------------
def t_in_range():
    r = ws.App._in_range
    assert r(None, T(12), T(7), T(19)) is True            # inside daytime
    assert r(None, T(20), T(7), T(19)) is False           # after end
    assert r(None, T(19), T(7), T(19)) is False           # end exclusive
    assert r(None, T(7), T(7), T(19)) is True             # start inclusive
    assert r(None, T(23), T(19), T(7)) is True            # cross midnight: 19→07
    assert r(None, T(3), T(19), T(7)) is True             # cross midnight: early am
    assert r(None, T(9), T(19), T(7)) is False            # cross midnight: daytime
    assert r(None, T(12), T(12, 30), T(12, 30)) is True   # start==end: whole day


# 3) schedule matching / fallback ------------------------------------------
def t_match_schedule():
    h = Fake()
    h.parse_time = ws.App.parse_time
    h._in_range = lambda v, s, e: ws.App._in_range(None, v, s, e)
    h._match_schedule = lambda: ws.App._match_schedule(h)
    h._fallback_schedule = lambda: ws.App._fallback_schedule(h)
    h.schedules = [
        {'name': '白天', 'start': '07:00', 'end': '19:00', 'folder': 'A', 'enabled': True},
        {'name': '夜间', 'start': '19:00', 'end': '07:00', 'folder': 'B', 'enabled': True},
    ]
    real_dt = ws.datetime

    class FakeDT:
        instant = T(20, 0)

        @classmethod
        def now(cls):
            return cls()

        def time(self):
            return self.instant

    try:
        ws.datetime = FakeDT
        FakeDT.instant = T(20, 0)
        assert ws.App._match_schedule(h)['folder'] == 'B'
        FakeDT.instant = T(8, 0)
        assert ws.App._match_schedule(h)['folder'] == 'A'
        # disabled segment is not matched; fallback goes to first enabled
        FakeDT.instant = T(8, 0)
        h.schedules[0]['enabled'] = False
        assert ws.App._match_schedule(h) is None
        assert ws.App.active_schedule(h)['folder'] == 'B'  # fallback
        h.schedules[1]['enabled'] = False
        assert ws.App.active_schedule(h) is None           # all disabled
        # malformed time in config → segment skipped without crashing
        FakeDT.instant = T(8, 0)
        h.schedules[0]['enabled'] = True
        h.schedules[0]['start'] = '25:00'
        h.schedules[1]['enabled'] = True
        assert ws.App._match_schedule(h) is None           # bad segment skipped
        ok, msg = ws.App._validate_schedules(h)
        assert ok is False and 'HH:MM' in msg              # blocked on save
        h.schedules[0]['start'] = '07:00'
        assert ws.App._validate_schedules(h)[0] is True
    finally:
        ws.datetime = real_dt


# 4) _normalize_config: migration & fallback --------------------------------
def t_normalize():
    n = ws.App._normalize_config
    # v6 day/night → schedules
    cfg = n(None, {'day_folder': 'D', 'night_folder': 'N', 'day_start': '06:00'})
    assert len(cfg['schedules']) == 2
    assert cfg['schedules'][0]['folder'] == 'D' and cfg['schedules'][0]['start'] == '06:00'
    assert cfg['schedules'][1]['folder'] == 'N'
    # removed background modes fall back to default
    cfg = n(None, {'schedules': [{'name': 'x', 'start': '00:00', 'end': '23:59',
                                  'folder': 'F'}],
                   'app_bg': {'mode': 'gradient', 'value': '#ff0000'}})
    assert cfg['app_bg']['mode'] == 'default'
    cfg = n(None, {'schedules': [{'name': 'x'}], 'app_bg': 'glass'})   # not a dict
    assert cfg['app_bg']['mode'] == 'default'
    # glass survives
    cfg = n(None, {'schedules': [{'name': 'x'}], 'app_bg': {'mode': 'glass'}})
    assert cfg['app_bg']['mode'] == 'glass'
    # field defaults
    cfg = n(None, {'schedules': [{'name': 'x'}]})
    s = cfg['schedules'][0]
    assert s['enabled'] is True and s['start'] == '00:00' and s['folder'] == ''
    assert cfg['interval'] == 30 and cfg['theme'] == 'system'
    assert cfg['version'] == ws.VERSION


# 5) scan_images --------------------------------------------------------------
def t_scan():
    root = Path(tempfile.mkdtemp())
    (root / 'b.png').write_bytes(b'x')
    (root / 'a.JPG').write_bytes(b'x')          # case-insensitive extension
    (root / 'c.txt').write_bytes(b'x')          # must be ignored
    sub = root / 'sub'
    sub.mkdir()
    (sub / 'd.webp').write_bytes(b'x')
    deep = root / 'x1' / 'x2' / 'x3' / 'x4' / 'x5'
    deep.mkdir(parents=True)
    (deep / 'e.jpg').write_bytes(b'x')          # beyond max_depth=4

    flat = ws.scan_images(root, recursive=False)
    assert [p.name for p in flat] == ['a.JPG', 'b.png'], flat
    rec = ws.scan_images(root, recursive=True)
    assert 'd.webp' in [p.name for p in rec] and 'c.txt' not in [p.name for p in rec]
    assert 'e.jpg' not in [p.name for p in rec]  # depth cap respected
    assert ws.scan_images(root / 'nope', recursive=False) == []
    assert ws.scan_images(root / 'nope', recursive=True) == []


# 6) shuffle bag: one round no repeats + last-used de-dup ---------------------
def t_pick_bag():
    imgs = [Path(f"d/p{i}.jpg") for i in range(5)]
    key = str(Path('d'))
    h = Fake()
    h._bag, h._bag_key, h._last_used = [], None, {}
    drawn = [ws.App._pick(h, Path('d'), imgs) for _ in range(5)]
    assert sorted(p.name for p in drawn) == sorted(p.name for p in imgs), \
        "one round must yield every image exactly once"
    # next round is a fresh permutation
    drawn2 = [ws.App._pick(h, Path('d'), imgs) for _ in range(5)]
    assert sorted(p.name for p in drawn2) == sorted(p.name for p in imgs)
    # last-used de-dup: never immediately repeat across a restart
    h._last_used[key] = 'p3.jpg'
    for _ in range(300):
        h._bag, h._bag_key = [], None
        got = ws.App._pick(h, Path('d'), imgs)
        assert got.name != 'p3.jpg', "must not repeat last_used immediately"
    # single-image folder: still returns it (no crash, bag may stay empty)
    h._bag, h._bag_key = [], None
    one = [Path('d/z.jpg')]
    assert ws.App._pick(h, Path('d'), one).name == 'z.jpg'
    # bag emptied by a stale trim AND the drawn pick equals last_used:
    # the re-shuffle fallback must almost always avoid the immediate repeat
    h._last_used[key] = 'p3.jpg'
    repeats = 0
    for _ in range(100):
        h._bag, h._bag_key = [3], key       # sole survivor is the last-used image
        got = ws.App._pick(h, Path('d'), imgs)
        if got.name == 'p3.jpg':
            repeats += 1
    assert repeats <= 5, f"fallback re-shuffle failed too often ({repeats}/100)"


# 7) save_config: atomic, complete, keeps last_used ---------------------------
def t_save_config():
    tmp = Path(tempfile.mkdtemp()) / 'WS'
    real_dir, real_file = ws.CONFIG_DIR, ws.CONFIG_FILE
    ws.CONFIG_DIR, ws.CONFIG_FILE = tmp, tmp / 'config.json'
    try:
        h = Fake()
        h.theme_var = types.SimpleNamespace(get=lambda: '跟随系统')
        h._THEME_MODES = ws.App._THEME_MODES
        h.accent, h.app_bg = '#8B5CF6', {'mode': 'glass', 'value': ''}
        h.interval, h._last_used = 15, {'d': 'p1.jpg'}
        h.schedules = [{'name': '白天', 'start': '07:00', 'end': '19:00',
                        'folder': 'A', 'enabled': True}]
        h.recursive_var = types.SimpleNamespace(get=lambda: True)
        h.slideshow_var = types.SimpleNamespace(get=lambda: False)
        h.startup_var = types.SimpleNamespace(get=lambda: False)
        ws.App.save_config(h)
        cfg = json.loads(ws.CONFIG_FILE.read_text(encoding='utf-8'))
        assert cfg['theme'] == 'system' and cfg['interval'] == 15
        assert cfg['accent'] == '#8B5CF6' and cfg['app_bg']['mode'] == 'glass'
        assert cfg['schedules'][0]['folder'] == 'A'
        assert cfg['last_used'] == {'d': 'p1.jpg'}, "save must persist last_used"
        assert cfg['version'] == ws.VERSION
        assert not (tmp / 'config.json.tmp').exists(), "temp file must be gone"
    finally:
        ws.CONFIG_DIR, ws.CONFIG_FILE = real_dir, real_file


# 8) zero-dependency invariants ------------------------------------------------
def t_no_crypto_imports():
    base = Path(__file__).resolve().parents[1]
    for fname in ('wallpaper_switcher_v7.py', 'wallpaper_switcher.py'):
        p = base / fname
        if not p.is_file():   # v6 may live only in the repo checkout
            continue
        src = p.read_text(encoding='utf-8')
        if 'import random' in src or 'import hashlib' in src:
            fails.append(f"{fname} must not import random/hashlib (uses os.urandom)")


def t_preset_palette():
    assert len(ws.PRESET_ACCENTS) == 6
    for c in ws.PRESET_ACCENTS:
        assert len(c) == 7 and c[0] == '#' and all(ch in '0123456789abcdefABCDEF' for ch in c[1:])
    assert '#3A7AFE' in ws.PRESET_ACCENTS  # default accent must be in palette


print("== v7.1 logic tests ==")
check('parse_time', t_parse_time)
check('in_range', t_in_range)
check('schedule matching/fallback', t_match_schedule)
check('config normalize/migrate', t_normalize)
check('scan_images', t_scan)
check('shuffle bag + last_used', t_pick_bag)
check('save_config atomic', t_save_config)
check('no random/hashlib imports', t_no_crypto_imports)
check('preset palette invariants', t_preset_palette)

print("FAILS:", fails if fails else "NONE")
sys.exit(1 if fails else 0)
