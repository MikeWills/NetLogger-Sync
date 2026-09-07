#!/usr/bin/env python3
"""
NetLogger Bridge
Tails the NetLogger Contacts.adi file for new QSOs and forwards them
to WaveLog (via HTTP API) and/or N3FJP AC Log (via TCP API).

Cross-platform: Windows, macOS, Linux
"""

import datetime
import json
import struct
import time
import socket
import logging
import logging.handlers
import subprocess
import sys
import os
import re
import signal
import threading
import configparser
from pathlib import Path
from urllib.parse import parse_qsl

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# App directory (for locating files when launched from an arbitrary cwd,
# e.g. by Task Scheduler / launchd / systemd)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent


def resolve_path(path: str) -> Path:
    """Resolve a possibly-relative path against APP_DIR rather than cwd."""
    p = Path(path)
    return p if p.is_absolute() else APP_DIR / p


PID_FILE = resolve_path("netlogger_bridge.pid")

# Written at every step of the poll loop (see heartbeat()). The PID file alone
# only proves a process exists, which a *hung* bridge satisfies just as well as
# a healthy one — this file is what proves it's still making progress.
HEARTBEAT_FILE = resolve_path("netlogger_bridge.heartbeat")

TASK_NAME = "NetLoggerBridge"
WATCHDOG_TASK_NAME = "NetLoggerBridgeWatchdog"


# ---------------------------------------------------------------------------
# Logging setup
#
# RotatingFileHandler caps netlogger_bridge.log at 5MB x 5 backups instead of
# growing forever. A sys.excepthook logs any exception that still escapes
# every try/except in the codebase (e.g. one raised during startup, before
# the poll loop's own try/except below is even reached) with a full
# traceback — previously an uncaught exception's traceback went to stderr,
# which is discarded when the bridge is launched hidden via the Task
# Scheduler VBS wrapper, so the process would just vanish mid-log with no
# record of why.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            resolve_path("netlogger_bridge.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger(__name__)


