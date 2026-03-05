#!/usr/bin/env python3
"""
RPi Audio Console — Flask backend + integrated SPI pot reader

Python handles ALL volume control:
  - Background thread reads MCP3008 via SPI (same as pot_test.py, confirmed working)
  - On pot movement: POSTs to PC /api/pot_changed directly (no UI involvement)
  - Updates state['pot_values'] so UI can poll and display current values

UI (rpi_ui.html) is display-only + button presses only. No volume POSTs from browser.

MCP3008 wiring (DIP-16):
  pin 16 (Vdd)     -> Pi pin 1  (3.3v)
  pin 15 (Vref)    -> Pi pin 1  (3.3v)
  pin 14 (AGND)    -> Pi pin 6  (GND)
  pin 13 (CLK)     -> Pi pin 23 (SCLK)
  pin 12 (Dout)    -> Pi pin 21 (MISO)
  pin 11 (Din)     -> Pi pin 19 (MOSI)
  pin 10 (CS/SHDN) -> Pi pin 22 (CE0)
  pin  9 (DGND)    -> Pi pin 6  (GND)
"""

import json, threading, subprocess, time, os, signal, sys
try:
    import requests
except ImportError:
    requests = None

from flask import Flask, request, jsonify
from flask_cors import CORS

BASE_DIR            = os.path.expanduser('~/button_grid')
CONFIG_FILE         = os.path.expanduser('~/button_display.json')
PC_CONFIG_FILE      = os.path.expanduser('~/pc_connection.json')
WIDGET_CONFIG_FILE  = os.path.expanduser('~/widget_config.json')
LEFT_WIDGET_FILE    = os.path.expanduser('~/left_widget_config.json')
DISPLAY_CONFIG_FILE = os.path.expanduser('~/display_config.json')
POT_CONFIG_FILE     = os.path.expanduser('~/pot_config.json')
LAUNCH_CONFIG_FILE  = os.path.expanduser('~/launch_config.json')
UI_HTML_FILE        = os.path.join(BASE_DIR, 'rpi_ui.html')
PORT                = 5000

# ── Shared state (read by Flask routes, written by background threads) ────────
state = {
    'pc_ip':       None,
    'pot_values':  [-1] * 8,   # 0-100 pct, -1 = not yet read
    'pot_display': [''] * 8,   # resolved display names pushed by PC for __auto__ channels
    'media': {
        'title': '', 'artist': '', 'playing': False,
        'source': '', 'duration': 0, 'position': 0,
        'position_pct': 0, 'elapsed_str': '0:00', 'duration_str': '0:00',
    },
}

app = Flask(__name__)
CORS(app)

# ── Helpers ───────────────────────────────────────────────────────────────────

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

def _ensure_config_files():
    """Write default config files if they don't already exist."""
    defaults = {
        POT_CONFIG_FILE:    {str(i): '' for i in range(8)},
        LAUNCH_CONFIG_FILE: {'buttons': [
            {'label': '', 'icon': '', 'command': '', 'color': '#2e3d52'}
            for _ in range(10)
        ]},
        WIDGET_CONFIG_FILE:  {'widget': 'vu'},
        LEFT_WIDGET_FILE:    {'widget': 'disc'},
        DISPLAY_CONFIG_FILE: {'scroll_enabled': True, 'scroll_interval': 8},
        PC_CONFIG_FILE:      {'pc_ip': ''},
    }
    for path, default in defaults.items():
        if not os.path.exists(path):
            _save_json(path, default)
            print(f'[Config] Created default: {path}')

def _notify_pc(endpoint, payload):
    pc_ip = state['pc_ip']
    if not pc_ip or not requests:
        return
    try:
        return requests.post(f'http://{pc_ip}:5001{endpoint}', json=payload, timeout=2)
    except Exception as e:
        print(f'[PC] {endpoint} failed: {e}')

# ══════════════════════════════════════════════════════════════════════════════
# SPI POT READER  — all volume control lives here, UI is purely display
# ══════════════════════════════════════════════════════════════════════════════

SPI_BUS       = 0
SPI_DEVICE    = 0
SPI_SPEED_HZ  = 1_350_000
POLL_HZ       = 10
POLL_INTERVAL = 1.0 / POLL_HZ

