#!/usr/bin/env python3
"""
RPi Audio Console — PC Backend Server
Runs on your Windows PC on port 5001.
The Pi connects to this to push pot changes, button presses, and pull media info.

Requirements (install once):
    pip install flask flask-cors psutil requests

Optional but recommended for full media/volume control on Windows:
    pip install pycaw winsdk

Launch buttons: the 'command' field in launch_config.json runs via subprocess.
  - "spotify.exe"  won't work — use the full path or a shell command
  - "start spotify" will open Spotify via the Windows shell
  - "notepad.exe" works if it's on PATH
  - "C:/Users/you/Desktop/something.lnk" also works

Start this script, then open pc_ui.html in your browser.
"""

import json, os, queue, subprocess, sys, threading, time, signal
import psutil
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

try:
    import requests as _requests
except ImportError:
    _requests = None

# ── Windows-only imports ───────────────────────────────────────────────────
IS_WINDOWS = sys.platform == 'win32'

def _popen_hidden(cmd, **kwargs):
    """Spawn a shell command with no visible console window on Windows."""
    if IS_WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=si,
            creationflags=subprocess.CREATE_NO_WINDOW,
            **kwargs
        )
    return subprocess.Popen(cmd, shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **kwargs)


_pycaw_ok = False
_winsdk_ok = False

if IS_WINDOWS:
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        _pycaw_ok = True
        print('[Audio] pycaw available — per-app volume control enabled')
    except ImportError:
        print('[Audio] pycaw not installed. Run: pip install pycaw')
        print('[Audio] Per-app volume will be simulated (pot values stored only)')

    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager
        )
        import asyncio
        _winsdk_ok = True
        print('[Media] winsdk available — real media info enabled')
    except ImportError:
        print('[Media] winsdk not installed. Run: pip install winsdk')
        print('[Media] Media info will show placeholder data')

PORT = 5001
CONFIG_DIR   = os.path.expanduser('~/.rpi_console')
LAUNCH_FILE  = os.path.join(CONFIG_DIR, 'launch_config.json')
POT_FILE     = os.path.join(CONFIG_DIR, 'pot_config.json')
SETTINGS_FILE= os.path.join(CONFIG_DIR, 'settings.json')

