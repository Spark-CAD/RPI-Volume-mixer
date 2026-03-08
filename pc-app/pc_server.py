#!/usr/bin/env python3
"""
RPi Audio Console — PC Backend Server  (v2)
Runs on your Windows PC on port 5001.

Requirements:
    pip install flask flask-cors psutil requests pycaw pillow pystray
    pip install winrt-Windows.Media.Control
    pip install pyaudiowpatch   # for true stereo L/R loopback metering

Architecture notes
──────────────────
The main thread is owned entirely by pystray (Win32 message pump).
All COM/pycaw work happens on dedicated background threads that each call
CoInitializeEx(STA) independently. The main thread never imports comtypes.

Media info (winsdk/winrt) runs in a subprocess to avoid any COM apartment
conflict with pycaw. Each subprocess has its own clean COM state.
"""

import json, os, queue, subprocess, sys, threading, time
import faulthandler, traceback
if sys.stderr is not None:
    faulthandler.enable()  # dump Python traceback on native crash (only when stderr exists)
import psutil
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# ── Frozen-exe helpers ────────────────────────────────────────────────────────
def _python_exe():
    """Return a usable Python interpreter path.
    When running as a PyInstaller frozen exe, sys.executable points to the .exe
    itself — running it with -c would re-launch the whole app (infinite loop).
    We walk PATH to find a real python.exe instead."""
    if not getattr(sys, 'frozen', False):
        return sys.executable          # normal script run — use current interpreter
    for candidate in ('python.exe', 'python3.exe', 'python.exe'):
        for directory in os.environ.get('PATH', '').split(os.pathsep):
            full = os.path.join(directory, candidate)
            if os.path.isfile(full):
                return full
    # Last resort: check common install locations
    for path in (
        r'C:\Python312\python.exe', r'C:\Python311\python.exe',
        r'C:\Python310\python.exe', r'C:\Python39\python.exe',
        os.path.expanduser(r'~\AppData\Local\Programs\Python\Python312\python.exe'),
        os.path.expanduser(r'~\AppData\Local\Programs\Python\Python311\python.exe'),
        os.path.expanduser(r'~\AppData\Local\Programs\Python\Python310\python.exe'),
    ):
        if os.path.isfile(path):
            return path
    return 'python.exe'  # fallback — will fail gracefully if not found

try:
    import requests as _req
except ImportError:
    _req = None

# ── Platform ───────────────────────────────────────────────────────────────
IS_WIN = sys.platform == 'win32'

# NOTE: We do NOT call CoInitializeEx on the main thread.
# The main thread runs pystray which calls CoInitializeEx(STA) itself internally.
# Calling it here first (even as STA) can conflict depending on comtypes version.
# All pycaw/comtypes work is isolated to background threads.

def _hidden_popen(cmd):
    """Run a shell command with no visible window on Windows."""
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return subprocess.Popen(cmd, shell=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── Optional Windows audio/media imports ──────────────────────────────────
_pycaw_ok  = False
_winrt_ok  = False

if IS_WIN:
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume, IAudioMeterInformation, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        _pycaw_ok = True
        print('[Audio] pycaw available — per-app volume control enabled')

        # ── Neutralise comtypes GC finalizers ─────────────────────────────
        # comtypes COM pointer objects call Release() in __del__. When the
        # GC fires on a thread with no COM apartment (or the wrong one) this
        # causes a native access violation that kills the process — it fires
        # BEFORE Python's exception handling can intercept it, so try/except
        # is useless. The only fix is to make __del__ a complete no-op so
        # Release() is never called from the GC at all.
        # COM objects created in a proper STA are released when that thread
        # exits anyway, so skipping __del__ here is safe.
        import comtypes._post_coinit.unknwn as _ct_unknwn
        _ct_unknwn._compointer_base.__del__ = lambda self: None
    except ImportError:
        print('[Audio] pycaw not found. Run: pip install pycaw')

    try:
        # Just test the import — actual calls happen in subprocess
        import importlib.util
        if importlib.util.find_spec('winrt') or importlib.util.find_spec('winsdk'):
            _winrt_ok = True
            print('[Media] winrt available — real media info enabled')
        else:
            print('[Media] winrt not found. Run: pip install winrt-Windows.Media.Control')
    except Exception:
        pass

    try:
        import importlib.util
        if importlib.util.find_spec('pyaudiowpatch'):
            print('[Peaks] pyaudiowpatch available — true stereo L/R loopback enabled')
        else:
            print('[Peaks] pyaudiowpatch not found — using IAudioMeterInformation fallback')
            print('[Peaks]   For true stereo metering: pip install pyaudiowpatch')
    except Exception:
        pass

# ── Config ────────────────────────────────────────────────────────────────
PORT         = 5001
CONFIG_DIR   = os.path.expanduser('~/.rpi_console')
LAUNCH_FILE  = os.path.join(CONFIG_DIR, 'launch_config.json')
POT_FILE     = os.path.join(CONFIG_DIR, 'pot_config.json')
SETTINGS_FILE= os.path.join(CONFIG_DIR, 'settings.json')
PRIORITY_FILE= os.path.join(CONFIG_DIR, 'app_priority.json')
os.makedirs(CONFIG_DIR, exist_ok=True)

def _res(filename):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)

UI_HTML = _res('pc_ui.html')

# ── Shared state ──────────────────────────────────────────────────────────
_state_lock = threading.Lock()
state = {
    'rpi_ip':     '',
    'pot_values': [-1] * 8,
    'pot_config': {str(i): '' for i in range(8)},
    '_user_pot_config': {str(i): '' for i in range(8)},
    'peaks': {'l': 0.0, 'r': 0.0, 'master': 0.0, 'sessions': {}},
    'media': {
        'title': '', 'artist': '', 'playing': False, 'source': '',
        'duration': 0, 'position': 0, 'position_pct': 0,
        'elapsed_str': '0:00', 'duration_str': '0:00',
    },
    'connected_at': None,
    'last_ping':    None,
}

