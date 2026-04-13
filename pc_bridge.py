#!/usr/bin/env python3
"""
RPi Audio Console — PC Bridge  (v4.4 — auto-assign + game audio fixes)
=======================================================================
Architecture:
  • Pure stdlib + websockets  — no Flask, minimal footprint
  • WebSocket server on :5009 — RPi connects to us
  • One persistent connection  — bidirectional, replaces all HTTP polling
  • COM thread                 — all pycaw calls serialised here (no leak)
  • Audio loopback             — pyaudiowpatch captures system audio for FFT
  • Media via winrt/winsdk     — Windows media session for title/artist
  • System tray (pystray)      — set Pi IP, exit

PC pushes to RPi:
  media updates, vol_sync, apps list, FFT frames, pot_display

RPi pushes to PC:
  vol (set volume), media_cmd (play/pause/next/prev), pot_config

Requirements:
    pip install websockets pycaw pystray pillow pyaudiowpatch numpy
    pip install winrt-Windows.Media.Control  (or winsdk)

Memory target: ~80-120 MB  (Flask+browser gone, COM leak fixed)

v4.3 memory leak fixes:
  • _media_poll_loop: reuses Manager object instead of creating a new COM
    object every second; only re-requests on exception
  • _get_album_art: reads album art in one call (read_bytes) instead of a
    per-byte loop; explicitly closes DataReader and stream after use
  • _rebuild_sessions: removed duplicate comtypes.CoInitialize() call that
    ran every 2 s on the sessions thread (CoInitialize already called once
    at thread start in _sessions_refresh_loop)
  • _log_bins / _fft_thread: log bin edge array pre-computed once before the
    FFT loop instead of being allocated fresh on every frame (30 fps)
  • Scheduled soft-restart every 48 h as a safety-net belt-and-suspenders
    measure; uses os.execv so the process replaces itself cleanly

v4.4 auto-assign + game audio fixes:
  • _rebuild_auto_assignments: manually-pinned apps are now excluded from the
    auto pool, so a pinned pot and an auto pot can never both control the same
    app simultaneously
  • _rebuild_sessions: when multiple audio sessions share the same process
    name (common with Steam/Epic games launched via a wrapper), the session
    with the highest current peak level is chosen rather than whichever one
    happened to be iterated last — fixes auto-assigned games that appeared
    to do nothing because the wrong session object was stored
"""

import asyncio, json, math, os, queue, sys, threading, time
from pathlib import Path

