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
- **MacLoggerDX** via the same WSJT-X binary UDP packets as N1MM, on port 2237 (Mac-only; unverified — no Mac was available to test against real software)
- **K1ALF OMISS Awards Tracker** (k1alf.com) via a reverse-engineered login + CSV upload (there is no API); only contacts logged under NetLogger's OMISS club are sent
- **QRZ Logbook** via HTTP REST API (`POST https://logbook.qrz.com/api`, `ACTION=INSERT`); requires a QRZ subscription (XML level or higher)

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

Always update the readme with relevant changes. Always do a security check. Always review project for unused code and remove.

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
   `[general]`, `[wavelog]`, `[n3fjp]`, `[n1mm]`, `[hrd]`, `[log4om]`, `[dxkeeper]`,
   `[macloggerdx]`, `[k1alf_omiss_awards]`, `[qrz]`.
2. **ADI file location** (`find_adi_file`, `ADI_PATHS`) — locates NetLogger's
   `Contacts.adi`, auto-detecting an OS-specific default path if `contacts_adi` is blank.
3. **ADIF file tailer** (`read_all_records`, `normalize_adif`, `extract_field`,
   `apply_omiss_comment_tag`, `record_dedup_key`) — `read_all_records` reads the
   *entire* `Contacts.adi` file
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
   below). `apply_omiss_comment_tag` runs on every record right after
   `normalize_adif`, before it reaches any sender: for contacts logged under
   NetLogger's OMISS club (`App_NetLogger_Club` = `OMISS`) with a
   `App_NetLogger_ClubMemberId`, it rewrites (or inserts) the `COMMENT` field
   to `#{member_id}#` plus the existing comment text, if any — matching the
   format NetLogger's own CSV export already uses. Doing this once, upstream
   of every sender, means the full-ADIF senders (WaveLog, N3FJP, Log4OM,
   DXKeeper) and the field-by-field senders (N1MM, HRD, MacLoggerDX, K1ALF
   OMISS Awards) all see the same tagged `COMMENT` via their normal field
   extraction, with no per-output special-casing; `build_k1alf_omiss_csv`'s
   `Remarks` column in particular used to synthesize this same `#id#` prefix
   itself and now just reads the already-tagged `COMMENT` field (see below).