# Locate pc_ui.html whether running as a .py script or a PyInstaller .exe
def _resource_path(filename):
    """Return the correct path to a bundled resource file."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)

UI_HTML = _resource_path('pc_ui.html')

os.makedirs(CONFIG_DIR, exist_ok=True)

# ── Shared state ──────────────────────────────────────────────────────────
state = {
    'rpi_ip':     None,
    'peaks': {'l': 0.0, 'r': 0.0, 'master': 0.0, 'sessions': {}},  # real audio peaks 0-1
    'pot_values': [-1] * 8,   # current values pushed by Pi
    'pot_config': {str(i): '' for i in range(8)},
    '_user_pot_config': {str(i): '' for i in range(8)},  # ground truth, never overwritten by auto-push echo
    'media': {
        'title': '', 'artist': '', 'playing': False,
        'source': '', 'duration': 0, 'position': 0,
        'position_pct': 0, 'elapsed_str': '0:00', 'duration_str': '0:00',
    },
    'connected_at': None,
    'last_ping':    None,
}

app = Flask(__name__)
CORS(app)

# ══════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def _fmt_time(s):
    s = max(0, int(s))
    return f'{s // 60}:{s % 60:02d}'

def _notify_rpi(endpoint, payload):
    ip = state.get('rpi_ip')
    if not ip or not _requests:
        return
    try:
        _requests.post(f'http://{ip}:5000{endpoint}', json=payload, timeout=2)
    except Exception as e:
        print(f'[RPi] Notify failed {endpoint}: {e}')

# ══════════════════════════════════════════════════════════════════════════
# MEDIA — Windows SDK
# ══════════════════════════════════════════════════════════════════════════

_media_lock = threading.Lock()

def _get_media_windows():
    """Fetch current media info from Windows GlobalSystemMediaTransportControls."""
    if not _winsdk_ok:
        return None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _fetch():
            sessions = await MediaManager.request_async()
            current = sessions.get_current_session()
            if not current:
                return None
            info = await current.try_get_media_properties_async()
            timeline = current.get_timeline_properties()
            pb_info  = current.get_playback_info()

            title  = info.title  or ''
            artist = info.artist or ''
            playing = (pb_info.playback_status.value == 4)  # 4 = Playing

            dur = timeline.end_time.total_seconds() if timeline.end_time else 0
            pos = timeline.position.total_seconds()  if timeline.position else 0

            # Try to get source app name
            try:
                source = current.source_app_user_model_id or ''
                # Simplify e.g. "Spotify.exe" -> "Spotify"
                source = source.split('!')[0].split('.')[0].title()
            except Exception:
                source = ''

            return {
                'title': title, 'artist': artist,
                'playing': playing, 'source': source,
                'duration': dur, 'position': pos,
            }

        try:
            result = loop.run_until_complete(_fetch())
        finally:
            loop.close()
        return result
    except Exception as e:
        print(f'[Media] winsdk error: {e}')
        return None


def _bg_media_loop():
    """Background thread: refreshes media state every second."""
    local_pos  = 0.0
    last_tick  = time.monotonic()

    while True:
        now     = time.monotonic()
        elapsed = now - last_tick
        last_tick = now

        with _media_lock:
            if state['media']['playing'] and state['media']['duration'] > 0:
                local_pos = min(state['media']['duration'], local_pos + elapsed)
            dur = state['media']['duration']
            state['media']['position']     = local_pos
            state['media']['position_pct'] = round(local_pos / dur * 100, 1) if dur else 0
            state['media']['elapsed_str']  = _fmt_time(local_pos)
            state['media']['duration_str'] = _fmt_time(dur)

        time.sleep(1.0)

        info = _get_media_windows()
        if info:
            with _media_lock:
                new_playing = info['playing']
                new_pos     = info['position']
                new_dur     = info['duration']
                new_title   = info['title']

                # Snap if track changed or position drifted
                if new_title != state['media']['title'] or abs(new_pos - local_pos) > 3:
                    local_pos = new_pos
                if not new_playing:
                    local_pos = new_pos

                state['media'].update({
                    'title':   new_title,
                    'artist':  info['artist'],
                    'playing': new_playing,
                    'source':  info['source'],
                    'duration': new_dur,
                })

# ══════════════════════════════════════════════════════════════════════════
# VOLUME CONTROL — pycaw
# ══════════════════════════════════════════════════════════════════════════

def _set_master_volume(volume_pct):
    """Set Windows master output volume (0-100)."""
    if not IS_WINDOWS:
        print(f'[Volume] Master -> {volume_pct}% (non-Windows, skipped)')
        return False
    try:
        volume_pct = max(0, min(100, volume_pct))
        if _pycaw_ok:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from comtypes import CLSCTX_ALL
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
            volume.SetMasterVolumeLevelScalar(volume_pct / 100.0, None)
            print(f'[Volume] Master -> {volume_pct}% (pycaw)')
            return True
        else:
            # Fallback: winmm.dll waveOutSetVolume via ctypes — no subprocess needed
            try:
                import ctypes
                vol = int(volume_pct / 100.0 * 0xFFFF)
                word = vol | (vol << 16)
                ctypes.windll.winmm.waveOutSetVolume(None, word)
                print(f'[Volume] Master -> {volume_pct}% (winmm ctypes fallback)')
                return True
            except Exception as fe:
                print(f'[Volume] winmm fallback failed: {fe}')
            return False
    except Exception as e:
        print(f'[Volume] Master error: {e}')
        return False

def _send_media_key(action):
    """Send a Windows media key. Works with Spotify, browsers, VLC, etc."""
    if not IS_WINDOWS:
        print(f'[Media] Key {action} (non-Windows, skipped)')
        return
    try:
        import ctypes
        VK_MAP = {'play_pause': 0xB3, 'next': 0xB0, 'prev': 0xB1, 'stop': 0xB2}
        vk = VK_MAP.get(action)
        if vk:
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            print(f'[Media] Key sent: {action}')
    except Exception as e:
        print(f'[Media] Key error: {e}')

def _set_app_volume(app_name, volume_pct, silent=False):
    """Set volume (0-100) for a named Windows application via pycaw."""
    if not _pycaw_ok or not app_name:
        return False
    try:
        volume_pct = max(0, min(100, volume_pct))
        scalar     = volume_pct / 100.0
        sessions   = AudioUtilities.GetAllSessions()
        matched    = False
        for session in sessions:
            proc = session.Process
            if proc and app_name.lower() in proc.name().lower():
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                vol.SetMasterVolume(scalar, None)
                matched = True
                if not silent:
                    print(f'[Volume] {proc.name()} -> {volume_pct}%')
        return matched
    except Exception as e:
        print(f'[Volume] Error setting {app_name}: {e}')
        return False

def _get_master_volume():
    """Get current Windows master volume as 0-100."""
    if not IS_WINDOWS or not _pycaw_ok:
        return None
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = interface.QueryInterface(IAudioEndpointVolume)
        return round(volume.GetMasterVolumeLevelScalar() * 100)
    except Exception as e:
        print(f'[Volume] GetMaster error: {e}')
        return None

def _get_all_volumes(pot_config):
    """Return current volumes for all apps mapped in pot_config."""
    result = {}
    for ch_str, app_id in pot_config.items():
        if not app_id or app_id == '__auto__':
            continue
        if app_id.lower() == 'master':
            v = _get_master_volume()
            if v is not None:
                result[ch_str] = v
            continue
        if not _pycaw_ok:
            # Return stored pot value as fallback
            ch = int(ch_str)
            if state['pot_values'][ch] >= 0:
                result[ch_str] = state['pot_values'][ch]
            continue
        try:
            from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                proc = session.Process
                if proc and app_id.lower() in proc.name().lower():
                    vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                    result[ch_str] = round(vol.GetMasterVolume() * 100)
                    break
        except Exception as e:
            print(f'[Volume] GetVolumes ch{ch_str} error: {e}')
    return result

# ══════════════════════════════════════════════════════════════════════════
# LAUNCH COMMANDS
# ══════════════════════════════════════════════════════════════════════════

def _run_command(command):
    """Execute a launch command. Uses shell=True so 'start spotify' etc. works."""
    if not command or not command.strip():
        print('[Launch] Empty command, skipping')
        return
    print(f'[Launch] Running: {command}')
    try:
        if IS_WINDOWS:
            # Use 'start' for apps that need the Windows shell (Spotify, etc.)
            # If command already starts with 'start', run as-is
            if command.strip().lower().startswith('start ') or \
               command.strip().lower().startswith('cmd ') or \
               command.strip().lower().startswith('powershell'):
                _popen_hidden(command)
            else:
                _popen_hidden(command)
        else:
            _popen_hidden(command)
    except Exception as e:
        print(f'[Launch] Error: {e}')

# ══════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.route('/api/pc_status', methods=['GET'])
def pc_status():
    return jsonify({
        'status':       'online',
        'pycaw':        _pycaw_ok,
        'winsdk':       _winsdk_ok,
        'platform':     sys.platform,
        'connected_at': state['connected_at'],
        'last_ping':    state['last_ping'],
    })

@app.route('/api/peaks', methods=['GET'])
def get_peaks():
    """Real-time audio peak levels 0-1 for Pi visualisations."""
    return jsonify(state['peaks'])

@app.route('/api/media', methods=['GET'])
def get_media():
    state['last_ping'] = time.time()
    with _media_lock:
        media = dict(state['media'])
    # If Pi sends its current title and it matches, return 204 — nothing changed
    client_title = request.headers.get('X-Media-Title', None)
    if client_title is not None and client_title == media.get('title', '') and media.get('title', ''):
        return ('', 204)
    return jsonify(media)

@app.route('/api/pot_changed', methods=['POST'])
def pot_changed():
    """Pi calls this when a physical pot is turned."""
    try:
        data    = request.json
        channel = int(data.get('channel', 0))
        value   = int(data.get('value', 0))
        app_id  = data.get('app', '').strip()

        if 0 <= channel < 8:
            state['pot_values'][channel] = value

        if app_id:
            if app_id == '__auto__':
                # Use per-channel resolved app if available, else fall back to primary
                ch_str = str(channel)
                per    = _auto_state['per_channel'].get(ch_str)
                active = per['app'] if per else _auto_state['current_app']
                if active:
                    matched = _set_app_volume(active, value)
                    status  = 'ok' if matched else 'no_match'
                    if status != 'ok':
                        print(f'[Pot/Auto] Ch{channel} -> {active}: {value}% [{status}]')
                else:
                    status = 'skipped'
            elif app_id.lower() == 'master':
                matched = _set_master_volume(value)
                status  = 'ok' if matched else 'no_match'
            else:
                matched = _set_app_volume(app_id, value)
                status  = 'ok' if matched else 'no_match'
            if status != 'ok':
                print(f'[Pot] Ch{channel} -> {app_id}: {value}% [{status}]')
            return jsonify({'status': status, 'channel': channel, 'value': value, 'app': app_id})

        return jsonify({'status': 'skipped', 'reason': 'no app_id'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/get_volumes', methods=['POST'])
def get_volumes():
    """Pi calls this to sync current PC app volumes back to the pot display.
    _user_pot_config is the ground truth for __auto__ channels — the Pi echoing
    back resolved names never overwrites the user's original assignment.
    """
    try:
        pot_config = request.json.get('pot_config', {})
        user_cfg   = state['_user_pot_config']
        merged = {}
        for ch_str, app_id in pot_config.items():
            if user_cfg.get(ch_str) == '__auto__':
                # Channel is __auto__ in ground truth — never let echo overwrite it
                merged[ch_str] = '__auto__'
            elif app_id == '__auto__':
                # Pi is sending __auto__ (initial load or manual assignment) — record it
                merged[ch_str] = '__auto__'
                user_cfg[ch_str] = '__auto__'
            else:
                merged[ch_str] = app_id
                # Only update user_cfg if this isn't one of our own resolved-name pushes.
                # We detect our own pushes by checking _last_pushed_names.
                if _last_pushed_names.get(ch_str) != app_id:
                    user_cfg[ch_str] = app_id
        state['pot_config'] = merged
        # Persist _user_pot_config so it survives PC restarts
        _save_json(POT_FILE, state['_user_pot_config'])
        volumes = _get_all_volumes(merged)
        # If Pi sent a fingerprint of its current values and they match, return 204
        client_fp = request.json.get('fingerprint', '')
        if client_fp:
            server_fp = ','.join(str(int(volumes.get(str(i), -1))) for i in range(8))
            if client_fp == server_fp:
                return ('', 204)
        return jsonify({'status': 'ok', 'volumes': volumes})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/button_pressed', methods=['POST'])
def button_pressed():
    """Pi sends button grid presses here — includes media control buttons."""
    try:
        idx = request.json.get('button_index')
        print(f'[Button] {idx}')

        # Media control buttons sent from the Pi UI
        MEDIA_MAP = {
            'media_play':  'play_pause',
            'media_prev':  'prev',
            'media_next':  'next',
            'media_stop':  'stop',
        }
        if idx in MEDIA_MAP:
            _send_media_key(MEDIA_MAP[idx])
            return jsonify({'status': 'ok', 'action': MEDIA_MAP[idx]})

        return jsonify({'status': 'ok', 'button_index': idx})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/launch_pressed', methods=['POST'])
def launch_pressed():
    """Pi sends quick-launch button presses here — we execute the command."""
    try:
        idx    = int(request.json.get('index', -1))
        config = _load_json(LAUNCH_FILE, {'buttons': []})
        buttons = config.get('buttons', [])
        if 0 <= idx < len(buttons):
            btn = buttons[idx]
            cmd = btn.get('command', '').strip()
            print(f'[Launch] Button {idx}: "{btn.get("label","")}" -> {cmd}')
            threading.Thread(target=_run_command, args=(cmd,), daemon=True).start()
            return jsonify({'status': 'ok', 'command': cmd})
        return jsonify({'status': 'error', 'reason': f'no button at index {idx}'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

# ── Config pass-through routes (UI talks to these directly) ───────────────

@app.route('/api/status', methods=['GET'])
def get_status():
    with _media_lock:
        media = dict(state['media'])
    return jsonify({
        'status':     'online',
        'pot_values': state['pot_values'],
        'pot_config': state['pot_config'],
        'rpi_ip':     state['rpi_ip'],
        'media':      media,
        'pycaw':      _pycaw_ok,
        'winsdk':     _winsdk_ok,
        'auto_app':      _auto_state['current_app'],
        'auto_display':  _auto_state['display_name'],
        'auto_channels': _auto_state['per_channel'],
    })

def _get_own_ip():
    """Best-effort: get this machine's LAN IP by opening a UDP socket toward the Pi."""
    import socket
    rpi = state.get('rpi_ip', '')
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((rpi if rpi else '8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''

def _push_pc_ip_to_pi():
    """Tell the Pi our own IP so it knows where to POST pot_changed events."""
    rpi_ip = state.get('rpi_ip', '')
    if not rpi_ip or not _requests:
        return
    own_ip = _get_own_ip()
    if not own_ip:
        return
    try:
        _requests.post(f'http://{rpi_ip}:5000/api/set_pc_ip',
                       json={'pc_ip': own_ip}, timeout=3)
        print(f'[Config] Told Pi our IP: {own_ip}')
    except Exception as e:
        print(f'[Config] Could not push PC IP to Pi: {e}')

@app.route('/api/set_rpi_ip', methods=['POST'])
def set_rpi_ip():
    ip = request.json.get('rpi_ip', '').strip()
    state['rpi_ip'] = ip
    settings = _load_json(SETTINGS_FILE, {})
    settings['rpi_ip'] = ip
    _save_json(SETTINGS_FILE, settings)
    state['connected_at'] = time.time()
    print(f'[Config] RPi IP set to {ip}')
    # Immediately tell the Pi our own IP so it can send pot_changed events
    threading.Thread(target=_push_pc_ip_to_pi, daemon=True).start()
    return jsonify({'status': 'ok', 'rpi_ip': ip})

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(_load_json(SETTINGS_FILE, {}))

@app.route('/api/pi_status', methods=['GET'])
def pi_status():
    """Live reachability check — the UI polls this to show the Pi connection light."""
    rpi_ip = state.get('rpi_ip', '')
    if not rpi_ip:
        return jsonify({'reachable': False, 'reason': 'no_ip'})
    if not _requests:
        return jsonify({'reachable': False, 'reason': 'no_requests'})
    try:
        r = _requests.get(f'http://{rpi_ip}:5000/api/status', timeout=2)
        return jsonify({'reachable': r.status_code == 200, 'rpi_ip': rpi_ip})
    except Exception as e:
        return jsonify({'reachable': False, 'reason': str(e), 'rpi_ip': rpi_ip})

@app.route('/api/launch_config', methods=['GET'])
def get_launch_config():
    return jsonify(_load_json(LAUNCH_FILE, {'buttons': [
        {'label': '', 'icon': '', 'command': '', 'color': '#2e3d52'} for _ in range(10)
    ]}))

@app.route('/api/launch_config', methods=['POST'])
def set_launch_config():
    try:
        _save_json(LAUNCH_FILE, request.json)
        # Push to RPi too so its UI shows updated buttons
        threading.Thread(
            target=_notify_rpi, args=('/api/launch_config', request.json), daemon=True
        ).start()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/set_volume', methods=['POST'])
def set_volume_direct():
    """UI can directly set a volume for a named app."""
    try:
        app_name = request.json.get('app', '')
        value    = int(request.json.get('value', 50))
        matched  = _set_app_volume(app_name, value)
        return jsonify({'status': 'ok' if matched else 'no_match', 'app': app_name, 'value': value})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

# ── Processes that are never "user apps" — filtered from pot assignment ────
_SYSTEM_BLOCKLIST = {
    # Windows system / shell
    'explorer.exe','svchost.exe','lsass.exe','csrss.exe','winlogon.exe',
    'dwm.exe','taskmgr.exe','conhost.exe','dllhost.exe','sihost.exe',
    'runtimebroker.exe','shellexperiencehost.exe','searchindexer.exe',
    'searchhost.exe','searchapp.exe','startmenuexperiencehost.exe',
    'textinputhost.exe','applicationframehost.exe','systemsettings.exe',
    'fontdrvhost.exe','wudfhost.exe','spoolsv.exe','services.exe',
    'wininit.exe','smss.exe','registry','system','secure system',
    'memory compression','antimalware service executable',
    'windows security health service','settingsynchost.exe',
    'ctfmon.exe','wlanext.exe','audiodg.exe','wmiprvse.exe',
    # Security / AV
    'msmpeng.exe','nissrv.exe','mssense.exe','securityhealthservice.exe',
    'avgui.exe','avguix.exe','mbam.exe','mbamservice.exe',
    # Runtimes / helpers
    'python.exe','python3.exe','pythonw.exe','java.exe','javaw.exe',
    'node.exe','cmd.exe','powershell.exe','pwsh.exe','wscript.exe',
    'cscript.exe','rundll32.exe','regsvr32.exe','msiexec.exe',
    'installer','setup','update','updater','helper','agent',
    # Common background helpers that aren't user-facing audio
    'nvidia web helper.exe','nvcontainer.exe','nvsphelper64.exe',
    'amdow.exe','igfxtray.exe','igfxem.exe','igfxhk.exe',
    'razer synapse','corsair','logitech','ghub',
    'onedrive.exe','dropbox.exe','googledrivefs.exe','box.exe',
    'teams.exe',  # Teams has its own audio; include only if user wants it
}

def _is_user_app(proc_name: str) -> bool:
    """Return True if this process looks like a real user app worth controlling."""
    name_lower = proc_name.lower().strip()
    # Block exact matches and substring matches for system processes
    for blocked in _SYSTEM_BLOCKLIST:
        if name_lower == blocked or name_lower.startswith(blocked.rstrip('.exe')):
            return False
    # Block generic patterns
    import re
    if re.search(r'(service|daemon|agent|helper|updater|installer|runtime|host|broker'
                 r'|notif|telemetry|crash|report|sentry|update)', name_lower):
        return False
    return True

@app.route('/api/running_apps', methods=['GET'])
def get_running_apps():
    """Return audio-session apps filtered to real user apps only."""
    if not _pycaw_ok:
        # Fallback: all processes, filtered
        procs = sorted({
            p.name() for p in psutil.process_iter(['name'])
            if p.info.get('name') and _is_user_app(p.info['name'])
        })
        return jsonify({'apps': [{'name': n} for n in procs[:80]], 'pycaw': False})
    try:
        sessions = AudioUtilities.GetAllSessions()
        apps = []
        seen = set()
        # Always include Master
        apps.append({'name': 'Master', 'display': 'Master Volume', 'pid': None})
        seen.add('master')
        for s in sessions:
            proc = s.Process
            if not proc:
                continue
            n = proc.name()
            if n.lower() in seen:
                continue
            if not _is_user_app(n):
                continue
            seen.add(n.lower())
            # Clean display name: strip .exe, title-case
            display = n.replace('.exe','').replace('.EXE','').replace('-',' ').replace('_',' ').title()
            apps.append({'name': n, 'display': display, 'pid': proc.pid})
        return jsonify({'apps': apps, 'pycaw': True})
    except Exception as e:
        return jsonify({'apps': [], 'error': str(e)})

# ── Auto-detect: find which apps are actually producing audio right now ────
_auto_state = {
    'current_app':    '',
    'display_name':   '',
    'per_channel':    {},
}
_last_pushed_names = {}
_auto_last_pct     = {}   # {ch_str: last_pct} — avoid re-applying unchanged auto volumes

def _get_audio_session_apps():
    """Return ALL apps with an open Windows audio session (playing OR paused).
    Returns list of (proc_name, display_name, peak) sorted by peak descending."""
    if not _pycaw_ok:
        return []
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        fg_pid = None
        if IS_WINDOWS:
            try:
                import ctypes
                hwnd    = ctypes.windll.user32.GetForegroundWindow()
                pid_buf = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                fg_pid = pid_buf.value
            except Exception:
                pass

        sessions = AudioUtilities.GetAllSessions()
        found = []
        seen  = set()
        for s in sessions:
            proc = s.Process
            if not proc or not _is_user_app(proc.name()):
                continue
            name_lower = proc.name().lower()
            if name_lower in seen:
                continue
            seen.add(name_lower)
            try:
                meter = s._ctl.QueryInterface(IAudioMeterInformation)
                peak  = meter.GetPeakValue()
            except Exception:
                peak = 0.0
            is_fg = (fg_pid is not None and proc.pid == fg_pid)
            display = proc.name().replace('.exe','').replace('.EXE',''). \
                                 replace('-',' ').replace('_',' ').title()
            found.append((proc.name(), display, peak, is_fg))
        found.sort(key=lambda x: (x[2], x[3]), reverse=True)
        return [(name, display, peak) for name, display, peak, _ in found]
    except Exception as e:
        print(f'[Auto] Session scan error: {e}')
        return []


# ── Priority tracker: cumulative play-time per app (persisted to disk) ────
_app_priority: dict = {}
_PRIORITY_FILE = os.path.join(CONFIG_DIR, 'app_priority.json')

def _load_priority():
    global _app_priority
    _app_priority = _load_json(_PRIORITY_FILE, {})

_priority_last_saved = 0.0

def _save_priority():
    global _priority_last_saved
    now = time.monotonic()
    if now - _priority_last_saved < 30.0:   # save at most every 30s to avoid constant disk I/O
        return
    _save_json(_PRIORITY_FILE, _app_priority)
    _priority_last_saved = now

def _priority_rank(name: str) -> float:
    return _app_priority.get(name.lower(), 0.0)

def _update_priority(playing_names: list, tick_secs: float):
    for name in playing_names:
        key = name.lower()
        _app_priority[key] = _app_priority.get(key, 0.0) + tick_secs
    if playing_names:
        _save_priority()


def _bg_auto_detect_loop():
    """Background thread: every 0.5s assign apps to __auto__ channels.

    Rules:
    - Any app with an open audio session is eligible (playing OR paused)
    - Priority score (cumulative play-time, persisted across restarts) determines
      which app gets the lowest-numbered channel
    - Each app occupies exactly ONE channel — no duplicates
    - An assignment is held as long as the app's audio session exists (not just
      while it's making noise) — pausing Spotify won't move it off its pot
    - When a session closes, the channel is freed and the next-ranked app claims it
    """
    TICK = 0.5

    while True:
        time.sleep(TICK)
        try:
            cfg = state.get('pot_config', {})
            auto_channels = sorted(
                [ch for ch, app in cfg.items() if app == '__auto__'],
                key=lambda x: int(x)
            )
            if not auto_channels:
                _auto_state.update({'current_app': '', 'display_name': '', 'per_channel': {}})
                continue

            # All apps with open sessions (peak=0 is fine — they're still there)
            all_apps = _get_audio_session_apps()   # (name, display, peak)
            session_names = {name.lower() for name, _, _ in all_apps}
            playing_names = [name for name, _, peak in all_apps if peak > 0.001]

            # Accumulate priority for playing apps
            _update_priority(playing_names, TICK)

            # Sort available apps: highest priority first
            # Ties broken by: currently playing > paused
            def _sort_key(entry):
                name, display, peak = entry
                return (_priority_rank(name), peak > 0.001)
            ranked = sorted(all_apps, key=_sort_key, reverse=True)

            # --- Assignment pass ---
            existing   = _auto_state.get('per_channel', {})
            per_ch     = {}
            assigned   = set()   # lowercase names already given a channel

            # Pass 1: retain existing assignments whose session is still open
            for ch_str in auto_channels:
                prev = existing.get(ch_str, {}).get('app', '')
                if prev and prev.lower() in session_names and prev.lower() not in assigned:
                    display = next((d for n, d, _ in all_apps if n == prev), prev)
                    per_ch[ch_str]  = {'app': prev, 'display': display}
                    assigned.add(prev.lower())

            # Pass 2: fill empty channels with highest-priority unassigned apps
            for ch_str in auto_channels:
                if ch_str in per_ch:
                    continue
                for name, display, peak in ranked:
                    if name.lower() not in assigned:
                        per_ch[ch_str] = {'app': name, 'display': display}
                        assigned.add(name.lower())
                        break
                else:
                    per_ch[ch_str] = {'app': '', 'display': ''}

            # Apply volumes only when the app assignment or pot value has changed
            prev_ch = _auto_state.get('per_channel', {})
            for ch_str, info in per_ch.items():
                app_name = info['app']
                if not app_name:
                    continue
                ch  = int(ch_str)
                pct = state['pot_values'][ch]
                if pct < 0:
                    continue
                prev_app = prev_ch.get(ch_str, {}).get('app', '')
                prev_pct = _auto_last_pct.get(ch_str)
                if app_name != prev_app or pct != prev_pct:
                    _set_app_volume(app_name, pct, silent=True)
                    _auto_last_pct[ch_str] = pct

            _auto_state['per_channel'] = per_ch
            first = auto_channels[0] if auto_channels else None
            if first and per_ch.get(first, {}).get('app'):
                _auto_state['current_app']  = per_ch[first]['app']
                _auto_state['display_name'] = per_ch[first]['display']
            else:
                _auto_state['current_app']  = ''
                _auto_state['display_name'] = ''

            _push_resolved_names_to_pi(cfg, per_ch)

        except Exception as e:
            print(f'[Auto] Loop error: {e}')


# Throttle Pi pushes — only push when resolved names actually change
_last_pushed_names = {}

# Single persistent worker thread for Pi display-name pushes — prevents thread pile-up
# when the Pi is slow or temporarily unreachable.
_push_queue: queue.Queue = queue.Queue(maxsize=1)

def _push_worker():
    while True:
        try:
            payload = _push_queue.get()
            if payload is None:
                break
            rpi_ip, display = payload
            try:
                _requests.post(f'http://{rpi_ip}:5000/api/pot_display', json=display, timeout=2)
            except Exception as e:
                print(f'[Auto] Pi display push failed: {e}')
        except Exception:
            pass

_push_worker_thread = threading.Thread(target=_push_worker, daemon=True)
_push_worker_thread.start()

def _push_resolved_names_to_pi(cfg, per_ch):
    """Push resolved display names to Pi's /api/pot_display so the Pi UI
    can show 'Spotify' / 'Chrome' on auto-assigned knobs.
    Uses a capped queue (maxsize=1) so only the latest payload is sent —
    no thread pile-up if the Pi is slow."""
    global _last_pushed_names
    if not _requests:
        return
    rpi_ip = state.get('rpi_ip', '')
    if not rpi_ip:
        return

    # Build {ch: raw_app_name} for all __auto__ channels — Pi UI does its own display formatting
    display = {}
    for ch_str, app_id in cfg.items():
        if app_id == '__auto__':
            info = per_ch.get(ch_str, {})
            display[ch_str] = info.get('app', '')  # e.g. 'Spotify.exe' — Pi looks this up in APPS table

    if display == _last_pushed_names:
        return
    _last_pushed_names = dict(display)
    print(f'[Auto] Display names changed: {display}')

    # Drain any stale pending push before enqueuing the fresh one
    try:
        _push_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        _push_queue.put_nowait((rpi_ip, display))
    except queue.Full:
        pass  # worker is busy with the item we just re-queued — it will pick up next cycle

@app.route('/api/media/control', methods=['POST'])
def media_control():
    """UI sends media key commands (play/pause/next/prev)."""
    if not IS_WINDOWS:
        return jsonify({'status': 'unsupported'})
    try:
        action = request.json.get('action', '')
        if action in ('play_pause', 'next', 'prev', 'stop'):
            _send_media_key(action)
            return jsonify({'status': 'ok', 'action': action})
        return jsonify({'status': 'error', 'reason': 'unknown action'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

# ══════════════════════════════════════════════════════════════════════════
# SERVE UI + PI PROXY
# ══════════════════════════════════════════════════════════════════════════

@app.route('/')
def serve_ui():
    """Serve the HTML UI — this eliminates all CORS issues."""
    try:
        with open(UI_HTML, 'r', encoding='utf-8') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    except FileNotFoundError:
        return f'<h2>pc_ui.html not found — expected at: {UI_HTML}</h2>', 404

@app.route('/pi/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def pi_proxy(subpath):
    """Proxy any /pi/<endpoint> call to the Pi on port 5000.
    Special case: /pi/api/status has __auto__ entries in pot_apps replaced
    with resolved app names so the Pi UI shows live labels."""
    if not _requests:
        return jsonify({'error': 'requests not installed'}), 500
    rpi_ip = state.get('rpi_ip', '')
    if not rpi_ip:
        return jsonify({'error': 'rpi_ip not set — use /api/set_rpi_ip first'}), 400
    url = f'http://{rpi_ip}:5000/{subpath}'
    try:
        if request.method == 'GET':
            r = _requests.get(url, timeout=4)
        else:
            r = _requests.post(url, json=request.get_json(silent=True), timeout=4)

        # Intercept Pi's /api/status — inject __auto__:AppName into pot_apps
        # The Pi UI parses this to keep mode='auto' while showing the resolved app name/colour
        if subpath == 'api/status' and r.status_code == 200:
            try:
                data = r.json()
                pot_apps = data.get('pot_apps', [])
                per_ch   = _auto_state.get('per_channel', {})
                for i, app in enumerate(pot_apps):
                    if app == '__auto__':
                        resolved = per_ch.get(str(i), {}).get('app', '')
                        if resolved:
                            pot_apps[i] = f'__auto__:{resolved}'
                        # else leave as '__auto__' — no app resolved yet
                data['pot_apps'] = pot_apps
                return jsonify(data)
            except Exception:
                pass  # fall through to raw response

        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get('Content-Type', 'application/json'))
    except Exception as e:
        return jsonify({'error': str(e), 'pi_ip': rpi_ip}), 502

# Cache sysinfo for 3s — avoids hammering NVML/WMI on every Pi poll
_sysinfo_cache = {'data': None, 'ts': 0}

@app.route('/api/sysinfo', methods=['GET'])
def get_sysinfo():
    now = time.monotonic()
    if _sysinfo_cache['data'] and (now - _sysinfo_cache['ts']) < 3.0:
        return jsonify(_sysinfo_cache['data'])
    """Return this PC's CPU %, RAM %, and GPU temp (preferred) or CPU temp."""
    try:
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory().percent
        temp = 0
        temp_label = 'TEMP'

        # 1. NVML via ctypes — no subprocess, works on Nvidia GPUs with drivers
        try:
            import ctypes
            nvml = ctypes.CDLL('nvml.dll' if IS_WINDOWS else 'libnvidia-ml.so')
            nvml.nvmlInit()
            handle = ctypes.c_void_p()
            nvml.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle))
            t = ctypes.c_uint()
            nvml.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(t))
            # Get GPU name
            name_buf = ctypes.create_string_buffer(96)
            nvml.nvmlDeviceGetName(handle, name_buf, 96)
            gpu_name = name_buf.value.decode(errors='ignore').split()[-1]
            temp = int(t.value)
            temp_label = f'GPU ({gpu_name})'
            nvml.nvmlShutdown()
        except Exception:
            pass

        # 2. Windows: try WMI for GPU temp via OpenHardwareMonitor namespace
        if temp == 0 and IS_WINDOWS:
            try:
                import wmi
                w = wmi.WMI(namespace=r'root\OpenHardwareMonitor')
                for sensor in w.Sensor():
                    if 'GPU' in sensor.Name and sensor.SensorType == 'Temperature':
                        temp = round(float(sensor.Value))
                        temp_label = 'GPU'
                        break
            except Exception:
                pass

        # 3. Windows WMI ACPI thermal (usually CPU — last resort)
        if temp == 0 and IS_WINDOWS:
            try:
                import wmi
                w = wmi.WMI(namespace=r'root\wmi')
                for t in w.MSAcpi_ThermalZoneTemperature():
                    temp = round(t.CurrentTemperature / 10.0 - 273.15)
                    temp_label = 'CPU'
                    break
            except Exception:
                pass

        # 4. psutil sensors (Linux/Mac fallback)
        if temp == 0:
            try:
                for name, entries in (psutil.sensors_temperatures() or {}).items():
                    if entries:
                        temp = round(entries[0].current)
                        temp_label = 'CPU'
                        break
            except Exception:
                pass

        result = {'cpu': round(cpu), 'ram': round(ram), 'temp': temp, 'temp_label': temp_label}
        _sysinfo_cache['data'] = result
        _sysinfo_cache['ts']   = now
        return jsonify(result)
    except Exception as e:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'temp_label': 'TEMP', 'error': str(e)})