IS_WIN    = sys.platform == 'win32'
PORT      = 5009   # control channel: pots, media, vol_sync, apps
PORT_FFT  = 5010   # FFT-only channel: high-frequency visualiser frames
CONFIG_DIR = Path.home() / '.rpi_console'
SETTINGS   = CONFIG_DIR / 'settings.json'
CONFIG_DIR.mkdir(exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
_DEFAULT_BLOCKLIST = [
    'python', 'python3', 'pythonw', 'rpiconsole',
    'system', 'audiodg', 'svchost',
    'runtimebroker', 'shellexperiencehost',
    'searchui', 'cortana', 'explorer',
    'wmplayer', '',
]

_DEFAULT_KNOWN = {
    'chrome':            'Chrome',
    'msedge':            'Edge',
    'firefox':           'Firefox',
    'brave':             'Brave',
    'opera':             'Opera',
    'vivaldi':           'Vivaldi',
    'spotify':           'Spotify',
    'discord':           'Discord',
    'vlc':               'VLC',
    'itunes':            'iTunes',
    'foobar2000':        'foobar2000',
    'aimp':              'AIMP',
    'mpc-hc64':          'MPC-HC',
    'mpc-hc':            'MPC-HC',
    'mpc-be64':          'MPC-BE',
    'mpc-be':            'MPC-BE',
    'potplayer64':       'PotPlayer',
    'potplayer':         'PotPlayer',
    'steam':             'Steam',
    'epicgameslauncher': 'Epic Games',
    'teams':             'Teams',
    'slack':             'Slack',
    'zoom':              'Zoom',
    'obs64':             'OBS',
    'obs32':             'OBS',
}

def load_settings() -> dict:
    try:
        if SETTINGS.exists():
            d = json.loads(SETTINGS.read_text())
            # Merge defaults for any missing keys
            if 'blocklist' not in d:
                d['blocklist'] = list(_DEFAULT_BLOCKLIST)
            if 'known' not in d:
                d['known'] = dict(_DEFAULT_KNOWN)
            return d
    except Exception:
        pass
    return {'rpi_ip': '', 'blocklist': list(_DEFAULT_BLOCKLIST), 'known': dict(_DEFAULT_KNOWN)}

def save_settings(d: dict):
    SETTINGS.write_text(json.dumps(d, indent=2))

settings = load_settings()

# Live references — updated whenever settings change so existing threads pick up changes
_app_blocklist: set = set(settings.get('blocklist', _DEFAULT_BLOCKLIST))
_known_names:   dict = dict(settings.get('known',     _DEFAULT_KNOWN))

def _reload_lists():
    """Refresh the live blocklist and known-names from current settings."""
    global _app_blocklist, _known_names
    _app_blocklist = set(e.lower() for e in settings.get('blocklist', _DEFAULT_BLOCKLIST))
    _known_names   = dict(settings.get('known', _DEFAULT_KNOWN))

# ── COM Thread — serialises all pycaw calls ───────────────────────────────────
# All pycaw objects MUST be created and released on the same COM apartment.
# A single dedicated thread owns them. Other code submits callables via queue.

_com_q:   queue.Queue = queue.Queue(maxsize=64)
_com_ok = threading.Event()

AudioUtilities = ISimpleAudioVolume = IAudioEndpointVolume = CLSCTX_ALL = None
_session_cache: dict = {}   # {pid: session}
_session_cache_ts = 0.0
_endpoint = None            # IAudioEndpointVolume for master volume

def _com_thread_main():
    global AudioUtilities, ISimpleAudioVolume, IAudioEndpointVolume, CLSCTX_ALL
    global _endpoint
    if not IS_WIN:
        print('[COM] Not Windows — audio control disabled')
        _com_ok.set()
        _drain_com_queue()
        return
    try:
        import comtypes
        comtypes.CoInitialize()
        from pycaw.pycaw import (AudioUtilities as AU, ISimpleAudioVolume as SAV,
                                  IAudioEndpointVolume as AEV)
        try:
            from pycaw.pycaw import CLSCTX_ALL as CA
        except ImportError:
            from comtypes import CLSCTX_ALL as CA
        AudioUtilities       = AU
        ISimpleAudioVolume   = SAV
        IAudioEndpointVolume = AEV
        CLSCTX_ALL           = CA
        devices = AU.GetSpeakers()
        _endpoint = devices.Activate(AEV._iid_, CA, None).QueryInterface(AEV)
        print('[COM] pycaw ready')
    except Exception as e:
        print(f'[COM] pycaw init failed: {e}')
    _com_ok.set()
    _drain_com_queue()

def _drain_com_queue():
    """Run forever, executing callables from _com_q."""
    while True:
        try:
            fn = _com_q.get(timeout=0.1)
            try:
                fn()
            except Exception as e:
                print(f'[COM] task error: {e}')
        except queue.Empty:
            pass

def _com(fn):
    """Submit fn to the COM thread. Non-blocking."""
    try:
        _com_q.put_nowait(fn)
    except queue.Full:
        pass  # Drop rather than block

def _com_sync(fn, timeout=2.0):
    """Submit fn and block until done, returning result."""
    result = [None]
    done   = threading.Event()
    def wrapped():
        try:
            result[0] = fn()
        except Exception as e:
            result[0] = e
        done.set()
    _com(wrapped)
    done.wait(timeout)
    return result[0]

# ── Session cache (rebuilt every 2s on COM thread) ───────────────────────────
_session_lock = threading.Lock()
_sessions: dict = {}        # {name_lower: ISimpleAudioVolume}
_session_meters: dict = {}  # {name_lower: IAudioMeterInformation}
_session_display: dict = {} # {name_lower: str} — human-friendly display name

# Per-channel auto state.
# Auto pots are assigned to apps in round-robin order: the first __auto__ pot
# gets apps[0], the second gets apps[1], etc. If there are more pots than apps,
# extra pots are left unassigned. Assignment is recalculated whenever the app
# list changes or a new connection arrives.
# {ch_str: app_name_lower}  — only populated for __auto__ channels
_auto_per_ch: dict = {}

# Global loudest — still tracked for display/future use but no longer drives assignment
_auto_last_active: str = ''
_auto_last_time: float = 0.0

def _friendly_name(raw: str) -> str:
    """Convert a process name to a human-friendly display name.
    Uses the user-editable known-names list from settings; falls back to title-case."""
    return _known_names.get(raw.lower(), raw.title())


def _rebuild_sessions():
    # FIX (v4.3): removed duplicate comtypes.CoInitialize() that ran every 2 s.
    # CoInitialize is already called once at thread start in _sessions_refresh_loop.
    # Calling it repeatedly on the same thread is harmless per COM spec but the
    # repeated import machinery and local-variable shadowing of CLSCTX_ALL was
    # preventing timely GC of the old session COM objects.
    #
    # FIX (v4.4): when multiple audio sessions share the same process name (common
    # with Steam games that launch via a wrapper process), the old code silently
    # overwrote earlier entries with later ones, so whichever session happened to
    # be iterated last won — often NOT the one actually producing game audio.
    # Now we collect ALL sessions per name and keep the one with the highest
    # current peak level, falling back to the last-seen session if all are silent.
    # This makes auto-assign reliably land on the real game audio session.
    global _sessions, _session_meters, _session_display
    if AudioUtilities is None:
        return
    try:
        from pycaw.pycaw import IAudioMeterInformation
        # Intermediate: {name: [(peak, vol, meter_or_None), ...]}
        candidates: dict = {}
        for s in AudioUtilities.GetAllSessions():
            try:
                vol = s.SimpleAudioVolume
                if s.Process:
                    raw_name = s.Process.name().lower().replace('.exe', '')
                else:
                    raw_name = 'system'
                meter = None
                peak  = 0.0
                try:
                    meter = s._ctl.QueryInterface(IAudioMeterInformation)
                    peak  = meter.GetPeakValue()
                except Exception:
                    pass
                if raw_name not in candidates:
                    candidates[raw_name] = []
                candidates[raw_name].append((peak, vol, meter))
            except Exception:
                pass

        # For each name, pick the session with the highest peak (i.e. the one
        # currently producing audio).  If all peaks are zero, keep the last entry
        # (preserves previous behaviour for silent apps).
        new_vol     = {}
        new_meter   = {}
        new_display = {}
        for raw_name, entries in candidates.items():
            best = max(entries, key=lambda e: e[0])
            peak, vol, meter = best
            new_vol[raw_name]     = vol
            new_display[raw_name] = _friendly_name(raw_name)
            if meter is not None:
                new_meter[raw_name] = meter

        with _session_lock:
            _sessions       = new_vol
            _session_meters = new_meter
            _session_display = new_display
    except Exception:
        pass   # silently skip — next 2s tick will retry

# Apps to hide from the assign list and auto-detection.
# Managed via settings.json — use tray menu to edit without restarting.
def _is_media_app(name: str) -> bool:
    """Return True if this session should be shown/used (not infrastructure)."""
    return name.lower() not in _app_blocklist

def _sessions_refresh_loop():
    """Runs in its own thread — rebuilds session list every 2 seconds."""
    import comtypes
    comtypes.CoInitialize()
    tick = 0
    _last_app_set: frozenset = frozenset()
    while True:
        if AudioUtilities:
            _rebuild_sessions()
            _update_auto_session()
            # Rebuild auto assignments if the app list has changed
            with _session_lock:
                current_app_set = frozenset(k for k in _sessions if _is_media_app(k))
            if current_app_set != _last_app_set:
                _last_app_set = current_app_set
                if _rpi_pots:
                    _rebuild_auto_assignments(_rpi_pots)
            if tick % 5 == 0:
                with _session_lock:
                    names = list(_sessions.keys())
                media_names = [n for n in names if _is_media_app(n)]
                print(f'[Sessions] media={media_names}  auto_assignments={dict(_auto_per_ch)}')
            tick += 1
        time.sleep(2)

def _update_auto_session():
    """Track global loudest session (info only — assignment is round-robin, not loudness-based)."""
    global _auto_last_active, _auto_last_time
    with _session_lock:
        meters = dict(_session_meters)
    best_name, best_peak = '', 0.0
    for name, meter in meters.items():
        if not _is_media_app(name):
            continue
        try:
            peak = meter.GetPeakValue()
            if peak > best_peak:
                best_peak = peak
                best_name = name
        except Exception:
            pass
    if best_peak > 0.01 and best_name:
        _auto_last_active = best_name
        _auto_last_time   = time.time()


def _rebuild_auto_assignments(pots: dict):
    """Assign __auto__ channels to apps in round-robin order.

    - Apps sorted alphabetically by display name (same as UI list).
    - Each app appears on AT MOST ONE channel — no duplicates.
    - Extra auto pots beyond the app count get '' (unassigned).
    - Called whenever the session list changes or pots are reconfigured.
    Updates _auto_per_ch in-place and returns it.

    FIX (v4.4): apps that are already manually pinned to a named channel are
    excluded from the auto pool.  Previously a pinned app would also appear in
    the auto pool, so you could end up with two pots both controlling the same
    app — one named and one auto.
    """
    # Collect apps that are explicitly pinned to a non-auto channel
    pinned_apps: set = {
        app.lower().replace('.exe', '')
        for app in pots.values()
        if app and app not in ('__auto__', '__master__', '')
    }

    with _session_lock:
        available = sorted(
            [k for k in _sessions.keys()
             if _is_media_app(k) and k not in pinned_apps],
            key=lambda n: _session_display.get(n, _friendly_name(n)).lower()
        )

    auto_channels = sorted(
        [ch for ch, app in pots.items() if app == '__auto__'],
        key=lambda c: int(c)
    )

    _auto_per_ch.clear()
    app_iter = iter(available)
    for ch_str in auto_channels:
        try:
            _auto_per_ch[ch_str] = next(app_iter)
        except StopIteration:
            _auto_per_ch[ch_str] = ''   # more pots than apps

    print(f'[Auto] Assignments rebuilt: {dict(_auto_per_ch)}  '
          f'(apps={available}, pinned={sorted(pinned_apps)})')
    return dict(_auto_per_ch)


def _get_auto_session_for_ch(ch_str: str):
    """Return (name, vol) for the __auto__ session assigned to channel ch_str."""
    with _session_lock:
        sessions = dict(_sessions)
    assigned = _auto_per_ch.get(ch_str, '')
    if assigned and assigned in sessions:
        return assigned, sessions[assigned]
    return None, None


# ── Volume operations ─────────────────────────────────────────────────────────

def _get_apps() -> list:
    """Return list of {id, name} for media/game audio sessions only."""
    with _session_lock:
        apps = [{'id': k, 'name': _session_display.get(k, _friendly_name(k))}
                for k in _sessions.keys() if _is_media_app(k)]
    apps.sort(key=lambda x: x['name'])
    return apps

def _get_auto_display_for_ch(ch_str: str) -> str:
    """Return the friendly display name for the auto session on this channel."""
    with _session_lock:
        display = dict(_session_display)
    name, _ = _get_auto_session_for_ch(ch_str)
    if name:
        return display.get(name, _friendly_name(name))
    return 'No app'

def _set_volume_com(app_id: str, pct: int, ch_str: str = '') -> str:
    """Set volume for app_id. Returns resolved app name (for logging). Must be called on CoInitialized thread."""
    if app_id == '__master__':
        if _endpoint:
            try:
                _endpoint.SetMasterVolumeLevelScalar(pct / 100, None)
            except Exception as e:
                print(f'[Vol] Master: {e}')
        return '__master__'

    if app_id == '__auto__':
        name, vol = _get_auto_session_for_ch(ch_str)
        if vol:
            try:
                vol.SetMasterVolume(pct / 100, None)
                with _session_lock:
                    disp = _session_display.get(name, _friendly_name(name)) if name else '__auto__'
                print(f'[Vol] ch{ch_str} __auto__ -> {name} ({disp}): {pct}%')
            except Exception as e:
                print(f'[Vol] __auto__ ({name}): {e}')
        else:
            print('[Vol] __auto__: no active session found')
        return name or '__auto__'

    target = app_id.lower().replace('.exe', '')
    with _session_lock:
        vol = _sessions.get(target)
    if vol is None:
        with _session_lock:
            for k, v in _sessions.items():
                if target in k or k in target:
                    vol = v
                    break
    if vol:
        try:
            vol.SetMasterVolume(pct / 100, None)
        except Exception as e:
            print(f'[Vol] {app_id}: {e}')
    else:
        print(f'[Vol] App not found: {app_id}')
    return app_id

def _get_volumes_com(pots: dict) -> dict:
    """Return {ch: pct} for assigned channels. Skips __auto__ pots — their
    value is set by the physical pot only, never overwritten by vol_sync."""
    result = {}
    for ch_str, app_id in pots.items():
        if not app_id or app_id == '__auto__':
            continue   # auto pots: never sync back from PC, avoids feedback loop
        if app_id == '__master__':
            if _endpoint:
                try:
                    result[ch_str] = round(_endpoint.GetMasterVolumeLevelScalar() * 100)
                except Exception:
                    pass
            continue
        target = app_id.lower().replace('.exe', '')
        with _session_lock:
            vol = _sessions.get(target)
        if vol is None:
            with _session_lock:
                for k, v in _sessions.items():
                    if target in k or k in target:
                        vol = v
                        break
        if vol:
            try:
                result[ch_str] = round(vol.GetMasterVolume() * 100)
            except Exception:
                pass
    return result

# ── Peaks via endpoint meter (lightweight, no loopback process needed for VU) ──
_peaks_cache = {'l': 0.0, 'r': 0.0}
_peaks_lock  = threading.Lock()

def _peaks_loop():
    """Reads stereo peak meter from endpoint, ~30ms."""
    import comtypes
    comtypes.CoInitialize()
    try:
        from pycaw.pycaw import IAudioMeterInformation
        try:
            from pycaw.pycaw import CLSCTX_ALL as CA
        except ImportError:
            from comtypes import CLSCTX_ALL as CA
        devices = AudioUtilities.GetSpeakers()
        meter   = devices.Activate(IAudioMeterInformation._iid_, CA, None
                                    ).QueryInterface(IAudioMeterInformation)
        while True:
            try:
                peak = meter.GetPeakValue()
                with _peaks_lock:
                    _peaks_cache['l'] = peak
                    _peaks_cache['r'] = peak
            except Exception:
                break
            time.sleep(0.03)
    except Exception as e:
        print(f'[Peaks] Meter init failed: {e}')

# ── Loopback FFT (pyaudiowpatch) ──────────────────────────────────────────────
# Runs in a separate thread. Puts FFT frames into a queue consumed by the
# WS broadcaster. If pyaudiowpatch not installed, FFT is disabled gracefully.

# ── FFT WebSocket server (port 5010) ─────────────────────────────────────────
# Runs in a completely separate thread with its own asyncio event loop.
# The RPi opens a second connection to ws://PC_IP:5010 for visualiser frames.
# This means FFT sends NEVER touch the main loop or share the control socket,
# eliminating any possibility of vol_sync or media polls causing stutter.

_fft_q:   queue.Queue = queue.Queue(maxsize=4)
FFT_BINS  = 64
FFT_RATE  = 30   # target frames/sec

_fft_clients: set = set()   # connected FFT websocket clients
_fft_clients_lock = threading.Lock()

def _fft_server_thread():
    """Runs the FFT WebSocket server on its own event loop in its own thread."""
    import websockets.asyncio.server as _ws_server

    async def _fft_handler(websocket):
        print(f'[FFT-WS] Client connected from {websocket.remote_address}')
        with _fft_clients_lock:
            _fft_clients.add(websocket)
        try:
            # Keep connection open; we only send, never receive
            await websocket.wait_closed()
        finally:
            with _fft_clients_lock:
                _fft_clients.discard(websocket)
            print(f'[FFT-WS] Client disconnected')

    async def _fft_broadcaster():
        loop = asyncio.get_event_loop()
        interval = 1.0 / FFT_RATE
        while True:
            t0 = time.monotonic()
            try:
                frame = await loop.run_in_executor(None, lambda: _fft_q.get(timeout=0.1))
                msg = json.dumps({'type': 'fft', **frame})
                with _fft_clients_lock:
                    clients = set(_fft_clients)
                dead = set()
                for ws in clients:
                    try:
                        await ws.send(msg)
                    except Exception:
                        dead.add(ws)
                if dead:
                    with _fft_clients_lock:
                        _fft_clients.difference_update(dead)
            except Exception:
                pass
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _run():
        import websockets
        print(f'[FFT-WS] Listening on ws://0.0.0.0:{PORT_FFT}')
        async with websockets.serve(_fft_handler, '0.0.0.0', PORT_FFT,
                                    ping_interval=20, ping_timeout=15):
            await asyncio.gather(
                asyncio.Future(),   # run forever
                _fft_broadcaster(),
            )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run())