4. **Output senders** (`send_to_wavelog`, `send_to_n3fjp`, `send_to_n1mm`,
   `send_to_hrd`, `send_to_log4om`, `send_to_dxkeeper`, `send_to_macloggerdx`,
   `send_to_k1alf_omiss_awards`, `send_to_qrz`) —
   each takes a built ADIF record string and pushes it to one destination,
   returning a bool success flag. `send_to_services` (in the "Output
   dispatch" section) wraps all nine behind a single `{service_name:
   sender_fn}` table, keyed by the same short names used throughout state
   persistence (`wavelog`, `n3fjp`, `n1mm`, `hrd`, `log4om`, `dxkeeper`,
   `macloggerdx`, `k1alf_omiss_awards`, `qrz`) and `SERVICE_LABELS` (their display names for logging, e.g.
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
   `send_to_n1mm` and `send_to_macloggerdx` both send two WSJT-X binary UDP
   messages per QSO, built by the shared `_build_wsjtx_qso_messages` — a
   structured "Log QSO" packet (type 5) and a "LoggedADIF" packet (type 12, a
   self-contained ADIF record as one length-prefixed blob) — using helpers
   `_wsjtx_str` (QByteArray: quint32 length + UTF-8), `_wsjtx_null` (a *null*,
   as opposed to empty, QByteArray: length -1), and `_wsjtx_datetime`
   (QDateTime: qint64 Julian day + quint32 ms + quint8 UTC spec). For N1MM,
   both the schema version (`_WSJTX_SCHEMA = 2`, not WSJT-X's own current
   schema 3) and the second (type 12) message were only discovered by
   diffing a real WSJT-X-to-N1MM capture against this function's original
   output — structurally-correct type-5-only packets at schema 3 sent
   without error but never appeared in N1MM. N1MM's WSJT-X Decode List must
   be enabled on the matching port *and N1MM fully restarted* (it only binds
   the listening socket on startup, not when the setting is saved). This is
   the same mechanism GridTracker2 uses. `send_to_macloggerdx` reuses the
   identical packet bytes on the assumption that MacLoggerDX (which
   documents listening for this same real-world WSJT-X traffic on port 2237)
   behaves the same way — **unverified**, since no Mac was available to test
   against a real install; treat it as the most likely output to need a
   fix once actually tested, the way N1MM and HRD did.
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
   given. `send_to_k1alf_omiss_awards` is the one output with no API at
   all — the [K1ALF OMISS Awards Tracker](https://k1alf.com/omiss_awards/)
   only accepts a NetLogger-format CSV upload behind a login, so both the
   login (`POST process.php {call_sign, password, login=Submit}`) and the
   upload (`POST process.php` multipart with `MAX_FILE_SIZE`, `my_end`
   (0/1/2 = Base/Mobile/Portable), `file_upload`, `import=Submit`) were
   reverse-engineered from the live site rather than any documented API —
   including the login form itself, whose markup is broken (the `<form>` is
   opened as a direct child of a `<tr>`, so its actual `<input>` fields are
   DOM siblings rather than descendants; verified via `input.form` in a real
   browser that this still works, since HTML5's "form pointer" parsing
   associates trailing orphan inputs with the last-opened form regardless of
   DOM nesting). A `requests.Session()` is kept at module scope
   (`_k1alf_session`) so login happens once per bridge run rather than once
   per QSO, and is transparently retried once if a session is ever found to
   have expired (detected by the response no longer containing "Log Out").
   `my_end` is always sent as `0` (Base) — it's the *uploader's own* station
   status for the whole import (the log_import page labels it "Mark my
   station as a ... station for the records being imported"), not the
   contacted station's. An earlier version fed it from NetLogger's
   `App_NetLogger_MP_Status` field on the assumption that field meant "my"
   status; it doesn't — NetLogger has no per-QSO field for the account
   holder's own operating mode at all, since NetLogger logs the *other*
   station checking into the net, so `MP_Status` instead records the
   contacted station's mobile/portable status (confirmed by checking every
   value NetLogger ever recorded for a station operating a portable "combo"
   callsign, consistently `P`). The site's CSV import has no field for the
   other station's status at all — "Other End" can only be corrected by hand
   afterward, per contact, via the dropdown on the Call Log page.
   `build_k1alf_omiss_csv` builds the one-record CSV itself, whose column
   layout was reverse-engineered by diffing a real NetLogger CSV export
   against the matching raw `Contacts.adi` records for the same QSOs (the
   site rejects ADIF outright: "ADIF files will not upload correctly") —
   two columns don't map straightforwardly from ADIF field names: `His_RST`/
   `My_RST` are swapped from what their names suggest (confirmed against two
   real records that `His_RST` is `RST_Rcvd` and `My_RST` is `RST_Sent`), and
   `Remarks` is just the ADIF record's (already `apply_omiss_comment_tag`-tagged)
   `COMMENT` field, rather than a column with its own mapping. Only contacts logged under NetLogger's OMISS club
   (`App_NetLogger_Club` = `OMISS`) are actually sent — NetLogger tracks
   contacts across many unrelated clubs/nets (separate folders under its
   data directory), and the site rejects anything else as "not OMISS
   related", so `send_to_k1alf_omiss_awards` treats a non-OMISS contact as
   trivially done (returns `True` without sending) rather than uploading and
   letting it fail. Success is detected by parsing "`N` records were new" /
   "`N` records were duplicates" out of the response body (a duplicate still
   counts as success, since it means the contact is already tracked
   server-side) rather than matching on the "File uploaded sucessfully."
   text, since that string's typo is presumably not something to depend on.
   `send_to_qrz` POSTs `ACTION=INSERT` and the raw ADIF record to
   `https://logbook.qrz.com/api` — a subscriber-only feature (XML
   subscription level or higher) gated on a per-user Logbook API key
   (distinct from QRZ's XML/callsign-lookup key). Verified working against
   a real QRZ Logbook upload. Unlike WaveLog's JSON
   response, QRZ's response is `name=value` pairs (parsed with
   `urllib.parse.parse_qsl`): `RESULT=OK` on success, `RESULT=REPLACE` if
   the QSO duplicated an existing record *and* `OPTION=REPLACE` was sent,
   or `RESULT=FAIL&REASON=...` otherwise. `OPTION=REPLACE` is deliberately
   never sent — QRZ's own docs warn it "WILL overwrite confirmed QSOs with
   the supplied unconfirmed QSO", so a plain `INSERT` that just fails on a
   duplicate is the safer default: a duplicate is reported as a failure
   (retried, then eventually `gave_up`) rather than silently accepted, the
   same tradeoff `send_to_wavelog` makes for its own 400 `status: abort`
   duplicate response.
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
   or retry sooner than `retry_interval_minutes`. `_is_done(record, enabled)`
   is `True` once `"gave_up"` is set, or the record has no service key at all
   (a no-detail record — see below), or *every currently enabled* service
   succeeded. It deliberately checks against `enabled` rather than just the
   keys already present in the record: checking only present keys would let
   a contact forwarded to WaveLog/N3FJP look permanently "done" even after
   K1ALF gets enabled afterwards, since the record predates K1ALF entirely
   and would never gain that key otherwise — this was a real bug (a service
   enabled after some contacts had already finished forwarding to the
   services active at the time never got attempted for those contacts).
   `load_state` treats a missing file as `initialized: False` and migrates
   older on-disk formats transparently, all to a no-detail (already-complete)
   record `{}` — a plain byte offset or a bare `QSO_DATE|TIME_ON|CALL|BAND`
   line (the original dedup-key-only format) has no `"key"`-bearing JSON to
   extract, and a short-lived JSON-dict version (`{"initialized": ...,
   "keys": {key: qso_date}}`) has its `"keys"` mapped to `{}` directly. Unlike
   a record that's missing just *some* enabled services' keys (still not
   done, per above), a no-detail record has no service keys whatsoever, which
   `_is_done` takes to mean it predates per-service tracking and must stay
   done forever regardless of which services get enabled later — this is
   what makes first-run seeding and `--reset-state` actually stick. A record
   from a version predating retry-tracking can also have a `False` service
   result with no `first_attempt`/`last_attempt` at all; since deciding
   whether a record needs backfilling now depends on `enabled` (via
   `_is_done`), which `load_state` doesn't have, this backfill happens in
   `run()` right after `enabled` is computed, rather than in `load_state`.
   `last_attempt` is deliberately backdated past `retry_interval` (rather than
   set to "now") so a record only missing a newly-enabled service's key gets
   picked up on the very next poll instead of waiting out a full
   `retry_interval` for a "first" attempt that never actually happened.
   `--reset-state` (handled in the entry point, not via `run()`)
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
   - no existing record, or `_is_done(record, enabled)` — handled as before
     (skip, or send to every enabled service via `send_to_services()` and
     store the per-service results, with `first_attempt`/`last_attempt` added
     only if something failed).
   - an existing, not-done record — skipped unless `retry_interval_minutes`
     (`[general]` in `config.ini`, default 60) has elapsed since `last_attempt`,
     then retried via `send_to_services(..., only={failed service names})`,
     where "failed" means any enabled service the record doesn't already have
     a `True` result for — an explicit `False` and a missing key (never
     attempted) are treated the same. If everything now succeeds, the
     timestamps are dropped; if
     `retry_give_up_days` (default 5) has elapsed since `first_attempt`, a
     warning is logged and `"gave_up": true` is set instead of retrying
     further; otherwise `last_attempt` is bumped and it's retried again next
     interval.

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
  DXKeeper. The N1MM, HRD, MacLoggerDX, and K1ALF OMISS Awards senders instead
  extract individual fields (`CALL`, `FREQ`, `QSO_DATE`, `TIME_ON`, `RST_SENT`,
  `RST_RCVD`, etc.) to build their own wire formats (a WSJT-X binary packet for
  N1MM/MacLoggerDX, a `db add` text command for HRD, a one-record CSV for K1ALF
  OMISS Awards) rather than forwarding the ADIF string directly.
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
