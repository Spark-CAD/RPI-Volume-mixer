# RPi Audio Console

A physical volume mixer built with a Raspberry Pi, potentiometers, and an MCP3008 ADC. Turn real knobs to control per-app volume on your Windows PC. The Pi runs a touchscreen UI; the PC runs a tray app that handles all audio routing, media info, and system stats.

```
┌─────────────────────┐         ┌──────────────────────────┐
│   Raspberry Pi      │  Wi-Fi  │   Windows PC             │
│                     │◄───────►│                          │
│  8x potentiometers  │         │  RPiConsole.exe (tray)   │
│  MCP3008 ADC        │         │  Per-app volume control  │
│  Touchscreen UI     │         │  Real-time audio peaks   │
│  Flask :5000        │         │  Flask :5001             │
└─────────────────────┘         └──────────────────────────┘
```

---

## Hardware Required

- Raspberry Pi (any model with SPI — 3B+, 4, Zero 2W tested)
- Touchscreen display (HDMI or DSI, any resolution)
- MCP3008 8-channel ADC (DIP-16 package)
- Up to 8x potentiometers (10kΩ recommended)
- Jumper wires

### MCP3008 Wiring

| MCP3008 Pin | Name     | Pi Pin | Pi Function |
|-------------|----------|--------|-------------|
| 16          | Vdd      | 1      | 3.3V        |
| 15          | Vref     | 1      | 3.3V        |
| 14          | AGND     | 6      | GND         |
| 13          | CLK      | 23     | SCLK        |
| 12          | Dout     | 21     | MISO        |
| 11          | Din      | 19     | MOSI        |
| 10          | CS/SHDN  | 22     | CE0         |
| 9           | DGND     | 6      | GND         |

Connect each potentiometer: outer pins to 3.3V and GND, wiper to CH0–CH7 on the MCP3008.

---

## Project Files

| File | Where it runs | Purpose |
|------|--------------|---------|
| `RPiConsole.exe` | Windows PC | Prebuilt pc_server.py + pc_ui.html|
| `rpi_controller.py` | Raspberry Pi | Flask server, SPI pot reader, proxies PC data |
| `rpi_ui.html` | Raspberry Pi | Touchscreen display UI |
| `pc_server.py` | Windows PC | Flask server, audio control, tray app |
| `pc_ui.html` | Windows PC | Browser-based control panel |
| `build.py` | Windows PC | Packages the PC side into `RPiConsole.exe` |
| `mixer-console.service` | Raspberry Pi | systemd service for auto-start on boot |

---

## Raspberry Pi Setup

### 1. Enable SPI

```bash
sudo raspi-config
# Interface Options → SPI → Enable
sudo reboot
```

### 2. Install dependencies

```bash
pip install flask flask-cors requests spidev
```

### 3. Copy files

```bash
mkdir ~/volume-controller
cp rpi_controller.py ~/volume-controller/
cp rpi_ui.html ~/volume-controller/
```

### 4. Run manually (to test)

```bash
cd ~/volume-controller
python3 rpi_controller.py
```

Chromium opens automatically in kiosk mode at `http://localhost:5000`. Open a browser there manually if it doesn't.

### 5. Auto-start on boot (systemd service)

Open `mixer-console.service` and replace `YOUR_USERNAME` with your Pi username (run `whoami` if unsure), then:

```bash
sudo cp mixer-console.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mixer-console
sudo systemctl start mixer-console
```

Check status and logs:
```bash
sudo systemctl status mixer-console
journalctl -u mixer-console -f
```

> **Note:** The service requires a desktop session. Ensure your Pi is set to boot to desktop with auto-login: `sudo raspi-config` → System Options → Boot / Auto Login → Desktop Autologin.

---

## Windows PC Setup

### Option A — Run from source

```bash
pip install flask flask-cors psutil requests pycaw winsdk pystray pillow
python pc_server.py
```

### Option B — Build a standalone .exe

```bash
pip install pyinstaller pystray pillow
python build.py
# Output: dist/RPiConsole.exe
```
To auto-start with Windows: press `Win+R`, type `shell:startup`, drop a shortcut to `RPiConsole.exe` in that folder.

### Option C — Use prebuilt .exe

```bash
use the prebuilt exe file in the pc-app directory. 
```
To auto-start with Windows: press `Win+R`, type `shell:startup`, drop a shortcut to `RPiConsole.exe` in that folder.


### Connecting Pi to PC

1. Open `http://localhost:5001` in a browser
2. Go to **Settings**, enter the Pi's IP address (`hostname -I` on the Pi)
3. Click **Set** — the PC immediately registers its own IP with the Pi so pot movements start flowing

The PC saves the Pi IP to `~/.rpi_console/settings.json` and re-registers on every restart, so you only need to do this once.

---

## PC Control Panel — `http://localhost:5001`

### Overview Tab

Live status at a glance: media title/artist, playback controls, CPU/RAM/GPU temperature, backend status pills, and all 8 channels with their current app and volume.

The **header dot** shows Pi connection status — green means the Pi is actively responding to pings, red means unreachable. The **✕** button next to the IP field clears the saved IP so you can enter a new one.

### Pots Tab

Assign apps to each of the 8 channels:

- Type an app name (e.g. `Spotify.exe`, `chrome.exe`) or pick from the live detected app list
- Use `Master` for Windows master volume
- Use `__auto__` for Auto-detect mode (see below)
- **ADC Tuning** — push poll rate, noise threshold, and debounce settings directly to the Pi

### Launch Buttons Tab

