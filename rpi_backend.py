#!/usr/bin/env python3
"""
RPi Audio Console — Backend  (v4 clean rebuild)
================================================
Architecture:
  • FastAPI + uvicorn   — single async process, no thread soup
  • One WebSocket to PC — bidirectional, replaces all HTTP polling
    PC pushes: media updates, volume acks, FFT frames, app list
    RPi pushes: pot moves, media commands, button presses
  • SPI reader          — asyncio-friendly, runs in executor
  • Chromium kiosk      — launched after server is ready

MCP3008 wiring (unchanged from v3):
  Vdd/Vref -> 3.3V   AGND/DGND -> GND
  CLK -> SCLK  Dout -> MISO  Din -> MOSI  CS -> CE0

Config files (~/mixer-console/):
  config.json          — PC IP, pot assignments, UI prefs
"""

import asyncio, json, os, signal, subprocess, sys, time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE    = Path.home() / 'mixer-console'
CFG     = BASE / 'config.json'
BASE.mkdir(exist_ok=True)

DEFAULT_CFG = {
    'pc_ip': '',
    'pots': {str(i): '' for i in range(8)},   # '' | 'appname' | '__auto__' | '__master__'
    'ui': {
        'theme': 'dark',
        'right_widget': 'fft',                 # 'fft' | 'clock' | 'sysinfo'
    }
}

def load_cfg() -> dict:
    if CFG.exists():
        try:
            d = json.loads(CFG.read_text())
            # Merge missing keys from default
            for k, v in DEFAULT_CFG.items():
                if k not in d:
                    d[k] = v
            return d
        except Exception:
            pass
    CFG.write_text(json.dumps(DEFAULT_CFG, indent=2))
    return dict(DEFAULT_CFG)

def save_cfg(cfg: dict):
    CFG.write_text(json.dumps(cfg, indent=2))

# ── Shared state ───────────────────────────────────────────────────────────────
cfg = load_cfg()

state = {
    'pot_values':  [50] * 8,
    'pot_display': [''] * 8,   # resolved names pushed by PC for __auto__ channels
    'apps':        [],          # list of {id, name} from PC
    'media': {
        'title': '', 'artist': '', 'album_art': '',
        'playing': False, 'source': '',
        'position': 0, 'duration': 0,
        'elapsed_str': '0:00', 'duration_str': '0:00',
    },
    'fft': [0] * 64,
    'peaks': {'l': 0.0, 'r': 0.0},
    'pc_connected': False,
}

# ── PC WebSocket connection ────────────────────────────────────────────────────
# Single persistent WS connection: RPi connects to PC as a client.
# All comms flow over this one socket. Much simpler than HTTP polling.

_pc_ws = None          # the live websocket connection or None
_pc_ws_lock = asyncio.Lock()

async def _send_to_pc(msg: dict):
    """Fire-and-forget send to PC. Silently drops if not connected."""
    ws = _pc_ws
    if ws is None:
        return
    try:
        await ws.send(json.dumps(msg))
    except Exception:
        pass

# ── Browser WebSocket clients ──────────────────────────────────────────────────
_browser_clients: set = set()

async def _broadcast(msg: dict):
    """Send a message to all connected browser tabs."""
    if not _browser_clients:
        return
    data = json.dumps(msg)
    dead = set()
    for ws in list(_browser_clients):
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    _browser_clients.difference_update(dead)

# ── SPI / pot reader ──────────────────────────────────────────────────────────
SPI_BUS      = 0
SPI_DEV      = 0
SPI_SPEED    = 1_350_000
RAW_THRESH   = 8
PCT_THRESH   = 1
DEBOUNCE_S   = 0.12
POLL_HZ      = 15

_spi         = None
_raw_vals    = [-1] * 8
_last_pct    = [-1] * 8
_last_moved  = [0.0] * 8
_deb_tasks   = [None] * 8   # asyncio Tasks for debounce

def _init_spi() -> bool:
    global _spi
    try:
        # spidev may be system-installed outside the venv — add fallback path
        try:
            import spidev
        except ImportError:
            import sys, os
            sys.path.insert(0, '/usr/lib/python3/dist-packages')
            import spidev
        _spi = spidev.SpiDev()
        _spi.open(SPI_BUS, SPI_DEV)
        _spi.max_speed_hz = SPI_SPEED
        _spi.mode = 0
        print('[SPI] MCP3008 ready')
        return True
    except Exception as e:
        print(f'[SPI] Not available: {e}')
        return False