# Noise filtering:
# RAW_THRESHOLD — minimum raw ADC delta (0-1023) before we even consider a change.
#   MCP3008 idle noise is typically ±2-4 counts. 8 = ~0.8% change required to react.
RAW_THRESHOLD = 8

# PCT_THRESHOLD — minimum percentage-point change before sending to PC.
#   Even if raw crosses threshold, skip if the rounded % didn't actually move.
PCT_THRESHOLD = 1

# DEBOUNCE_MS — ms of knob silence before the POST fires. Prevents rapid-fire on slow turns.
DEBOUNCE_MS   = 150

_deb_timers  = [None] * 8
_deb_pending = [None] * 8
_deb_lock    = threading.Lock()
_raw_vals    = [-1]   * 8
_last_pct    = [-1]   * 8   # last percentage value actually sent, for PCT_THRESHOLD check

_cfg_reload_requested = False

def _request_cfg_reload():
    """Signal the SPI reader to reload pot_config.json on its next tick."""
    global _cfg_reload_requested
    _cfg_reload_requested = True

spi = None

def _init_spi():
    global spi
    try:
        import spidev
        spi = spidev.SpiDev()
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED_HZ
        spi.mode = 0b00
        print('[SPI] MCP3008 initialised')
        return True
    except ImportError:
        print('[SPI] spidev not installed — run: pip install spidev')
    except FileNotFoundError:
        print(f'[SPI] Bus {SPI_BUS}/dev {SPI_DEVICE} not found — enable SPI in raspi-config')
    except Exception as e:
        print(f'[SPI] Init failed: {e}')
    return False

def _read_channel(ch):
    r = spi.xfer2([0x01, 0x80 | (ch << 4), 0x00])
    return ((r[1] & 0x03) << 8) | r[2]

def _raw_to_pct(raw):
    return round(raw / 1023 * 100)

def _send_to_pc(ch, pct, app_id):
    if not requests or not state['pc_ip']:
        return
    try:
        r = requests.post(
            f'http://{state["pc_ip"]}:5001/api/pot_changed',
            json={'channel': ch, 'value': pct, 'app': app_id},
            timeout=1.5
        )
        status = 'OK' if r.status_code == 200 else f'HTTP {r.status_code}'
        print(f'[Pot] Ch{ch} -> {app_id}: {pct}% [{status}]')
    except Exception as e:
        print(f'[Pot] Ch{ch} send failed: {e}')

def _fire_debounced(ch):
    with _deb_lock:
        pending = _deb_pending[ch]
        _deb_pending[ch] = None
        _deb_timers[ch]  = None
    if pending:
        pct, app_id = pending
        threading.Thread(target=_send_to_pc, args=(ch, pct, app_id), daemon=True).start()

def _schedule_send(ch, pct, app_id):
    with _deb_lock:
        _deb_pending[ch] = (pct, app_id)
        if _deb_timers[ch]:
            _deb_timers[ch].cancel()
        t = threading.Timer(DEBOUNCE_MS / 1000.0, _fire_debounced, args=(ch,))
        t.daemon = True
        t.start()
        _deb_timers[ch] = t

