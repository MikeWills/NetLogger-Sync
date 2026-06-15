# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python bridge (`netlogger_bridge.py`) that polls the NetLogger SQLite
contacts database for new QSOs and forwards each one (as an ADIF record) to:
- **WaveLog** via HTTP REST API (`POST {url}/api/qso`)
- **N3FJP AC Log** via a raw TCP API (`<CMD><ADDADIFRECORD><VALUE>...</CMD>`)

Either or both outputs can be enabled independently via `config.ini`.

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
2. **NetLogger DB access** (`find_netlogger_db`, `get_column_names`, `fetch_new_contacts`) —
   opens the SQLite `Contacts` table read-only (`mode=ro`) and selects rows by
   `rowid > last_id`. DB path auto-detection is OS-specific (`NETLOGGER_DB_PATHS`).
3. **ADIF builder** (`COLUMN_TO_ADIF`, `row_to_adif`) — maps NetLogger column names to
   ADIF field tags and formats a single ADIF record (`<TAG:len>value ... <EOR>`).
   NetLogger's schema is not publicly documented, so this mapping is best-effort;
   unmapped columns are skipped silently. On startup the bridge logs the actual
   `Contacts` table columns to help diagnose mapping gaps.
4. **Output senders** (`send_to_wavelog`, `send_to_n3fjp`) — each takes a built ADIF
   string and pushes it to one destination, returning a bool success flag.
5. **State persistence** (`load_last_id`, `save_last_id`) — last processed `rowid` is
   persisted to `state_file` (default `last_contact_id.txt`) so restarts resume
   correctly.
6. **Main loop** (`run`) — for each poll cycle: fetch new contacts, build ADIF, send to
   each enabled output, update/persist `last_id`, sleep `poll_interval` seconds.

## Key implementation notes

- When extending `COLUMN_TO_ADIF`, the dict is keyed by NetLogger column name and
  valued by ADIF field tag — date values get `-` stripped (`YYYY-MM-DD` → `YYYYMMDD`)
  and time values get `:` stripped and padded to `HHMMSS`.
- `fetch_new_contacts` swallows `sqlite3.OperationalError` (NetLogger may be writing to
  the DB concurrently) and returns an empty list rather than crashing.
- Logging goes to both stdout and `netlogger_bridge.log` (set up at module import time
  in `logging.basicConfig`).