_media_lock = threading.Lock()

app = Flask(__name__)
CORS(app)

# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _load_json(path, default=None):
    if default is None:
        default = {}
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_json(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f'[Config] Save error {path}: {e}')

def _fmt_time(s):
    s = max(0, int(s))
    return f'{s // 60}:{s % 60:02d}'

def _notify_rpi(endpoint, payload):
    ip = state.get('rpi_ip', '')
    if not ip or not _req:
        return
    try:
        _req.post(f'http://{ip}:5000{endpoint}', json=payload, timeout=2)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════
# MEDIA — subprocess helper
# ══════════════════════════════════════════════════════════════════════════

_MEDIA_SCRIPT = r"""
import sys, asyncio, json

async def fetch():
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MM)
    except ImportError:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MM)
    sessions = await MM.request_async()
    cur = sessions.get_current_session()
    if not cur:
        print('null'); return
    info = await cur.try_get_media_properties_async()
    tl   = cur.get_timeline_properties()
    pb   = cur.get_playback_info()
    try:
        src = cur.source_app_user_model_id or ''
        src = src.split('!')[0].split('.')[0].title()
    except Exception:
        src = ''
    print(json.dumps({
        'title':    info.title  or '',
        'artist':   info.artist or '',
        'playing':  pb.playback_status.value == 4,
        'source':   src,
        'duration': tl.end_time.total_seconds()  if tl.end_time  else 0,
        'position': tl.position.total_seconds()  if tl.position  else 0,
    }))

asyncio.run(fetch())
"""

_media_timeout_streak = 0
_media_backoff_until  = 0.0

def _get_media():
    """Fetch media info via subprocess. Returns dict or None."""
    global _media_timeout_streak, _media_backoff_until
    if not _winrt_ok:
        return None
    now = time.monotonic()
    if now < _media_backoff_until:
        return None
    # Backoff window has expired — reset streak so it doesn't accumulate forever
    if _media_backoff_until > 0 and now >= _media_backoff_until:
        _media_timeout_streak = 0
        _media_backoff_until  = 0.0
    try:
        r = subprocess.run(
            [_python_exe(), '-c', _MEDIA_SCRIPT],
            capture_output=True, text=True, timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        )
        out = r.stdout.strip()
        if r.returncode != 0 and r.stderr.strip():
            # Only log novel errors, not repeated NPSMSvc noise
            err = r.stderr.strip().splitlines()[-1][:120]
            if 'NullReferenceException' not in err:
                print(f'[Media] subprocess: {err}')
        if not out or out == 'null':
            _media_timeout_streak = 0
            return None
        _media_timeout_streak = 0
        return json.loads(out)
    except subprocess.TimeoutExpired:
        _media_timeout_streak += 1
        if _media_timeout_streak >= 3:
            # Cap backoff at 30s; after it expires the streak resets (above)
            wait = min(30, _media_timeout_streak * 5)
            print(f'[Media] NPSMSvc unresponsive — backing off {wait}s')
            _media_backoff_until = time.monotonic() + wait
        return None
    except Exception as e:
        print(f'[Media] error: {e!r}')
        return None

def _bg_media_loop():
    local_pos = 0.0
    last_tick = time.monotonic()
    while True:
        try:
            now     = time.monotonic()
            elapsed = now - last_tick
            last_tick = now

            with _media_lock:
                playing  = state['media']['playing']
                dur      = state['media']['duration']
                if playing and dur > 0:
                    local_pos = min(dur, local_pos + elapsed)
                state['media']['position']     = local_pos
                state['media']['position_pct'] = round(local_pos / dur * 100, 1) if dur else 0
                state['media']['elapsed_str']  = _fmt_time(local_pos)
                state['media']['duration_str'] = _fmt_time(dur)

            time.sleep(1.0)

            info = _get_media()
            if info:
                with _media_lock:
                    new_title = info['title']
                    new_pos   = info['position']
                    # Snap position on track change or >3s drift
                    if new_title != state['media']['title'] or abs(new_pos - local_pos) > 3:
                        local_pos = new_pos
                    if not info['playing']:
                        local_pos = new_pos
                    state['media'].update({
                        'title':    new_title,
                        'artist':   info['artist'],
                        'playing':  info['playing'],
                        'source':   info['source'],
                        'duration': info['duration'],
                    })
        except Exception as e:
            print(f'[Media] loop error: {e!r}')
            time.sleep(1.0)

# ══════════════════════════════════════════════════════════════════════════
# VOLUME CONTROL — pycaw  (runs on Flask/background threads, not main thread)
# ══════════════════════════════════════════════════════════════════════════