def _fft_thread():
    try:
        import pyaudiowpatch as pa
        import numpy as np
    except ImportError:
        print('[FFT] pyaudiowpatch or numpy not installed — visualiser disabled')
        print('[FFT]   pip install pyaudiowpatch numpy')
        return

    CHUNK  = 1024
    FORMAT = pa.paFloat32
    p      = pa.PyAudio()

    # Find loopback device
    loopback = None
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        default_idx = wasapi['defaultOutputDevice']
        dev_info    = p.get_device_info_by_index(default_idx)
        for loopback in p.get_loopback_device_info_generator():
            if dev_info['name'] in loopback['name']:
                break
        else:
            loopback = None
    except Exception as e:
        print(f'[FFT] Could not find loopback: {e}')

    if loopback is None:
        print('[FFT] No loopback device — visualiser disabled')
        p.terminate()
        return

    channels = int(loopback['maxInputChannels'])
    rate     = int(loopback['defaultSampleRate'])
    print(f'[FFT] Loopback: {loopback["name"]} | {channels}ch @ {rate}Hz')

    import numpy as np
    stream = p.open(
        format=FORMAT, channels=channels, rate=rate, input=True,
        input_device_index=loopback['index'], frames_per_buffer=CHUNK,
    )

    # Rolling buffer for smoother FFT
    buf_len  = CHUNK * 4
    interval = 1.0 / FFT_RATE

    # dB scaling constants
    # REF_LEVEL: FFT magnitude that maps to 1.0 output — tune if bars feel too
    #            tall or too short. Typical WASAPI float32 loopback magnitudes
    #            for moderate listening volume sit around 5–30.
    REF_LEVEL  = 1024.0        # magnitude → 0 dB reference (increase to scale down)
    NOISE_GATE = 1e-4          # bins below this magnitude are zeroed (silence)
    DB_FLOOR   = -60.0         # dB value that maps to 0.0 output
    DB_CEIL    =   0.0         # dB value that maps to 1.0 output
    DB_RANGE   = DB_CEIL - DB_FLOOR   # 60 dB

    # Smooth the peak hold with a fast attack / slow decay envelope
    _smooth = np.zeros(FFT_BINS, dtype=np.float32)
    ATTACK  = 0.8   # how quickly bars rise  (0–1, higher = faster)
    DECAY   = 0.15  # how quickly bars fall  (0–1, lower  = slower)

    # Sideband suppression — attenuate bins that are leakage from a nearby
    # dominant bin. After binning, any bin more than SIDEBAND_DB below the
    # loudest bin within SIDEBAND_RADIUS positions is multiplied by SIDEBAND_ATT.
    SIDEBAND_RADIUS = 3        # bins either side to check for a dominant peak
    SIDEBAND_DB     = 18.0     # dB below local max → suppress
    SIDEBAND_ATT    = 0.15     # suppressed bin multiplier (0 = kill, 1 = keep)

    # Pre-compute Blackman-Harris window — far better sideband rejection than
    # Hanning (-92 dB vs -31 dB), so spectral leakage barely reaches adjacent bins.
    # Normalise so FFT magnitudes stay consistent with REF_LEVEL.
    window  = np.blackman(buf_len).astype(np.float32)
    window /= window.mean()

    # Pre-compute spectral tilt compensation curve.
    # Audio naturally has far more low-frequency energy, and our log binning
    # means low bins cover fewer FFT lines than high bins (so low bins get the
    # full magnitude of 1-2 spectral lines while high bins average over many).
    # This curve applies a gentle dB boost that increases with bin index,
    # counteracting both effects so the display looks perceptually balanced.
    # TILT_DB: total dB boost applied from bin 0 → bin N-1 (linear ramp in dB).
    #          12 dB means the highest bin is boosted 12 dB relative to the lowest.
    TILT_DB   = 16.0
    tilt_curve = np.linspace(0.0, TILT_DB, FFT_BINS, dtype=np.float32)
    # Convert dB ramp to linear multipliers
    tilt_mult  = 10.0 ** (tilt_curve / 20.0)

    buf = np.zeros(buf_len, dtype=np.float32)

    # FIX (v4.3): pre-compute log bin edges once here rather than allocating a
    # new array on every call to _log_bins (30 allocations/sec over days adds up).
    fft_size  = buf_len // 2 + 1
    log_freqs = np.clip(
        np.logspace(0, np.log10(fft_size - 1), FFT_BINS + 1).astype(int),
        0, fft_size - 1,
    )

    while True:
        t0 = time.monotonic()
        try:
            raw  = stream.read(CHUNK, exception_on_overflow=False)
            data = np.frombuffer(raw, dtype=np.float32)

            if channels == 2:
                left  = data[0::2]
                right = data[1::2]
                mono  = (left + right) / 2
                l_peak = float(np.max(np.abs(left)))
                r_peak = float(np.max(np.abs(right)))
            else:
                mono  = data
                l_peak = r_peak = float(np.max(np.abs(data)))

            # FFT — use pre-computed Blackman-Harris window
            buf = np.roll(buf, -CHUNK)
            buf[-CHUNK:] = mono[:CHUNK]
            spectrum  = np.abs(np.fft.rfft(buf * window))

            # Bin into FFT_BINS logarithmically
            bins = _log_bins(spectrum, FFT_BINS, log_freqs)

            # Apply spectral tilt — gently boost highs / cut lows so the
            # display looks balanced rather than bass-heavy
            bins *= tilt_mult

            # Noise gate — zero out bins below the noise floor
            bins[bins < NOISE_GATE] = 0.0

            # Sideband suppression — find bins that are just leakage from a
            # nearby dominant peak and attenuate them.
            # Convert bins to dB for comparison, suppress weak neighbours.
            with np.errstate(divide='ignore'):
                bins_db = 20.0 * np.log10(np.maximum(bins, 1e-12))
            for i in range(FFT_BINS):
                lo = max(0, i - SIDEBAND_RADIUS)
                hi = min(FFT_BINS, i + SIDEBAND_RADIUS + 1)
                local_max_db = np.max(bins_db[lo:hi])
                if bins_db[i] < local_max_db - SIDEBAND_DB:
                    bins[i] *= SIDEBAND_ATT

            # Convert to dB relative to REF_LEVEL, then map to 0–1
            # Avoid log(0): replace zeros with a very small value below DB_FLOOR
            with np.errstate(divide='ignore'):
                db = 20.0 * np.log10(np.maximum(bins / REF_LEVEL, 1e-12))
            normalised = np.clip((db - DB_FLOOR) / DB_RANGE, 0.0, 1.0)

            # Smooth: fast attack, slow decay
            rising  = normalised > _smooth
            _smooth[rising]  = _smooth[rising]  * (1 - ATTACK) + normalised[rising]  * ATTACK
            _smooth[~rising] = _smooth[~rising] * (1 - DECAY)  + normalised[~rising] * DECAY

            with _peaks_lock:
                _peaks_cache['l'] = l_peak
                _peaks_cache['r'] = r_peak

            frame = {'fft': [round(float(x), 3) for x in _smooth],
                     'l': round(l_peak, 3), 'r': round(r_peak, 3)}
            try:
                _fft_q.put_nowait(frame)
            except queue.Full:
                try:
                    _fft_q.get_nowait()
                    _fft_q.put_nowait(frame)
                except Exception:
                    pass

        except Exception as e:
            print(f'[FFT] Read error: {e}')
            break

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, interval - elapsed))

    stream.stop_stream()
    stream.close()
    p.terminate()
    print('[FFT] Thread exited')