def _spi_reader_loop():
    """
    Runs forever in a daemon thread.
    Reads all 8 pot channels, updates state['pot_values'] immediately on movement,
    and schedules a debounced POST to the PC.
    The UI only reads state['pot_values'] via /api/status — it never POSTs volume.
    """
    if not _init_spi():
        print('[SPI] Reader disabled')
        return

    print(f'[SPI] Polling at {POLL_HZ}Hz, threshold {RAW_THRESHOLD} raw, debounce {DEBOUNCE_MS}ms')

    # Initial read so knobs show real position immediately on UI load
    for ch in range(8):
        try:
            raw = _read_channel(ch)
            _raw_vals[ch] = raw
            pct = _raw_to_pct(raw)
            state['pot_values'][ch] = pct
            _last_pct[ch] = pct
        except Exception:
            pass

    pot_cfg          = _load_json(POT_CONFIG_FILE, {str(i): '' for i in range(8)})
    _cfg_last_loaded = time.monotonic()
    _CFG_RELOAD_SECS = 5.0   # re-read pot_config.json from disk every 5 seconds

    while True:
        global _cfg_reload_requested
        t0 = time.monotonic()

        # Reload config from disk periodically, or immediately when API updates it
        if t0 - _cfg_last_loaded >= _CFG_RELOAD_SECS or _cfg_reload_requested:
            pot_cfg               = _load_json(POT_CONFIG_FILE, {str(i): '' for i in range(8)})
            _cfg_last_loaded      = t0
            _cfg_reload_requested = False

        for ch in range(8):
            try:
                raw   = _read_channel(ch)
                delta = abs(raw - _raw_vals[ch])
                if delta >= RAW_THRESHOLD:
                    _raw_vals[ch] = raw
                    pct = _raw_to_pct(raw)
                    state['pot_values'][ch] = pct   # instant UI update
                    _pot_last_moved[ch] = time.time()  # lock out PC sync briefly

                    # Additional % filter — skip if rounded value didn't actually change
                    if abs(pct - _last_pct[ch]) < PCT_THRESHOLD:
                        state['pot_values'][ch] = pct  # still update display
                        continue

                    app_id = pot_cfg.get(str(ch), '').strip()
                    if app_id and app_id != '__auto__':
                        _last_pct[ch] = pct
                        _schedule_send(ch, pct, app_id)
                    elif app_id == '__auto__':
                        # Send to PC with app='__auto__' so PC can resolve and apply volume
                        _last_pct[ch] = pct
                        _schedule_send(ch, pct, '__auto__')
                    else:
                        print(f'[Pot] Ch{ch} -> {pct}% (not assigned, not sent)')
            except Exception as e:
                print(f'[SPI] Ch{ch} error: {e}')

        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - t0)))

