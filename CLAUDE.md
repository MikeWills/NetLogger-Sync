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
- **N1MM Logger+** via WSJT-X binary UDP "Log QSO" + "LoggedADIF" packets (types 5/12, schema 2) on port 2237
- **Ham Radio Deluxe (HRD) Logbook** via its Network Server TCP API (`db add {FIELD="VALUE" ...}`) on port 7826
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
instances sharing one `state_file` can race.

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

# Mark every contact currently in Contacts.adi as already forwarded
python netlogger_bridge.py --reset-state
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
3. **ADIF file tailer** (`read_all_records`, `normalize_adif`, `extract_field`,
   `record_dedup_key`) — `read_all_records` reads the *entire* `Contacts.adi` file
   on every poll and splits on `<eor>` (case-insensitive) to find complete
   records, leaving any trailing incomplete record unconsumed for the next poll.
   `normalize_adif` re-parses each `<TAG:LENGTH>value` field using its declared
   length, collapses internal whitespace (NetLogger writes one field per line,
   with some values like Address spanning multiple lines), recomputes the
   length, and concatenates fields with no separators followed by `<EOR>` —
   matching the exact format N3FJP's `ADDADIFRECORD` API expects. `extract_field`
   does a simple regex pull of a field value (used for log messages and by
   `record_dedup_key`, which builds a `QSO_DATE|TIME_ON|CALL|BAND` identity used
   to decide whether a record has already been forwarded — see state persistence
   below).
4. **Output senders** (`send_to_wavelog`, `send_to_n3fjp`, `send_to_n1mm`,
   `send_to_hrd`, `send_to_log4om`, `send_to_dxkeeper`) — each takes a built ADIF
   record string and pushes it to one destination, returning a bool success flag.
   `send_to_services` (in the "Output dispatch" section) wraps all six behind a
   single `{service_name: sender_fn}` table, keyed by the same short names used
   throughout state persistence (`wavelog`, `n3fjp`, `n1mm`, `hrd`, `log4om`,
   `dxkeeper`) and `SERVICE_LABELS` (their display names for logging, e.g.
   `"n3fjp" -> "N3FJP"`). It takes an `only` set so the same function serves both
   a first attempt (every enabled service) and a retry (just the services that
   previously failed for that contact) — see state persistence below.
   `send_to_wavelog` treats HTTP 200/201 with `status: created` and `adif_count > 0`
   as success (WaveLog returns 400 `status: abort` for duplicate QSOs — expected
   when replaying already-logged contacts). `send_to_n3fjp` sends
   `<CMD><ADDADIFRECORD><VALUE>...</VALUE></CMD>` followed by `<CMD><CHECKLOG></CMD>`
   over TCP — ADDADIFRECORD writes directly to N3FJP's log file but doesn't refresh
   its on-screen list, and CHECKLOG forces that reload. ADDADIFRECORD itself has no
   documented response, so a timeout/no-response is normal, not an error.
   `send_to_n1mm` sends two WSJT-X binary UDP messages per QSO — a structured
   "Log QSO" packet (type 5) and a "LoggedADIF" packet (type 12, a
   self-contained ADIF record as one length-prefixed blob) — using helpers
   `_wsjtx_str` (QByteArray: quint32 length + UTF-8), `_wsjtx_null` (a *null*,
   as opposed to empty, QByteArray: length -1), and `_wsjtx_datetime`
   (QDateTime: qint64 Julian day + quint32 ms + quint8 UTC spec). Both the
   schema version (`_WSJTX_SCHEMA = 2`, not WSJT-X's own current schema 3)
   and the second (type 12) message were only discovered by diffing a real
   WSJT-X-to-N1MM capture against this function's original output —
   structurally-correct type-5-only packets at schema 3 sent without error
   but never appeared in N1MM. N1MM's WSJT-X Decode List must be enabled on
   the matching port *and N1MM fully restarted* (it only binds the listening
   socket on startup, not when the setting is saved). This is the same
   mechanism GridTracker2 uses.
   `send_to_hrd` sends a plain-text `db add {FIELD="VALUE" ...}` command over TCP
   to HRD's Network Server (default port 7826) — a different HRD feature from
   "QSO Forwarding" (UDP/XML), which was tried first and never worked. This
   syntax was reverse-engineered from a real GridTracker-to-HRD packet
   capture: HRD's own published Logbook API docs (a quoted database name
   before the field list) are stale for current HRD versions, which expect no
   database name and `FREQ` in Hz rather than MHz. Most fields map directly
   from ADIF field names NetLogger already provides (`CALL`, `QSO_DATE`,
   `TIME_ON`, `BAND`, `RST_SENT`/`RST_RCVD`, `STATE`, `CNTY`,
   `STATION_CALLSIGN`, `OPERATOR`, etc.); a response containing `"Added"`
   (e.g. `Found 12 Valid Fields... Added 31 Fields to My Logbook...`) is
   treated as success. `send_to_log4om` sends the ADIF record
   as a raw UDP datagram to Log4OM's inbound ADIF service. `send_to_dxkeeper`
   builds a DXLab ADIF-encoded TCP message
   (`<command:11>externallog<parameters:N><ExternalLogADIF:M>[adif fields incl.
   <EOR>]`) and sends it to DXKeeper on port 52001 — DXLab's own documented
   example keeps `<EOR>` inside that length-prefixed payload; stripping it (an
   earlier version of this function did) leaves an incomplete ADIF record
   that DXKeeper silently refuses with "could not be logged:" and no reason
   given.