def _read_channel(ch: int) -> int:
    r = _spi.xfer2([0x01, 0x80 | (ch << 4), 0x00])
    return ((r[1] & 0x03) << 8) | r[2]

def _raw_to_pct(raw: int) -> int:
    return round(raw / 1023 * 100)

async def _fire_pot(ch: int, pct: int, is_boot: bool = False):
    """Called after debounce — update state and notify PC + browser."""
    app_id = cfg['pots'].get(str(ch), '').strip()
    state['pot_values'][ch] = pct

    await _broadcast({'type': 'pot', 'ch': ch, 'value': pct,
                      'display': state['pot_display'][ch]})

    # Don't send vol to PC on boot — only when the user physically moves the pot
    if app_id and not is_boot:
        _last_moved[ch] = time.time()
        await _send_to_pc({'type': 'vol', 'ch': ch, 'value': pct, 'app': app_id})

async def _spi_loop():
    """Async pot polling loop — runs in the event loop, blocking read in executor."""
    if not _init_spi():
        print('[SPI] Disabled — no hardware or spidev')
        # Seed state so UI shows 50% on all channels
        for ch in range(8):
            state['pot_values'][ch] = 50
        return

    loop = asyncio.get_event_loop()
    interval = 1.0 / POLL_HZ

    # Initial read — populate state silently, no vol messages to PC
    for ch in range(8):
        try:
            raw = await loop.run_in_executor(None, _read_channel, ch)
            _raw_vals[ch] = raw
            pct = _raw_to_pct(raw)
            _last_pct[ch] = pct
            state['pot_values'][ch] = pct
        except Exception:
            pass

    print(f'[SPI] Polling at {POLL_HZ} Hz')
    while True:
        t0 = time.monotonic()
        for ch in range(8):
            try:
                raw   = await loop.run_in_executor(None, _read_channel, ch)
                delta = abs(raw - _raw_vals[ch])
                if delta >= RAW_THRESH:
                    _raw_vals[ch] = raw
                    pct = _raw_to_pct(raw)
                    state['pot_values'][ch] = pct

                    if abs(pct - _last_pct[ch]) < PCT_THRESH:
                        continue

                    _last_pct[ch] = pct

                    # Cancel existing debounce, schedule new one
                    if _deb_tasks[ch] and not _deb_tasks[ch].done():
                        _deb_tasks[ch].cancel()
                    _deb_tasks[ch] = asyncio.create_task(
                        _debounced_fire(ch, pct)
                    )
            except Exception as e:
                print(f'[SPI] ch{ch} error: {e}')

        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.0, interval - elapsed))


async def _debounced_fire(ch: int, pct: int):
    await asyncio.sleep(DEBOUNCE_S)
    await _fire_pot(ch, pct)

# ── PC WebSocket connector ─────────────────────────────────────────────────────

async def _handle_pc_message(msg: str):
    """Process a message pushed from the PC."""
    global state, cfg
    try:
        d = json.loads(msg)
    except Exception:
        return
    t = d.get('type', '')

    if t == 'media':
        state['media'].update(d.get('data', {}))
        await _broadcast({'type': 'media', 'data': state['media']})

    elif t == 'apps':
        state['apps'] = d.get('apps', [])
        await _broadcast({'type': 'apps', 'apps': state['apps']})

    elif t == 'pot_display':
        for ch_str, name in d.get('data', {}).items():
            try:
                state['pot_display'][int(ch_str)] = name or ''
            except Exception:
                pass
        await _broadcast({'type': 'pot_display', 'data': state['pot_display']})

    elif t == 'vol_sync':
        # PC sends current Windows volumes; update channels not recently touched
        now = time.time()
        changed = []
        for ch_str, pct in d.get('volumes', {}).items():
            ch = int(ch_str)
            if 0 <= ch < 8 and now - _last_moved[ch] > 3.0:
                new_val = int(pct)
                if state['pot_values'][ch] != new_val:
                    state['pot_values'][ch] = new_val
                    _last_pct[ch] = new_val
                    changed.append({'ch': ch, 'value': new_val})
        if changed:
            await _broadcast({'type': 'vol_sync', 'changes': changed})

    elif t == 'pong':
        pass  # heartbeat ack