def _log_bins(spectrum: 'np.ndarray', n: int,
              freqs: 'np.ndarray' = None) -> 'np.ndarray':
    """Map spectrum into n logarithmically-spaced bins using RMS energy.
    RMS is a true energy measure — fair across both narrow low-freq bins
    (where mean≈max anyway) and wide high-freq bins (where max overcounts).

    FIX (v4.3): accepts a pre-computed freqs array so the caller can allocate
    it once before the FFT loop instead of re-allocating on every frame."""
    import numpy as np
    size  = len(spectrum)
    if freqs is None:
        freqs = np.clip(np.logspace(0, np.log10(size - 1), n + 1).astype(int),
                        0, size - 1)
    bins  = np.array([
        np.sqrt(np.mean(spectrum[freqs[i]:freqs[i+1]+1] ** 2))
        for i in range(n)
    ], dtype=np.float32)
    return bins

# ── Media (winrt / winsdk) ────────────────────────────────────────────────────
_media_state = {
    'title': '', 'artist': '', 'album_art': '',
    'playing': False, 'source': '',
    'position': 0, 'duration': 0,
    'elapsed_str': '0:00', 'duration_str': '0:00',
}
_media_lock = threading.Lock()
_media_changed = threading.Event()

def _fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f'{s//60}:{s%60:02d}'