def _coinit():
    """Initialize COM as STA on the calling thread. Safe to call multiple times."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        except Exception:
            pass

def _set_master_volume(pct):
    if not IS_WIN:
        return False
    pct = max(0, min(100, int(pct)))
    try:
        if _pycaw_ok:
            devices = AudioUtilities.GetSpeakers()
            iface   = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            iface.QueryInterface(IAudioEndpointVolume).SetMasterVolumeLevelScalar(pct / 100.0, None)
            return True
        else:
            import ctypes
            v = int(pct / 100.0 * 0xFFFF)
            ctypes.windll.winmm.waveOutSetVolume(None, v | (v << 16))
            return True
    except Exception as e:
        print(f'[Volume] Master error: {e}')
        return False

def _get_master_volume():
    if not IS_WIN or not _pycaw_ok:
        return None
    try:
        devices = AudioUtilities.GetSpeakers()
        iface   = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return round(iface.QueryInterface(IAudioEndpointVolume).GetMasterVolumeLevelScalar() * 100)
    except Exception as e:
        print(f'[Volume] GetMaster error: {e}')
        return None

def _set_app_volume(app_name, pct, silent=False):
    if not _pycaw_ok or not app_name:
        return False
    pct = max(0, min(100, int(pct)))
    try:
        matched = False
        for s in AudioUtilities.GetAllSessions():
            if s.Process and app_name.lower() in s.Process.name().lower():
                s._ctl.QueryInterface(ISimpleAudioVolume).SetMasterVolume(pct / 100.0, None)
                if not silent:
                    print(f'[Volume] {s.Process.name()} -> {pct}%')
                matched = True
        return matched
    except Exception as e:
        print(f'[Volume] App error {app_name}: {e}')
        return False

def _get_app_volume(app_name):
    if not _pycaw_ok or not app_name:
        return None
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and app_name.lower() in s.Process.name().lower():
                return round(s._ctl.QueryInterface(ISimpleAudioVolume).GetMasterVolume() * 100)
    except Exception:
        pass
    return None

def _get_all_volumes(pot_config):
    result = {}
    for ch, app_id in pot_config.items():
        if not app_id or app_id == '__auto__':
            continue
        if app_id.lower() == 'master':
            v = _get_master_volume()
            if v is not None:
                result[ch] = v
        else:
            v = _get_app_volume(app_id)
            if v is not None:
                result[ch] = v
    return result

def _send_media_key(action):
    if not IS_WIN:
        return
    VK = {'play_pause': 0xB3, 'next': 0xB0, 'prev': 0xB1, 'stop': 0xB2}
    vk = VK.get(action)
    if not vk:
        return
    try:
        import ctypes
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
        print(f'[Media] Key sent: {action}')
    except Exception as e:
        print(f'[Media] Key error: {e}')

# ══════════════════════════════════════════════════════════════════════════
# AUDIO PEAKS — WASAPI loopback capture for true stereo L/R
# ══════════════════════════════════════════════════════════════════════════

# WASAPI loopback subprocess: captures ~80ms of PCM from the default render
# endpoint in loopback mode, computes RMS for L and R channels independently,
# and prints JSON. This is the only reliable way to get true stereo L/R on
# Windows — IAudioMeterInformation on the endpoint reports the same mixed
# value for both channels on most consumer hardware.
_LOOPBACK_SCRIPT = r"""
import sys, json, math, struct, ctypes
from ctypes import wintypes

CLSCTX_ALL = 0x17
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_SHAREMODE_SHARED = 0

# COM GUIDs
IID_IMMDeviceEnumerator = '{A95664D2-9614-4F35-A746-DE8DB63617E6}'
IID_IAudioClient        = '{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}'
IID_IAudioCaptureClient = '{C8ADBD64-E71E-48A0-A4DE-185C395CD317}'
CLSID_MMDeviceEnumerator = '{BCDE0395-E52F-467C-8E3D-C4579291692E}'

try:
    import comtypes.client as cc
    import comtypes
    from comtypes import GUID

    cc.GetModule('mmdevapi')
except Exception:
    pass

try:
    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    # Find default loopback device
    default_speakers = p.get_default_wasapi_loopback()
    CHUNK  = int(default_speakers['defaultSampleRate'] * 0.08)  # 80ms
    RATE   = int(default_speakers['defaultSampleRate'])
    CHANS  = min(2, int(default_speakers['maxInputChannels']))

    stream = p.open(
        format=pyaudio.paFloat32,
        channels=CHANS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
        input_device_index=default_speakers['index'],
    )
    raw = stream.read(CHUNK, exception_on_overflow=False)
    stream.stop_stream(); stream.close(); p.terminate()

    samples = struct.unpack(f'{len(raw)//4}f', raw)
    if CHANS >= 2:
        l_samp = samples[0::2]
        r_samp = samples[1::2]
        l_rms  = math.sqrt(max(sum(x*x for x in l_samp) / len(l_samp), 0))
        r_rms  = math.sqrt(max(sum(x*x for x in r_samp) / len(r_samp), 0))
    else:
        rms = math.sqrt(max(sum(x*x for x in samples) / len(samples), 0))
        l_rms = r_rms = rms

    # Convert RMS to approximate peak-equivalent (RMS * sqrt(2) for sine, clamp to 1)
    l_pk = min(1.0, l_rms * 2.5)
    r_pk = min(1.0, r_rms * 2.5)
    print(json.dumps({'l': round(l_pk, 4), 'r': round(r_pk, 4), 'ok': True}))
except Exception as e:
    print(json.dumps({'l': 0.0, 'r': 0.0, 'ok': False, 'err': str(e)}))
