# NetLogger Bridge

Tails NetLogger's `Contacts.adi` file and forwards new QSOs in real-time to:
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
```

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
