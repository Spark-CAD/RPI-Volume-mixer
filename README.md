# RPI Volume Mixer v4.4

This project is a DIY RPI project that allows you to individually control your Windows application’s volume levels.
Main features: 
- RPI hosted Web UI
- uses >60mb of memory and > 0.2Mbps of network (Max peak during testing was 0.9Mbps for >1sec) 
- Media controller 
- Audio spectrum visualiser
- Physical Potentiometer control
- Automatic or Manual application to controller assignment

also see [rpi-volume-mixer](https://makerworld.com/en/models/2481269-rpi-volume-mixer#profileId-2725270) for an example system.

## Architecture

```
RPi 4                              Windows PC
──────────────────                 ─────────────────────────
rpi_backend.py                     RPiConsole.exe (pc_bridge.py)
  FastAPI + uvicorn                  Control server  :5009  (main event loop)
  SPI reader (MCP3008)               FFT server      :5010  (own thread + loop)
  Browser WebSocket :5000/ws         pycaw  — volume control
  Chromium kiosk                     winrt  — media sessions
       │                             pyaudiowpatch — FFT loopback
       ├── ws://PC_IP:5009 ──────────── control channel
       └── ws://PC_IP:5010 ──────────── FFT-only channel
```

**Two WebSocket connections — why:**
- Port 5009 handles pots, vol_sync, media, app list — low frequency
- Port 5010 handles FFT visualiser frames only — up to 30 fps
- The FFT server runs in its own thread with its own asyncio event loop,
  completely isolated from the control channel
- vol_sync, media polls, and COM calls on 5009 cannot delay or stutter 5010

## Files

| File | Purpose |
|------|---------|
| `rpi_backend.py` | RPi backend — FastAPI, SPI reader, dual WS client |
| `pc_bridge.py` | PC bridge — dual WS server, volume, media, FFT |
| `console_ui.html` | Browser UI served by RPi |
| `install_rpi.sh` | One-shot RPi install |
| `build_pc.py` | Builds PC exe with PyInstaller |
| `mixer-console.service` | systemd unit for RPi |

## RPi Setup

```bash
# Clone or copy files to RPi, then:
bash install_rpi.sh
```

The script:
1. Installs chromium-browser, python3-pip
2. Enables SPI in /boot/firmware/config.txt
3. Installs fastapi uvicorn websockets spidev
4. Installs + starts the systemd service
5. Service auto-starts Chromium in kiosk mode

**After install:** open http://<rpi-ip>:5000 in any browser,
tap Settings and enter your PC's IP address.

## PC Setup

```bash
pip install websockets pycaw pystray pillow pyaudiowpatch numpy
# For media info (one of):
pip install winrt-Windows.Media.Control
# or
pip install winsdk

# Run directly:
python pc_bridge.py

# Or build exe:
pip install pyinstaller
python build_pc.py
# -> dist/RPiConsole.exe
```

feel free to build your own .exe or use the prebuilt .exe in the /dist directory

Auto-start: Win+R -> shell:startup -> drop shortcut to RPiConsole.exe

### Windows Firewall

Two inbound TCP rules are required — one per port:

```
Windows Defender Firewall -> Advanced Settings
-> Inbound Rules -> New Rule
  Rule type: Port
  Protocol: TCP
  Ports: 5009, 5010
  Action: Allow the connection
  Profile: Private
  Name: RPi Audio Console
```

Or via PowerShell (run as Administrator):

```powershell
New-NetFirewallRule -DisplayName "RPi Console Control" -Direction Inbound -Protocol TCP -LocalPort 5009 -Action Allow
New-NetFirewallRule -DisplayName "RPi Console FFT"     -Direction Inbound -Protocol TCP -LocalPort 5010 -Action Allow
```

## MCP3008 Wiring (unchanged from v3)

```
MCP3008 Pin   RPi Pin    Signal
──────────    ────────   ──────
16 (Vdd)  ->  1  (3.3V)
15 (Vref) ->  1  (3.3V)
14 (AGND) ->  6  (GND)
13 (CLK)  ->  23 (SCLK)
12 (Dout) ->  21 (MISO)
11 (Din)  ->  19 (MOSI)
10 (CS)   ->  24 (CE0)   <- use CE0, not CE1
9  (DGND) ->  6  (GND)
```

Potentiometers: wiper to CH0-CH7, one end to GND, other end to 3.3V.

## Channel Assignments

Tap any fader on the touchscreen to assign it:

| Option | Effect |
|--------|--------|
| Unassign | Pot does nothing |
| Master Volume | Controls system master volume |
| Auto (active app) | Round-robin assigned to a detected app |
| App name | Controls that specific app's volume |

Config saved to ~/mixer-console/config.json on RPi.

### Auto-assign behaviour

Pots set to Auto are distributed across detected audio sessions in round-robin
order, sorted alphabetically by app name. For example, with Discord, Spotify and
Steam running and three Auto pots:

```
Ch 1 -> Discord
Ch 2 -> Spotify
Ch 3 -> Steam
```

Rules:
- Each app appears on at most one Auto pot — no duplicates.
- Extra Auto pots beyond the number of running apps show "No app" and do nothing.
- If a new app starts (or an existing one closes), assignments are automatically
  rebuilt within ~2 seconds without needing to reconnect.
- Assignments are stable and do not change based on which app is loudest or
  currently focused.
- Apps contained in the blocklist are not shown
- Apps names are reset via the apps name list

* Access the blocklist and name list via right clicking on the pc taskbar widget 

If you want a specific app on a specific pot, pin it manually using the app name
assignment instead of Auto. Use Auto for the remainder to catch whatever else is running.

## App Name Display

Common process names are mapped to friendly display names automatically:

| Process | Display |
|---------|---------|
| msedge | Edge |
| chrome | Chrome |
| spotify | Spotify |
| discord | Discord |
| obs64 | OBS |
| epicgameslauncher | Epic Games |
| others | Title-cased process name |

## Troubleshooting

**No SPI / pots not working:**
```bash
ls /dev/spidev*    # Should show spidev0.0
sudo raspi-config  # Interface Options -> SPI -> Enable
```

**PC won't connect:**
- Check if the pc_bridge.py is running via task amanger either in a 'python' task if not using the .exe or RPiConslole.exe if you're using the .exe
- Check PC firewall allows ports 5009 and 5010 (TCP inbound) — see PC Setup above
- Verify IP in UI settings matches PC's local IP
- PC bridge log: run python pc_bridge.py in terminal — should show both
  [WS] Listening on ws://0.0.0.0:5009 and [FFT-WS] Listening on ws://0.0.0.0:5010

**FFT visualiser stutters or freezes every few seconds:**
- This was caused by vol_sync blocking the FFT send path on the same event loop
- Fixed in v4.2 — FFT now runs on a completely separate port and event loop
- Ensure you are running v4.2 of both pc_bridge.py and rpi_backend.py
- Verify port 5010 is open in Windows Firewall (RPi log should show
  [FFT] Connected to ws://PC_IP:5010)

**No FFT visualiser:**
- Install pyaudiowpatch: pip install pyaudiowpatch numpy
- Must have a WASAPI loopback device (standard on Windows 10+)
- Check RPi log for [FFT] Connected — if it shows Connection failed, port 5010
  is blocked by the firewall

**FFT visualiser to noisy or small**  
- locate the line 'REF_LEVEL'~line 556, in the pc_bridge.py file and modify its value to your liking

**No media info:**
- Install pip install winrt-Windows.Media.Control (or winsdk)
- Media must be playing in an app that exposes Windows media session
  (Spotify, Chrome/Edge with media, Windows Media Player, etc.)

**Auto pots all show the same app:**
- Ensure you are running v4.1 or later (round-robin auto-assign)
- Check the bridge log for [Auto] Assignments rebuilt: on connect — it should
  show different apps for each channel
- If only one app appears, verify the other apps have produced audio at least once
  since launch — Windows only registers a session after an app first plays sound