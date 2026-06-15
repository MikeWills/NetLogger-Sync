# NetLogger Bridge

Watches the NetLogger contacts database and forwards new QSOs in real-time to:
- **WaveLog** — via HTTP REST API
- **N3FJP AC Log** — via TCP API (`ADDADIFRECORD` command)

Either or both outputs can be enabled independently.

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
poll_interval = 10          # seconds between DB polls
netlogger_db =              # leave blank to auto-detect
state_file = last_contact_id.txt

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

### 2. NetLogger database auto-detection

If `netlogger_db` is blank, the bridge looks here by default:

| OS      | Default path |
|---------|--------------|
| Windows | `%APPDATA%\NetLogger\contacts.db` |
| macOS   | `~/Library/Application Support/NetLogger/contacts.db` |
| Linux   | `~/.config/NetLogger/contacts.db` |

If your install differs, set the full path in `[general] netlogger_db`.

### 3. WaveLog setup

1. In WaveLog, go to **Admin → API** and generate an API key
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
```

Logs go to console and `netlogger_bridge.log`.

---

## Schema note

NetLogger's SQLite schema isn't publicly documented. On first run the bridge
logs the detected column names. If your column names differ from what's mapped,
edit the `COLUMN_TO_ADIF` dictionary near the top of `netlogger_bridge.py`.

Common column names the bridge already handles:
`Callsign`, `Call`, `QSODate`, `Date`, `TimeOn`, `Time`, `Band`, `Frequency`,
`Mode`, `RSTSent`, `RSTRcvd`, `Name`, `QTH`, `State`, `County`, `Country`,
`Comment`, `Notes`, `GridSquare`, `Grid`, `Operator`, `NetName`, `Power`

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
