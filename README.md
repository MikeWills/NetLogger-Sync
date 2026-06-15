# NetLogger Bridge

Tails NetLogger's `Contacts.adi` file and forwards new QSOs in real-time to:
- **WaveLog** — via HTTP REST API
- **N3FJP AC Log** — via TCP API (`ADDADIFRECORD` command)

Either or both outputs can be enabled independently.

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
url = https://log.example.com
api_key = YOUR_WAVELOG_API_KEY
station_id = 1

[n3fjp]
enabled = true
host = 127.0.0.1
port = 1100
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
