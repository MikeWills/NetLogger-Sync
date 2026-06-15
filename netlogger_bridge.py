#!/usr/bin/env python3
"""
NetLogger Bridge
Tails the NetLogger Contacts.adi file for new QSOs and forwards them
to WaveLog (via HTTP API) and/or N3FJP AC Log (via TCP API).

Cross-platform: Windows, macOS, Linux
"""

import time
import socket
import logging
import sys
import os
import re
import configparser
from pathlib import Path

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
# Config
# ---------------------------------------------------------------------------
def load_config(config_path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not Path(config_path).exists():
        log.error(f"Config file not found: {config_path}")
        log.info("Run with --create-config to generate a sample config.ini")
        sys.exit(1)
    cfg.read(config_path, encoding="utf-8")
    return cfg


def create_sample_config():
    sample = """\
[general]
# Seconds between file polls
poll_interval = 10

# Path to NetLogger's Contacts.adi file.
# Leave blank to auto-detect from default OS locations.
# Windows default: %APPDATA%\\NetLogger\\Contacts.adi
# macOS default:   ~/Library/Application Support/NetLogger/Contacts.adi
# Linux default:   ~/.config/NetLogger/Contacts.adi
contacts_adi =

# File used to store the byte offset between restarts
state_file = last_offset.txt

[wavelog]
# Set enabled = true to forward contacts to WaveLog
enabled = false

# Base URL of your WaveLog instance (no trailing slash)
url = https://log.example.com

# WaveLog API key (Admin > API Keys in WaveLog)
api_key = YOUR_WAVELOG_API_KEY

# Station profile ID from WaveLog
station_id = 1

[n3fjp]
# Set enabled = true to forward contacts to N3FJP AC Log via TCP API
# N3FJP AC Log runs on Windows only; enable in Settings > API > TCP API Enabled
enabled = false

# Hostname or IP of the machine running N3FJP AC Log
host = 127.0.0.1

# TCP port (default 1100)
port = 1100
"""
    with open("config.ini", "w", encoding="utf-8") as f:
        f.write(sample)
    print("Sample config.ini created. Edit it and re-run.")


# ---------------------------------------------------------------------------
# ADI file location
# ---------------------------------------------------------------------------
ADI_PATHS = {
    "win32":  Path(os.environ.get("APPDATA", "~"), "NetLogger", "Contacts.adi"),
    "darwin": Path("~/Library/Application Support/NetLogger/Contacts.adi").expanduser(),
    "linux":  Path("~/.config/NetLogger/Contacts.adi").expanduser(),
}


def find_adi_file(cfg_path: str) -> Path:
    if cfg_path:
        p = Path(cfg_path).expanduser()
        if not p.exists():
            log.error(f"Configured Contacts.adi not found: {p}")
            sys.exit(1)
        return p

    platform = sys.platform if sys.platform != "win32" else "win32"
    default = ADI_PATHS.get(platform)
    if default and default.exists():
        log.info(f"Auto-detected Contacts.adi: {default}")
        return default

    log.error(
        "Could not auto-detect Contacts.adi. "
        "Set [general] contacts_adi in config.ini"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# ADIF file tailer
# ---------------------------------------------------------------------------

def read_new_records(adi_path: Path, offset: int) -> tuple[list[str], int]:
    """
    Read any new complete ADIF records appended since `offset`.
    Returns (list_of_adif_record_strings, new_offset).
    Each record string is the raw text between the previous <eor> and the next one.
    """
    try:
        with open(adi_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = offset + len(chunk)

        if not chunk:
            return [], offset

        text = chunk.decode("utf-8", errors="replace")

        # Split on <eor> (case-insensitive); keep only complete records
        parts = re.split(r'<eor>', text, flags=re.IGNORECASE)

        # Last element is either empty or an incomplete record — don't process it
        complete = parts[:-1]

        # Calculate offset: only advance past complete records
        incomplete_tail = parts[-1].encode("utf-8", errors="replace")
        adjusted_offset = new_offset - len(incomplete_tail)

        records = [p.strip() for p in complete if p.strip()]
        return records, adjusted_offset

    except OSError as e:
        log.warning(f"File read error: {e}")
        return [], offset


def normalize_adif(raw: str) -> str:
    """
    Return the raw ADIF record with <EOR> appended (uppercase, clean).
    The record is already valid ADIF — just ensure it ends with <EOR>.
    """
    return raw.strip() + " <EOR>"


def extract_field(adif: str, field: str) -> str:
    """Extract a single field value from an ADIF string for logging purposes."""
    match = re.search(rf'<{field}:\d+>([^<]*)', adif, re.IGNORECASE)
    return match.group(1).strip() if match else "?"


# ---------------------------------------------------------------------------
# WaveLog
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
# N3FJP
# ---------------------------------------------------------------------------

def send_to_n3fjp(host: str, port: int, adif: str) -> bool:
    """
    Send ADDADIFRECORD command to N3FJP AC Log via TCP.
    Protocol: <CMD><ADDADIFRECORD><VALUE>{adif}</CMD>
    Reference: http://www.n3fjp.com/help/api.html
    """
    command = f"<CMD><ADDADIFRECORD><VALUE>{adif}</CMD>\r\n"
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(command.encode("utf-8"))
            sock.settimeout(2)
            try:
                response = sock.recv(1024).decode("utf-8", errors="replace")
                log.debug(f"N3FJP response: {response.strip()}")
            except socket.timeout:
                pass
        return True
    except (socket.error, OSError) as e:
        log.error(f"N3FJP connection error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# State persistence (byte offset)
# ---------------------------------------------------------------------------

def load_offset(state_file: str) -> int:
    try:
        return int(Path(state_file).read_text().strip())
    except (FileNotFoundError, ValueError):
        return -1  # -1 = not yet initialized


def save_offset(state_file: str, offset: int):
    Path(state_file).write_text(str(offset))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(config_path: str = "config.ini"):
    cfg = load_config(config_path)
    general = cfg["general"]

    poll_interval  = general.getint("poll_interval", fallback=10)
    state_file     = general.get("state_file", "last_offset.txt")
    adi_path       = find_adi_file(general.get("contacts_adi", ""))

    wavelog_enabled = cfg.getboolean("wavelog", "enabled", fallback=False)
    n3fjp_enabled   = cfg.getboolean("n3fjp",   "enabled", fallback=False)

    if not wavelog_enabled and not n3fjp_enabled:
        log.error("No outputs enabled. Set enabled = true in [wavelog] and/or [n3fjp].")
        sys.exit(1)

    log.info(f"NetLogger Bridge starting — polling every {poll_interval}s")
    log.info(f"File    : {adi_path}")
    log.info(f"WaveLog : {'enabled' if wavelog_enabled else 'disabled'}")
    log.info(f"N3FJP   : {'enabled' if n3fjp_enabled else 'disabled'}")

    offset = load_offset(state_file)

    # First run: skip to end of existing file so we only forward NEW contacts
    if offset == -1:
        with open(adi_path, "rb") as f:
            f.seek(0, 2)  # Seek to end
            offset = f.tell()
        save_offset(state_file, offset)
        log.info(f"First run — starting at end of file (offset {offset}). "
                 "Only new contacts logged from this point will be forwarded.")

    else:
        log.info(f"Resuming from byte offset {offset}")

    while True:
        records, offset = read_new_records(adi_path, offset)

        for raw in records:
            adif     = normalize_adif(raw)
            callsign = extract_field(adif, "Call")
            band     = extract_field(adif, "Band")
            mode     = extract_field(adif, "Mode")

            log.info(f"New contact: {callsign} {band} {mode}")
            log.debug(f"ADIF: {adif}")

            if wavelog_enabled:
                ok = send_to_wavelog(cfg["wavelog"], adif)
                log.info(f"  WaveLog : {'OK' if ok else 'FAILED'}")

            if n3fjp_enabled:
                host = cfg["n3fjp"].get("host", "127.0.0.1")
                port = cfg["n3fjp"].getint("port", fallback=1100)
                ok   = send_to_n3fjp(host, port, adif)
                log.info(f"  N3FJP   : {'OK' if ok else 'FAILED'}")

            save_offset(state_file, offset)

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