# ══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def serve_ui():
    try:
        with open(UI_HTML_FILE, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    except FileNotFoundError:
        return '<h1>rpi_ui.html not found in ' + BASE_DIR + '</h1>', 404

@app.route('/api/status', methods=['GET'])
def get_status():
    pot_cfg = _load_json(POT_CONFIG_FILE, {str(i): '' for i in range(8)})
    return jsonify({
        'status':     'online',
        'pot_values': state['pot_values'],
        'pot_apps':   [pot_cfg.get(str(i), '') for i in range(8)],
        'pot_display': state['pot_display'],
        'pc_ip':      state['pc_ip'],
        'media':      state['media'],
    })

@app.route('/api/pot_display', methods=['POST'])
def set_pot_display():
    """PC pushes resolved auto-app display names here.
    e.g. {'1': 'Spotify', '2': 'Chrome', '3': ''} for channels set to __auto__."""
    try:
        data = request.json or {}
        for ch_str, name in data.items():
            ch = int(ch_str)
            if 0 <= ch < 8:
                state['pot_display'][ch] = name or ''
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/set_pc_ip', methods=['POST'])
def set_pc_ip():
    pc_ip = request.json.get('pc_ip', '')
    state['pc_ip'] = pc_ip
    _save_json(PC_CONFIG_FILE, {'pc_ip': pc_ip})
    return jsonify({'status': 'success', 'pc_ip': pc_ip})

@app.route('/api/pot_config', methods=['GET'])
def get_pot_config():
    return jsonify(_load_json(POT_CONFIG_FILE, {str(i): '' for i in range(8)}))

@app.route('/api/pot_config', methods=['POST'])
def set_pot_config():
    """UI calls this when user assigns a pot via touchscreen modal.
    Saves locally, pushes to PC, and immediately sends current pot value
    so the PC reflects the new assignment right away."""
    try:
        cfg = request.json
        _save_json(POT_CONFIG_FILE, cfg)
        _request_cfg_reload()   # SPI loop picks up new mapping on next tick
        for ch_str, app_id in cfg.items():
            if app_id and app_id != '__auto__':
                ch  = int(ch_str)
                pct = state['pot_values'][ch]
                if pct >= 0:
                    threading.Thread(
                        target=_send_to_pc, args=(ch, pct, app_id), daemon=True
                    ).start()
            elif app_id == '__auto__':
                ch  = int(ch_str)
                pct = state['pot_values'][ch]
                if pct >= 0:
                    threading.Thread(
                        target=_send_to_pc, args=(ch, pct, '__auto__'), daemon=True
                    ).start()
        threading.Thread(target=_notify_pc, args=('/api/pot_config', cfg), daemon=True).start()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/pot_changed', methods=['POST'])
def pot_changed():
    """Legacy/test endpoint. Normal operation uses the SPI thread, not this route.
    Kept so the /test page button and pot_test.py still work."""
    try:
        data    = request.json
        channel = int(data.get('channel', 0))
        value   = int(data.get('value', 0))
        app_id  = data.get('app', '').strip()

        if 0 <= channel < 8:
            state['pot_values'][channel] = value

        if not app_id:
            app_id = _load_json(POT_CONFIG_FILE, {}).get(str(channel), '').strip()

        if app_id and app_id != '__auto__':
            threading.Thread(
                target=_send_to_pc, args=(channel, value, app_id), daemon=True
            ).start()
            print(f'[Pot/HTTP] Ch{channel} -> {app_id}: {value}%')
            return jsonify({'status': 'ok', 'app': app_id, 'value': value})

        return jsonify({'status': 'skipped', 'reason': 'not mapped'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/widget', methods=['GET'])
def get_widget():
    return jsonify(_load_json(WIDGET_CONFIG_FILE, {'widget': 'vu'}))

@app.route('/api/widget', methods=['POST'])
def set_widget_route():
    w = request.json.get('widget', 'vu')
    if w not in ('vu', 'clock', 'sysinfo'):
        return jsonify({'status': 'error', 'message': 'Unknown widget'}), 400
    _save_json(WIDGET_CONFIG_FILE, {'widget': w})
    return jsonify({'status': 'success', 'widget': w})

@app.route('/api/left_widget', methods=['GET'])
def get_left_widget():
    return jsonify(_load_json(LEFT_WIDGET_FILE, {'widget': 'disc'}))

@app.route('/api/left_widget', methods=['POST'])
def set_left_widget():
    w = request.json.get('widget', 'disc')
    if w not in ('disc', 'circvu', 'waveform', 'spectrum'):
        return jsonify({'status': 'error', 'message': 'Unknown left widget'}), 400
    _save_json(LEFT_WIDGET_FILE, {'widget': w})
    return jsonify({'status': 'success', 'widget': w})

@app.route('/api/display_config', methods=['GET'])
def get_display_config():
    return jsonify(_load_json(DISPLAY_CONFIG_FILE,
                              {'scroll_enabled': True, 'scroll_interval': 8}))

@app.route('/api/display_config', methods=['POST'])
def set_display_config():
    try:
        data = request.json
        cfg  = _load_json(DISPLAY_CONFIG_FILE, {'scroll_enabled': True, 'scroll_interval': 8})
        if 'scroll_enabled'  in data: cfg['scroll_enabled']  = bool(data['scroll_enabled'])
        if 'scroll_interval' in data: cfg['scroll_interval'] = int(data['scroll_interval'])
        _save_json(DISPLAY_CONFIG_FILE, cfg)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/poll_config', methods=['POST'])
def set_poll_config():
    """PC pushes pot reader tuning params (poll_hz, raw_thresh, pct_thresh, debounce)."""
    global POLL_HZ, POLL_INTERVAL, RAW_THRESHOLD, PCT_THRESHOLD, DEBOUNCE_MS
    try:
        d = request.json
        if 'poll_hz'    in d: POLL_HZ = max(1, int(d['poll_hz']));    POLL_INTERVAL = 1.0 / POLL_HZ
        if 'raw_thresh' in d: RAW_THRESHOLD = max(1, int(d['raw_thresh']))
        if 'pct_thresh' in d: PCT_THRESHOLD = max(0, int(d['pct_thresh']))
        if 'debounce'   in d: DEBOUNCE_MS   = max(0, int(d['debounce']))
        print(f'[Poll] Config updated: {POLL_HZ}Hz raw≥{RAW_THRESHOLD} pct≥{PCT_THRESHOLD} deb={DEBOUNCE_MS}ms')
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/button_pressed', methods=['POST'])
def button_pressed():
    try:
        idx = request.json.get('button_index')
        threading.Thread(target=_notify_pc, args=('/api/button_pressed', {'button_index': idx}), daemon=True).start()
        print(f'[Button] {idx}')
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/launch_config', methods=['GET'])
def get_launch_config():
    return jsonify(_load_json(LAUNCH_CONFIG_FILE, {'buttons': []}))

@app.route('/api/launch_config', methods=['POST'])
def set_launch_config():
    try:
        _save_json(LAUNCH_CONFIG_FILE, request.json)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/launch_pressed', methods=['POST'])
def launch_pressed():
    try:
        idx = request.json.get('index')
        threading.Thread(target=_notify_pc, args=('/api/launch_pressed', {'index': idx}), daemon=True).start()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/media', methods=['GET'])
def get_media():
    return jsonify(state['media'])

@app.route('/api/sysinfo', methods=['GET'])
def get_sysinfo():
    try:
        with open('/proc/stat') as f:
            cpu_line = f.readline().split()
        idle  = int(cpu_line[4])
        total = sum(int(x) for x in cpu_line[1:])
        if not hasattr(get_sysinfo, '_last'):
            get_sysinfo._last = (idle, total)
        li, lt = get_sysinfo._last
        get_sysinfo._last = (idle, total)
        di, dt = idle - li, total - lt
        cpu_pct = round(100 * (1 - di / dt)) if dt else 0
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':')
                mem[k.strip()] = int(v.split()[0])
        ram_pct = round(100 * (1 - mem['MemAvailable'] / mem['MemTotal']))
        temp = 0
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                temp = round(int(f.read()) / 1000)
        except Exception:
            pass
        return jsonify({'cpu': cpu_pct, 'ram': ram_pct, 'temp': temp})
    except Exception as e:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'error': str(e)})

