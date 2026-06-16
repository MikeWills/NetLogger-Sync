# NetLogger Bridge

Tails NetLogger's `Contacts.adi` file and forwards new QSOs in real-time to:
- **WaveLog** — via HTTP REST API
- **N3FJP AC Log** — via its TCP server
- **N1MM Logger+** — via WSJT-X binary UDP "Log QSO" packet (same as GridTracker2)
- **Ham Radio Deluxe (HRD) Logbook** — via N1MM-compatible UDP XML
- **Log4OM v2** — via UDP inbound ADIF
- **DXLab Suite DXKeeper** — via TCP `externallog` command

Any combination of outputs can be enabled independently.

---

## Easy install (no Python required)

1. Go to the [Releases](https://github.com/MikeWills/NetLogger-Sync/releases) page
2. Download the zip for your platform from the latest release:
   - `NetLogger-Bridge-Windows.zip`
   - `NetLogger-Bridge-macOS.zip`
   - `NetLogger-Bridge-Linux.zip`
3. Extract it
4. Run `netlogger_gui` (`netlogger_gui.exe` on Windows) — a window opens where
   you can fill in your WaveLog/N3FJP details, click **Save Config**, then
   **Start**. The log pane shows activity live.

A command-line `netlogger_bridge` executable is also included for running
without the GUI (e.g. as a background service — see below).

This is the recommended option for club members who don't have Python installed.
The sections below describe the Python-based setup, used for development.

---

## Requirements

- Python 3.10+
- `requests` library

```bash
pip install requests
```

---

## Setup

### 1. Generate config

```bash
python netlogger_bridge.py --create-config
```

This creates `config.ini`. Edit it:

```ini
[general]
poll_interval = 10          # seconds between file polls
contacts_adi =               # leave blank to auto-detect
state_file = last_offset.txt

[wavelog]
enabled = true
url = https://log.example.com/index.php
api_key = YOUR_WAVELOG_API_KEY
station_id = 1

[n3fjp]
enabled = true
host = 127.0.0.1
port = 1100

[n1mm]
enabled = false
host = 127.0.0.1
port = 2237
my_call = W1AW

[hrd]
enabled = false
host = 127.0.0.1
port = 12060
my_call = W1AW

[log4om]
enabled = false
host = 127.0.0.1
port = 2237

[dxkeeper]
enabled = false
host = 127.0.0.1
port = 52001
```

### 2. NetLogger Contacts.adi auto-detection

If `contacts_adi` is blank, the bridge looks here by default:

| OS      | Default path |
|---------|--------------|
| Windows | `%APPDATA%\NetLogger\Contacts.adi` |
| macOS   | `~/Library/Application Support/NetLogger/Contacts.adi` |
| Linux   | `~/.config/NetLogger/Contacts.adi` |

If your install differs, set the full path in `[general] contacts_adi`.

> **Note:** On first run, the bridge starts reading from the *end* of the
> existing `Contacts.adi` file — only QSOs logged after that point are
> forwarded. The current byte offset is saved to `state_file` so restarts
> resume from where they left off.

### 3. WaveLog setup

1. In WaveLog, go to **Admin → API Keys** and generate an API key
2. Set `url`, `api_key`, and `station_id` in `config.ini`
3. Set `enabled = true`

API reference: https://docs.wavelog.org/developer/api/

### 4. N3FJP setup

1. In N3FJP AC Log, go to **Settings → Application Program Interface (API)**
2. Check **TCP API Enabled (Server)**
3. Note the port (default: `1100`)
4. Set `host` and `port` in `config.ini`
5. Set `enabled = true`

> **Note:** N3FJP AC Log runs on Windows only. The bridge client can run
> on any platform, but N3FJP must be reachable over the network.

API reference: http://www.n3fjp.com/help/api.html

### 5. N1MM Logger+ setup

The bridge sends QSOs as WSJT-X binary UDP "Log QSO" packets (type 5, schema 3)
— the same method used by GridTracker2 and JTAlert.

1. In N1MM: **Config → Configure Ports → WSJT-X tab**
2. Check **Enable WSJT-X Decode List**
3. Note the UDP port (default: `2237`)
4. Set `host`, `port`, and `my_call` (your station callsign) in `config.ini`
5. Set `enabled = true`

> **Note:** N1MM Logger+ runs on Windows only.

API reference: https://n1mmwp.hamdocs.com/manual-windows/wsjt-x-decode-list-window/

### 7. Ham Radio Deluxe (HRD) setup

The bridge sends QSOs using N1MM-compatible UDP XML, which HRD Logbook natively accepts.

1. In HRD Logbook, go to **Tools → QSO Forwarding**
2. Enable the **N1MM** source and set the port (default: `12060`)
3. Set `host`, `port`, and `my_call` (your station callsign) in `config.ini`
4. Set `enabled = true`

> **Note:** HRD Logbook is Windows-only commercial software. The bridge can
> run on any platform as long as HRD is reachable over the network.

### 8. Log4OM v2 setup

The bridge sends QSOs as plain ADIF records over UDP to Log4OM's inbound ADIF service.

1. In Log4OM, go to **Communicator → Inbound Connections**
2. Click **Add**, select type **ADIF**, and enter a port number (e.g. `2237`)
3. Click the **+** button to activate the listener
4. Set `host` and `port` in `config.ini` to match
5. Set `enabled = true`

> **Note:** The port is freely configurable — pick any unused port and make
> sure Log4OM's inbound connection uses the same number.

API reference: Log4OM forum — Communicator > Inbound Connections > ADIF

### 9. DXLab Suite DXKeeper setup

The bridge connects to DXKeeper's TCP port and issues an `externallog` command.

1. Ensure DXKeeper is running
2. Note the base port: in DXKeeper go to **Config → Ports**; DXKeeper listens on
   **base port + 1** (default base is `52000`, so DXKeeper uses `52001`)
3. Set `host` and `port` in `config.ini`
4. Set `enabled = true`

API reference: https://www.dxlabsuite.com/Interoperation.htm

---

## Running

```bash
# Basic
python netlogger_bridge.py

# Custom config file
python netlogger_bridge.py /path/to/myconfig.ini

# GUI (config editor + start/stop + live log)
python netlogger_gui.py
```

The GUI requires Tk, which ships with most Python installs. On some Linux
distros, install it separately: `sudo apt install python3-tk`.

Logs go to console and `netlogger_bridge.log`.

---

## Running as a background service

The GUI has a **"Run automatically at login (background)"** checkbox that sets
this up for you — it registers the headless CLI bridge (pointed at the GUI's
`config.ini`) with Task Scheduler (Windows), launchd (macOS), or a systemd user
service (Linux), and unregisters it when unchecked. Checking it also starts
the bridge immediately (not just at the next login), as long as it isn't
already running. On Windows, if Task Scheduler reports access denied, a UAC
prompt will appear — approve it to register the task. On all three platforms,
the bridge is automatically restarted if it crashes or is killed. The sections
below describe doing this manually.

The GUI's **"Bridge process"** indicator shows whether the bridge is currently
running, whether it was started from the GUI's Start button or by the
autostart task/service — it checks a PID file (`netlogger_bridge.pid`) that
the bridge writes on startup and removes on exit. If you click **Start** while
another instance is already running (e.g. the autostart task), the GUI warns
you before launching a second one.

### Windows (Task Scheduler)
- Action: `python C:\path\to\netlogger_bridge.py`
- Trigger: At log on / At startup

### macOS (launchd)
Create `~/Library/LaunchAgents/com.wx0mik.netloggerbridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.wx0mik.netloggerbridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/netlogger_bridge.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.wx0mik.netloggerbridge.plist`

### Linux (systemd)
```ini
[Unit]
Description=NetLogger Bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/netlogger_bridge.py
Restart=always

[Install]
WantedBy=default.target
```
