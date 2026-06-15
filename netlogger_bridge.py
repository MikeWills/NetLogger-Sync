#!/usr/bin/env python3
"""
NetLogger Bridge
Watches the NetLogger SQLite database for new contacts and forwards them
to WaveLog (via HTTP API) and/or N3FJP AC Log (via TCP API).

Cross-platform: Windows, macOS, Linux
"""

import sqlite3
import time
import socket
import json
import logging
import sys
import os
import configparser
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("netlogger_bridge.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config(config_path: str = "config.ini") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not Path(config_path).exists():
        log.error(f"Config file not found: {config_path}")
        log.info("Run with --create-config to generate a sample config.ini")
        sys.exit(1)
    cfg.read(config_path, encoding="utf-8")
    return cfg


def create_sample_config():
    """Write a sample config.ini to disk."""
    sample = """\
[general]
# Seconds between database polls
poll_interval = 10

# Path to NetLogger's SQLite contacts database.
# Leave blank to auto-detect from default OS locations.
# Windows default: %APPDATA%\\NetLogger\\contacts.db
# macOS default:   ~/Library/Application Support/NetLogger/contacts.db
# Linux default:   ~/.config/NetLogger/contacts.db
netlogger_db =

# File used to persist the last-seen contact rowid across restarts
state_file = last_contact_id.txt

[wavelog]
# Set enabled = true to forward contacts to WaveLog
enabled = false

# Base URL of your WaveLog instance (no trailing slash)
url = https://log.example.com

# WaveLog API key (Settings > API in WaveLog)
api_key = YOUR_WAVELOG_API_KEY

# Station profile ID from WaveLog
station_id = 1

[n3fjp]
# Set enabled = true to forward contacts to N3FJP AC Log via TCP API
# NOTE: N3FJP AC Log runs on Windows only; this client can run anywhere
enabled = false

# Hostname or IP of the machine running N3FJP AC Log
host = 127.0.0.1

# TCP port (default 1100; set in N3FJP: Settings > API > TCP API Enabled)
port = 1100
"""
    with open("config.ini", "w", encoding="utf-8") as f:
        f.write(sample)
    print("Sample config.ini created. Edit it and re-run.")


# ---------------------------------------------------------------------------
# NetLogger database helpers
# ---------------------------------------------------------------------------
NETLOGGER_DB_PATHS = {
    "win32":  Path(os.environ.get("APPDATA", "~"), "NetLogger", "contacts.db"),
    "darwin": Path("~/Library/Application Support/NetLogger/contacts.db").expanduser(),
    "linux":  Path("~/.config/NetLogger/contacts.db").expanduser(),
}


def find_netlogger_db(cfg_path: str) -> Path:
    if cfg_path:
        p = Path(cfg_path).expanduser()
        if not p.exists():
            log.error(f"Configured NetLogger DB not found: {p}")
            sys.exit(1)
        return p

    platform = sys.platform
    default = NETLOGGER_DB_PATHS.get(platform if platform != "win32" else "win32")
    if default and default.exists():
        log.info(f"Auto-detected NetLogger DB: {default}")
        return default

    log.error(
        "Could not auto-detect NetLogger database. "
        "Set [general] netlogger_db in config.ini"
    )
    sys.exit(1)


def get_column_names(db_path: Path) -> list[str]:
    """Return column names for the Contacts table (handles schema variations)."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cur = conn.execute("PRAGMA table_info(Contacts)")
        return [row[1] for row in cur.fetchall()]


def fetch_new_contacts(db_path: Path, last_id: int) -> list[dict]:
    """Return all Contacts rows with rowid > last_id, ordered by rowid."""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT rowid, * FROM Contacts WHERE rowid > ? ORDER BY rowid",
                (last_id,),
            )
            return [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError as e:
        log.warning(f"DB read error (NetLogger may be writing): {e}")
        return []


# ---------------------------------------------------------------------------
# ADIF builder
# ---------------------------------------------------------------------------

# Map common NetLogger column names → ADIF field names
# NetLogger's exact schema isn't publicly documented; common column names are
# listed here. Unknown columns are skipped gracefully.
COLUMN_TO_ADIF = {
    "Callsign":    "CALL",
    "Call":        "CALL",
    "QSODate":     "QSO_DATE",
    "Date":        "QSO_DATE",
    "TimeOn":      "TIME_ON",
    "Time":        "TIME_ON",
    "Band":        "BAND",
    "Frequency":   "FREQ",
    "Freq":        "FREQ",
    "Mode":        "MODE",
    "RSTSent":     "RST_SENT",
    "RST_Sent":    "RST_SENT",
    "RSTRcvd":     "RST_RCVD",
    "RST_Rcvd":    "RST_RCVD",
    "Name":        "NAME",
    "QTH":         "QTH",
    "State":       "STATE",
    "County":      "CNTY",
    "Country":     "COUNTRY",
    "Comment":     "COMMENT",
    "Notes":       "COMMENT",
    "GridSquare":  "GRIDSQUARE",
    "Grid":        "GRIDSQUARE",
    "Operator":    "OPERATOR",
    "NetName":     "APP_NETLOGGER_NET",
    "Net":         "APP_NETLOGGER_NET",
    "Power":       "TX_PWR",
    "MyCall":      "STATION_CALLSIGN",
    "StationCall": "STATION_CALLSIGN",
}


def row_to_adif(row: dict) -> str:
    """Convert a Contacts table row dict to an ADIF record string."""
    fields = []

    for col, adif_tag in COLUMN_TO_ADIF.items():
        value = row.get(col)
        if value is None:
            continue
        value = str(value).strip()
        if not value:
            continue

        # Normalize date: YYYY-MM-DD → YYYYMMDD
        if adif_tag == "QSO_DATE" and "-" in value:
            value = value.replace("-", "")

        # Normalize time: HH:MM:SS or HH:MM → HHMMSS
        if adif_tag == "TIME_ON":
            value = value.replace(":", "")
            if len(value) == 4:
                value += "00"

        fields.append(f"<{adif_tag}:{len(value)}>{value}")

    if not fields:
        return ""

    return " ".join(fields) + " <EOR>"


# ---------------------------------------------------------------------------
# WaveLog output
# ---------------------------------------------------------------------------

def send_to_wavelog(cfg: configparser.SectionProxy, adif: str) -> bool:
    url = cfg["url"].rstrip("/") + "/api/qso"
    payload = {
        "key": cfg["api_key"],
        "station_profile_id": cfg.getint("station_id", fallback=1),
        "type": "adif",
        "string": adif,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "created" or "added" in str(data).lower():
                return True
            log.warning(f"WaveLog unexpected response: {data}")
            return False
        log.error(f"WaveLog HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as e:
        log.error(f"WaveLog connection error: {e}")
        return False


# ---------------------------------------------------------------------------
# N3FJP output
# ---------------------------------------------------------------------------

def send_to_n3fjp(host: str, port: int, adif: str) -> bool:
    """
    Send an ADDADIFRECORD command to N3FJP AC Log via TCP.
    Protocol: <CMD><ADDADIFRECORD><VALUE><adif string></CMD>
    Reference: http://www.n3fjp.com/help/api.html
    """
    command = f"<CMD><ADDADIFRECORD><VALUE>{adif}</CMD>\r\n"
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(command.encode("utf-8"))
            # N3FJP sends back a response; read it briefly
            sock.settimeout(2)
            try:
                response = sock.recv(1024).decode("utf-8", errors="replace")
                log.debug(f"N3FJP response: {response.strip()}")
            except socket.timeout:
                pass  # No response is also fine
        return True
    except (socket.error, OSError) as e:
        log.error(f"N3FJP connection error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_last_id(state_file: str) -> int:
    try:
        return int(Path(state_file).read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_last_id(state_file: str, last_id: int):
    Path(state_file).write_text(str(last_id))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(config_path: str = "config.ini"):
    cfg = load_config(config_path)
    general = cfg["general"]

    poll_interval = general.getint("poll_interval", fallback=10)
    state_file    = general.get("state_file", "last_contact_id.txt")
    db_path       = find_netlogger_db(general.get("netlogger_db", ""))

    wavelog_enabled = cfg.getboolean("wavelog", "enabled", fallback=False)
    n3fjp_enabled   = cfg.getboolean("n3fjp",   "enabled", fallback=False)

    if not wavelog_enabled and not n3fjp_enabled:
        log.error("No outputs enabled. Set enabled = true in [wavelog] and/or [n3fjp].")
        sys.exit(1)

    log.info(f"NetLogger Bridge starting — polling every {poll_interval}s")
    log.info(f"Database : {db_path}")
    log.info(f"WaveLog  : {'enabled' if wavelog_enabled else 'disabled'}")
    log.info(f"N3FJP    : {'enabled' if n3fjp_enabled else 'disabled'}")

    # Show detected columns on first run (helps users verify schema mapping)
    try:
        cols = get_column_names(db_path)
        log.info(f"Contacts table columns: {cols}")
    except Exception as e:
        log.warning(f"Could not read column names: {e}")

    last_id = load_last_id(state_file)
    log.info(f"Resuming from contact rowid > {last_id}")

    while True:
        contacts = fetch_new_contacts(db_path, last_id)

        for contact in contacts:
            rowid    = contact["rowid"]
            callsign = contact.get("Callsign") or contact.get("Call") or "?"
            adif     = row_to_adif(contact)

            if not adif:
                log.warning(f"rowid {rowid}: could not build ADIF (empty row?), skipping")
                last_id = rowid
                save_last_id(state_file, last_id)
                continue

            log.info(f"New contact rowid={rowid} call={callsign}")
            log.debug(f"ADIF: {adif}")

            if wavelog_enabled:
                ok = send_to_wavelog(cfg["wavelog"], adif)
                log.info(f"  WaveLog: {'OK' if ok else 'FAILED'}")

            if n3fjp_enabled:
                host = cfg["n3fjp"].get("host", "127.0.0.1")
                port = cfg["n3fjp"].getint("port", fallback=1100)
                ok   = send_to_n3fjp(host, port, adif)
                log.info(f"  N3FJP  : {'OK' if ok else 'FAILED'}")

            last_id = rowid
            save_last_id(state_file, last_id)

        if contacts:
            log.info(f"Processed {len(contacts)} new contact(s). Last rowid={last_id}")

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--create-config" in sys.argv:
        create_sample_config()
        sys.exit(0)

    config_file = "config.ini"
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            config_file = arg
            break

    try:
        run(config_file)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