@app.route('/api/pc_sysinfo', methods=['GET'])
def get_pc_sysinfo():
    """Proxy PC's sysinfo (CPU/RAM/GPU temp) to the Pi UI."""
    pc_ip = state.get('pc_ip', '')
    if not pc_ip:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'temp_label': 'N/A', 'error': 'pc_ip not set'})
    try:
        r = requests.get(f'http://{pc_ip}:5001/api/sysinfo', timeout=3)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'cpu': 0, 'ram': 0, 'temp': 0, 'temp_label': 'N/A', 'error': str(e)})

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(_load_json(CONFIG_FILE, {'buttons': []}))

@app.route('/api/config', methods=['POST'])
def update_config():
    try:
        _save_json(CONFIG_FILE, request.json)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/test')
def test_page():
    pc_ip  = state.get('pc_ip') or 'not set'
    pc_ok  = False
    pc_msg = ''
    media_data = {}
    try:
        r = requests.get(f'http://{pc_ip}:5001/api/pc_status', timeout=2)
        pc_ok  = r.status_code == 200
        pc_msg = f'HTTP {r.status_code}'
    except Exception as e:
        pc_msg = str(e)
    try:
        r = requests.get(f'http://{pc_ip}:5001/api/media', timeout=4)
        media_data = r.json()
    except Exception:
        pass

    pot_cfg    = _load_json(POT_CONFIG_FILE, {})
    launch_cfg = _load_json(LAUNCH_CONFIG_FILE, {}).get('buttons', [])
    pc_dot  = '#2ecc71' if pc_ok  else '#e74c3c'
    med_dot = '#2ecc71' if media_data.get('title') else '#e67e22'
    spi_dot = '#2ecc71' if spi    else '#e74c3c'

    rows_pot = ''.join(
        f'<tr><td>POT {i}</td><td>{pot_cfg.get(str(i),"—")}</td>'
        f'<td style="color:#27ae60">{state["pot_values"][i] if state["pot_values"][i]>=0 else "—"}%</td></tr>'
        for i in range(8)
    )
    rows_launch = ''.join(
        f'<tr><td>{i}</td><td>{b.get("label","")}</td>'
        f'<td style="font-size:10px;color:#7f9ab0">{b.get("command","")}</td></tr>'
        for i, b in enumerate(launch_cfg)
    )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>RPi Audio Console — Diagnostics</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:monospace;background:#0d1520;color:#c8daf0;margin:0;padding:16px;font-size:13px}}