async def _pc_connector():
    """Persistent connection to PC. Reconnects automatically."""
    global _pc_ws, state
    import websockets

    while True:
        pc_ip = cfg.get('pc_ip', '').strip()
        if not pc_ip:
            await asyncio.sleep(5)
            cfg.update(load_cfg())
            continue

        url = f'ws://{pc_ip}:5009'
        try:
            print(f'[PC] Connecting to {url} ...', flush=True)
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=10,
                max_size=2**20, open_timeout=10
            ) as ws:
                _pc_ws = ws
                state['pc_connected'] = True
                print(f'[PC] Connected to {url}', flush=True)
                await _broadcast({'type': 'pc_status', 'connected': True})

                await ws.send(json.dumps({
                    'type': 'hello',
                    'pots': cfg['pots'],
                    'pot_values': state['pot_values'],
                }))

                async for msg in ws:
                    await _handle_pc_message(msg)

        except Exception as e:
            print(f'[PC] Connection to {url} failed: {e}', flush=True)
        finally:
            _pc_ws = None
            state['pc_connected'] = False
            await _broadcast({'type': 'pc_status', 'connected': False})

        await asyncio.sleep(5)

async def _fft_connector():
    """Persistent connection to the PC FFT server on port 5010.
    Completely separate from the control channel — receives only FFT frames
    and forwards them to browsers without ever touching the control socket."""
    global state
    import websockets

    while True:
        pc_ip = cfg.get('pc_ip', '').strip()
        if not pc_ip:
            await asyncio.sleep(5)
            continue

        url = f'ws://{pc_ip}:5010'
        try:
            print(f'[FFT] Connecting to {url} ...', flush=True)
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=10,
                max_size=2**20, open_timeout=10
            ) as ws:
                print(f'[FFT] Connected to {url}', flush=True)
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        if d.get('type') == 'fft':
                            state['fft']        = d.get('fft', state['fft'])
                            state['peaks']['l'] = d.get('l', 0)
                            state['peaks']['r'] = d.get('r', 0)
                            await _broadcast({'type': 'fft',
                                              'fft': state['fft'],
                                              'l':   state['peaks']['l'],
                                              'r':   state['peaks']['r']})
                    except Exception:
                        pass
        except Exception as e:
            print(f'[FFT] Connection to {url} failed: {e}', flush=True)

        await asyncio.sleep(5)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title='RPi Audio Console')

UI_FILE = BASE / 'console_ui.html'

@app.get('/', response_class=HTMLResponse)
async def serve_ui():
    if UI_FILE.exists():
        return HTMLResponse(UI_FILE.read_text())
    return HTMLResponse('<h1>console_ui.html not found</h1>', status_code=404)

@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _browser_clients.add(ws)
    print(f'[WS] Browser connected ({len(_browser_clients)} total)')
    # Send full state on connect so UI populates immediately
    await ws.send_text(json.dumps({
        'type': 'init',
        'pots':        state['pot_values'],
        'pot_display': state['pot_display'],
        'pot_config':  cfg['pots'],
        'apps':        state['apps'],
        'media':       state['media'],
        'fft':         state['fft'],
        'peaks':       state['peaks'],
        'pc_connected': state['pc_connected'],
        'cfg':          cfg,
    }))
    try:
        while True:
            raw = await ws.receive_text()
            await _handle_browser_message(ws, raw)
    except WebSocketDisconnect:
        pass
    finally:
        _browser_clients.discard(ws)
        print(f'[WS] Browser disconnected ({len(_browser_clients)} total)')

async def _handle_browser_message(ws: WebSocket, raw: str):
    """Handle commands sent from the browser UI."""
    global cfg
    try:
        d = json.loads(raw)
    except Exception:
        return
    t = d.get('type', '')

    if t == 'set_pc_ip':
        cfg['pc_ip'] = d.get('ip', '').strip()
        save_cfg(cfg)
        await ws.send_text(json.dumps({'type': 'cfg_saved', 'key': 'pc_ip'}))
        # Trigger reconnect by resetting connector (it'll re-read cfg)
        if _pc_ws:
            try:
                await _pc_ws.close()
            except Exception:
                pass

    elif t == 'set_pot':
        ch  = int(d.get('ch', 0))
        app = d.get('app', '').strip()
        cfg['pots'][str(ch)] = app
        save_cfg(cfg)
        # Clear stale display name whenever assignment changes
        state['pot_display'][ch] = ''
        # Push updated config to PC
        await _send_to_pc({'type': 'pot_config', 'pots': cfg['pots']})
        # Apply current pot value immediately — but NOT for auto (would stomp active app)
        pct = state['pot_values'][ch]
        if app and app != '__auto__':
            await _send_to_pc({'type': 'vol', 'ch': ch, 'value': pct, 'app': app})
        await _broadcast({'type': 'pot_config', 'pots': cfg['pots']})

    elif t == 'media_cmd':
        # play/pause/next/prev
        await _send_to_pc({'type': 'media_cmd', 'cmd': d.get('cmd')})

    elif t == 'set_cfg':
        key = d.get('key')
        val = d.get('value')
        if key in cfg:
            cfg[key] = val
            save_cfg(cfg)
        await _broadcast({'type': 'cfg', 'key': key, 'value': val})

    elif t == 'get_apps':
        await _send_to_pc({'type': 'get_apps'})

    elif t == 'ping':
        await ws.send_text(json.dumps({'type': 'pong'}))