async def _get_album_art(info) -> str:
    """Read album art thumbnail from media session and return as base64 data URL.

    FIX (v4.3): reads all bytes in a single read_bytes() call instead of a
    per-byte loop, and explicitly closes the DataReader and stream afterwards.
    The per-byte loop was creating thousands of tiny Python objects per image
    load, and the unclosed stream kept a WinRT COM reference alive until the
    next GC cycle — over days this accumulated significantly."""
    import base64
    # Try winsdk first, then winrt
    _backends = []
    try:
        from winsdk.windows.storage.streams import DataReader as _DR_winsdk
        _backends.append(_DR_winsdk)
    except ImportError:
        pass
    try:
        from winrt.windows.storage.streams import DataReader as _DR_winrt
        _backends.append(_DR_winrt)
    except ImportError:
        pass

    for DataReader in _backends:
        try:
            thumb = info.thumbnail
            if thumb is None:
                return ''
            stream = await thumb.open_read_async()
            size   = stream.size
            if size == 0:
                try: stream.close()
                except Exception: pass
                return ''
            dr = DataReader(stream.get_input_stream_at(0))
            try:
                await dr.load_async(size)
                buf = bytes(dr.read_bytes(size))   # single bulk read — no per-byte loop
            finally:
                try: dr.close()
                except Exception: pass
                try: stream.close()
                except Exception: pass
            return f'data:image/jpeg;base64,{base64.b64encode(buf).decode()}'
        except Exception:
            continue
    return ''