5. **State persistence** (`load_state`, `save_state`, `prune_records`,
   `_seed_keys_from_existing`, `reset_state`, `_is_done`) — tracks per-contact,
   per-service forwarding status by `record_dedup_key`, not file position,
   because NetLogger lets a logged QSO be edited or deleted, which would
   silently desync a byte offset (shifting everything after the edit) without
   any way to detect it. `state_file` (default `forwarded_qsos.txt`) is one
   JSON object per line — `{"key": "QSO_DATE|TIME_ON|CALL|BAND", "wavelog":
   true, "n3fjp": false, "first_attempt": "...", "last_attempt": "..."}` — with
   the dedup key's date/time leading specifically so `sorted()` (in
   `save_state`) puts the file in chronological order even though it's
   serialized JSON. `first_attempt`/`last_attempt` (ISO 8601 UTC) and a
   `"gave_up": true` flag are only present while a contact has at least one
   failed service still being retried — see the main loop below. Deleting a
   contact's line by hand and restarting the bridge forces a full re-send to
   every enabled service; this is the supported way to re-log a fixed-up QSO
   or retry sooner than the hourly schedule. `_is_done(record)` is `True` once
   every service it was attempted against succeeded, or `"gave_up"` is set —
   used to decide whether a contact needs any further attention at all.
   `load_state` treats a missing file as `initialized: False` and migrates
   older on-disk formats transparently, all to a no-detail (already-complete)
   record `{}` — since `_is_done({})` is `True` (no service key has a falsy
   value), this safely treats anything pre-dating per-service tracking as
   already finished rather than retrying it: a plain byte offset or a bare
   `QSO_DATE|TIME_ON|CALL|BAND` line (the original dedup-key-only format) has
   no `"key"`-bearing JSON to extract, and a short-lived JSON-dict version
   (`{"initialized": ..., "keys": {key: qso_date}}`) has its `"keys"` mapped to
   `{}` directly. `--reset-state` (handled in the entry point, not via `run()`)
   calls `reset_state()`, which uses `_seed_keys_from_existing()` to mark every
   contact currently in the file as forwarded *without sending any of them*, so
   a restarted bridge only forwards QSOs logged from that point on.
   `prune_records` drops a record only once it's no longer found in the
   current full read of `Contacts.adi` (i.e. you deleted it in NetLogger) —
   pruning by age instead was tried and is wrong here: since every poll
   rescans the *whole* file, a years-old record is still "found" on every
   single poll, so dropping it for being old would make it look new again on
   the very next poll, forwarding it again forever.
6. **Main loop** (`run`) — on first run (`state["initialized"]` is `False`), seeds
   `records` from every existing record via `_seed_keys_from_existing()` (same
   no-send behavior as `--reset-state`) so only QSOs logged *after* startup are
   forwarded; on subsequent runs resumes with the persisted records. Each poll
   cycle: re-read every complete record in the file, track the dedup key of
   every record seen this cycle (`current_keys`), and for each:
   - no existing record, or `_is_done()` — handled as before (skip, or send to
     every enabled service via `send_to_services()` and store the per-service
     results, with `first_attempt`/`last_attempt` added only if something failed).
   - an existing, not-done record — skipped unless `RETRY_INTERVAL` (1 hour) has
     elapsed since `last_attempt`, then retried via `send_to_services(..., only=
     {failed service names})`. If everything now succeeds, the timestamps are
     dropped; if `RETRY_GIVE_UP_AFTER` (5 days) has elapsed since
     `first_attempt`, a warning is logged and `"gave_up": true` is set instead
     of retrying further; otherwise `last_attempt` is bumped and it's retried
     again next hour.

   After the per-record loop, `prune_records` drops any record not in
   `current_keys` (i.e. its QSO is gone from the file). While running, the
   process's PID is written to `netlogger_bridge.pid` (via
   `PID_FILE`/`resolve_path`) and removed in a `finally` block on exit, so the GUI
   can detect whether a bridge process is alive regardless of how it was started.

## Key implementation notes

- `APP_DIR` (`resolve_path`) anchors `netlogger_bridge.log` and a relative
  `state_file` to the script/exe's own directory rather than the process's cwd,
  so the bridge behaves correctly when launched by a scheduler with an
  arbitrary working directory.
- The tailer operates on raw bytes/text — it does not parse NetLogger's ADIF fields
  beyond pulling `Call`/`Band`/`Mode` for log messages via `extract_field`. Records
  are forwarded as-is (with `<EOR>` re-appended) to WaveLog, N3FJP, Log4OM, and
  DXKeeper. The N1MM and HRD senders instead extract individual fields (`CALL`,
  `FREQ`, `QSO_DATE`, `TIME_ON`, `RST_SENT`, `RST_RCVD`, etc.) to build their
  own wire formats (a WSJT-X binary packet and a `db add` text command,
  respectively) rather than forwarding the ADIF string directly.
- `read_all_records` only includes *complete* records (i.e. those followed by
  `<eor>`); a partially-written trailing record is left for the next poll. This
  also makes seeding (first run / `--reset-state`) safe against NetLogger being
  mid-write of the newest record — an in-progress record is simply excluded from
  the seed and picked up as genuinely new once its `<eor>` lands on a later poll,
  rather than being split by a byte-position snapshot taken mid-write.
- Dedup keys are intentionally *not* derived from a hash of the full record:
  hashing would treat any edit to a previously-logged QSO (e.g. fixing a typo'd
  name) as a brand-new contact and re-forward it. Keying on
  `QSO_DATE|TIME_ON|CALL|BAND` instead survives edits to other fields while still
  giving each distinct QSO (including repeat contacts with the same station on
  a different band the same day) a unique identity.
- Logging goes to both stdout and `netlogger_bridge.log` (set up at module import time
  in `logging.basicConfig`).