"""

_loopback_ok  = False   # set True once first successful loopback result comes back
_loopback_bad = 0       # consecutive failure count

def _bg_peaks_loop():
    _coinit()
    global _loopback_ok, _loopback_bad

    FAST      = 0.06   # 60ms poll for meter fallback
    _meter    = None

    def _get_meter():
        nonlocal _meter
        try:
            from ctypes import cast, POINTER
            import ctypes
            devices = AudioUtilities.GetSpeakers()
            iface   = devices.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            _meter  = cast(iface, POINTER(IAudioMeterInformation))
        except Exception:
            _meter = None

    def _loopback_peaks():
        """Run loopback subprocess, return (l, r) floats or None on failure."""
        global _loopback_ok, _loopback_bad
        try:
            r = subprocess.run(
                [_python_exe(), '-c', _LOOPBACK_SCRIPT],
                capture_output=True, text=True, timeout=2,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0
            )
            out = r.stdout.strip()
            if not out:
                raise ValueError('no output')
            data = json.loads(out)
            if data.get('ok'):
                _loopback_ok  = True
                _loopback_bad = 0
                return data['l'], data['r']
            else:
                raise ValueError(data.get('err', 'failed'))
        except Exception as e:
            _loopback_bad += 1
            if _loopback_bad == 1:
                print(f'[Peaks] Loopback unavailable ({e}) — using IAudioMeterInformation fallback')
            return None

    # ── Loopback thread: runs every ~100ms, writes shared values ──────────
    _lb_vals = [0.0, 0.0]   # [l, r] written by loopback thread

    def _loopback_thread():
        while True:
            result = _loopback_peaks()
            if result:
                _lb_vals[0], _lb_vals[1] = result
            time.sleep(0.10)

    if IS_WIN:
        threading.Thread(target=_loopback_thread, daemon=True).start()

    while True:
        time.sleep(FAST)
        if not _pycaw_ok:
            continue
        try:
            import ctypes

            # ── Per-session peaks (for session dict + meter fallback L/R) ──
            sess   = {}
            m_l    = 0.0
            m_r    = 0.0
            try:
                for s in AudioUtilities.GetAllSessions():
                    if not s.Process:
                        continue
                    name = s.Process.name().lower()
                    try:
                        meter = s._ctl.QueryInterface(IAudioMeterInformation)
                        n = meter.GetMeteringChannelCount()
                        if n >= 2:
                            buf = (ctypes.c_float * n)()
                            meter.GetChannelsPeakValues(n, buf)
                            sl = float(buf[0])
                            sr = float(buf[1])
                        else:
                            pk = float(meter.GetPeakValue())
                            sl = sr = pk
                        sess[name] = max(sess.get(name, 0.0), max(sl, sr))
                        m_l = max(m_l, sl)
                        m_r = max(m_r, sr)
                    except Exception:
                        try:
                            pk = float(s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
                            sess[name] = max(sess.get(name, 0.0), pk)
                            m_l = max(m_l, pk)
                            m_r = max(m_r, pk)
                        except Exception:
                            pass
            except Exception:
                pass
            state['peaks']['sessions'] = {k: round(v, 3) for k, v in sess.items()}

            # ── Choose best L/R source ─────────────────────────────────────
            # Priority: loopback PCM (true stereo) > endpoint meter > session max
            lb_l, lb_r = _lb_vals[0], _lb_vals[1]
            if _loopback_ok and (lb_l + lb_r) > 0.0:
                l, r = lb_l, lb_r
            else:
                # Endpoint meter fallback
                ep_l = ep_r = 0.0
                if _meter is None:
                    _get_meter()
                if _meter:
                    try:
                        n   = _meter.GetMeteringChannelCount()
                        buf = (ctypes.c_float * n)()
                        _meter.GetChannelsPeakValues(n, buf)
                        ep_l = float(buf[0]) if n >= 1 else 0.0
                        ep_r = float(buf[1]) if n >= 2 else ep_l
                    except Exception:
                        _meter = None
                if max(ep_l, ep_r) > 0.001:
                    l, r = ep_l, ep_r
                else:
                    l, r = m_l, m_r

            state['peaks']['l']      = round(l, 3)
            state['peaks']['r']      = round(r, 3)
            state['peaks']['master'] = round(max(l, r), 3)

        except Exception:
            _meter = None

# ══════════════════════════════════════════════════════════════════════════
# AUTO-DETECT — assign apps to __auto__ channels
# ══════════════════════════════════════════════════════════════════════════

_BLOCKLIST = {
    'explorer.exe','svchost.exe','lsass.exe','csrss.exe','winlogon.exe',
    'dwm.exe','taskmgr.exe','conhost.exe','dllhost.exe','sihost.exe',
    'runtimebroker.exe','shellexperiencehost.exe','searchindexer.exe',
    'searchhost.exe','searchapp.exe','startmenuexperiencehost.exe',
    'textinputhost.exe','applicationframehost.exe','systemsettings.exe',
    'fontdrvhost.exe','audiodg.exe','wmiprvse.exe','ctfmon.exe',
    'python.exe','python3.exe','pythonw.exe','java.exe','javaw.exe',
    'node.exe','cmd.exe','powershell.exe','pwsh.exe','rundll32.exe',
    'msmpeng.exe','nissrv.exe','mssense.exe','securityhealthservice.exe',
    'nvcontainer.exe','onedrive.exe','dropbox.exe','googledrivefs.exe',
}

def _is_user_app(name):
    nl = name.lower().strip()
    if nl in _BLOCKLIST:
        return False
    import re
    if re.search(r'(service|daemon|agent|helper|updater|installer|runtime|host|broker'
                 r'|notif|telemetry|crash|report|update)', nl):
        return False
    return True

_auto_state    = {'current_app': '', 'display_name': '', 'per_channel': {}}
_last_pushed   = {}
_auto_last_pct = {}
_app_priority  = {}
_priority_saved_at = 0.0

def _load_priority():
    global _app_priority
    _app_priority = _load_json(PRIORITY_FILE, {})

def _save_priority():
    global _priority_saved_at
    if time.monotonic() - _priority_saved_at < 30:
        return
    _save_json(PRIORITY_FILE, _app_priority)
    _priority_saved_at = time.monotonic()

def _get_session_apps():
    if not _pycaw_ok:
        return []
    try:
        fg_pid = None
        if IS_WIN:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            buf  = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(buf))
            fg_pid = buf.value

        found, seen = [], set()
        for s in AudioUtilities.GetAllSessions():
            proc = s.Process
            if not proc or not _is_user_app(proc.name()):
                continue
            nl = proc.name().lower()
            if nl in seen:
                continue
            seen.add(nl)
            try:
                pk = float(s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
            except Exception:
                pk = 0.0
            display = proc.name().replace('.exe','').replace('.EXE','') \
                                 .replace('-',' ').replace('_',' ').title()
            found.append((proc.name(), display, pk, proc.pid == fg_pid))
        found.sort(key=lambda x: (x[2], x[3]), reverse=True)
        return [(n, d, p) for n, d, p, _ in found]
    except Exception as e:
        print(f'[Auto] Session scan error: {e}')
        return []

_push_queue: queue.Queue = queue.Queue(maxsize=1)

def _push_worker():
    while True:
        try:
            item = _push_queue.get()
            if item is None:
                break
            rpi_ip, payload = item
            if _req:
                try:
                    _req.post(f'http://{rpi_ip}:5000/api/pot_display', json=payload, timeout=2)
                except Exception:
                    pass
        except Exception:
            pass

threading.Thread(target=_push_worker, daemon=True).start()

def _push_names(cfg, per_ch):
    global _last_pushed
    rpi_ip = state.get('rpi_ip', '')
    if not rpi_ip or not _req:
        return
    display = {ch: per_ch.get(ch, {}).get('app', '')
               for ch, app in cfg.items() if app == '__auto__'}
    if display == _last_pushed:
        return
    _last_pushed = dict(display)
    print(f'[Auto] Display names changed: {display}')
    try: _push_queue.get_nowait()
    except queue.Empty: pass
    try: _push_queue.put_nowait((rpi_ip, display))
    except queue.Full: pass

def _bg_auto_detect_loop():
    _coinit()
    TICK = 0.5
    while True:
        time.sleep(TICK)
        try:
            cfg = state.get('pot_config', {})
            auto_chs = sorted([c for c, a in cfg.items() if a == '__auto__'], key=int)
            if not auto_chs:
                _auto_state.update({'current_app': '', 'display_name': '', 'per_channel': {}})
                continue

            apps = _get_session_apps()  # (name, display, peak)
            sess_names = {n.lower() for n, _, _ in apps}
            playing    = [n for n, _, pk in apps if pk > 0.001]
            for name in playing:
                _app_priority[name.lower()] = _app_priority.get(name.lower(), 0.0) + TICK
            if playing:
                _save_priority()

            ranked = sorted(apps, key=lambda x: (_app_priority.get(x[0].lower(), 0.0), x[2] > 0.001), reverse=True)

            existing = _auto_state.get('per_channel', {})
            per_ch   = {}
            assigned = set()

            # Pass 1: keep existing assignments still alive
            for ch in auto_chs:
                prev = existing.get(ch, {}).get('app', '')
                if prev and prev.lower() in sess_names and prev.lower() not in assigned:
                    display = next((d for n, d, _ in apps if n == prev), prev)
                    per_ch[ch] = {'app': prev, 'display': display}
                    assigned.add(prev.lower())

            # Pass 2: fill gaps
            for ch in auto_chs:
                if ch in per_ch:
                    continue
                for name, display, _ in ranked:
                    if name.lower() not in assigned:
                        per_ch[ch] = {'app': name, 'display': display}
                        assigned.add(name.lower())
                        break
                else:
                    per_ch[ch] = {'app': '', 'display': ''}

            # Apply volumes on change
            prev_ch = _auto_state.get('per_channel', {})
            for ch, info in per_ch.items():
                name = info['app']
                if not name:
                    continue
                pct = state['pot_values'][int(ch)]
                if pct < 0:
                    continue
                if name != prev_ch.get(ch, {}).get('app', '') or pct != _auto_last_pct.get(ch):
                    _set_app_volume(name, pct, silent=True)
                    _auto_last_pct[ch] = pct

            _auto_state['per_channel'] = per_ch
            first = auto_chs[0] if auto_chs else None
            if first and per_ch.get(first, {}).get('app'):
                _auto_state['current_app']  = per_ch[first]['app']
                _auto_state['display_name'] = per_ch[first]['display']
            else:
                _auto_state['current_app']  = ''
                _auto_state['display_name'] = ''

            _push_names(cfg, per_ch)
        except Exception as e:
            print(f'[Auto] Loop error: {e}')

# ══════════════════════════════════════════════════════════════════════════
# NETWORK HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _own_ip():
    import socket
    rpi = state.get('rpi_ip', '')
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((rpi or '8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''

def _push_pc_ip():
    rpi = state.get('rpi_ip', '')
    if not rpi or not _req:
        return
    ip = _own_ip()
    if not ip:
        return
    try:
        _req.post(f'http://{rpi}:5000/api/set_pc_ip', json={'pc_ip': ip}, timeout=3)
        print(f'[Config] Told Pi our IP: {ip}')
    except Exception as e:
        print(f'[Config] Could not push IP to Pi: {e}')

# ══════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.route('/')
def serve_ui():
    try:
        with open(UI_HTML, encoding='utf-8') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    except FileNotFoundError:
        return f'<h2>pc_ui.html not found at: {UI_HTML}</h2>', 404

@app.route('/api/pc_status')
def pc_status():
    return jsonify({'status': 'online', 'pycaw': _pycaw_ok, 'winsdk': _winrt_ok,
                    'platform': sys.platform, 'connected_at': state['connected_at'],
                    'last_ping': state['last_ping']})

@app.route('/api/status')
def get_status():
    with _media_lock:
        media = dict(state['media'])
    return jsonify({
        'status': 'online', 'pot_values': state['pot_values'],
        'pot_config': state['pot_config'], 'rpi_ip': state['rpi_ip'],
        'media': media, 'pycaw': _pycaw_ok, 'winsdk': _winrt_ok,
        'auto_app': _auto_state['current_app'],
        'auto_display': _auto_state['display_name'],
        'auto_channels': _auto_state['per_channel'],
    })

@app.route('/api/peaks')
def get_peaks():
    return jsonify(state['peaks'])

@app.route('/api/media')
def get_media():
    state['last_ping'] = time.time()
    with _media_lock:
        media = dict(state['media'])
    client_title = request.headers.get('X-Media-Title')
    if client_title is not None and client_title == media.get('title', '') and media.get('title'):
        return ('', 204)
    return jsonify(media)

@app.route('/api/set_rpi_ip', methods=['POST'])
def set_rpi_ip():
    ip = request.json.get('rpi_ip', '').strip()
    state['rpi_ip'] = ip
    state['connected_at'] = time.time()
    s = _load_json(SETTINGS_FILE, {})
    s['rpi_ip'] = ip
    _save_json(SETTINGS_FILE, s)
    print(f'[Config] RPi IP set to {ip}')
    threading.Thread(target=_push_pc_ip, daemon=True).start()
    return jsonify({'status': 'ok', 'rpi_ip': ip})

@app.route('/api/settings')
def get_settings():
    return jsonify(_load_json(SETTINGS_FILE, {}))

@app.route('/api/pi_status')
def pi_status():
    rpi = state.get('rpi_ip', '')
    if not rpi:
        return jsonify({'reachable': False, 'reason': 'no_ip'})
    if not _req:
        return jsonify({'reachable': False, 'reason': 'no_requests'})
    try:
        r = _req.get(f'http://{rpi}:5000/api/status', timeout=2)
        return jsonify({'reachable': r.status_code == 200, 'rpi_ip': rpi})
    except Exception as e:
        return jsonify({'reachable': False, 'reason': str(e), 'rpi_ip': rpi})

@app.route('/api/pot_changed', methods=['POST'])
def pot_changed():
    try:
        data    = request.json
        channel = int(data.get('channel', 0))
        value   = int(data.get('value', 0))
        app_id  = data.get('app', '').strip()

        if 0 <= channel < 8:
            state['pot_values'][channel] = value

        if not app_id:
            return jsonify({'status': 'skipped'})

        ch_str = str(channel)
        if app_id == '__auto__':
            per    = _auto_state['per_channel'].get(ch_str)
            active = per['app'] if per else _auto_state['current_app']
            if active:
                matched = _set_app_volume(active, value)
                return jsonify({'status': 'ok' if matched else 'no_match', 'resolved': active})
            return jsonify({'status': 'skipped', 'reason': 'no_app_resolved'})
        elif app_id.lower() == 'master':
            matched = _set_master_volume(value)
        else:
            matched = _set_app_volume(app_id, value)

        return jsonify({'status': 'ok' if matched else 'no_match', 'channel': channel, 'value': value})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/get_volumes', methods=['POST'])
def get_volumes():
    try:
        pot_config = request.json.get('pot_config', {})
        user_cfg   = state['_user_pot_config']
        merged = {}
        for ch, app_id in pot_config.items():
            if user_cfg.get(ch) == '__auto__':
                merged[ch] = '__auto__'
            elif app_id == '__auto__':
                merged[ch] = '__auto__'
                user_cfg[ch] = '__auto__'
            else:
                merged[ch] = app_id
                if _last_pushed.get(ch) != app_id:
                    user_cfg[ch] = app_id
        state['pot_config'] = merged
        _save_json(POT_FILE, user_cfg)

        volumes = _get_all_volumes(merged)
        client_fp = request.json.get('fingerprint', '')
        if client_fp:
            server_fp = ','.join(str(int(volumes.get(str(i), -1))) for i in range(8))
            if client_fp == server_fp:
                return ('', 204)
        return jsonify({'status': 'ok', 'volumes': volumes})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/pot_display', methods=['GET'])
def get_pot_display():
    """Pi calls this on startup to pull current resolved auto-app display names
    without waiting for the PC to spontaneously push them."""
    per_ch = _auto_state.get('per_channel', {})
    cfg    = state.get('pot_config', {})
    display = {ch: per_ch.get(ch, {}).get('display', per_ch.get(ch, {}).get('app', ''))
               for ch, app in cfg.items() if app == '__auto__'}
    return jsonify(display)

@app.route('/api/button_pressed', methods=['POST'])
def button_pressed():
    try:
        idx = request.json.get('button_index')
        print(f'[Button] {idx}')
        MEDIA = {'media_play': 'play_pause', 'media_prev': 'prev',
                 'media_next': 'next', 'media_stop': 'stop'}
        if idx in MEDIA:
            _send_media_key(MEDIA[idx])
            return jsonify({'status': 'ok', 'action': MEDIA[idx]})
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/launch_pressed', methods=['POST'])
def launch_pressed():
    try:
        idx     = int(request.json.get('index', -1))
        buttons = _load_json(LAUNCH_FILE, {'buttons': []}).get('buttons', [])
        if 0 <= idx < len(buttons):
            cmd = buttons[idx].get('command', '').strip()
            if cmd:
                threading.Thread(target=_hidden_popen, args=(cmd,), daemon=True).start()
            return jsonify({'status': 'ok', 'command': cmd})
        return jsonify({'status': 'error', 'reason': 'invalid index'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/launch_config', methods=['GET'])
def get_launch_config():
    return jsonify(_load_json(LAUNCH_FILE, {'buttons': [
        {'label': '', 'icon': '', 'command': '', 'color': '#2e3d52'} for _ in range(10)
    ]}))

@app.route('/api/launch_config', methods=['POST'])
def set_launch_config():
    try:
        _save_json(LAUNCH_FILE, request.json)
        threading.Thread(target=_notify_rpi, args=('/api/launch_config', request.json), daemon=True).start()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/set_volume', methods=['POST'])
def set_volume_direct():
    try:
        name    = request.json.get('app', '')
        value   = int(request.json.get('value', 50))
        matched = _set_app_volume(name, value)
        return jsonify({'status': 'ok' if matched else 'no_match'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/media/control', methods=['POST'])
def media_control():
    try:
        action = request.json.get('action', '')
        if action in ('play_pause', 'next', 'prev', 'stop'):
            _send_media_key(action)
            return jsonify({'status': 'ok'})
        return jsonify({'status': 'error', 'reason': 'unknown action'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/running_apps')
def running_apps():
    if not _pycaw_ok:
        procs = sorted({p.name() for p in psutil.process_iter(['name'])
                        if p.info.get('name') and _is_user_app(p.info['name'])})
        return jsonify({'apps': [{'name': n} for n in procs[:80]], 'pycaw': False})
    try:
        apps = [{'name': 'Master', 'display': 'Master Volume', 'pid': None}]
        seen = {'master'}
        for s in AudioUtilities.GetAllSessions():
            proc = s.Process
            if not proc or not _is_user_app(proc.name()):
                continue
            if proc.name().lower() in seen:
                continue
            seen.add(proc.name().lower())
            display = proc.name().replace('.exe','').replace('.EXE','') \
                                 .replace('-',' ').replace('_',' ').title()
            apps.append({'name': proc.name(), 'display': display, 'pid': proc.pid})
        return jsonify({'apps': apps, 'pycaw': True})
    except Exception as e:
        return jsonify({'apps': [], 'error': str(e)})

_sysinfo_cache = {'data': None, 'ts': 0.0}

@app.route('/api/sysinfo')
def get_sysinfo():
    now = time.monotonic()
    if _sysinfo_cache['data'] and now - _sysinfo_cache['ts'] < 3.0:
        return jsonify(_sysinfo_cache['data'])
    try:
        cpu  = psutil.cpu_percent(interval=0.2)
        ram  = psutil.virtual_memory().percent
        temp = 0
        label = 'TEMP'
        # NVML (Nvidia)
        try:
            import ctypes
            nvml = ctypes.CDLL('nvml.dll' if IS_WIN else 'libnvidia-ml.so')
            nvml.nvmlInit()
            h = ctypes.c_void_p()
            nvml.nvmlDeviceGetHandleByIndex(0, ctypes.byref(h))
            t = ctypes.c_uint()
            nvml.nvmlDeviceGetTemperature(h, 0, ctypes.byref(t))
            nb = ctypes.create_string_buffer(96)
            nvml.nvmlDeviceGetName(h, nb, 96)
            temp  = int(t.value)
            label = f'GPU ({nb.value.decode(errors="ignore").split()[-1]})'
            nvml.nvmlShutdown()
        except Exception:
            pass
        # WMI OpenHardwareMonitor
        if temp == 0 and IS_WIN:
            try:
                import wmi
                for s in wmi.WMI(namespace=r'root\OpenHardwareMonitor').Sensor():
                    if 'GPU' in s.Name and s.SensorType == 'Temperature':
                        temp = round(float(s.Value)); label = 'GPU'; break
            except Exception:
                pass
        # WMI ACPI
        if temp == 0 and IS_WIN:
            try:
                import wmi
                for t in wmi.WMI(namespace=r'root\wmi').MSAcpi_ThermalZoneTemperature():
                    temp = round(t.CurrentTemperature / 10.0 - 273.15); label = 'CPU'; break
            except Exception:
                pass
        # psutil fallback
        if temp == 0:
            try:
                for entries in (psutil.sensors_temperatures() or {}).values():
                    if entries:
                        temp = round(entries[0].current); label = 'CPU'; break
            except Exception:
                pass

        result = {'cpu': round(cpu), 'ram': round(ram), 'temp': temp, 'temp_label': label}
        _sysinfo_cache['data'] = result
        _sysinfo_cache['ts']   = now
        return jsonify(result)
    except Exception as e:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'temp_label': 'TEMP', 'error': str(e)})

@app.route('/pi/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def pi_proxy(subpath):
    if not _req:
        return jsonify({'error': 'requests not installed'}), 500
    rpi = state.get('rpi_ip', '')
    if not rpi:
        return jsonify({'error': 'rpi_ip not set'}), 400
    url = f'http://{rpi}:5000/{subpath}'
    try:
        if request.method == 'GET':
            r = _req.get(url, timeout=4)
        else:
            r = _req.post(url, json=request.get_json(silent=True), timeout=4)
        if subpath == 'api/status' and r.status_code == 200:
            try:
                data = r.json()
                pot_apps = data.get('pot_apps', [])
                per_ch   = _auto_state.get('per_channel', {})
                for i, a in enumerate(pot_apps):
                    if a == '__auto__':
                        resolved = per_ch.get(str(i), {}).get('app', '')
                        if resolved:
                            pot_apps[i] = f'__auto__:{resolved}'
                data['pot_apps'] = pot_apps
                return jsonify(data)
            except Exception:
                pass
        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get('Content-Type', 'application/json'))
    except Exception as e:
        return jsonify({'error': str(e)}), 502

# ══════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════

def _load_settings():
    s = _load_json(SETTINGS_FILE, {})
    state['rpi_ip'] = s.get('rpi_ip', '')
    if state['rpi_ip']:
        print(f'[Boot] RPi IP loaded: {state["rpi_ip"]}')
        threading.Thread(target=_push_pc_ip, daemon=True).start()
    saved = _load_json(POT_FILE, {})
    if saved:
        state['pot_config'].update(saved)
        state['_user_pot_config'].update(saved)
        print(f'[Boot] Pot config loaded: {saved}')

def _open_browser():
    import urllib.request as ur
    for _ in range(40):
        try:
            ur.urlopen(f'http://localhost:{PORT}/', timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    import webbrowser
    webbrowser.open(f'http://localhost:{PORT}/')
    print(f'[Browser] Opened http://localhost:{PORT}/')

def _run_flask():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    print(f'[Flask] Starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)

# ══════════════════════════════════════════════════════════════════════════
# SYSTEM TRAY  (owns main thread — must be last)
# ══════════════════════════════════════════════════════════════════════════

def _make_icon():
    try:
        from PIL import Image, ImageDraw
        SIZE = 64
        img  = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
        d    = ImageDraw.Draw(img)
        C    = SIZE // 2
        CYAN = (0, 229, 255, 255)
        DIM  = (0, 100, 130, 200)
        BG   = (13, 16, 23, 255)
        d.ellipse([2, 2, SIZE-2, SIZE-2], fill=BG, outline=CYAN, width=2)
        for r in [22, 16, 10]:
            d.ellipse([C-r, C-r, C+r, C+r], outline=DIM, width=1)
        d.ellipse([C-5, C-5, C+5, C+5], fill=CYAN)
        return img
    except Exception:
        try:
            from PIL import Image
            return Image.new('RGB', (16, 16), (0, 229, 255))
        except Exception:
            return None

def _run_tray():
    try:
        import pystray
        ver = getattr(pystray, '__version__', 'unknown')
        print(f'[Tray] pystray version: {ver}')
    except ImportError:
        print('[Tray] pystray not installed — press Ctrl+C to exit')
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    img = _make_icon()

    _last_open_time = [0.0]  # mutable container so inner function can write it

    def on_open(icon, item):
        now = time.monotonic()
        if now - _last_open_time[0] < 3.0:
            return  # ignore clicks within 3s of the last open
        _last_open_time[0] = now
        threading.Thread(target=_open_browser, daemon=True).start()

    def on_exit(icon, item):
        print('[Tray] Exiting')
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem('RPi Audio Console', None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Open UI', on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Exit', on_exit),
    )

    icon = pystray.Icon('rpi_console', img, f'RPi Audio Console  ·  :{PORT}', menu)

    def setup(icon):
        icon.visible = True
        while True:
            try:
                pi = state.get('rpi_ip') or 'not set'
                icon.title = f'RPi Audio Console  ·  Pi: {pi}'
            except Exception:
                pass
            time.sleep(10)

    print('[Tray] Icon running — right-click the system tray to open UI or exit')
    try:
        icon.run(setup)
        # icon.run() should only return after icon.stop() is called (i.e. on_exit).
        # If it returns on its own, pystray crashed internally — keep the process alive.
        print('[Tray] WARNING: icon.run() returned unexpectedly — tray died silently')
        print('[Tray] Keeping process alive without tray. UI still available at '
              f'http://localhost:{PORT}/')
        while True:
            time.sleep(1)
    except Exception as e:
        import traceback
        print(f'[Tray] CRASH: {e}')
        traceback.print_exc()
        print('[Tray] Keeping process alive without tray.')
        while True:
            time.sleep(1)

if __name__ == '__main__':
    # Required for PyInstaller frozen exe — prevents infinite re-spawn of subprocesses
    import multiprocessing
    multiprocessing.freeze_support()

    # ── Diagnostics: catch every exit path ────────────────────────────────
    import atexit
    def _on_exit():
        print('[Main] Process exiting — stack of all threads:')
        import threading
        for t in threading.enumerate():
            print(f'  thread: {t.name} alive={t.is_alive()}')
    atexit.register(_on_exit)

    def _thread_crash(args):
        print(f'[Thread] UNHANDLED EXCEPTION in thread {args.thread.name}:')
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    threading.excepthook = _thread_crash

    _load_settings()
    _load_priority()

    for target in (_bg_media_loop, _bg_auto_detect_loop, _bg_peaks_loop, _run_flask):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=_open_browser, daemon=True).start()

    print()
    print('╔══════════════════════════════════════════════╗')
    print('║     RPi Audio Console — PC Backend           ║')
    print(f'║     UI:  http://localhost:{PORT}               ║')
    print(f'║     pycaw  (volume):  {"✓" if _pycaw_ok  else "✗ (pip install pycaw)"}{"             " if _pycaw_ok else ""}   ║')
    print(f'║     winsdk (media):   {"✓" if _winrt_ok  else "✗ (pip install winrt-Windows.Media.Control)"}{"             " if _winrt_ok else ""}   ║')
    print('╚══════════════════════════════════════════════╝')
    print()

    # pystray runs on its OWN thread (safe on Windows per pystray docs).
    # The main thread stays completely clean — no COM, no imports that touch COM.
    # This prevents comtypes GC finalizers (__del__ -> Release()) from firing on
    # the same thread as pystray's Win32 message pump, causing silent crashes.
    tray_thread = threading.Thread(target=_run_tray, daemon=False, name='pystray')
    tray_thread.start()

    try:
        tray_thread.join()
    except KeyboardInterrupt:
        print('[Main] Ctrl+C — exiting')
        os._exit(0)