async def _media_poll_loop(send_fn):
    """Polls Windows media session and sends updates when changed.

    FIX (v4.3): the Manager COM object is requested once and reused for the
    lifetime of the connection.  Previously a fresh manager + session COM object
    pair was created on every 1-second iteration; WinRT COM objects are not
    promptly released by Python's GC so over days this caused significant
    accumulation.  The manager is only re-requested when an exception occurs
    (e.g. the session becomes temporarily unavailable)."""
    if not IS_WIN:
        return

    # Try winsdk first, fall back to winrt
    Manager = None
    for mod_path in ('winsdk.windows.media.control', 'winrt.windows.media.control'):
        try:
            m = __import__(mod_path, fromlist=['GlobalSystemMediaTransportControlsSessionManager'])
            Manager = m.GlobalSystemMediaTransportControlsSessionManager
            break
        except ImportError:
            continue

    if Manager is None:
        print('[Media] winrt/winsdk not available — media disabled')
        print('[Media]   pip install winrt-Windows.Media.Control')
        return

    last_hash = ''
    manager   = None      # requested once, re-requested only on error
    while True:
        try:
            if manager is None:
                manager = await Manager.request_async()
            session = manager.get_current_session()
            if session:
                info    = await session.try_get_media_properties_async()
                tl      = session.get_timeline_properties()
                pb      = session.get_playback_info()
                title   = info.title or ''
                artist  = info.artist or ''
                playing = pb.playback_status.value == 4
                dur     = tl.end_time.total_seconds() if tl.end_time else 0
                pos     = tl.position.total_seconds() if tl.position else 0
                source  = session.source_app_user_model_id or ''
                new_hash = f'{title}{artist}{playing}{int(pos//5)}'
                if new_hash != last_hash:
                    last_hash = new_hash
                    album_art = await _get_album_art(info)
                    upd = {
                        'title':        title,
                        'artist':       artist,
                        'album_art':    album_art,
                        'playing':      playing,
                        'source':       source.lower(),
                        'duration':     round(dur),
                        'position':     round(pos),
                        'elapsed_str':  _fmt_time(pos),
                        'duration_str': _fmt_time(dur),
                    }
                    with _media_lock:
                        _media_state.update(upd)
                    await send_fn({'type': 'media', 'data': upd})
            else:
                if last_hash != '__none__':
                    last_hash = '__none__'
                    blank = {k: (False if isinstance(v, bool) else
                                 0 if isinstance(v, (int, float)) else '')
                             for k, v in _media_state.items()}
                    with _media_lock:
                        _media_state.update(blank)
                    await send_fn({'type': 'media', 'data': blank})
        except Exception:
            # Session briefly unavailable — drop the cached manager so we get a
            # fresh one next tick rather than hammering a stale COM object.
            manager = None
        await asyncio.sleep(1)

async def _handle_media_cmd(cmd: str):
    """Execute a media command from the RPi."""
    if not IS_WIN:
        return
    try:
        try:
            from winsdk.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as Manager
        except ImportError:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as Manager
        manager = await Manager.request_async()
        session = manager.get_current_session()
        if session is None:
            return
        if cmd == 'play_pause':
            await session.try_toggle_play_pause_async()
        elif cmd == 'next':
            await session.try_skip_next_async()
        elif cmd == 'prev':
            await session.try_skip_previous_async()
        elif cmd == 'stop':
            await session.try_stop_async()
    except Exception as e:
        print(f'[Media] cmd {cmd} failed: {e}')

# ── WebSocket server ───────────────────────────────────────────────────────────

_rpi_ws     = None    # single connection from RPi
_rpi_pots   = {str(i): '' for i in range(8)}   # pot config from RPi

async def _send_rpi(msg: dict):
    ws = _rpi_ws
    if ws is None:
        return
    try:
        await ws.send(json.dumps(msg))
    except Exception:
        pass

