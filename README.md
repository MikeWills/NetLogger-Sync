# NetLogger Bridge

Reads NetLogger's `Contacts.adi` file and forwards new QSOs in near real-time to:
- **WaveLog**
- **N3FJP AC Log**
- **N1MM Logger+**
- **Ham Radio Deluxe (HRD) Logbook**
- **Log4OM v2**
- **DXLab Suite DXKeeper**
- **MacLoggerDX** (Mac-only; untested, see setup section below)
- **K1ALF OMISS Awards Tracker** (k1alf.com; only forwards contacts logged under NetLogger's OMISS club)

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
For the nerds, the sections below describe the Python-based setup, used for development.

---

## Requirements

**If using the pre-built executables (Easy Install above):** no requirements —
Python and all dependencies are bundled.

**If running from source:**

- Python 3.10+
- `requests` library

```bash
pip install requests
```

---

## Setup

### 1. Configure

**GUI (`netlogger_gui.py`):** No pre-setup needed — the GUI loads built-in
defaults on first launch. Fill in your settings and click **Save Config**;
this creates `config.ini` in the same directory.

**CLI (`netlogger_bridge.py`):** Generate a starter `config.ini` first:

```bash
python netlogger_bridge.py --create-config
```

Either way, the file looks like this:

```ini
[general]
poll_interval = 10          # seconds between file polls
contacts_adi =               # leave blank to auto-detect
state_file = forwarded_qsos.txt
retry_interval_minutes = 60  # how often to retry a contact that failed to forward
retry_give_up_days = 5       # days to keep retrying before giving up on a contact

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
port = 7826
my_call = W1AW

[log4om]
enabled = false
host = 127.0.0.1
port = 2234

[dxkeeper]
enabled = false
host = 127.0.0.1
port = 52001

[macloggerdx]
enabled = false
host = 127.0.0.1
port = 2237
my_call = W1AW

[k1alf_omiss_awards]
enabled = false
call_sign = W1AW
password = YOUR_K1ALF_OMISS_AWARDS_PASSWORD
```

### 2. NetLogger Contacts.adi auto-detection

If `contacts_adi` is blank, the bridge looks here by default:

| OS      | Default path |
|---------|--------------|
| Windows | `%APPDATA%\NetLogger\Contacts.adi` |
| macOS   | `~/Library/Application Support/NetLogger/Contacts.adi` |
| Linux   | `~/.config/NetLogger/Contacts.adi` |

If your install differs, set the full path in `[general] contacts_adi`.

> **Note:** On first run, the bridge marks every QSO already in
> `Contacts.adi` as already forwarded (without sending any of them) — only
> QSOs logged after that point go out. Which contacts have been forwarded is
> tracked in `state_file` by callsign + date + time + band, not file
> position, so editing or deleting old entries in `Contacts.adi` won't cause
> contacts to be skipped or re-sent.

### 3. WaveLog setup

1. In WaveLog, go to **Admin → API Keys** and generate an API key
2. Set `url`, `api_key`, and `station_id` in `config.ini`
3. Set `enabled = true`

API reference: https://docs.wavelog.org/developer/api/

### 4. N3FJP setup

1. In N3FJP AC Log, go to **Settings → Application Program Interface (API)...**
2. Check **TCP API Enabled (Server)**
3. Note the port (default: `1100`)
4. Set `host` and `port` in `config.ini`
5. Set `enabled = true`

API reference: http://www.n3fjp.com/help/api.html

### 5. N1MM Logger+ setup

The bridge sends QSOs as WSJT-X binary UDP messages (a "Log QSO" packet plus
a "LoggedADIF" packet) — the same method used by GridTracker2 and JTAlert.

1. In N1MM: **Config → Configure Ports, Mode Control, Winkey, etc...  → WSJT/JTDX Setup tab**
2. Check **Enable** under WSJT-X and JTDX UDP Settings → Radio #1 Settings
3. Note the UDP port (default: `2237`)
4. Set `host`, `port`, and `my_call` (your station callsign) in `config.ini`
5. Set `enabled = true`
6. **Fully restart N1MM+** — the dialog warns changes need a restart, and it
   won't actually open the listening socket until you do

API reference: https://n1mmwp.hamdocs.com/manual-windows/wsjt-x-decode-list-window/

### 6. Ham Radio Deluxe (HRD) setup

1. In HRD Logbook, go to **Tools → Configure → Network Server**
2. Ensure **Autostart** is checked, and note the command port on the Logbook
   tab (`7826` by default in recent HRD versions)
3. Set `host`, `port`, and `my_call` (fallback callsign, only used if a
   contact's ADIF record has no `Station_Callsign` of its own) in `config.ini`
4. Set `enabled = true`

### 7. Log4OM v2 setup

The bridge sends QSOs as plain ADIF records over UDP to Log4OM's inbound ADIF service.

1. In Log4OM, go to **Settings → Program Configuration → Software integration → Connections**
2. Select **UDP*** then click **Add new item** (plus sign)
3. Click on the "JTDX/WSJT ADIF" preset.
4. Then **click Save** — it's easy to miss, and the connection won't actually start listening until you do
5. Set `host` and `port` in `config.ini` to match
6. Set `enabled = true`

> **Note:** The port is freely configurable — pick any unused port and make
> sure Log4OM's inbound connection uses the same number.

API reference: Log4OM forum — Communicator > Inbound Connections > ADIF

### 8. DXLab Suite DXKeeper setup

The bridge connects to DXKeeper's TCP port and issues an `externallog` command.

1. Ensure DXKeeper is running
2. Note the base port: in DXKeeper go to **Config → Defaults → Network Service**; DXKeeper listens on
   **base port + 1** (default base is `52000`, so DXKeeper uses `52001`)
3. Set `host` and `port` in `config.ini`
4. Set `enabled = true`

API reference: https://www.dxlabsuite.com/Interoperation.htm

### 9. MacLoggerDX setup

> **Untested:** unlike the other five outputs, this one hasn't been verified
> against a real running MacLoggerDX — it's built from MacLoggerDX's own
> documentation only (Mac-only software, no Mac was available to test
> against). N1MM and HRD both needed real bug fixes after their first
> implementations despite following official-looking docs before they
> actually worked, so treat this one the same way until it's been tested.

The bridge sends QSOs as the same WSJT-X binary UDP messages used for N1MM (a
"Log QSO" packet plus a "LoggedADIF" packet) — MacLoggerDX documents
listening for this exact traffic from WSJT-X, JTDX, and JS8Call.

1. In MacLoggerDX: **Station prefs**, enable the WSJT-X/JTDX/JS8Call UDP option
2. Note the UDP port (default: `2237`, same as N1MM's default — if both run
   on the same machine they'll need different ports)
3. Set `host`, `port`, and `my_call` (your station callsign) in `config.ini`
4. Set `enabled = true`

API reference: https://dogparksoftware.com/MacLoggerDX%20Help/mldxfc_wsjtx.html

### 10. K1ALF OMISS Awards Tracker setup

The [OMISS Awards Tracker](https://k1alf.com/omiss_awards/) has no API — the
bridge logs in and uploads a one-record CSV per QSO the same way you'd
manually upload a NetLogger export through its **Call Log → Log Import**
page. Only contacts logged under NetLogger's **OMISS** club are sent; every
other club/net is silently skipped, since the site rejects anything else as
"not OMISS related".

1. Register/log in once at https://k1alf.com/omiss_awards/ if you haven't already
2. Set `call_sign` and `password` in `config.ini` to your login for that site
3. Set `enabled = true`

Your station type (Base/Mobile/Portable) for each upload is read automatically
from the same per-QSO setting NetLogger itself records — there's nothing to
configure for it.

---

## Running

```bash
# Basic
python netlogger_bridge.py

# Custom config file
python netlogger_bridge.py /path/to/myconfig.ini

# Mark every contact currently in Contacts.adi as already forwarded (skip
# everything already in the file; only QSOs logged from now on will be sent)
python netlogger_bridge.py --reset-state

# GUI (config editor + start/stop + live log)
python netlogger_gui.py
```

The GUI requires Tk, which ships with most Python installs. On some Linux
distros, install it separately: `sudo apt install python3-tk`.

Logs go to console and `netlogger_bridge.log`.

The bridge tracks forwarding status per contact in `forwarded_qsos.txt` (or
whatever `state_file` is set to in `config.ini`) — one JSON object per line,
sorted chronologically by QSO date/time rather than by byte position, so
editing or deleting old entries in `Contacts.adi` can't cause it to skip or
re-send anything. Each line records which enabled outputs succeeded, e.g.:

```json
{"key": "20260618|031552|KE9ESR|80M", "wavelog": true, "n3fjp": false, "first_attempt": "2026-06-18T03:15:52Z", "last_attempt": "2026-06-18T03:15:52Z"}
```

A contact with any `false` output is retried automatically every
`retry_interval_minutes` (default 60) for up to `retry_give_up_days` (default
5) — both configurable in `[general]` in `config.ini` — handling a logger or
web service being briefly unreachable. Once everything succeeds, the
timestamps are dropped and the line shrinks to just the per-output results.
If an output still hasn't succeeded once `retry_give_up_days` has elapsed,
the bridge stops retrying and adds `"gave_up": true` so you can spot it later
— search the file for `false` or `gave_up` to find contacts that never fully
made it out.

A line's entry is only dropped once its contact is no longer found in
`Contacts.adi` (i.e. you deleted it in NetLogger), keeping the state file in
sync with what's actually still logged.

Turning on a new output *after* older contacts already finished forwarding to
the outputs enabled at the time is treated the same as a failure needing
retry: the newly-enabled output has no entry yet on those older lines, so on
the next poll (or bridge restart) it gets sent to just that output, same as
any other pending retry — no need to touch `forwarded_qsos.txt` by hand.

To force a specific contact to be re-sent to *every* enabled output (e.g. you
fixed it in NetLogger, or want to retry sooner than `retry_interval_minutes`),
stop the bridge, find and delete its line in `forwarded_qsos.txt`, then start
the bridge again — it'll forward just that one contact on the next poll.

Run `--reset-state` (with the bridge stopped) any time you want it to forget
everything already in the file and only forward new contacts going forward —
e.g. after testing, or if it was offline for a while and you don't want a
backlog of old QSOs replayed to your loggers.

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
