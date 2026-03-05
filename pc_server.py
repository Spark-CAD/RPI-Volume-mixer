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

import json, os, subprocess, sys, threading, time, signal
import psutil
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

try:
    import requests as _requests
except ImportError:
    _requests = None

# ── Windows-only imports ───────────────────────────────────────────────────
IS_WINDOWS = sys.platform == 'win32'

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

# UI HTML lives next to this script
UI_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pc_ui.html')

os.makedirs(CONFIG_DIR, exist_ok=True)

# ── Shared state ──────────────────────────────────────────────────────────
state = {
    'rpi_ip':     None,
    'pot_values': [-1] * 8,   # current values pushed by Pi
    'pot_config': {str(i): '' for i in range(8)},
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

        result = loop.run_until_complete(_fetch())
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

def _set_app_volume(app_name, volume_pct):
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
                print(f'[Volume] {proc.name()} -> {volume_pct}%')
        return matched
    except Exception as e:
        print(f'[Volume] Error setting {app_name}: {e}')
        return False

def _get_all_volumes(pot_config):
    """Return current volumes for all apps mapped in pot_config."""
    if not _pycaw_ok:
        # Return stored pot values as fallback
        return {str(ch): state['pot_values'][ch] for ch in range(8) if state['pot_values'][ch] >= 0}
    try:
        sessions = AudioUtilities.GetAllSessions()
        proc_vols = {}
        for session in sessions:
            proc = session.Process
            if proc:
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                proc_vols[proc.name().lower()] = round(vol.GetMasterVolume() * 100)

        result = {}
        for ch_str, app_id in pot_config.items():
            if not app_id or app_id == '__auto__':
                continue
            for proc_name, vol in proc_vols.items():
                if app_id.lower() in proc_name:
                    result[ch_str] = vol
                    break
        return result
    except Exception as e:
        print(f'[Volume] GetAllVolumes error: {e}')
        return {}

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
                subprocess.Popen(command, shell=True)
            else:
                # Try direct launch first, fall back to shell
                subprocess.Popen(command, shell=True)
        else:
            subprocess.Popen(command, shell=True)
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

@app.route('/api/media', methods=['GET'])
def get_media():
    state['last_ping'] = time.time()
    with _media_lock:
        return jsonify(state['media'])

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
            matched = _set_app_volume(app_id, value)
            status  = 'ok' if matched else 'no_match'
            print(f'[Pot] Ch{channel} -> {app_id}: {value}% [{status}]')
            return jsonify({'status': status, 'channel': channel, 'value': value, 'app': app_id})

        return jsonify({'status': 'skipped', 'reason': 'no app_id'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/get_volumes', methods=['POST'])
def get_volumes():
    """Pi calls this to sync current PC app volumes back to the pot display."""
    try:
        pot_config = request.json.get('pot_config', {})
        state['pot_config'] = pot_config
        volumes = _get_all_volumes(pot_config)
        return jsonify({'status': 'ok', 'volumes': volumes})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/button_pressed', methods=['POST'])
def button_pressed():
    """Pi sends button grid presses here."""
    try:
        idx = request.json.get('button_index')
        print(f'[Button] Grid button {idx} pressed')
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
    })

@app.route('/api/set_rpi_ip', methods=['POST'])
def set_rpi_ip():
    ip = request.json.get('rpi_ip', '').strip()
    state['rpi_ip'] = ip
    settings = _load_json(SETTINGS_FILE, {})
    settings['rpi_ip'] = ip
    _save_json(SETTINGS_FILE, settings)
    state['connected_at'] = time.time()
    print(f'[Config] RPi IP set to {ip}')
    return jsonify({'status': 'ok', 'rpi_ip': ip})

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(_load_json(SETTINGS_FILE, {}))

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

@app.route('/api/running_apps', methods=['GET'])
def get_running_apps():
    """Return list of processes that have audio sessions — useful for pot assignment."""
    if not _pycaw_ok:
        # Fallback: return all running process names
        procs = sorted(set(p.name() for p in psutil.process_iter(['name'])))
        return jsonify({'apps': procs[:100], 'pycaw': False})
    try:
        sessions = AudioUtilities.GetAllSessions()
        apps = []
        for s in sessions:
            if s.Process:
                apps.append({'name': s.Process.name(), 'pid': s.Process.pid})
        return jsonify({'apps': apps, 'pycaw': True})
    except Exception as e:
        return jsonify({'apps': [], 'error': str(e)})

@app.route('/api/media/control', methods=['POST'])
def media_control():
    """Send media key commands (play/pause/next/prev) via Windows key simulation."""
    if not IS_WINDOWS:
        return jsonify({'status': 'unsupported'})
    try:
        action = request.json.get('action', '')
        import ctypes
        # Virtual key codes
        VK_MEDIA = {'play_pause': 0xB3, 'next': 0xB0, 'prev': 0xB1, 'stop': 0xB2}
        vk = VK_MEDIA.get(action)
        if vk:
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            print(f'[Media] {action} key sent')
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
    This means the browser only ever talks to localhost, never the Pi directly."""
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
        return Response(r.content, status=r.status_code, content_type=r.headers.get('Content-Type','application/json'))
    except Exception as e:
        return jsonify({'error': str(e), 'pi_ip': rpi_ip}), 502

@app.route('/api/sysinfo', methods=['GET'])
def get_sysinfo_proxy():
    """Fetch sysinfo from the Pi and return it."""
    rpi_ip = state.get('rpi_ip', '')
    if not rpi_ip or not _requests:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'error': 'not connected'})
    try:
        r = _requests.get(f'http://{rpi_ip}:5000/api/sysinfo', timeout=3)
        return Response(r.content, content_type='application/json')
    except Exception as e:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'error': str(e)})

# ══════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════

def load_settings():
    s = _load_json(SETTINGS_FILE, {})
    state['rpi_ip'] = s.get('rpi_ip', '')
    if state['rpi_ip']:
        print(f'[Boot] RPi IP loaded: {state["rpi_ip"]}')

def print_banner():
    print()
    print('╔══════════════════════════════════════════════╗')
    print('║     RPi Audio Console — PC Backend           ║')
    print(f'║     UI:  http://localhost:{PORT}               ║')
    print(f'║     pycaw  (volume):  {"✓" if _pycaw_ok  else "✗ (pip install pycaw)"}{"             " if _pycaw_ok else ""}   ║')
    print(f'║     winsdk (media):   {"✓" if _winsdk_ok else "✗ (pip install winsdk)"}{"             " if _winsdk_ok else ""}   ║')
    print('╠══════════════════════════════════════════════╣')
    print(f'║  Browser opening: http://localhost:{PORT}      ║')
    print('╚══════════════════════════════════════════════╝')
    print()

def _open_browser():
    """Wait for Flask to start, then open the UI in the default browser."""
    import urllib.request as _ur
    for _ in range(20):
        try:
            _ur.urlopen(f'http://localhost:{PORT}/', timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    import webbrowser
    webbrowser.open(f'http://localhost:{PORT}/')
    print(f'[Browser] Opened http://localhost:{PORT}/')

def signal_handler(sig, frame):
    print('\n[Server] Shutting down...')
    sys.exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    load_settings()
    threading.Thread(target=_bg_media_loop, daemon=True).start()
    threading.Thread(target=_open_browser,  daemon=True).start()
    print_banner()
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)