async def _handle_rpi_message(msg: str):
    """Handle messages from RPi."""
    global _rpi_pots
    try:
        d = json.loads(msg)
    except Exception:
        return
    t = d.get('type', '')

    if t == 'hello':
        # RPi sends pot config on connect
        _rpi_pots = d.get('pots', _rpi_pots)
        print(f'[RPi] Hello — pots: {_rpi_pots}')
        # Assign __auto__ channels to apps in round-robin order
        _rebuild_auto_assignments(_rpi_pots)
        loop = asyncio.get_event_loop()
        # Send current app list
        apps = await loop.run_in_executor(None, _get_apps) or []
        await _send_rpi({'type': 'apps', 'apps': apps})
        # Send current volumes
        pots_snap = dict(_rpi_pots)
        vols = await loop.run_in_executor(None, lambda: _get_volumes_com(pots_snap)) or {}
        if vols:
            await _send_rpi({'type': 'vol_sync', 'volumes': vols})

    elif t == 'vol':
        # RPi pot moved — set Windows volume
        ch    = d.get('ch', 0)
        pct   = int(d.get('value', 0))
        app   = d.get('app', '').strip()
        ch_str = str(ch)
        print(f'[Vol] Ch{ch} {app} -> {pct}%')
        _com(lambda a=app, p=pct, c=ch_str: _set_volume_com(a, p, c))

    elif t == 'pot_config':
        _rpi_pots = d.get('pots', _rpi_pots)
        # Reassign auto channels with updated pot config
        _rebuild_auto_assignments(_rpi_pots)
        loop = asyncio.get_event_loop()
        pots_snap = dict(_rpi_pots)
        vols = await loop.run_in_executor(None, lambda: _get_volumes_com(pots_snap)) or {}
        if vols:
            await _send_rpi({'type': 'vol_sync', 'volumes': vols})

    elif t == 'media_cmd':
        await _handle_media_cmd(d.get('cmd', ''))

    elif t == 'get_apps':
        loop = asyncio.get_event_loop()
        apps = await loop.run_in_executor(None, _get_apps) or []
        await _send_rpi({'type': 'apps', 'apps': apps})

    elif t == 'pong':
        pass

async def _rpi_handler(websocket):
    global _rpi_ws
    print(f'[WS] RPi connected from {websocket.remote_address}')
    _rpi_ws = websocket

    # Kick off media polling and vol sync for this connection
    # FFT is sent independently on port 5010 via _fft_server_thread
    media_task = asyncio.create_task(_media_poll_loop(_send_rpi))
    vol_task   = asyncio.create_task(_vol_sync_loop())

    try:
        async for msg in websocket:
            await _handle_rpi_message(msg)
    except Exception as e:
        print(f'[WS] RPi disconnected: {e}')
    finally:
        _rpi_ws = None
        media_task.cancel()
        vol_task.cancel()
        print('[WS] RPi disconnected')