def _log_unhandled_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log.critical("Unhandled exception — bridge is exiting", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = _log_unhandled_exception


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


SAMPLE_CONFIG = """\
[general]
# Seconds between file polls
poll_interval = 10

# Path to NetLogger's Contacts.adi file.
# Leave blank to auto-detect from default OS locations.
# Windows default: %APPDATA%\\NetLogger\\Contacts.adi
# macOS default:   ~/.config/NetLogger/Contacts.adi
# Linux default:   ~/.config/NetLogger/Contacts.adi
# On macOS/Linux that directory is hidden; in Finder press Cmd+Shift+G and
# paste ~/.config/NetLogger to reach it.
contacts_adi =

# File used to track which contacts have already been forwarded, between restarts
state_file = forwarded_qsos.txt

# Minutes to wait before retrying a contact that failed to forward to one or
# more enabled outputs
retry_interval_minutes = 60

# Days to keep retrying a failed contact before giving up on it permanently
retry_give_up_days = 5

# Seconds to wait for any single output before giving up on it and moving on
# to the next one. Each sender already sets its own network timeouts, but
# those cap individual socket operations rather than the whole call — this is
# the backstop that keeps one unresponsive service from stalling the others.
sender_timeout_seconds = 60

# Minutes without any forward progress before the --watchdog task treats a
# still-running bridge as hung, kills it, and starts a fresh one.
heartbeat_stale_minutes = 5

[wavelog]
# Set enabled = true to forward contacts to WaveLog
enabled = false

# Base URL of your WaveLog instance, including index.php (no trailing slash)
url = https://log.example.com/index.php

# WaveLog 3.1.0 added a new v2 API alongside the original one. Leave this as
# true to keep using the original API. Set it to false to use the v2 API, which
# needs a separate v2 API key (it starts with "wl2_"); v1 keys are rejected by
# the v2 endpoints and vice versa.
use_legacy_api = true

# WaveLog API key, matching the API version selected above
# (Account > API Keys in WaveLog)
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

[n1mm]
# Set enabled = true to forward contacts to N1MM Logger+
# In N1MM: Config > Configure Ports > WSJT-X tab, check "Enable WSJT-X Decode List",
# set UDP port to match below; QSOs arrive as WSJT-X "Log QSO" packets (type 5)
enabled = false

# Hostname or IP of the machine running N1MM Logger+
host = 127.0.0.1

# UDP port (N1MM WSJT-X listener port; default 2237)
port = 2237

# Your station callsign (sent inside the WSJT-X Log QSO packet)
my_call =

[hrd]
# Set enabled = true to forward contacts to Ham Radio Deluxe (HRD) Logbook
# In HRD: Tools > Network Server, ensure Autostart is enabled and note the
# command port on the Logbook tab (NOT the "QSO Forwarding" UDP feature)
enabled = false

# Hostname or IP of the machine running HRD Logbook
host = 127.0.0.1

# TCP port for HRD's Network Server command interface (default 7826)
port = 7826

# Fallback station callsign, only used if a contact's ADIF record has no
# Station_Callsign field of its own
my_call =

[log4om]
# Set enabled = true to forward contacts to Log4OM v2
# In Log4OM: Communicator > Inbound Connections > Add, type ADIF, port must match below
enabled = false

# Hostname or IP of the machine running Log4OM
host = 127.0.0.1

# UDP port (must match the Log4OM inbound ADIF connection port you configured)
port = 2234

[dxkeeper]
# Set enabled = true to forward contacts to DXLab Suite DXKeeper
# DXKeeper must be running; its TCP base port is set in DXKeeper > Config > Ports
enabled = false

# Hostname or IP of the machine running DXKeeper
host = 127.0.0.1

# TCP port (DXKeeper default: 52001, which is base port 52000 + 1)
port = 52001

[macloggerdx]
# Set enabled = true to forward contacts to MacLoggerDX
# In MacLoggerDX: Station prefs, enable WSJT-X/JTDX/JS8Call UDP, note the port
enabled = false

# Hostname or IP of the Mac running MacLoggerDX
host = 127.0.0.1

# UDP port (MacLoggerDX default: 2237, same as N1MM's WSJT-X listener)
port = 2237

# Your station callsign (sent inside the WSJT-X Log QSO packet)
my_call =

[k1alf_omiss_awards]
# Set enabled = true to forward contacts to the K1ALF OMISS Awards Tracker
# (https://k1alf.com/omiss_awards/). Only contacts logged under NetLogger's
# OMISS club are sent — everything else is silently skipped, since the site
# only accepts OMISS net contacts.
enabled = false

# Your K1ALF OMISS Awards Tracker login (same call sign you use to log in on the site)
call_sign =

# Your K1ALF OMISS Awards Tracker password
password =

[qrz]
# Set enabled = true to forward contacts to QRZ Logbook
# Requires a QRZ subscription (XML level or higher). Get your Logbook API
# key from QRZ.com > Logbook > Settings > API Key (not the XML lookup key).
enabled = false

# Your QRZ Logbook API key
api_key = YOUR_QRZ_LOGBOOK_API_KEY
"""


def create_sample_config():
    with open("config.ini", "w", encoding="utf-8") as f:
        f.write(SAMPLE_CONFIG)
    print("Sample config.ini created. Edit it and re-run.")


def default_config() -> configparser.ConfigParser:
    """Return a ConfigParser pre-populated with the sample config's defaults."""
    cfg = configparser.ConfigParser()
    cfg.read_string(SAMPLE_CONFIG)
    return cfg


def load_config_for_gui(config_path: str) -> configparser.ConfigParser:
    """Load config_path over top of the sample defaults, without exiting if missing."""
    cfg = default_config()
    if Path(config_path).exists():
        cfg.read(config_path, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# ADI file location
# ---------------------------------------------------------------------------
# Candidates are tried in order; the first that exists wins. macOS is listed
# with ~/.config first because that is where NetLogger actually writes on a
# Mac (verified against a real install) — it follows the same XDG-style
# convention it uses on Linux rather than ~/Library/Application Support, which
# it never creates. Getting this wrong made auto-detection impossible on every
# Mac, and ~/.config is hidden in Finder, so the user could not easily find the
# file to configure it by hand either (issue #29).
ADI_PATHS = {
    "win32": [
        Path(os.environ.get("APPDATA", "~"), "NetLogger", "Contacts.adi"),
        Path("~/.config/NetLogger/Contacts.adi").expanduser(),
    ],
    "darwin": [
        Path("~/.config/NetLogger/Contacts.adi").expanduser(),
        Path("~/Library/Application Support/NetLogger/Contacts.adi").expanduser(),
    ],
    "linux": [
        Path("~/.config/NetLogger/Contacts.adi").expanduser(),
    ],
}


def adi_candidates() -> list[Path]:
    """Default Contacts.adi locations for this platform, in priority order."""
    return ADI_PATHS.get(sys.platform, ADI_PATHS["linux"])


def autodetect_adi_file() -> Path | None:
    """First existing default Contacts.adi location, or None if there is none."""
    for candidate in adi_candidates():
        if candidate.exists():
            return candidate
    return None


def find_adi_file(cfg_path: str) -> Path:
    if cfg_path:
        p = Path(cfg_path).expanduser()
        if not p.exists():
            log.error(f"Configured Contacts.adi not found: {p}")
            sys.exit(1)
        return p

    found = autodetect_adi_file()
    if found:
        log.info(f"Auto-detected Contacts.adi: {found}")
        return found

    tried = "\n  ".join(str(c) for c in adi_candidates())
    log.error(
        "Could not auto-detect Contacts.adi. Looked in:\n  "
        f"{tried}\n"
        "Set [general] contacts_adi in config.ini to the full path."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# ADIF file tailer
# ---------------------------------------------------------------------------

def read_all_records(adi_path: Path) -> list[str]:
    """
    Read every complete ADIF record currently in the file.
    Each record string is the raw text between one <eor> and the next; a
    trailing partial record (still being written by NetLogger) is dropped.
    """
    try:
        with open(adi_path, "rb") as f:
            data = f.read()
    except OSError as e:
        log.warning(f"File read error: {e}")
        return []

    text = data.decode("utf-8", errors="replace")

    # Split on <eor> (case-insensitive); keep only complete records
    parts = re.split(r'<eor>', text, flags=re.IGNORECASE)
    complete = parts[:-1]  # last element is empty or an incomplete trailing record

    return [p.strip() for p in complete if p.strip()]


ADIF_FIELD_RE = re.compile(r'<(\w+):(\d+)(?::\w+)?>', re.IGNORECASE)


def normalize_adif(raw: str) -> str:
    """
    Re-serialize the ADIF record as a single line, ending with <EOR>.

    NetLogger writes one field per line, with some values (e.g. Address)
    spanning multiple lines. Blindly collapsing whitespace would shorten
    those values without updating their declared <TAG:LENGTH>, desyncing
    every field after it. Instead, each field is read using its declared
    length, internal whitespace is collapsed, and the length is recomputed
    to match. Fields are concatenated with no separators, matching the
    format N3FJP's ADDADIFRECORD API documents:
    <CALL:6>KA3SEQ<QSO_Date:8>20220317<Time_On:6>205405<Band:3>40M<Mode:3>SSB<EOR>
    """
    fields = []
    for match in ADIF_FIELD_RE.finditer(raw):
        tag = match.group(1)
        length = int(match.group(2))
        value = raw[match.end():match.end() + length]
        value = " ".join(value.split())
        fields.append(f"<{tag}:{len(value)}>{value}")
    return "".join(fields) + "<EOR>"


def extract_field(adif: str, field: str) -> str:
    """Extract a single field value from an ADIF string for logging purposes."""
    match = re.search(rf'<{field}:\d+>([^<]*)', adif, re.IGNORECASE)
    return match.group(1).strip() if match else "?"


def apply_omiss_comment_tag(adif: str) -> str:
    """
    For contacts logged under NetLogger's OMISS club, prepend the station's
    OMISS member number to COMMENT as "#000000#" (plus the existing comment
    text, if any) — the same format NetLogger's own CSV export already uses.

    Applied once here, right after normalize_adif, so every output sees the
    same tagged COMMENT: the full-ADIF senders (WaveLog, N3FJP, Log4OM,
    DXKeeper) read it straight from this record, and the field-by-field
    senders (N1MM, HRD, MacLoggerDX, K1ALF OMISS Awards) pull it via
    extract_field(adif, "COMMENT") same as any other field — no per-output
    special-casing needed.
    """
    if extract_field(adif, "APP_NETLOGGER_CLUB").upper() != "OMISS":
        return adif

    member_id = extract_field(adif, "APP_NETLOGGER_CLUBMEMBERID")
    if member_id == "?" or not member_id:
        return adif

    comment = extract_field(adif, "COMMENT")
    comment = "" if comment == "?" else comment
    tagged  = f"#{member_id}#" + (f" {comment}" if comment else "")

    new_field = f"<COMMENT:{len(tagged)}>{tagged}"
    if re.search(r'<COMMENT:\d+>', adif, re.IGNORECASE):
        return re.sub(r'<COMMENT:\d+>[^<]*', new_field, adif, count=1, flags=re.IGNORECASE)
    return adif.replace("<EOR>", f"{new_field}<EOR>")


def record_dedup_key(adif: str) -> str:
    """
    Build a stable identity for a QSO: QSO_DATE|TIME_ON|CALL|BAND.

    Used instead of file position to track what's already been forwarded, so
    edits/deletions elsewhere in Contacts.adi can't desync the bridge. BAND is
    included because a multi-band net can plausibly work the same station
    several times in one day on different bands; QSO_DATE+TIME_ON+CALL alone
    wouldn't distinguish those. Date/time lead the key (rather than call) so
    the state file sorts chronologically — easier to scan by net session when
    looking for one contact to delete and force a re-log.
    """
    call     = extract_field(adif, "CALL").upper()
    qso_date = extract_field(adif, "QSO_DATE")
    time_on  = extract_field(adif, "TIME_ON")
    band     = extract_field(adif, "BAND").upper()
    return f"{qso_date}|{time_on}|{call}|{band}"


# ---------------------------------------------------------------------------
# WaveLog
# ---------------------------------------------------------------------------

def _json_dict(resp: "requests.Response") -> dict:
    """resp.json() as a dict — {} if the body is valid JSON but not an object."""
    data = resp.json()
    return data if isinstance(data, dict) else {}


def send_to_wavelog(cfg: configparser.SectionProxy, adif: str) -> bool:
    if cfg.getboolean("use_legacy_api", fallback=True):
        return _send_to_wavelog_v1(cfg, adif)
    return _send_to_wavelog_v2(cfg, adif)


def _send_to_wavelog_v1(cfg: configparser.SectionProxy, adif: str) -> bool:
    """Original (pre-3.1.0) WaveLog API: POST {url}/api/qso with the key in the body."""
    url = cfg["url"].rstrip("/") + "/api/qso"
    payload = {
        "key": cfg["api_key"],
        "station_profile_id": cfg.getint("station_id", fallback=1),
        "type": "adif",
        "string": adif,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code in (200, 201):
            data = _json_dict(resp)
            if data.get("status") == "created" and data.get("adif_count", 0) > 0:
                return True
            log.warning(f"WaveLog did not import the record: {data}")
            return False
        log.error(f"WaveLog HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except (requests.RequestException, ValueError, TypeError) as e:
        log.error(f"WaveLog connection error: {e}")
        return False


def _send_to_wavelog_v2(cfg: configparser.SectionProxy, adif: str) -> bool:
    """WaveLog 3.1.0+ v2 API: POST {url}/api/v2/qso, bearer token, ADIF import body."""
    url = cfg["url"].rstrip("/") + "/api/v2/qso"
    payload = {
        "import_type": "adif",
        "station_profile_id": cfg.getint("station_id", fallback=1),
        "adif": adif,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            data = _json_dict(resp).get("data")
            if not isinstance(data, dict):
                data = {}
            if data.get("imported", 0) > 0:
                return True
            # skipped > 0 means WaveLog considered it a duplicate — same
            # treatment as the legacy API's 400 "abort" response.
            log.warning(f"WaveLog did not import the record: {data}")
            return False
        log.error(f"WaveLog API v2 HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except (requests.RequestException, ValueError, TypeError) as e:
        log.error(f"WaveLog API v2 error: {e}")
        return False


# ---------------------------------------------------------------------------
# N3FJP
# ---------------------------------------------------------------------------

def send_to_n3fjp(host: str, port: int, adif: str) -> bool:
    """
    Send ADDADIFRECORD command to N3FJP AC Log via TCP.
    Protocol: <CMD><ADDADIFRECORD><VALUE>{adif}</VALUE></CMD>
    Reference: http://www.n3fjp.com/help/api.html
    """
    command = f"<CMD><ADDADIFRECORD><VALUE>{adif}</VALUE></CMD>\r\n"
    log.debug(f"N3FJP command: {command.strip()}")
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            log.debug(f"N3FJP connected to {host}:{port}")
            sock.sendall(command.encode("utf-8"))

            # ADDADIFRECORD writes straight to the log file without
            # refreshing N3FJP's on-screen list; CHECKLOG forces a reload
            # so the new QSO appears immediately.
            sock.sendall(b"<CMD><CHECKLOG></CMD>\r\n")

            sock.settimeout(2)
            try:
                response = sock.recv(1024).decode("utf-8", errors="replace")
                if not response:
                    log.debug("N3FJP closed the connection with no response")
                else:
                    log.debug(f"N3FJP response: {response.strip()}")
                    if "error" in response.lower():
                        log.error(f"N3FJP rejected record: {response.strip()}")
                        return False
            except socket.timeout:
                log.debug("N3FJP sent no response within 2s (timeout)")
        return True
    except (socket.error, OSError) as e:
        log.error(f"N3FJP connection error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# N1MM Logger+
# ---------------------------------------------------------------------------

# Offset from Python datetime.date.toordinal() to Qt Julian Day Number.
# Verified: datetime.date(1970,1,1).toordinal() + 1721425 == 2440588 (Qt epoch).
_QTDATE_OFFSET  = 1721425
_WSJTX_MAGIC    = 0xADBCCBDA
_WSJTX_SCHEMA   = 2  # matches a real WSJT-X capture against N1MM+ 1.0.x; N1MM
                      # appears to ignore packets declaring schema 3


def _wsjtx_str(s: str) -> bytes:
    """Pack a string as WSJT-X QByteArray: quint32 byte-length + UTF-8 bytes."""
    enc = s.encode("utf-8") if s else b""
    return struct.pack(">I", len(enc)) + enc


def _wsjtx_null() -> bytes:
    """
    Pack a *null* WSJT-X QByteArray (length -1), distinct from an empty one
    (length 0, what `_wsjtx_str("")` produces). A real WSJT-X capture showed
    every unset string field using length 0 except the trailing field, which
    used -1 — replicated here rather than guessed at.
    """
    return struct.pack(">i", -1)


def _wsjtx_datetime(dt_str: str) -> bytes:
    """
    Pack a QDateTime in WSJT-X wire format.
    Layout: qint64 Julian day + quint32 ms-since-midnight + quint8 time-spec (1=UTC).
    """
    try:
        dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        jd = dt.toordinal() + _QTDATE_OFFSET
        ms = (dt.hour * 3600 + dt.minute * 60 + dt.second) * 1000
    except (ValueError, TypeError):
        jd = datetime.date(1970, 1, 1).toordinal() + _QTDATE_OFFSET
        ms = 0
    return struct.pack(">qIB", jd, ms, 1)


def _build_wsjtx_qso_messages(adif: str, my_call: str) -> tuple[bytes, bytes]:
    """
    Build the WSJT-X 'Log QSO' (type 5) and 'LoggedADIF' (type 12) UDP
    messages for one QSO. Shared by send_to_n1mm and send_to_macloggerdx,
    which both consume the same real-world WSJT-X wire format — confirmed
    for N1MM via a real WSJT-X-to-N1MM packet capture, which showed both
    message types sent for a single logged QSO (some receivers key off one
    or the other, so both are built here rather than guessing which one a
    given receiver actually uses).
    """
    call     = extract_field(adif, "CALL")
    freq     = extract_field(adif, "FREQ")
    mode     = extract_field(adif, "MODE")
    qso_date = extract_field(adif, "QSO_DATE")
    time_on  = extract_field(adif, "TIME_ON")
    time_off_raw = extract_field(adif, "TIME_OFF")
    time_off = time_off_raw if time_off_raw != "?" else time_on
    grid     = extract_field(adif, "GRIDSQUARE")
    grid     = grid if grid != "?" else ""
    rst_sent = extract_field(adif, "RST_SENT")
    rst_sent = rst_sent if rst_sent != "?" else ""
    rst_rcvd = extract_field(adif, "RST_RCVD")
    rst_rcvd = rst_rcvd if rst_rcvd != "?" else ""
    name     = extract_field(adif, "NAME")
    name     = name if name != "?" else ""
    operator = extract_field(adif, "OPERATOR")
    operator = operator if operator != "?" else my_call

    try:
        freq_hz = int(float(freq) * 1_000_000) if freq and freq != "?" else 0
    except ValueError:
        freq_hz = 0

    def _ts(date8: str, time6: str) -> str:
        if len(date8) == 8 and len(time6) >= 6:
            return (f"{date8[:4]}-{date8[4:6]}-{date8[6:8]} "
                    f"{time6[:2]}:{time6[2:4]}:{time6[4:6]}")
        return ""

    msg = (
        struct.pack(">III", _WSJTX_MAGIC, _WSJTX_SCHEMA, 5)  # header + type=5
        + _wsjtx_str("WSJT-X")                   # Id (client name)
        + _wsjtx_datetime(_ts(qso_date, time_off))  # Date/Time Off
        + _wsjtx_str(call)                        # DX call
        + _wsjtx_str(grid)                        # DX grid
        + struct.pack(">Q", freq_hz)              # Tx frequency Hz (quint64)
        + _wsjtx_str(mode)                        # Mode
        + _wsjtx_str(rst_sent)                    # Report sent
        + _wsjtx_str(rst_rcvd)                    # Report received
        + _wsjtx_str("")                          # Tx power
        + _wsjtx_str("")                          # Comments
        + _wsjtx_str(name)                        # Name
        + _wsjtx_datetime(_ts(qso_date, time_on)) # Date/Time On
        + _wsjtx_str(operator)                    # Operator call
        + _wsjtx_str(my_call)                     # My call
        + _wsjtx_str("")                          # My grid
        + _wsjtx_str("")                          # Exchange sent
        + _wsjtx_str("")                          # Exchange received
        + _wsjtx_null()                           # ADIF propagation mode (unset)
    )

    adif_blob = f"<ADIF_VER:5>3.1.0<PROGRAMID:16>NetLogger-Bridge<EOH>{adif}"
    msg_adif = (
        struct.pack(">III", _WSJTX_MAGIC, _WSJTX_SCHEMA, 12)  # header + type=12
        + _wsjtx_str("WSJT-X")    # Id (client name)
        + _wsjtx_str(adif_blob)   # ADIF text
    )

    return msg, msg_adif


def send_to_n1mm(cfg: configparser.SectionProxy, adif: str) -> bool:
    """
    Send QSO to N1MM Logger+ as WSJT-X binary UDP messages: a structured
    'Log QSO' packet (type 5) plus a 'LoggedADIF' packet (type 12) wrapping a
    self-contained ADIF record, matching what a real WSJT-X capture showed it
    sends for one logged QSO. In N1MM: Config > Configure Ports > WSJT-X tab,
    enable WSJT-X Decode List, set UDP port to match [n1mm] port in
    config.ini, then *fully restart N1MM* (the dialog warns changes need a
    restart, and won't bind the listening socket until you do).
    Reference: https://github.com/roelandjansen/wsjt-x/blob/master/NetworkMessage.hpp
    """
    host    = cfg.get("host", "127.0.0.1")
    port    = cfg.getint("port", fallback=2237)
    my_call = cfg.get("my_call", "")

    msg, msg_adif = _build_wsjtx_qso_messages(adif, my_call)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(msg, (host, port))
            sock.sendto(msg_adif, (host, port))
        return True
    except (socket.error, OSError) as e:
        log.error(f"N1MM UDP error ({host}:{port}): {e}")
        return False


def send_to_macloggerdx(cfg: configparser.SectionProxy, adif: str) -> bool:
    """
    Send QSO to MacLoggerDX as WSJT-X binary UDP messages (same wire format
    as send_to_n1mm — see _build_wsjtx_qso_messages). MacLoggerDX listens for
    WSJT-X/JTDX/JS8Call broadcasts on UDP port 2237 by default (Station
    prefs) and logs the 'QSO Logged' (type 5) message by default, or the
    'Logged ADIF' (type 12) message instead if its "WSJT-X Log ADIF"
    checkbox (Log prefs) is checked — both are sent here so either setting
    works without needing to match it.

    UNVERIFIED: built from MacLoggerDX's own documentation only; unlike the
    other five outputs, this hasn't been tested against a real install
    (Mac-only software, no Mac available when this was written). Treat as
    more likely than the others to need a fix once actually tested.
    Reference: https://dogparksoftware.com/MacLoggerDX%20Help/mldxfc_wsjtx.html
    """
    host    = cfg.get("host", "127.0.0.1")
    port    = cfg.getint("port", fallback=2237)
    my_call = cfg.get("my_call", "")

    msg, msg_adif = _build_wsjtx_qso_messages(adif, my_call)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(msg, (host, port))
            sock.sendto(msg_adif, (host, port))
        return True
    except (socket.error, OSError) as e:
        log.error(f"MacLoggerDX UDP error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# Ham Radio Deluxe (HRD)
# ---------------------------------------------------------------------------

def send_to_hrd(cfg: configparser.SectionProxy, adif: str) -> bool:
    """
    Send QSO to HRD Logbook via its Network Server's plain-text TCP API
    ('db add {FIELD="VALUE" ...}'). In HRD: Tools > Network Server, ensure
    Autostart is enabled; the command port (Logbook tab, default 7826 in
    recent versions) must match [hrd] port in config.ini. HRD's "QSO
    Forwarding" (UDP, N1MM-compatible XML) is a *different* feature and was
    not used here — this command syntax was reverse-engineered from a real
    GridTracker-to-HRD TCP capture, since HRD's own published API docs (a
    quoted database name before the field list, e.g. 'db add "My Logbook"
    {...}') turned out to be stale for current HRD versions, which both omit
    the database name and expect FREQ in Hz rather than MHz.
    """
    host = cfg.get("host", "127.0.0.1")
    port = cfg.getint("port", fallback=7826)

    qso_date = extract_field(adif, "QSO_DATE")
    time_on  = extract_field(adif, "TIME_ON")
    time_off = extract_field(adif, "TIME_OFF")
    time_off = time_off if time_off != "?" else time_on
    freq     = extract_field(adif, "FREQ")

    station_callsign = extract_field(adif, "STATION_CALLSIGN")
    station_callsign = station_callsign if station_callsign != "?" else cfg.get("my_call", "")

    fields = {
        "CALL":             extract_field(adif, "CALL"),
        "MODE":             extract_field(adif, "MODE"),
        "RST_SENT":         extract_field(adif, "RST_SENT"),
        "RST_RCVD":         extract_field(adif, "RST_RCVD"),
        "QSO_DATE":         qso_date,
        "TIME_ON":          time_on,
        "QSO_DATE_OFF":     qso_date,
        "TIME_OFF":         time_off,
        "BAND":             extract_field(adif, "BAND"),
        "GRIDSQUARE":       extract_field(adif, "GRIDSQUARE"),
        "NAME":             extract_field(adif, "NAME"),
        "CNTY":             extract_field(adif, "CNTY"),
        "STATE":            extract_field(adif, "STATE"),
        "DXCC":             extract_field(adif, "DXCC"),
        "COUNTRY":          extract_field(adif, "COUNTRY"),
        "COMMENT":          extract_field(adif, "COMMENT"),
        "OPERATOR":         extract_field(adif, "OPERATOR"),
        "STATION_CALLSIGN": station_callsign,
    }

    try:
        if freq and freq != "?":
            fields["FREQ"] = str(round(float(freq) * 1_000_000))
    except ValueError:
        pass

    # Double quotes would break the "FIELD="VALUE"" syntax; ham log comments
    # essentially never contain them, but swap rather than risk corrupting
    # every field after it in the command.
    parts = " ".join(
        f'{name}="{value.replace(chr(34), chr(39))}"'
        for name, value in fields.items() if value and value != "?"
    )
    command = f"ver\r\ndb add {{{parts}}}\r\nexit\r\n"

    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(command.encode("utf-8"))
            sock.settimeout(2)
            try:
                response = sock.recv(4096).decode("utf-8", errors="replace")
                log.debug(f"HRD response: {response.strip()}")
                if "Added" not in response:
                    log.error(f"HRD rejected record: {response.strip()}")
                    return False
            except socket.timeout:
                log.debug("HRD sent no response within 2s")
        return True
    except (socket.error, OSError) as e:
        log.error(f"HRD TCP error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# Log4OM
# ---------------------------------------------------------------------------

def send_to_log4om(host: str, port: int, adif: str) -> bool:
    """
    Send ADIF QSO record to Log4OM v2 via UDP inbound ADIF service.
    Configure Log4OM: Communicator > Inbound Connections > Add, type ADIF,
    port must match the [log4om] port in config.ini.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(adif.encode("utf-8"), (host, port))
        return True
    except (socket.error, OSError) as e:
        log.error(f"Log4OM UDP error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# DXLab Suite DXKeeper
# ---------------------------------------------------------------------------

def send_to_dxkeeper(host: str, port: int, adif: str) -> bool:
    """
    Send QSO to DXKeeper via its TCP externallog command.
    DXKeeper listens on base_port + 1 (default 52001).
    Message format uses DXLab ADIF field encoding:
      <command:11>externallog<parameters:N><ExternalLogADIF:M>[adif fields incl. <EOR>]
    DXLab's own documented example keeps the trailing <EOR> inside
    ExternalLogADIF's length-prefixed payload; an earlier version of this
    function stripped it, leaving an incomplete ADIF record that DXKeeper
    silently refused to log ("could not be logged:" with no reason given).
    Reference: https://www.dxlabsuite.com/Interoperation.htm
    """
    adif_fields = adif if adif.upper().endswith("<EOR>") else adif.rstrip() + "<EOR>"

    M      = len(adif_fields.encode("utf-8"))
    params = f"<ExternalLogADIF:{M}>{adif_fields}"
    N      = len(params.encode("utf-8"))
    message = f"<command:11>externallog<parameters:{N}>{params}"

    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(message.encode("utf-8"))
            sock.settimeout(2)
            try:
                response = sock.recv(1024).decode("utf-8", errors="replace")
                if response:
                    log.debug(f"DXKeeper response: {response.strip()}")
                    if "error" in response.lower():
                        log.error(f"DXKeeper rejected record: {response.strip()}")
                        return False
            except socket.timeout:
                log.debug("DXKeeper sent no response within 2s (normal)")
        return True
    except (socket.error, OSError) as e:
        log.error(f"DXKeeper TCP error ({host}:{port}): {e}")
        return False


# ---------------------------------------------------------------------------
# QRZ Logbook
# ---------------------------------------------------------------------------
#
# POST to logbook.qrz.com/api with ACTION=INSERT and the raw ADIF record —
# a subscriber-only feature (XML subscription level or higher) requiring a
# per-user logbook API key (QRZ.com > Logbook > Settings > API Key), not the
# separate XML/callsign-lookup API key. Response is name=value pairs, not
# JSON: RESULT=OK (or RESULT=REPLACE, if the QSO duplicated an existing
# record and OPTION=REPLACE was set) on success, RESULT=FAIL&REASON=... on
# failure. OPTION=REPLACE is deliberately not sent — QRZ's own docs warn it
# "WILL overwrite confirmed QSOs", so a plain INSERT that just fails on a
# duplicate (already-logged QSOs get replayed on every bridge restart's
# first-run seed) is the safer default. Reference:
# https://www.qrz.com/docs/logbook/QRZLogbookAPI.html
_QRZ_API_URL = "https://logbook.qrz.com/api"
_QRZ_USER_AGENT = "NetLoggerBridge (github.com/MikeWills/NetLogger-Sync)"


def send_to_qrz(cfg: configparser.SectionProxy, adif: str) -> bool:
    payload = {
        "KEY": cfg["api_key"],
        "ACTION": "INSERT",
        "ADIF": adif,
    }
    try:
        resp = requests.post(
            _QRZ_API_URL,
            data=payload,
            headers={"User-Agent": _QRZ_USER_AGENT},
            timeout=10,
        )
    except requests.RequestException as e:
        log.error(f"QRZ Logbook connection error: {e}")
        return False

    if resp.status_code != 200:
        log.error(f"QRZ Logbook HTTP {resp.status_code}: {resp.text[:200]}")
        return False

    result = dict(parse_qsl(resp.text))
    if result.get("RESULT") in ("OK", "REPLACE"):
        return True
    log.error(f"QRZ Logbook rejected record: {result.get('REASON', resp.text[:200])}")
    return False


# ---------------------------------------------------------------------------
# K1ALF OMISS Awards Tracker (k1alf.com)
# ---------------------------------------------------------------------------
#
# There is no API — the site only accepts a NetLogger-format CSV upload
# behind a login (https://k1alf.com/omiss_awards/index.php?page=log_import).
# Both the login and the upload are plain HTML forms whose markup is broken
# (the login <form> is opened as a direct child of a <tr>, so the browser's
# HTML5 "form pointer" parsing quirk associates the actual <input> elements
# with it even though they're DOM siblings, not descendants — verified via
# `input.form` in a real browser rather than assumed), so both were
# reverse-engineered by inspecting the live DOM/network traffic rather than
# any documented API:
#   Login:  POST process.php  {call_sign, password, login=Submit}
#   Upload: POST process.php  multipart {MAX_FILE_SIZE=10485760, my_end,
#           file_upload=<csv>, import=Submit}  (my_end: 0=Base 1=Mobile 2=Portable)
# A plain requests.Session() carries the login cookie across both calls, kept
# in the module-level _k1alf_session so login only happens once per bridge
# run (not once per QSO) and is retried once, transparently, if the session
# is ever found to have expired.
#
# `my_end` is the *uploader's own* station status for the whole import — the
# log_import page literally labels it "Mark my station as a ... station for
# the records being imported" — always sent as Base (0). An earlier version
# fed it from NetLogger's APP_NETLOGGER_MP_STATUS field, on the assumption
# that field meant "my" status; it doesn't. NetLogger has no per-QSO field at
# all for the *account holder's* own operating mode (there'd be no reason for
# it to, since NetLogger logs the *other* station checking into the net) —
# MP_Status instead records the contacted station's mobile/portable status,
# confirmed by checking every MP_Status value NetLogger ever recorded for a
# station operating a portable "combo" callsign, which was consistently "P".
# Feeding that into my_end told the site the *uploader* was portable whenever
# the contact was, which is backwards (caught via a live account showing a
# contact's portable status landing in the "My End" column instead of "Other
# End"). The site's CSV import has no field for the other station's status at
# all; "Other End" can only be corrected by hand afterward, per contact, via
# the dropdown on the Call Log page.
#
# The CSV itself has to match NetLogger's own "export contacts as CSV"
# format exactly ("ADIF files will not upload correctly", per the site) —
# there's no way to see that format from the ADIF tailer alone, so the exact
# column mapping below was reverse-engineered by diffing a real NetLogger
# CSV export against the matching raw Contacts.adi records for the same
# QSOs. Two columns don't map straightforwardly from ADIF field names:
#   - His_RST/My_RST are swapped from what their names suggest: confirmed
#     against two real records that His_RST is RST_Rcvd and My_RST is
#     RST_Sent, not the other way around.
#   - Remarks is just COMMENT, which apply_omiss_comment_tag() has already
#     prefixed with "#{App_NetLogger_ClubMemberId}#" for every OMISS contact
#     before any sender sees the record — not a separate synthesis here.
# County also needs stripping: NetLogger's Cnty field is "STATE,County"
# (e.g. "MS,JASPER") but the CSV column wants just "County".
#
# Only contacts logged under NetLogger's OMISS club are sent — NetLogger
# tracks contacts across many unrelated clubs/nets (visible as separate
# folders under NetLogger's data directory), and the site rejects anything
# else as "not OMISS related". Forwarding those anyway would just make every
# non-OMISS QSO retry hourly for retry_give_up_days before failing
# permanently, for no benefit, so send_to_k1alf_omiss_awards treats a
# non-OMISS contact as trivially done (returns True without sending) instead.

_K1ALF_BASE_URL = "https://k1alf.com/omiss_awards"

# Persisted across calls so login happens once per bridge run, not once per
# QSO. Keyed by call_sign so a config change (or GUI restart with a
# different account) forces a fresh login rather than reusing a stale session.
_k1alf_session = None
_k1alf_session_call = None


def _k1alf_login(call_sign: str, password: str):
    session = requests.Session()
    try:
        resp = session.post(
            f"{_K1ALF_BASE_URL}/process.php",
            data={"call_sign": call_sign, "password": password, "login": "Submit"},
            timeout=10,
        )
    except requests.RequestException as e:
        log.error(f"K1ALF OMISS Awards login connection error: {e}")
        return None
    if "Log Out" not in resp.text:
        log.error("K1ALF OMISS Awards login failed — check call_sign/password in config.ini")
        return None
    return session


def _k1alf_county(cnty: str) -> str:
    """NetLogger's Cnty field is 'STATE,County'; the CSV column wants just 'County'."""
    return cnty.split(",", 1)[1] if "," in cnty else cnty


def _k1alf_csv_field(value: str, quote: bool) -> str:
    value = value.replace('"', '""')
    return f'"{value}"' if quote else value


def build_k1alf_omiss_csv(adif: str) -> str:
    """Build a one-record CSV matching NetLogger's own CSV export format (see
    the K1ALF OMISS Awards Tracker section comment above for how each column
    was derived). Quoting matches NetLogger's exporter column-for-column,
    though the site's CSV parser almost certainly doesn't care which fields
    are quoted as long as the file is valid CSV.
    """
    def field(name: str, default: str = "") -> str:
        v = extract_field(adif, name)
        return v if v != "?" else default

    qso_date = field("QSO_DATE")
    time_on  = field("TIME_ON")
    date_fmt = f"{qso_date[0:4]}/{qso_date[4:6]}/{qso_date[6:8]}" if len(qso_date) == 8 else qso_date
    time_fmt = f"{time_on[0:2]}:{time_on[2:4]}:{time_on[4:6]}" if len(time_on) == 6 else time_on

    remarks = field("COMMENT")

    columns = [
        (date_fmt,                      False),
        (time_fmt,                      False),
        (field("CALL"),                 False),
        (field("FREQ"),                 False),
        (field("MODE"),                 False),
        (field("BAND"),                 False),
        (field("DXCC"),                 False),
        (field("RST_RCVD"),             False),
        (field("RST_SENT"),             False),
        (field("NAME"),                 True),
        (field("QTH"),                  True),
        (field("STATE"),                False),
        (_k1alf_county(field("CNTY")),  True),
        (field("GRIDSQUARE"),           False),
        (field("QSL_SENT", "N"),        False),
        (field("QSL_RCVD", "N"),        False),
        (remarks,                       True),
        (field("ADDRESS"),              True),
        (field("APP_NETLOGGER_NET"),    True),
        (field("OPERATOR"),             False),
        (field("QSL_VIA"),              True),
        ("",                            False),
    ]

    header = ("Date,Time,Callsign,Frequency,Mode,Band,DXCC,His_RST,My_RST,Name,City,"
              "State,County,Grid,QSL_S,QSL_R,Remarks,Address,Net Name,Operator,"
              "QSL Info, QSL Message")
    row = ",".join(_k1alf_csv_field(v, q) for v, q in columns)
    return f"{header}\r\n{row}\r\n"


def send_to_k1alf_omiss_awards(cfg: configparser.SectionProxy, adif: str) -> bool:
    global _k1alf_session, _k1alf_session_call

    club = extract_field(adif, "APP_NETLOGGER_CLUB")
    if club != "?" and club.upper() != "OMISS":
        return True

    call_sign = cfg.get("call_sign", "")
    password  = cfg.get("password", "")

    if _k1alf_session is None or _k1alf_session_call != call_sign:
        _k1alf_session = _k1alf_login(call_sign, password)
        _k1alf_session_call = call_sign if _k1alf_session else None
        if _k1alf_session is None:
            return False

    csv_text = build_k1alf_omiss_csv(adif)

    def _upload():
        try:
            return _k1alf_session.post(
                f"{_K1ALF_BASE_URL}/process.php",
                # "my_end" is the *uploader's own* station status for this
                # import (the page literally labels it "Mark my station as a
                # ... station for the records being imported") — always Base,
                # since NetLogger has no per-QSO field for the account
                # holder's own operating mode (only APP_NETLOGGER_MP_STATUS,
                # which records the *contacted* station's mobile/portable
                # status). The site has no CSV-import field for the other
                # station's status at all — "Other End" can only be set by
                # hand afterward via the Call Log page's per-row dropdown.
                data={"MAX_FILE_SIZE": "10485760", "my_end": "0", "import": "Submit"},
                files={"file_upload": ("netlogger.csv", csv_text, "text/csv")},
                timeout=15,
            )
        except requests.RequestException as e:
            log.error(f"K1ALF OMISS Awards connection error: {e}")
            return None

    resp = _upload()

    # A session that's expired server-side gets bounced back to the login
    # form instead of the call log; log in again and retry once.
    if resp is not None and "Log Out" not in resp.text:
        _k1alf_session = _k1alf_login(call_sign, password)
        _k1alf_session_call = call_sign if _k1alf_session else None
        if _k1alf_session is None:
            return False
        resp = _upload()

    if resp is None:
        return False
    if resp.status_code != 200:
        log.error(f"K1ALF OMISS Awards HTTP {resp.status_code}")
        return False

    match = re.search(r"(\d+) records were new.*?(\d+) records were duplicates", resp.text, re.DOTALL)
    if match and (int(match.group(1)) + int(match.group(2))) > 0:
        return True
    log.error(f"K1ALF OMISS Awards import did not confirm success: {resp.text[:300]}")
    return False


# ---------------------------------------------------------------------------
# Output dispatch (used for both first attempts and per-service retries)
# ---------------------------------------------------------------------------

SERVICE_LABELS = {
    "wavelog":             "WaveLog",
    "n3fjp":               "N3FJP",
    "n1mm":                "N1MM",
    "hrd":                 "HRD",
    "log4om":              "Log4OM",
    "dxkeeper":            "DXKeeper",
    "macloggerdx":         "MacLoggerDX",
    "k1alf_omiss_awards":  "K1ALF OMISS Awards",
    "qrz":                 "QRZ Logbook",
}


# ---------------------------------------------------------------------------
# Hang detection
#
# Every network sender sets its own timeout, but those cap individual socket
# operations rather than a whole call: in a real incident send_to_qrz() sat
# inside urllib3's TLS handshake to logbook.qrz.com for the better part of an
# hour despite requests' timeout=10. Because run()'s poll loop is
# single-threaded, that one wedged call stopped *every* output — contacts
# logged in the meantime reached nothing at all — and --watchdog couldn't see
# it, since the process was still very much alive and only get_running_bridge_pid()
# was ever consulted.
#
# Two independent defenses, mirroring the main-task/watchdog-task split:
#   * _call_with_timeout puts a hard wall-clock cap on each sender, so the
#     loop can't be held hostage by one of them and recovers on its own.
#   * heartbeat()/heartbeat_age() record forward progress, so --watchdog can
#     restart a bridge that has stopped working rather than only one that has
#     stopped existing.
# ---------------------------------------------------------------------------

DEFAULT_SENDER_TIMEOUT = 60
DEFAULT_HEARTBEAT_STALE_MINUTES = 5

# A sender that blows its deadline is abandoned, not killed — Python can't
# interrupt a thread blocked in a C-level socket call. It normally unwedges on
# its own once the OS gives up on the connection, so a few of these are
# survivable; a pile-up means something is badly wrong, and run() asks to be
# restarted rather than leaking threads and sockets indefinitely.
STUCK_SENDER_LIMIT = 3
_abandoned_sender_threads = []

# Longest the loop ever goes between beats while idle (see sleep_with_heartbeat).
_HEARTBEAT_INTERVAL = 15


def heartbeat():
    """Record that the poll loop is still making progress."""
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError:
        pass  # A heartbeat we can't write isn't worth killing the bridge over.


def heartbeat_age() -> "float | None":
    """Seconds since the bridge last made progress, or None if not knowable."""
    try:
        return max(0.0, time.time() - float(HEARTBEAT_FILE.read_text().strip()))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Shutdown handling
#
# run()'s finally block removes PID_FILE/HEARTBEAT_FILE, but it only gets the
# chance if the process actually unwinds. By default it doesn't: on macOS and
# Linux, launchd and systemd stop the bridge with SIGTERM, whose default
# disposition kills the process outright — no finally, no atexit. On Windows,
# shutdown/logoff sends the console CTRL_SHUTDOWN_EVENT/CTRL_LOGOFF_EVENT,
# which Python's default handler turns into an immediate ExitProcess. Either
# way the bridge leaves a PID file naming a process that no longer exists,
# which makes the GUI report a running bridge that isn't, and gives the
# single-instance guard and the watchdog a dead PID to reason about (one the
# OS is free to hand to some other python process later).
#
# So: catch the signals we can, set a flag the poll loop already knows how to
# honour, and let the normal finally do the cleanup.
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()
_shutdown_complete = threading.Event()

# SetConsoleCtrlHandler does not keep a reference to the callback, so it has
# to stay alive here or it gets collected and the process faults on shutdown.
_console_ctrl_handler = None

# How long a Windows console handler waits for the poll loop to finish its own
# cleanup before doing it directly. The OS kills the process shortly after the
# handler returns (about 5s for CTRL_CLOSE), so this cannot be generous.
_SHUTDOWN_GRACE_SECONDS = 3.0

# How often the inter-cycle sleep checks for a shutdown request.
_SHUTDOWN_POLL_INTERVAL = 1.0


def shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def _cleanup_runtime_files():
    """Remove the PID and heartbeat files. Idempotent and never raises."""
    for path in (PID_FILE, HEARTBEAT_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning(f"Could not remove {path} during shutdown")


def request_shutdown(reason: str):
    """Ask the poll loop to stop and unwind normally."""
    if _shutdown_event.is_set():
        return
    log.info(f"Shutdown requested ({reason}) — stopping bridge")
    _shutdown_event.set()


def install_shutdown_handlers():
    """
    Install handlers so an OS shutdown, logoff, or service stop unwinds
    cleanly. Must be called from the main thread (signal.signal's own
    requirement); a failure to install is logged, never fatal, since a bridge
    that runs with untidy shutdown beats one that won't start.
    """
    def _signal_handler(signum, _frame):
        request_shutdown(f"signal {signum}")

    # SIGTERM is what systemd and launchd send to stop a service. SIGINT is
    # deliberately left alone: Ctrl-C already unwinds through run()'s finally
    # as a KeyboardInterrupt, and taking it over would cost the one way an
    # interactive user has to break out of a sender wedged in a socket — the
    # exact failure this program has already been bitten by.
    signals = [signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)

    for sig in signals:
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError, AttributeError):
            log.debug(f"Could not install handler for signal {sig}", exc_info=True)

    if sys.platform == "win32":
        _install_console_ctrl_handler()


def _install_console_ctrl_handler():
    """
    Handle the Windows console control events for shutdown and logoff.

    Windows does not deliver SIGTERM; at shutdown it sends CTRL_SHUTDOWN_EVENT
    (and CTRL_LOGOFF_EVENT at logoff) to console processes, which is what the
    autostart task's hidden bridge is. Python's default handling of those is to
    exit immediately, so without this the finally never runs.

    The handler runs on its own thread and the OS terminates the process soon
    after it returns, so it gives the poll loop a short grace period to finish
    its own cleanup and then does the (idempotent) cleanup itself rather than
    risk being killed waiting.
    """
    global _console_ctrl_handler

    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6
    handled = {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}

    try:
        import ctypes

        prototype = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        @prototype
        def _handler(event):
            if event not in handled:
                return False  # let Python's own Ctrl-C/Ctrl-Break handling run
            request_shutdown(f"console event {event}")
            _shutdown_complete.wait(_SHUTDOWN_GRACE_SECONDS)
            _cleanup_runtime_files()
            return True

        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True):
            log.debug("SetConsoleCtrlHandler failed")
            return
        _console_ctrl_handler = _handler
    except (ImportError, AttributeError, OSError):
        log.debug("Could not install console control handler", exc_info=True)


def sleep_with_heartbeat(seconds: float, stop_event=None) -> bool:
    """
    Wait between poll cycles without letting the heartbeat go stale, and
    report whether stop_event (or a shutdown request) was set. An idle bridge
    is still a healthy one, but poll_interval is a free-form user setting with
    nothing coupling it to heartbeat_stale_minutes — set it to 10 minutes and
    an unbroken sleep would have the watchdog killing a perfectly good bridge
    every 5.

    Shutdown is checked on the same slices: the bridge is asleep here for most
    of its life, so this is where a shutdown signal almost always lands, and
    waiting out the rest of poll_interval first could easily overrun the time
    the OS is willing to give us.
    """
    deadline = time.monotonic() + seconds
    next_beat = 0.0
    while True:
        now = time.monotonic()
        if now >= next_beat:
            heartbeat()
            next_beat = now + _HEARTBEAT_INTERVAL
        if _shutdown_event.is_set():
            return True
        remaining = deadline - now
        if remaining <= 0:
            return False

        # Waking about once a second costs nothing and bounds how long a
        # shutdown can sit unnoticed. Windows will not run a Python signal
        # handler while the main thread is parked in a lock wait, so the flag
        # is only seen when the wait returns — with a full _HEARTBEAT_INTERVAL
        # slice that was measured at ~13s, well past the ~5s Windows gives a
        # process at shutdown. The heartbeat keeps its own slower cadence.
        slice_ = min(remaining, next_beat - now, _SHUTDOWN_POLL_INTERVAL)
        waiter = stop_event if stop_event is not None else _shutdown_event
        if waiter.wait(slice_):
            return True


def _live_stuck_senders() -> int:
    """How many abandoned sender threads are still wedged right now."""
    _abandoned_sender_threads[:] = [t for t in _abandoned_sender_threads if t.is_alive()]
    return len(_abandoned_sender_threads)


def _call_with_timeout(name: str, sender, timeout: int) -> bool:
    """
    Run one sender under a hard wall-clock cap. A timeout is reported as an
    ordinary failure, so the contact keeps its normal retry treatment instead
    of being lost.
    """
    result = {}

    def target():
        try:
            result["ok"] = sender()
        except Exception:
            log.exception(f"{SERVICE_LABELS[name]} raised an unexpected error")
            result["ok"] = False

    thread = threading.Thread(target=target, name=f"send-{name}", daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        _abandoned_sender_threads.append(thread)
        log.error(f"{SERVICE_LABELS[name]} did not return within {timeout}s — abandoning it "
                  "so the remaining outputs aren't blocked")
        return False
    return bool(result.get("ok"))


def send_to_services(cfg: configparser.ConfigParser, adif: str, enabled: dict, only: set = None,
                     sender_timeout: int = DEFAULT_SENDER_TIMEOUT) -> dict:
    """
    Send `adif` to every enabled service, or just the ones named in `only`
    (used to retry previously-failed services without re-sending to ones
    that already succeeded). Returns {service: success} for each one tried.
    """
    senders = {
        "wavelog":  lambda: send_to_wavelog(cfg["wavelog"], adif),
        "n3fjp":    lambda: send_to_n3fjp(cfg["n3fjp"].get("host", "127.0.0.1"),
                                           cfg["n3fjp"].getint("port", fallback=1100), adif),
        "n1mm":     lambda: send_to_n1mm(cfg["n1mm"], adif),
        "hrd":      lambda: send_to_hrd(cfg["hrd"], adif),
        "log4om":   lambda: send_to_log4om(cfg["log4om"].get("host", "127.0.0.1"),
                                            cfg["log4om"].getint("port", fallback=2234), adif),
        "dxkeeper": lambda: send_to_dxkeeper(cfg["dxkeeper"].get("host", "127.0.0.1"),
                                              cfg["dxkeeper"].getint("port", fallback=52001), adif),
        "macloggerdx": lambda: send_to_macloggerdx(cfg["macloggerdx"], adif),
        "k1alf_omiss_awards": lambda: send_to_k1alf_omiss_awards(cfg["k1alf_omiss_awards"], adif),
        "qrz":      lambda: send_to_qrz(cfg["qrz"], adif),
    }
    results = {}
    for name, sender in senders.items():
        if not enabled.get(name) or (only is not None and name not in only):
            continue
        ok = results[name] = _call_with_timeout(name, sender, sender_timeout)
        log.info(f"  {SERVICE_LABELS[name]:<9}: {'OK' if ok else 'FAILED'}")
        # Beat between senders, not just between poll cycles: forwarding one
        # contact to every output legitimately takes minutes, and without this
        # a healthy-but-slow cycle would look indistinguishable from a hang.
        heartbeat()
    return results


# ---------------------------------------------------------------------------
# State persistence (per-contact, per-service forwarding status)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime.datetime:
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def _is_done(record: dict, enabled: dict) -> bool:
    """
    A record needs no more attention once retries were abandoned, it predates
    per-service tracking entirely (no service key at all — seeded on first
    run/--reset-state, or migrated from an older on-disk format; must stay
    done forever regardless of which services get enabled later, since the
    whole point of seeding is to never forward those contacts), or every
    *currently enabled* service succeeded. Checking only the keys already
    present (rather than every enabled service) would let a record forwarded
    before a new service was enabled — e.g. WaveLog+N3FJP succeeded weeks
    before K1ALF was turned on — look permanently "done" and never pick up
    the new service at all.
    """
    if record.get("gave_up"):
        return True
    if not any(k in SERVICE_LABELS for k in record):
        return True
    return all(record.get(name) for name, on in enabled.items() if on)


def load_state(state_file: str) -> dict:
    """
    Returns {"initialized": bool, "records": {dedup_key: record}}.
    Each record is a dict of {service_name: success_bool} for whichever
    services were attempted, plus "first_attempt"/"last_attempt" (ISO UTC)
    once a retry is pending, and "gave_up": True once retries are abandoned
    after the configured retry_give_up_days. A fully-successful record has no extra keys.

    A missing file means 'never run before' (initialized=False), causing the
    caller to silently seed from the current file rather than forward
    everything. An existing file is one JSON object per line — e.g.
    {"key": "...", "wavelog": true, "n3fjp": false, "first_attempt": "...",
    "last_attempt": "..."} — sorted chronologically (the key's date/time
    lead) so it's easy to scan for one contact. To force a contact to be
    re-logged to every enabled service, delete its line and restart the
    bridge. Older on-disk formats are migrated transparently: a bare byte
    offset or a plain pipe-delimited key (pre-retry-tracking) become a
    no-detail record (treated as already complete, since those formats only
    ever recorded a key once it had been attempted), and the brief JSON-dict
    version's "keys" are extracted the same way. Backfilling
    "first_attempt"/"last_attempt" for records that need retry-tracking but
    lack it happens in run(), not here, since that decision depends on which
    services are currently enabled (see _is_done).
    """
    path = Path(state_file)
    if not path.exists():
        return {"initialized": False, "records": {}}

    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "keys" in data:
            return {"initialized": True, "records": {k: {} for k in data["keys"]}}
    except ValueError:
        pass

    records = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict) or "key" not in obj:
                raise ValueError
            key = obj.pop("key")
        except (ValueError, KeyError):
            key, obj = line, {}
        records[key] = obj

    return {"initialized": True, "records": records}


def save_state(state_file: str, records: dict):
    """
    Write the state file atomically: a temp file in the same directory, then
    os.replace() onto the real name.

    A plain write_text truncates first and fills in after, so a process killed
    mid-write (an OS shutdown, a taskkill, a power cut) leaves a half-written
    file. That is not a harmless loss: the file still *exists*, so load_state
    reports initialized=True and simply doesn't see the records that were lost
    past the truncation point — and every one of those contacts gets forwarded
    a second time on the next start. Duplicate QSOs pushed to WaveLog/QRZ are
    the most user-visible failure this program has, so the window is worth
    closing whether or not the shutdown itself was graceful.
    """
    lines = [json.dumps({"key": key, **records[key]}) for key in sorted(records)]
    text = "\n".join(lines) + ("\n" if lines else "")

    path = Path(state_file)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        # Never let a state-file write take the bridge down; the records stay
        # in memory and the next cycle tries again.
        log.exception(f"Could not write state file {path}")
        try:
            tmp.unlink()
        except OSError:
            pass


def prune_records(records: dict, current_keys: set) -> dict:
    """
    Drop records for contacts no longer present in Contacts.adi (i.e. you
    deleted them in NetLogger), keeping the state file from growing forever.

    Pruning by age instead of presence is wrong here: `read_all_records`
    rescans the *entire* file every poll (by design, so edits/deletions can't
    desync anything), so a record from years ago is still "found" on every
    poll for as long as it stays in the file. Dropping it just because it's
    old would make it look new again on the very next poll — forwarding it
    again, re-adding it, then dropping it again next cycle, forever.
    """
    return {k: v for k, v in records.items() if k in current_keys}


def _seed_keys_from_existing(adi_path: Path) -> dict:
    """Build no-detail (already-complete) records for every QSO currently in the file, without forwarding any of them."""
    return {record_dedup_key(normalize_adif(raw)): {} for raw in read_all_records(adi_path)}


def reset_state(config_path: str = "config.ini"):
    """
    Re-arm 'first run' behavior: mark every QSO currently in Contacts.adi as
    already forwarded (without sending any of them), so the next `run()`
    only forwards QSOs logged from this point on.
    """
    cfg = load_config(config_path)
    general = cfg["general"]
    state_file = str(resolve_path(general.get("state_file", "forwarded_qsos.txt")))
    adi_path = find_adi_file(general.get("contacts_adi", ""))

    records = _seed_keys_from_existing(adi_path)
    save_state(state_file, records)
    log.info(f"State reset — marking {len(records)} existing contact(s) in {adi_path} as already logged. "
             "Only new contacts logged from this point will be forwarded.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(config_path: str = "config.ini", stop_event=None):
    """
    Run the poll loop. If stop_event (a threading.Event) is given, the loop
    exits once it's set instead of running forever — used by the GUI to
    start/stop the bridge in a background thread.
    """
    cfg = load_config(config_path)
    general = cfg["general"]

    poll_interval   = general.getint("poll_interval", fallback=10)
    state_file      = str(resolve_path(general.get("state_file", "forwarded_qsos.txt")))
    adi_path        = find_adi_file(general.get("contacts_adi", ""))
    retry_interval  = datetime.timedelta(minutes=general.getint("retry_interval_minutes", fallback=60))
    retry_give_up   = datetime.timedelta(days=general.getint("retry_give_up_days", fallback=5))
    sender_timeout  = general.getint("sender_timeout_seconds", fallback=DEFAULT_SENDER_TIMEOUT)

    enabled = {
        "wavelog":     cfg.getboolean("wavelog",     "enabled", fallback=False),
        "n3fjp":       cfg.getboolean("n3fjp",       "enabled", fallback=False),
        "n1mm":        cfg.getboolean("n1mm",        "enabled", fallback=False),
        "hrd":         cfg.getboolean("hrd",         "enabled", fallback=False),
        "log4om":      cfg.getboolean("log4om",      "enabled", fallback=False),
        "dxkeeper":    cfg.getboolean("dxkeeper",    "enabled", fallback=False),
        "macloggerdx": cfg.getboolean("macloggerdx", "enabled", fallback=False),
        "k1alf_omiss_awards": cfg.getboolean("k1alf_omiss_awards", "enabled", fallback=False),
        "qrz":         cfg.getboolean("qrz",         "enabled", fallback=False),
    }

    if not any(enabled.values()):
        log.error("No outputs enabled. Set enabled = true in at least one output section.")
        sys.exit(1)

    log.info(f"NetLogger Bridge starting — polling every {poll_interval}s")
    log.info(f"File     : {adi_path}")
    for name, label in SERVICE_LABELS.items():
        log.info(f"{label:<9}: {'enabled' if enabled[name] else 'disabled'}")

    state   = load_state(state_file)
    records = state["records"]

    # First run: mark every QSO already in the file as seen, without
    # forwarding it, so only contacts logged from this point on go out.
    if not state["initialized"]:
        records = _seed_keys_from_existing(adi_path)
        save_state(state_file, records)
        log.info(f"First run — marking {len(records)} existing contact(s) as already logged. "
                 "Only new contacts logged from this point will be forwarded.")
    else:
        log.info(f"Resuming — tracking {len(records)} previously forwarded contact(s)")

    # A record can be missing a currently-enabled service without ever having
    # failed anything — e.g. it was forwarded to WaveLog/N3FJP before K1ALF
    # was turned on. Give it retry-tracking now (rather than at load_state
    # time, which doesn't know what's enabled) so it's picked up on the very
    # next poll. last_attempt is backdated past retry_interval rather than
    # set to "now" so this doesn't force a wait — nothing was actually
    # attempted yet, so there's no reason to delay the first try.
    backfill_now = datetime.datetime.now(datetime.timezone.utc)
    backdated = (backfill_now - retry_interval - datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    backfilled = False
    for record in records.values():
        if not _is_done(record, enabled) and "last_attempt" not in record:
            record.setdefault("first_attempt", _now_iso())
            record["last_attempt"] = backdated
            backfilled = True
    if backfilled:
        save_state(state_file, records)

    # Two bridges sharing one state_file race each other, and the second one
    # to start also overwrites PID_FILE — so killing it leaves the survivor
    # invisible to --watchdog, which then cheerfully starts yet another. The
    # GUI does its own detect-and-confirm before Start, so this only guards
    # the headless path, where nothing else is asking.
    if stop_event is None:
        other = get_running_bridge_pid()
        if other is not None and other != os.getpid() and _pid_looks_like_bridge(other):
            log.error(f"Another bridge is already running (PID {other}) — exiting rather than "
                      "racing it for the state file")
            return

    restart_requested = False

    try:
        # The GUI can Start/Stop/Start within one process, so this can't be
        # left set from a previous run or a console handler would skip its
        # grace period and clean up while the new loop is still going.
        _shutdown_complete.clear()

        PID_FILE.write_text(str(os.getpid()))
        heartbeat()

        while not _shutdown_event.is_set() and (stop_event is None or not stop_event.is_set()):
            # Anything unexpected here (a parsing edge case, a transient I/O
            # error, a bug in a sender not already caught internally) used to
            # propagate straight out of this loop and kill the whole process
            # — silently, since the traceback went to stderr, which is
            # discarded when launched hidden via the autostart wrapper.
            # Logging and continuing instead makes one bad poll cycle
            # self-healing rather than fatal.
            try:
                heartbeat()
                current_keys = set()
                now = datetime.datetime.now(datetime.timezone.utc)

                for raw in read_all_records(adi_path):
                    adif = apply_omiss_comment_tag(normalize_adif(raw))
                    key  = record_dedup_key(adif)
                    current_keys.add(key)

                    record = records.get(key)
                    if record is not None and _is_done(record, enabled):
                        continue

                    callsign = extract_field(adif, "Call")
                    band     = extract_field(adif, "Band")
                    mode     = extract_field(adif, "Mode")

                    if record is None:
                        log.info(f"New contact: {callsign} {band} {mode}")
                        log.debug(f"ADIF: {adif}")
                        results = send_to_services(cfg, adif, enabled, sender_timeout=sender_timeout)
                        if all(results.values()):
                            records[key] = results
                        else:
                            ts = _now_iso()
                            records[key] = {**results, "first_attempt": ts, "last_attempt": ts}
                        save_state(state_file, records)
                        continue

                    # Previously attempted but incomplete — retry on retry_interval, give up after retry_give_up
                    if now - _parse_iso(record["last_attempt"]) < retry_interval:
                        continue

                    # Missing (never attempted, e.g. a service enabled after this
                    # contact was already forwarded) counts the same as an
                    # explicit False — both need a send.
                    failed = {name for name in SERVICE_LABELS if enabled.get(name) and not record.get(name)}
                    log.info(f"Retrying contact: {callsign} {band} {mode} (pending: {', '.join(sorted(failed))})")
                    record.update(send_to_services(cfg, adif, enabled, only=failed,
                                                   sender_timeout=sender_timeout))

                    if all(record.get(name) for name, on in enabled.items() if on):
                        record.pop("first_attempt", None)
                        record.pop("last_attempt", None)
                    elif now - _parse_iso(record["first_attempt"]) >= retry_give_up:
                        still = sorted(name for name in SERVICE_LABELS if enabled.get(name) and not record.get(name))
                        log.warning(f"Giving up on {callsign} {band} {mode} after {retry_give_up.days} day(s) — never reached: {', '.join(still)}")
                        record["gave_up"] = True
                    else:
                        record["last_attempt"] = _now_iso()

                    records[key] = record
                    save_state(state_file, records)

                pruned = prune_records(records, current_keys)
                if len(pruned) != len(records):
                    records = pruned
                    save_state(state_file, records)
            except Exception:
                log.exception("Unexpected error during poll cycle — will retry next cycle")

            # Abandoned sender threads normally unwedge once the OS times the
            # connection out. If they're piling up instead, they're holding
            # sockets we can't reclaim in-process, so exit and let the
            # scheduler (or --watchdog) start a clean one.
            stuck = _live_stuck_senders()
            if stuck >= STUCK_SENDER_LIMIT:
                log.error(f"{stuck} sender call(s) still stuck — restarting the bridge to clear them")
                restart_requested = True
                break

            if sleep_with_heartbeat(poll_interval, stop_event):
                break

        if stop_event is not None:
            log.info("Bridge stopped.")
    finally:
        _cleanup_runtime_files()
        # Tells a waiting Windows console handler it can stop holding the
        # process open on our behalf.
        _shutdown_complete.set()

    # Exiting non-zero is what makes the restart actually happen: Task
    # Scheduler / launchd / systemd all treat a failed exit as something to
    # relaunch. Only meaningful for the headless CLI — under the GUI, run() is
    # a worker thread, so the message in the log window is all we can offer.
    if restart_requested and stop_event is None:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Process detection — shared by the GUI (to show whether a bridge instance,
# GUI- or autostart-launched, is running) and --watchdog below. Lives here
# rather than in netlogger_gui.py so a headless watchdog check doesn't need
# a tkinter dependency.
# ---------------------------------------------------------------------------
def _read_bridge_pid() -> "int | None":
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_running(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102

        # A process that has exited stays openable for as long as anything
        # still holds a handle to it — and something always does here, since
        # the autostart wrapper (wscript.exe) launches the bridge and waits on
        # it. So "OpenProcess succeeded" is not the same as "still running",
        # which matters now that _kill_pid has to confirm a kill actually
        # took. Waiting zero milliseconds on the process object answers the
        # real question: it's signaled once the process is gone.
        handle = ctypes.windll.kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

        # SYNCHRONIZE can be refused where plain queries aren't (another
        # user's process, say). Fall back to the weaker "does it exist" test
        # rather than reporting a live process as gone.
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def get_running_bridge_pid() -> "int | None":
    """Return the PID of a running bridge process, or None if not running."""
    pid = _read_bridge_pid()
    if pid is not None and _pid_running(pid):
        return pid
    return None


def _process_image(pid: int) -> str:
    """Best-effort name of the executable behind `pid`, lowercased, or ""."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return Path(buf.value).name.lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    # /proc first on Linux: `ps -o comm=` reads /proc/<pid>/comm, which the
    # kernel truncates to 15 characters (TASK_COMM_LEN is 16 including the
    # NUL). The frozen Linux build is named "netlogger_bridge" — 16 characters
    # — so comm reports "netlogger_bridg" and no amount of matching on the
    # real name would ever hit. cmdline isn't truncated.
    if sys.platform.startswith("linux"):
        try:
            argv0 = Path("/proc") / str(pid) / "cmdline"
            first = argv0.read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            if first:
                return Path(first).name.lower()
        except OSError:
            pass

    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                             capture_output=True, text=True, timeout=10)
        return Path(out.stdout.strip()).name.lower()
    except (OSError, subprocess.SubprocessError):
        return ""


def _pid_looks_like_bridge(pid: int) -> bool:
    """
    Guard against PID reuse before killing anything. The PID file is only ever
    as trustworthy as the last process that wrote it — if a bridge died without
    running its cleanup, the OS is free to hand that number to something
    completely unrelated, and a stale heartbeat would then point the watchdog
    at an innocent process. Checking the image name doesn't make that
    impossible, but it does mean the victim has to be a Python interpreter or
    a frozen bridge build, not any process that happened to inherit the number.
    """
    image = _process_image(pid)
    if not image:
        return False  # Can't confirm it's ours, so leave it alone.
    # "netlogger_bridg" (not ...ge) so this still matches if _process_image
    # had to fall back to a 15-character-truncated Linux comm.
    return image.startswith("python") or image.startswith("netlogger_bridg")


def _kill_pid(pid: int) -> bool:
    """
    Terminate a hung bridge so the scheduler can start a healthy one, and
    report whether it's actually gone. The caller must not restart on a
    failed kill: the old process would still hold the state file, and the
    replacement would race it — precisely the situation this is unwinding.
    """
    if not _pid_looks_like_bridge(pid):
        log.error(f"Watchdog: PID {pid} doesn't look like a bridge process "
                  "(stale PID file?) — not killing it")
        return False
    try:
        if sys.platform == "win32":
            done = subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True)
            if done.returncode != 0:
                log.error(f"Watchdog: taskkill failed for PID {pid}: "
                          f"{done.stderr.decode(errors='replace').strip()}")
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError as e:
        log.error(f"Watchdog: could not terminate PID {pid}: {e}")

    # Trust the exit status for nothing — ask the OS whether it's really gone.
    # Even a successful SIGKILL isn't instantaneous.
    for _ in range(10):
        if not _pid_running(pid):
            return True
        time.sleep(0.5)

    log.error(f"Watchdog: PID {pid} is still alive after being killed")
    return False


def watchdog_check(config_path: str = "config.ini"):
    """Called via --watchdog, from a separate time-triggered scheduled task
    (see netlogger_gui.py's enable_autostart). Task Scheduler's own
    RestartOnFailure setting on the main NetLoggerBridge task is meant to
    relaunch the bridge on a crash, but was found in practice to silently
    never fire (confirmed against Task Scheduler's own event log after a
    real crash — no restart attempt was ever logged), so this polls
    independently on a reliable time trigger and nudges Task Scheduler to
    restart the main task if the bridge isn't actually running.

    "Running" originally meant nothing more than a live PID, which a hung
    bridge satisfies perfectly well — and one duly hung for the better part of
    an hour inside a TLS handshake while the watchdog kept finding it healthy
    and every contact logged in the meantime went nowhere. A bridge whose
    heartbeat has gone stale therefore counts as down too: it's killed first,
    since the scheduler won't start a second copy while the first is alive
    (and two copies sharing one state_file would race anyway)."""
    pid = get_running_bridge_pid()

    if pid is not None:
        stale_after = _watchdog_stale_seconds(config_path)
        age = heartbeat_age()
        if age is None or age < stale_after:
            return
        log.warning(f"Watchdog: bridge (PID {pid}) has made no progress in "
                    f"{int(age)}s — killing it and triggering restart")
        if not _kill_pid(pid):
            log.error("Watchdog: hung bridge is still running — not starting a second one")
            return
    else:
        log.warning("Watchdog: bridge is not running — triggering restart")

    if sys.platform == "win32":
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True)


def _watchdog_stale_seconds(config_path: str) -> float:
    """
    How long a heartbeat may stand still before the bridge counts as hung.

    Read with a bare ConfigParser rather than load_config, which `sys.exit`s
    on a missing file — a watchdog run must never be the thing that reports a
    bad config, and the default is a perfectly good answer.

    Floored at twice sender_timeout_seconds, because a single slow-but-legal
    send is the one stretch where nothing beats: the two options are
    independently editable, and a config with a long sender timeout and a
    short stale window would otherwise have the watchdog killing healthy
    bridges mid-upload.
    """
    stale = DEFAULT_HEARTBEAT_STALE_MINUTES * 60
    sender_timeout = DEFAULT_SENDER_TIMEOUT
    try:
        cfg = configparser.ConfigParser()
        cfg.read(config_path, encoding="utf-8")
        stale = max(1, cfg.getint("general", "heartbeat_stale_minutes",
                                  fallback=DEFAULT_HEARTBEAT_STALE_MINUTES)) * 60
        sender_timeout = max(1, cfg.getint("general", "sender_timeout_seconds",
                                           fallback=DEFAULT_SENDER_TIMEOUT))
    except (configparser.Error, OSError, ValueError):
        pass
    return max(stale, 2 * sender_timeout)


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

    if "--reset-state" in sys.argv:
        reset_state(config_file)
        sys.exit(0)

    if "--watchdog" in sys.argv:
        watchdog_check(config_file)
        sys.exit(0)

    # Must happen on the main thread, before run() blocks in the poll loop, so
    # an OS shutdown or a service stop unwinds through run()'s finally instead
    # of killing the process where it stands.
    install_shutdown_handlers()

    try:
        run(config_file)
    except KeyboardInterrupt:
        log.info("Stopped by user.")