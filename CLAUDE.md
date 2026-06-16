# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow

Do not commit directly to `main`. For any code change, create a new branch,
commit there, push it, and open a pull request for review (the
`claude-code-review` GitHub Action will run an automated review on it).

## Overview

Single-file Python bridge (`netlogger_bridge.py`) that tails NetLogger's `Contacts.adi`
ADIF log file for newly appended QSO records and forwards each one to:
- **WaveLog** via HTTP REST API (`POST {url}/api/qso`)
- **N3FJP AC Log** via a raw TCP API (`<CMD><ADDADIFRECORD><VALUE>...</CMD>`)
- **N1MM Logger+** via WSJT-X binary UDP "Log QSO" packet (type 5, schema 3) on port 2237
- **Ham Radio Deluxe (HRD) Logbook** via N1MM-compatible UDP XML ContactInfo on port 12060
- **Log4OM v2** via UDP inbound ADIF (plain ADIF record datagram, user-configured port)
- **DXLab Suite DXKeeper** via TCP `externallog` command on port 52001

Any combination of outputs can be enabled independently via `config.ini`.

`netlogger_gui.py` is a Tkinter front-end over the same module: it edits
`config.ini` via form fields and runs `bridge.run()` in a background thread
(stoppable via a `threading.Event`), streaming log records into a text widget
via a `QueueHandler`. It also has a "Run automatically at login (background)"
checkbox that registers/unregisters the headless CLI bridge (`netlogger_bridge`,
pointed at the GUI's `config.ini`) with the OS scheduler — Task Scheduler
(`schtasks`) on Windows, a `launchd` agent on macOS, or a `systemd --user`
service on Linux. On Windows, `schtasks /create`/`/delete` can fail with
"Access is denied" depending on local policy; `_run_schtasks` retries via a
UAC-elevated `ShellExecuteExW("runas", ...)` in that case.

On Windows, the task is created from an XML definition (`/create /xml`, not
`/tr`) so it can set `RestartOnFailure`. The action runs a VBScript wrapper
(`netlogger_bridge_autostart.vbs`) that launches the bridge hidden *and waits*
for it (`WScript.Shell.Run(..., 0, True)`), propagating its exit code via
`WScript.Quit` — this keeps the task "running" for the bridge's lifetime so
Task Scheduler notices if it's killed/crashes and restarts it. macOS/Linux get
the same behavior via launchd's `KeepAlive` and systemd's `Restart=always`,
which were already in place.

The GUI's "Bridge process" label polls `get_running_bridge_pid()` every 2s,
which reads `bridge.PID_FILE` (`netlogger_bridge.pid`) and checks the PID is
still alive (`OpenProcess` on Windows, `os.kill(pid, 0)` elsewhere) — this
detects the bridge whether it was started by the GUI's own worker thread or by
the autostart task/service. `_toggle_run` (Start) warns and asks for
confirmation if it detects another instance already running, since two
instances sharing one `state_file`/offset can race.

Checking the autostart box also starts the bridge immediately via
`start_bridge_now()`, rather than waiting for the next login — on macOS/Linux
this is a side effect of `launchctl load -w` (`RunAtLoad`) and
`systemctl --user enable --now`, which already start the service on
registration; on Windows it's an explicit `schtasks /run /tn NetLoggerBridge`
since `LogonTrigger` alone wouldn't fire until the next logon.
`_toggle_autostart` only calls this if `get_running_bridge_pid()` is `None`,
to avoid starting a duplicate instance.

Always update the readme with relavant changes. Always do a security check. Always review project for unused code an remove.

`config.ini` is the user's local runtime config (contains live API keys/host info) and
is gitignored. Do not read, edit, or otherwise touch it unless the user explicitly asks.

## Running

```bash
pip install requests

# Generate config.ini (first run)
python netlogger_bridge.py --create-config

# Run with default config.ini
python netlogger_bridge.py

# Run with a custom config path
python netlogger_bridge.py /path/to/myconfig.ini
```

There is no test suite or linter configured.

## Release builds

`.github/workflows/release.yml` builds standalone executables with PyInstaller
on a Windows/macOS/Linux matrix and publishes them to GitHub Releases
(`NetLogger-Bridge-{Windows,macOS,Linux}.zip`) whenever a tag matching `v*` is
pushed. Each zip bundles both the CLI (`netlogger_bridge`) and GUI
(`netlogger_gui`, built with `--windowed` on Windows) executables plus a fresh
`config.ini` from `--create-config`. This is the install path for
non-technical users (see README "Easy install"). Linux builds install
`python3-tk` via apt before running PyInstaller.

## Architecture

The script is organized as a sequence of self-contained sections, run via a single
polling loop in `run()`:

1. **Config** (`load_config`, `create_sample_config`) — `configparser`-based, sections
   `[general]`, `[wavelog]`, `[n3fjp]`, `[n1mm]`, `[hrd]`, `[log4om]`, `[dxkeeper]`.
2. **ADI file location** (`find_adi_file`, `ADI_PATHS`) — locates NetLogger's
   `Contacts.adi`, auto-detecting an OS-specific default path if `contacts_adi` is blank.
3. **ADIF file tailer** (`read_new_records`, `normalize_adif`, `extract_field`) —
   reads bytes appended to `Contacts.adi` since the last saved offset, splits on
   `<eor>` (case-insensitive) to find complete records, and leaves any trailing
   incomplete record unconsumed for the next poll. `normalize_adif` re-parses each
   `<TAG:LENGTH>value` field using its declared length, collapses internal
   whitespace (NetLogger writes one field per line, with some values like Address
   spanning multiple lines), recomputes the length, and concatenates fields with no
   separators followed by `<EOR>` — matching the exact format N3FJP's
   `ADDADIFRECORD` API expects. `extract_field` does a simple regex pull of a field
   value (used only for log messages).
4. **Output senders** (`send_to_wavelog`, `send_to_n3fjp`, `send_to_n1mm`,
   `send_to_hrd`, `send_to_log4om`, `send_to_dxkeeper`) — each takes a built ADIF
   record string and pushes it to one destination, returning a bool success flag.
   `send_to_wavelog` treats HTTP 200/201 with `status: created` and `adif_count > 0`
   as success (WaveLog returns 400 `status: abort` for duplicate QSOs — expected
   when replaying already-logged contacts). `send_to_n3fjp` sends
   `<CMD><ADDADIFRECORD><VALUE>...</VALUE></CMD>` followed by `<CMD><CHECKLOG></CMD>`
   over TCP — ADDADIFRECORD writes directly to N3FJP's log file but doesn't refresh
   its on-screen list, and CHECKLOG forces that reload. ADDADIFRECORD itself has no
   documented response, so a timeout/no-response is normal, not an error.
   `send_to_n1mm` builds a WSJT-X binary "Log QSO" UDP packet (type 5, schema 3)
   using helpers `_wsjtx_str` (QByteArray: quint32 length + UTF-8) and
   `_wsjtx_datetime` (QDateTime: qint64 Julian day + quint32 ms + quint8 UTC spec);
   N1MM's WSJT-X Decode List must be enabled on the matching port. This is the same
   mechanism GridTracker2 uses.
   `send_to_hrd` builds an N1MM-compatible ContactInfo XML packet (UDP) from the
   ADIF record's fields, using `_BAND_TO_MHZ` to map ADIF band to MHz and
   `_xml_escape` to sanitize values; HRD's QSO Forwarding must be configured with
   N1MM as the source on the matching port. `send_to_log4om` sends the ADIF record
   as a raw UDP datagram to Log4OM's inbound ADIF service. `send_to_dxkeeper`
   builds a DXLab ADIF-encoded TCP message
   (`<command:11>externallog<parameters:N><ExternalLogADIF:M>[adif fields]`)
   and sends it to DXKeeper on port 52001.
5. **State persistence** (`load_offset`, `save_offset`) — the byte offset into
   `Contacts.adi` is persisted to `state_file` (default `last_offset.txt`) after each
   record is processed, so restarts resume correctly. A missing/invalid state file
   returns `-1`, meaning "uninitialized".
6. **Main loop** (`run`) — on first run (`offset == -1`), seeks to EOF so only QSOs
   logged *after* startup are forwarded; on subsequent runs resumes from the saved
   offset. Each poll cycle: read new complete records, build ADIF, send to each
   enabled output, save offset, sleep `poll_interval` seconds. While running, the
   process's PID is written to `netlogger_bridge.pid` (via `PID_FILE`/`resolve_path`)
   and removed in a `finally` block on exit, so the GUI can detect whether a bridge
   process is alive regardless of how it was started.

## Key implementation notes

- `APP_DIR` (`resolve_path`) anchors `netlogger_bridge.log` and a relative
  `state_file` to the script/exe's own directory rather than the process's cwd,
  so the bridge behaves correctly when launched by a scheduler with an
  arbitrary working directory.
- The tailer operates on raw bytes/text — it does not parse NetLogger's ADIF fields
  beyond pulling `Call`/`Band`/`Mode` for log messages via `extract_field`. Records
  are forwarded as-is (with `<EOR>` re-appended) to WaveLog, N3FJP, Log4OM, and
  DXKeeper. The HRD sender additionally extracts `FREQ`, `QSO_DATE`, `TIME_ON`,
  `RST_SENT`, and `RST_RCVD` to build the N1MM XML payload.
- `read_new_records` only advances the offset past *complete* records (i.e. those
  followed by `<eor>`); a partially-written trailing record is left for the next poll.
- Logging goes to both stdout and `netlogger_bridge.log` (set up at module import time
  in `logging.basicConfig`).