# ══════════════════════════════════════════════════════════════════════════
# STARTUP & SYSTEM TRAY
# ══════════════════════════════════════════════════════════════════════════

def load_settings():
    s = _load_json(SETTINGS_FILE, {})
    state['rpi_ip'] = s.get('rpi_ip', '')
    if state['rpi_ip']:
        print(f'[Boot] RPi IP loaded: {state["rpi_ip"]}')
        # Re-register our IP with the Pi so it resumes sending pot_changed after a restart
        threading.Thread(target=_push_pc_ip_to_pi, daemon=True).start()
    # Load saved pot config so auto-detect loop knows which channels are __auto__ immediately
    saved_cfg = _load_json(POT_FILE, {})
    if saved_cfg:
        state['pot_config'].update(saved_cfg)
        state['_user_pot_config'].update(saved_cfg)
        print(f'[Boot] Pot config loaded: {saved_cfg}')

def _open_browser():
    """Wait for Flask to be ready, then open the UI."""
    import urllib.request as _ur
    for _ in range(40):
        try:
            _ur.urlopen(f'http://localhost:{PORT}/', timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    import webbrowser
    webbrowser.open(f'http://localhost:{PORT}/')
    print(f'[Browser] Opened http://localhost:{PORT}/')

# ══════════════════════════════════════════════════════════════════════════
# REAL-TIME AUDIO PEAKS
# ══════════════════════════════════════════════════════════════════════════

def _bg_peaks_loop():
    """Reads master stereo L/R peak levels + per-session peaks at 60ms intervals.
    Uses IAudioMeterInformation.GetChannelsPeakValues for true stereo,
    falls back to mono GetPeakValue if needed."""
    import ctypes
    INTERVAL = 0.06
    _meter   = None

    def _acquire_meter():
        nonlocal _meter
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            from comtypes import CLSCTX_ALL
            from ctypes import cast, POINTER
            devices = AudioUtilities.GetSpeakers()
            iface   = devices.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            _meter  = cast(iface, POINTER(IAudioMeterInformation))
        except Exception:
            _meter = None

    while True:
        time.sleep(INTERVAL)
        if not _pycaw_ok:
            continue
        try:
            if _meter is None:
                _acquire_meter()
            if _meter:
                try:
                    n   = _meter.GetMeteringChannelCount()
                    arr = (ctypes.c_float * n)()
                    _meter.GetChannelsPeakValues(n, arr)
                    l = float(arr[0])
                    r = float(arr[1]) if n >= 2 else l
                except Exception:
                    v = float(_meter.GetPeakValue())
                    l = r = v
                master = max(l, r)
                state['peaks']['l']      = round(l,      3)
                state['peaks']['r']      = round(r,      3)
                state['peaks']['master'] = round(master, 3)

            # Per-session peaks
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation as IAMI
            sess_peaks = {}
            for s2 in AudioUtilities.GetAllSessions():
                if not s2.Process:
                    continue
                name = s2.Process.name().lower()
                try:
                    pk = float(s2._ctl.QueryInterface(IAMI).GetPeakValue())
                    sess_peaks[name] = max(sess_peaks.get(name, 0), pk)
                except Exception:
                    pass
            state['peaks']['sessions'] = {k: round(v, 3) for k, v in sess_peaks.items()}
        except Exception:
            _meter = None  # re-acquire on next tick

def _run_flask():
    """Run Flask in its own thread — never call app.run() on the main thread
    when pystray owns it."""
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    print(f'[Flask] Starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False,
            threaded=True, use_debugger=False)

# ── Tray icon image (drawn with Pillow — no external file needed) ──────────
def _make_tray_icon():
    """Draw a simple vinyl-disc icon in the brand cyan colour."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    SIZE = 64
    img  = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)

    C    = SIZE // 2
    CYAN = (0, 229, 255, 255)
    DIM  = (0, 100, 130, 200)
    BG   = (13, 16, 23, 255)

    # Outer disc
    d.ellipse([2, 2, SIZE-2, SIZE-2], fill=BG, outline=CYAN, width=2)
    # Grooves
    for r in [22, 16, 10]:
        d.ellipse([C-r, C-r, C+r, C+r], outline=DIM, width=1)
    # Hub
    d.ellipse([C-5, C-5, C+5, C+5], fill=CYAN)

    return img

def _build_tray():
    """Build and run the pystray tray icon. Blocks until the icon is stopped."""
    try:
        import pystray
    except ImportError:
        print('[Tray] pystray not installed — running without tray icon.')
        print('[Tray] Install with: pip install pystray pillow')
        # Fall back to blocking forever so the process stays alive
        import signal as _sig
        _sig.pause() if hasattr(_sig, 'pause') else time.sleep(1e9)
        return

    icon_img = _make_tray_icon()
    if icon_img is None:
        # Pillow missing — create a tiny blank image as fallback
        try:
            from PIL import Image
            icon_img = Image.new('RGB', (16, 16), (0, 229, 255))
        except Exception:
            print('[Tray] Pillow not installed — tray icon will be blank.')
            print('[Tray] Install with: pip install pillow')

    pi_ip_display = lambda: state.get('rpi_ip') or 'not set'

    def on_open(icon, item):
        threading.Thread(target=_open_browser, daemon=True).start()

    def on_exit(icon, item):
        print('[Tray] Exit requested')
        icon.stop()
        os._exit(0)

    def get_title():
        pi = pi_ip_display()
        return f'RPi Audio Console\nPort {PORT}  ·  Pi: {pi}'

    menu = pystray.Menu(
        pystray.MenuItem('RPi Audio Console', None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Open UI', on_open, default=True),   # double-click action
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: f'Pi: {pi_ip_display()}', None, enabled=False),
        pystray.MenuItem(f'Port: {PORT}', None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Exit', on_exit),
    )

    icon = pystray.Icon(
        name  = 'rpi_console',
        icon  = icon_img,
        title = get_title(),
        menu  = menu,
    )

    print('[Tray] Icon running — right-click the system tray to open UI or exit')
    icon.run()   # blocks main thread; pystray requires this

if __name__ == '__main__':
    load_settings()
    _load_priority()

    # Start background threads
    threading.Thread(target=_bg_media_loop,        daemon=True).start()
    threading.Thread(target=_bg_auto_detect_loop,  daemon=True).start()
    threading.Thread(target=_bg_peaks_loop,         daemon=True).start()
    threading.Thread(target=_run_flask,            daemon=True).start()
    threading.Thread(target=_open_browser,         daemon=True).start()

    print()
    print('╔══════════════════════════════════════════════╗')
    print('║     RPi Audio Console — PC Backend           ║')
    print(f'║     UI:  http://localhost:{PORT}               ║')
    print(f'║     pycaw  (volume):  {"✓" if _pycaw_ok  else "✗ (pip install pycaw)"}{"             " if _pycaw_ok else ""}   ║')
    print(f'║     winsdk (media):   {"✓" if _winsdk_ok else "✗ (pip install winsdk)"}{"             " if _winsdk_ok else ""}   ║')
    print('╚══════════════════════════════════════════════╝')
    print()

    # _build_tray() owns the main thread — it blocks until "Exit" is clicked.
    # Closing the browser window just hides the UI; the process keeps running.
    _build_tray()
