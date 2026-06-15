# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python bridge (`netlogger_bridge.py`) that tails NetLogger's `Contacts.adi`
ADIF log file for newly appended QSO records and forwards each one to:
- **WaveLog** via HTTP REST API (`POST {url}/api/qso`)
- **N3FJP AC Log** via a raw TCP API (`<CMD><ADDADIFRECORD><VALUE>...</CMD>`)

Either or both outputs can be enabled independently via `config.ini`.

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

There is no test suite, build step, or linter configured.

## Architecture

The script is organized as a sequence of self-contained sections, run via a single
polling loop in `run()`:

1. **Config** (`load_config`, `create_sample_config`) — `configparser`-based, sections
   `[general]`, `[wavelog]`, `[n3fjp]`.
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
4. **Output senders** (`send_to_wavelog`, `send_to_n3fjp`) — each takes a built ADIF
   record string and pushes it to one destination, returning a bool success flag.
   `send_to_wavelog` treats HTTP 200/201 with `status: created` and `adif_count > 0`
   as success (WaveLog returns 400 `status: abort` for duplicate QSOs — expected
   when replaying already-logged contacts). `send_to_n3fjp` sends
   `<CMD><ADDADIFRECORD><VALUE>...</VALUE></CMD>` followed by `<CMD><CHECKLOG></CMD>`
   over TCP — ADDADIFRECORD writes directly to N3FJP's log file but doesn't refresh
   its on-screen list, and CHECKLOG forces that reload. ADDADIFRECORD itself has no
   documented response, so a timeout/no-response is normal, not an error.
5. **State persistence** (`load_offset`, `save_offset`) — the byte offset into
   `Contacts.adi` is persisted to `state_file` (default `last_offset.txt`) after each
   record is processed, so restarts resume correctly. A missing/invalid state file
   returns `-1`, meaning "uninitialized".
6. **Main loop** (`run`) — on first run (`offset == -1`), seeks to EOF so only QSOs
   logged *after* startup are forwarded; on subsequent runs resumes from the saved
   offset. Each poll cycle: read new complete records, build ADIF, send to each
   enabled output, save offset, sleep `poll_interval` seconds.

## Key implementation notes

- The tailer operates on raw bytes/text — it does not parse NetLogger's ADIF fields
  beyond pulling `Call`/`Band`/`Mode` for log messages via `extract_field`. Records
  are forwarded as-is (with `<EOR>` re-appended), so any fields NetLogger writes are
  passed through to WaveLog/N3FJP unchanged.
- `read_new_records` only advances the offset past *complete* records (i.e. those
  followed by `<eor>`); a partially-written trailing record is left for the next poll.
- Logging goes to both stdout and `netlogger_bridge.log` (set up at module import time
  in `logging.basicConfig`).