async def _vol_sync_loop():
    """Every 5s, push current Windows volumes to RPi for display sync.
    Auto pots are intentionally excluded — they are write-only from the pot.
    All COM calls run in an executor so the event loop (and FFT broadcaster) are never blocked."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(5)
        if not _rpi_pots:
            continue
        pots_snap = dict(_rpi_pots)
        vols = await loop.run_in_executor(None, lambda: _get_volumes_com(pots_snap))
        if vols:
            await _send_rpi({'type': 'vol_sync', 'volumes': vols})
        # Push updated app list
        apps = await loop.run_in_executor(None, _get_apps)
        if apps is not None:
            await _send_rpi({'type': 'apps', 'apps': apps})
        # Push per-channel display names for auto pots
        auto_display = {}
        for ch, app in pots_snap.items():
            if app == '__auto__':
                disp = _get_auto_display_for_ch(ch)
                if disp:
                    auto_display[ch] = disp
        if auto_display:
            await _send_rpi({'type': 'pot_display', 'data': auto_display})

# ── System tray ───────────────────────────────────────────────────────────────

def _make_icon():
    try:
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.ellipse([2, 2, 62, 62], fill=(13, 16, 23), outline=(0, 229, 255), width=2)
        for r in [22, 16, 10]:
            d.ellipse([32-r, 32-r, 32+r, 32+r], outline=(0, 100, 130), width=1)
        d.ellipse([27, 27, 37, 37], fill=(0, 229, 255))
        return img
    except Exception:
        return None

def _tk_input(prompt: str, title: str, default: str = '') -> str:
    """Single-line Tkinter input dialog. Returns '' if cancelled."""
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    result = simpledialog.askstring(title, prompt, initialvalue=default, parent=root)
    root.destroy()
    return result or ''


def _tk_multiline_dialog(title: str, label: str, initial: str) -> str | None:
    """Scrollable multi-line text editor dialog.
    Returns the edited text, or None if cancelled."""
    import tkinter as tk
    from tkinter import ttk

    result_holder = [None]

    root = tk.Tk()
    root.title(title)
    root.resizable(True, True)
    root.attributes('-topmost', True)
    root.minsize(520, 380)

    # Darken to match the app aesthetic
    BG, FG, ACCENT = '#111520', '#c8d8f0', '#00e5ff'
    root.configure(bg=BG)

    tk.Label(root, text=label, bg=BG, fg=FG, wraplength=500,
             justify='left', font=('Segoe UI', 9)).pack(padx=14, pady=(12, 6), anchor='w')

    frame = tk.Frame(root, bg=BG)
    frame.pack(fill='both', expand=True, padx=14, pady=(0, 6))

    scrollbar = tk.Scrollbar(frame)
    scrollbar.pack(side='right', fill='y')

    text = tk.Text(frame, height=18, width=60, bg='#0a0c10', fg=FG,
                   insertbackground=ACCENT, selectbackground='#1e2535',
                   relief='flat', font=('Consolas', 10),
                   yscrollcommand=scrollbar.set)
    text.pack(side='left', fill='both', expand=True)
    scrollbar.config(command=text.yview)
    text.insert('1.0', initial)

    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(fill='x', padx=14, pady=(0, 12))

    def on_save():
        result_holder[0] = text.get('1.0', 'end-1c')
        root.destroy()

    def on_cancel():
        root.destroy()

    tk.Button(btn_frame, text='Save', command=on_save, bg=ACCENT, fg='#0a0c10',
              font=('Segoe UI', 9, 'bold'), relief='flat', padx=16).pack(side='right', padx=(4, 0))
    tk.Button(btn_frame, text='Cancel', command=on_cancel, bg='#1e2535', fg=FG,
              font=('Segoe UI', 9), relief='flat', padx=16).pack(side='right')

    root.mainloop()
    return result_holder[0]


def _edit_blocklist_dialog():
    """Scrollable editor for the blocklist.
    Shows detected session names at the top, one blocked name per line in the text box.
    """
    with _session_lock:
        all_detected = sorted(k for k in _sessions.keys() if k)

    current_blocked = sorted(_app_blocklist - {''})
    initial = '\n'.join(current_blocked)

    detected_str = '  '.join(all_detected) if all_detected else '(none detected yet)'
    label = f'Detected sessions: {detected_str}\n\nOne process name per line to block. Case-insensitive.'

    raw = _tk_multiline_dialog('Edit Blocklist', label, initial)
    if raw is None:
        return  # cancelled

    new_entries = [e.strip().lower() for e in raw.splitlines()]
    new_entries = [e for e in new_entries if e]
    new_entries.append('')  # always block unnamed sessions

    settings['blocklist'] = new_entries
    save_settings(settings)
    _reload_lists()
    print(f'[Settings] Blocklist updated: {new_entries}')


def _edit_known_names_dialog():
    """Scrollable editor for the process → display name map.
    One entry per line in the format:  processname=Display Name
    """
    current = dict(_known_names)

    with _session_lock:
        all_detected = sorted(_sessions.keys())
    unmapped = [n for n in all_detected if n and n not in current and _is_media_app(n)]

    current_str = '\n'.join(f'{k}={v}' for k, v in sorted(current.items()))
    unmapped_str = '  '.join(unmapped) if unmapped else 'none'
    label = (f'Unmapped detected apps: {unmapped_str}\n\n'
             f'Format: processname=Display Name   (one per line)')

    raw = _tk_multiline_dialog('Edit App Names', label, current_str)
    if raw is None:
        return  # cancelled

    new_known: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if '=' in line:
            k, _, v = line.partition('=')
            k, v = k.strip().lower(), v.strip()
            if k and v:
                new_known[k] = v

    settings['known'] = new_known
    save_settings(settings)
    _reload_lists()
    with _session_lock:
        for name in _sessions:
            _session_display[name] = _friendly_name(name)
    print(f'[Settings] Known names updated: {new_known}')


def _tray_main(loop=None):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print('[Tray] pystray/Pillow not installed — no tray icon')
        return

    icon_img = _make_icon()
    if icon_img is None:
        return

    def set_ip(icon, item):
        ip = _tk_input('Enter RPi IP address:', 'RPi Audio Console',
                       settings.get('rpi_ip', ''))
        if ip:
            settings['rpi_ip'] = ip
            save_settings(settings)
            print(f'[Tray] RPi IP set to {ip}')

    def edit_blocklist(icon, item):
        threading.Thread(target=_edit_blocklist_dialog, daemon=True).start()

    def edit_known_names(icon, item):
        threading.Thread(target=_edit_known_names_dialog, daemon=True).start()

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    tray = pystray.Icon(
        'rpi_console', icon_img, 'RPi Audio Console',
        menu=pystray.Menu(
            pystray.MenuItem('Set Pi IP',       set_ip),
            pystray.MenuItem('Edit Blocklist',  edit_blocklist),
            pystray.MenuItem('Edit App Names',  edit_known_names),
            pystray.MenuItem('Exit',            quit_app),
        )
    )
    tray.run()

# ── Main ──────────────────────────────────────────────────────────────────────

async def _main():
    import websockets

    # ── Scheduled soft-restart (memory safety net) ────────────────────────────
    # Even with the v4.3 leak fixes, a periodic restart is a belt-and-suspenders
    # measure against any residual drift from WinRT/COM objects that Python's GC
    # doesn't promptly collect.  48 h is chosen so it never fires during typical
    # daytime use but keeps multi-day RSS under control.
    #
    # FIX: os.execv does not work for PyInstaller frozen exes — the process
    # unpacks to a _MEI temp dir that is deleted before execv can re-execute it.
    # Instead: frozen exe uses os.startfile (Windows launches a fresh copy then
    # this one exits); plain Python script still uses os.execv.
    RESTART_INTERVAL_H = 48

    def _soft_restart_thread():
        time.sleep(RESTART_INTERVAL_H * 3600)
        print(f'[Bridge] Scheduled soft-restart after {RESTART_INTERVAL_H}h')
        try:
            if getattr(sys, 'frozen', False):
                # Running as a PyInstaller .exe — launch a new copy then exit
                import subprocess
                subprocess.Popen([sys.executable] + sys.argv[1:],
                                 creationflags=0x00000008)  # DETACHED_PROCESS
                time.sleep(2)   # give the new process time to start
                os._exit(0)
            else:
                # Running as a plain Python script — replace process in-place
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f'[Bridge] Restart failed: {e} — exiting so OS can relaunch')
            os._exit(0)

    threading.Thread(target=_soft_restart_thread, daemon=True,
                     name='soft-restart').start()

    # Start COM thread
    com_thread = threading.Thread(target=_com_thread_main, daemon=True, name='com')
    com_thread.start()
    _com_ok.wait(timeout=5)

    # Session refresh and peaks run in their own threads (each calls CoInitialize)
    threading.Thread(target=_sessions_refresh_loop, daemon=True, name='sessions').start()
    threading.Thread(target=_peaks_loop,            daemon=True, name='peaks').start()

    # Capture the main event loop reference for use by the FFT send thread
    # Start FFT producer thread + dedicated FFT WebSocket server (port 5010, own loop)
    threading.Thread(target=_fft_thread,        daemon=True, name='fft').start()
    threading.Thread(target=_fft_server_thread, daemon=True, name='fft-ws').start()

    # WebSocket control server (port 5009)
    print(f'[WS] Listening on ws://0.0.0.0:{PORT}')
    async with websockets.serve(
        _rpi_handler, '0.0.0.0', PORT,
        ping_interval=20, ping_timeout=15,
        max_size=2**20,
    ):
        await asyncio.Future()   # run forever

def main():
    threading.Thread(target=_tray_main, args=(None,), daemon=True, name='tray').start()

    print('RPi Audio Console — PC Bridge')
    print(f'  Control: ws://0.0.0.0:{PORT}')
    print(f'  FFT:     ws://0.0.0.0:{PORT_FFT}')
    print(f'  RPi should connect to ws://<this-pc-ip>:{PORT} and :{PORT_FFT}')
    print(f'  Config: {SETTINGS}')

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print('\n[Bridge] Shutting down')

if __name__ == '__main__':
    main()