h2{{color:#3a8fd8;margin-bottom:4px}}h3{{color:#7faed0;margin:16px 0 6px}}
table{{border-collapse:collapse;width:100%;margin-bottom:12px}}
td,th{{border:1px solid #1e3048;padding:6px 10px;text-align:left}}
th{{background:#131e2e;color:#7f9ab0}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}}
.card{{background:#131e2e;border:1px solid #1e3048;border-radius:8px;padding:12px;margin-bottom:12px}}
.ok{{color:#2ecc71}}.warn{{color:#e67e22}}.err{{color:#e74c3c}}
button{{background:#1a4a8a;color:white;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;margin:4px}}
button:hover{{background:#2160b0}}
</style></head><body>
<h2>🔧 RPi Audio Console — Diagnostics</h2>
<div class="card"><b>Pi Flask Server</b><br>
  <span class="dot" style="background:#2ecc71"></span><span class="ok">Running</span>
  &nbsp;|&nbsp; PC IP: <b>{pc_ip}</b></div>
<div class="card"><b>SPI / MCP3008 Pot Reader</b><br>
  <span class="dot" style="background:{spi_dot}"></span>
  {'<span class="ok">Initialised — hardware pot reading active</span>' if spi else '<span class="err">NOT initialised — check wiring &amp; raspi-config SPI</span>'}</div>
<div class="card"><b>PC Connection</b><br>
  <span class="dot" style="background:{pc_dot}"></span>
  {'<span class="ok">Connected</span>' if pc_ok else f'<span class="err">FAILED — {pc_msg}</span>'}
  &nbsp;<button onclick="location.reload()">Recheck</button></div>
<div class="card"><b>Media (from PC)</b><br>
  <span class="dot" style="background:{med_dot}"></span>
  {f'<span class="ok">{media_data.get("title","?")} [{"Playing" if media_data.get("playing") else "Paused"}]</span>' if media_data.get("title") else '<span class="warn">No media / winsdk not installed</span>'}</div>
<h3>Potentiometers</h3>
<table><tr><th>Channel</th><th>Assigned App</th><th>Live Value</th></tr>{rows_pot}</table>
<h3>Quick Launch</h3>
<table><tr><th>#</th><th>Label</th><th>Command</th></tr>
{rows_launch or '<tr><td colspan=3 style="color:#e67e22">None configured</td></tr>'}</table>
<h3>Manual Tests</h3>
<button onclick="fetch('/api/pot_changed',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{channel:0,value:50}})}}).then(r=>r.json()).then(d=>alert(JSON.stringify(d)))">Test Pot Ch0 → 50%</button>
<button onclick="fetch('/api/status').then(r=>r.json()).then(d=>alert(JSON.stringify(d,null,2)))">Show Full Status</button>
<button onclick="fetch('/api/launch_pressed',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{index:0}})}}).then(r=>r.json()).then(d=>alert(JSON.stringify(d)))">Fire Launch Button 0</button>
<button onclick="fetch('/api/sysinfo').then(r=>r.json()).then(d=>alert(JSON.stringify(d,null,2)))">Sysinfo</button>
</body></html>"""

# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_time(s):
    s = max(0, int(s))
    return f'{s // 60}:{s % 60:02d}'

def _bg_media_poll():
    """Polls PC every 1s. Tracks position locally between polls so the
    UI always has a smooth, current value. Pre-computes display strings
    so the HTML does zero math."""
    local_pos  = 0.0
    last_tick  = time.monotonic()
    last_title = ''

    while True:
        now     = time.monotonic()
        elapsed = now - last_tick
        last_tick = now

        # Advance position locally if playing
        if state['media']['playing'] and state['media']['duration'] > 0:
            local_pos = min(state['media']['duration'], local_pos + elapsed)

        # Update display values every loop
        dur = state['media']['duration']
        state['media']['position']     = local_pos
        state['media']['position_pct'] = round(local_pos / dur * 100, 1) if dur else 0
        state['media']['elapsed_str']  = _fmt_time(local_pos)
        state['media']['duration_str'] = _fmt_time(dur)

        # Poll PC for ground truth every 1s
        time.sleep(0.5)
        pc_ip = state.get('pc_ip')
        if not pc_ip or not requests:
            continue
        try:
            r    = requests.get(f'http://{pc_ip}:5001/api/media', timeout=2)
            data = r.json()

            new_title   = data.get('title', '')
            new_playing = bool(data.get('playing', False))
            new_pos     = float(data.get('position') or 0)
            new_dur     = float(data.get('duration') or 0)

            # Snap local position if track changed or drifted > 3s
            if new_title != last_title or abs(new_pos - local_pos) > 3:
                local_pos = new_pos
            if not new_playing:
                local_pos = new_pos  # paused - trust PC exactly
            last_title = new_title

            state['media'].update({
                'title':   new_title,
                'artist':  data.get('artist', ''),
                'playing': new_playing,
                'source':  data.get('source', ''),
                'duration': new_dur,
            })
            print(f"[Media] {new_title} [{'Playing' if new_playing else 'Paused'}] {int(local_pos)}s/{int(new_dur)}s")
        except Exception as e:
            print(f'[Media] poll error: {e}')

# ── Track last physical pot movement time (to avoid overwriting active pots) ──
_pot_last_moved = [0.0] * 8   # epoch seconds of last hardware movement per channel
POT_LOCK_SECS   = 3.0         # seconds after pot moves before PC sync can overwrite it

def _bg_volume_sync_loop():
    """Background thread: polls PC /api/get_volumes every 2s and updates
    state['pot_values'] for any channel that hasn't been physically touched recently.
    This keeps the Pi UI in sync when volume is changed on the PC side."""
    POLL_INTERVAL = 2.0
    while True:
        time.sleep(POLL_INTERVAL)
        pc_ip = state.get('pc_ip')
        if not pc_ip or not requests:
            continue
        try:
            # Send Pi's pot_config so PC always knows the mapping
            pot_cfg = _load_json(POT_CONFIG_FILE, {str(i): '' for i in range(8)})
            r = requests.post(
                f'http://{pc_ip}:5001/api/get_volumes',
                json={'pot_config': pot_cfg},
                timeout=2
            )
            if r.status_code != 200:
                print(f'[VolSync] HTTP {r.status_code}')
                continue
            data = r.json()
            if data.get('status') != 'ok':
                print(f'[VolSync] bad status: {data}')
                continue
            volumes = data.get('volumes', {})
            if not volumes:
                print(f'[VolSync] empty volumes response')
                continue
            now = time.time()
            for ch_str, pct in volumes.items():
                ch = int(ch_str)
                if 0 <= ch < 8:
                    if now - _pot_last_moved[ch] > POT_LOCK_SECS:
                        state['pot_values'][ch] = int(pct)
                        print(f'[VolSync] ch{ch} <- {pct}% (PC update)')
        except Exception as e:
            print(f'[VolSync] poll error: {e}')

def load_startup_state():
    cfg = _load_json(PC_CONFIG_FILE, {})
    state['pc_ip'] = cfg.get('pc_ip')
    print(f'[Boot] PC IP: {state["pc_ip"]}' if state['pc_ip'] else '[Boot] No PC IP set')

def launch_chromium():
    import urllib.request as _ur
    for _ in range(20):
        try:
            _ur.urlopen('http://localhost:5000/api/status', timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    for binary in ['chromium-browser', 'chromium']:
        try:
            subprocess.Popen(
                [binary, '--kiosk', '--noerrdialogs', '--disable-infobars',
                 '--no-first-run', '--disable-session-crashed-bubble',
                 '--disable-restore-session-state',
                 '--check-for-update-interval=31536000',
                 '--password-store=basic',
                 '--app=http://localhost:5000/'],
                env={**os.environ, 'DISPLAY': ':0'}
            )
            print(f'[Chromium] Launched with {binary}')
            return
        except FileNotFoundError:
            continue
    print('[Chromium] Not found')

def signal_handler(sig, frame):
    print('\n[Flask] Shutting down...')
    if spi:
        spi.close()
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    _ensure_config_files()
    load_startup_state()
    threading.Thread(target=_spi_reader_loop,      daemon=True).start()
    threading.Thread(target=_bg_media_poll,        daemon=True).start()
    threading.Thread(target=_bg_volume_sync_loop,  daemon=True).start()
    threading.Thread(target=launch_chromium,       daemon=True).start()
    print(f'[Flask] Starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)

if __name__ == '__main__':
    main()