Up to 10 shortcut buttons on the Pi touchscreen. Each has a label, icon emoji, shell command to run on the PC, and accent colour.

### Display Tab

- **Right widget** — VU meter, Clock, or PC System stats
- **Left widget** — Album disc, Circular VU, Waveform, or Spectrum analyser
- **Track title scrolling** — enable/disable and set interval

### Settings Tab

Pi IP address and connection status.

### Diagnostics Tab

Raw dump of all 8 channels — useful for debugging connections.

---

## Pi Touchscreen UI

**Left column** — album art / visualiser (swipe left/right to cycle):
- Spinning album disc
- Circular VU meter (real audio data, L/R channels)
- Waveform oscilloscope (real audio amplitude)
- Spectrum analyser (real L/R frequency energy)

**Centre column** — 8 potentiometer knobs. Each shows channel number, AUTO/MAN pill, a lit dial in the app's colour, volume percentage, and app name.

**Right column** — widget (tap to cycle):
- **VU** — real L/R output level bars
- **Clock** — time and date
- **PC System** — CPU, RAM, GPU temp

**Launch bar** — shortcut buttons along the bottom.

> All four visualisations show **real audio data** from the PC via `IAudioMeterInformation`. The VU bars show true stereo L/R levels. The waveform amplitude and spectrum heights reflect actual output levels — they go flat when nothing is playing.

---

## Auto-Detect Mode (`__auto__`)

Setting a channel to `__auto__` makes it automatically follow whichever app is producing audio.

**How it works:**
- The PC scans all Windows audio sessions every 0.5 seconds
- Active apps are ranked by **priority score** — cumulative play-time, saved to `~/.rpi_console/app_priority.json` and persisted across restarts
- Highest-priority app claims the lowest-numbered auto channel; next highest claims the next, and so on
- **One app per channel** — idle channels stay dark until a new app opens
- **Pausing holds the slot** — an app keeps its channel as long as its audio session is open, even if silent for hours
- The Pi screen shows the resolved app name and lights the knob in that app's colour
- The pot config is saved to `~/.rpi_console/pot_config.json` and reloaded on restart, so auto channels are known immediately without waiting for the Pi to sync

---

## Real-Time Audio Peaks

The PC samples audio peak levels every 60ms using `IAudioMeterInformation.GetChannelsPeakValues`, giving true stereo L and R values. These are served via `/api/peaks` and proxied through the Pi to drive all four visualisations.

- Left half of circular VU ring = L channel; right half = R channel
- Left half of spectrum bars = L channel; right half = R channel
- Waveform amplitude scales with master output level
- VU bars show smoothed L/R directly

All animations freeze to their idle state within ~1.5 seconds of losing connection to the Pi.

---

## Known App IDs

| String | App |
|--------|-----|
| `Master` | Windows master volume |
| `__auto__` | Auto-detect |
| `Spotify.exe` | Spotify |
| `chrome.exe` | Google Chrome |
| `discord.exe` | Discord |
| `firefox.exe` | Firefox |
| `msedge.exe` | Microsoft Edge |
| `vlc.exe` | VLC |
| `steam.exe` | Steam |
| `foobar2000.exe` | Foobar2000 |
| `epicgameslauncher.exe` | Epic Games |

Any `.exe` name works — use **Refresh Apps** in the Pots tab to see apps with active audio sessions.

---

## Config Files

### Raspberry Pi (`~/`)

| File | Contents |
|------|----------|
| `pot_config.json` | Channel → app assignments |
| `launch_config.json` | Launch button definitions |
| `widget_config.json` | Right panel widget |
| `left_widget_config.json` | Left panel widget |
| `display_config.json` | Scroll settings |
| `pc_connection.json` | PC IP address |

### Windows PC (`~/.rpi_console/`)

| File | Contents |
|------|----------|
| `settings.json` | Pi IP address |
| `pot_config.json` | Saved channel assignments (loaded on boot) |
| `launch_config.json` | Launch button commands |
| `app_priority.json` | Auto-detect priority scores |

---

## Network Traffic

The system is designed to minimise unnecessary traffic:

- **Pot movements** only send to PC when the ADC reading changes beyond the configured threshold
- **Volume sync** (Pi → PC, every 2s) returns `204 No Content` if values haven't changed — no processing on either side
- **Media poll** (Pi → PC, every 0.5s) returns `204 No Content` if the track title is unchanged
- **Auto-detect** only calls Windows audio APIs and pushes display names when app assignments or pot values actually change
- **Audio peaks** are polled every 60ms — a separate lightweight endpoint that doesn't touch the volume API

---

## Troubleshooting

**Pots don't control volume / Pi shows no app names**
The Pi needs to know the PC's IP to send pot movements. Open the PC control panel, go to Settings, and set the Pi IP — this also registers the PC's IP with the Pi automatically.

**Auto channels show "AUTO" instead of the app name**
The PC needs at least one pot config sync from the Pi before it knows which channels are `__auto__`. Turn a pot or wait a few seconds for the volume sync loop to run.

**No apps in the Pots dropdown**
Run `pip install pycaw` and restart the PC server.

**Media info missing / playback controls don't work**
Run `pip install winsdk` and restart.

**SPI not working**
Run `sudo raspi-config` → Interface Options → SPI → Enable, then reboot. Double-check MCP3008 wiring.

**GPU temp shows 0**
Reads via `nvml.dll` (Nvidia). AMD GPUs fall back to WMI via OpenHardwareMonitor if running, then CPU temp.

**Visualisations stay animated when Pi is disconnected**
After 3 consecutive failed polls (~1.5s), all animations drop to idle automatically.