# ── REST endpoints (for initial page load / settings form fallback) ────────────

@app.get('/api/status')
async def api_status():
    return {
        'pc_connected': state['pc_connected'],
        'pot_values':   state['pot_values'],
        'pot_config':   cfg['pots'],
        'media':        state['media'],
        'pc_ip':        cfg.get('pc_ip', ''),
    }

@app.post('/api/media_push')
async def media_push(req: Request):
    """PC can also push media updates via HTTP if WS unavailable."""
    d = await req.json()
    state['media'].update(d)
    await _broadcast({'type': 'media', 'data': state['media']})
    return {'status': 'ok'}

# ── Sysinfo ────────────────────────────────────────────────────────────────────

async def _sysinfo_loop():
    """Push sysinfo to browsers every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        if not _browser_clients:
            continue
        try:
            with open('/proc/stat') as f:
                cpu_line = f.readline().split()
            idle  = int(cpu_line[4])
            total = sum(int(x) for x in cpu_line[1:])
            if not hasattr(_sysinfo_loop, '_last'):
                _sysinfo_loop._last = (idle, total)
            li, lt = _sysinfo_loop._last
            _sysinfo_loop._last = (idle, total)
            di, dt = idle - li, total - lt
            cpu = round(100 * (1 - di / dt)) if dt else 0

            mem = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    k, v = line.split(':')
                    mem[k.strip()] = int(v.split()[0])
            total_mb = mem.get('MemTotal', 1) / 1024
            avail_mb = mem.get('MemAvailable', 0) / 1024
            used_mb  = total_mb - avail_mb
            mem_pct  = round(used_mb / total_mb * 100)

            # CPU temp
            temp = None
            tp = Path('/sys/class/thermal/thermal_zone0/temp')
            if tp.exists():
                temp = round(int(tp.read_text()) / 1000, 1)

            await _broadcast({'type': 'sysinfo',
                              'cpu': cpu, 'mem_pct': mem_pct,
                              'mem_used_mb': round(used_mb),
                              'mem_total_mb': round(total_mb),
                              'temp': temp})
        except Exception:
            pass

# ── Chromium launcher ─────────────────────────────────────────────────────────

async def _launch_chromium():
    """Wait for uvicorn to be ready, then open kiosk Chromium."""
    import urllib.request as ur
    for _ in range(60):
        try:
            ur.urlopen('http://localhost:5000/', timeout=1)
            break
        except Exception:
            await asyncio.sleep(0.5)

    env = {**os.environ, 'DISPLAY': ':0'}
    flags = [
        '--kiosk', '--noerrdialogs', '--disable-infobars',
        '--no-first-run', '--disable-session-crashed-bubble',
        '--disable-restore-session-state',
        '--disable-features=TranslateUI',
        '--check-for-update-interval=31536000',
        '--password-store=basic',
        '--app=http://localhost:5000/',
    ]
    for binary in ['chromium', 'chromium-browser']:
        try:
            subprocess.Popen([binary] + flags, env=env)
            print(f'[Chromium] Launched with {binary}')
            return
        except FileNotFoundError:
            continue
    print('[Chromium] Not found — open http://localhost:5000 manually')

# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event('startup')
async def startup():
    asyncio.create_task(_spi_loop())
    asyncio.create_task(_pc_connector())
    asyncio.create_task(_fft_connector())
    asyncio.create_task(_sysinfo_loop())
    asyncio.create_task(_launch_chromium())
    print('[Console] RPi backend started on port 5000')

def main():
    uvicorn.run(
        'rpi_backend:app',
        host='0.0.0.0', port=5000,
        log_level='warning',
        access_log=False,
    )

if __name__ == '__main__':
    main()
