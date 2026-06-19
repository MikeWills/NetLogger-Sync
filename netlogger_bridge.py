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


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(resolve_path("netlogger_bridge.log"), encoding="utf-8"),
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


SAMPLE_CONFIG = """\
[general]
# Seconds between file polls
poll_interval = 10

# Path to NetLogger's Contacts.adi file.
# Leave blank to auto-detect from default OS locations.
# Windows default: %APPDATA%\\NetLogger\\Contacts.adi
# macOS default:   ~/Library/Application Support/NetLogger/Contacts.adi
# Linux default:   ~/.config/NetLogger/Contacts.adi
contacts_adi =

# File used to track which contacts have already been forwarded, between restarts
state_file = forwarded_qsos.txt

[wavelog]
# Set enabled = true to forward contacts to WaveLog
enabled = false

# Base URL of your WaveLog instance, including index.php (no trailing slash)
url = https://log.example.com/index.php

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
        if resp.status_code in (200, 201):
            data = resp.json()
            if data.get("status") == "created" and data.get("adif_count", 0) > 0:
                return True
            log.warning(f"WaveLog did not import the record: {data}")
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
# Output dispatch (used for both first attempts and per-service retries)
# ---------------------------------------------------------------------------

SERVICE_LABELS = {
    "wavelog":     "WaveLog",
    "n3fjp":       "N3FJP",
    "n1mm":        "N1MM",
    "hrd":         "HRD",
    "log4om":      "Log4OM",
    "dxkeeper":    "DXKeeper",
    "macloggerdx": "MacLoggerDX",
}


def send_to_services(cfg: configparser.ConfigParser, adif: str, enabled: dict, only: set = None) -> dict:
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
    }
    results = {}
    for name, sender in senders.items():
        if not enabled.get(name) or (only is not None and name not in only):
            continue
        ok = results[name] = sender()
        log.info(f"  {SERVICE_LABELS[name]:<9}: {'OK' if ok else 'FAILED'}")
    return results


# ---------------------------------------------------------------------------
# State persistence (per-contact, per-service forwarding status)
# ---------------------------------------------------------------------------

RETRY_INTERVAL       = datetime.timedelta(hours=1)
RETRY_GIVE_UP_AFTER  = datetime.timedelta(days=5)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime.datetime:
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def _is_done(record: dict) -> bool:
    """A record needs no more attention once every service it was attempted against succeeded, or retries were abandoned."""
    if record.get("gave_up"):
        return True
    return all(v for k, v in record.items() if k in SERVICE_LABELS)


def load_state(state_file: str) -> dict:
    """
    Returns {"initialized": bool, "records": {dedup_key: record}}.
    Each record is a dict of {service_name: success_bool} for whichever
    services were attempted, plus "first_attempt"/"last_attempt" (ISO UTC)
    once a retry is pending, and "gave_up": True once retries are abandoned
    after RETRY_GIVE_UP_AFTER. A fully-successful record has no extra keys.

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
    version's "keys" are extracted the same way.
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
    lines = [json.dumps({"key": key, **records[key]}) for key in sorted(records)]
    Path(state_file).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


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

    enabled = {
        "wavelog":     cfg.getboolean("wavelog",     "enabled", fallback=False),
        "n3fjp":       cfg.getboolean("n3fjp",       "enabled", fallback=False),
        "n1mm":        cfg.getboolean("n1mm",        "enabled", fallback=False),
        "hrd":         cfg.getboolean("hrd",         "enabled", fallback=False),
        "log4om":      cfg.getboolean("log4om",      "enabled", fallback=False),
        "dxkeeper":    cfg.getboolean("dxkeeper",    "enabled", fallback=False),
        "macloggerdx": cfg.getboolean("macloggerdx", "enabled", fallback=False),
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

    try:
        PID_FILE.write_text(str(os.getpid()))

        while stop_event is None or not stop_event.is_set():
            current_keys = set()
            now = datetime.datetime.now(datetime.timezone.utc)

            for raw in read_all_records(adi_path):
                adif = normalize_adif(raw)
                key  = record_dedup_key(adif)
                current_keys.add(key)

                record = records.get(key)
                if record is not None and _is_done(record):
                    continue

                callsign = extract_field(adif, "Call")
                band     = extract_field(adif, "Band")
                mode     = extract_field(adif, "Mode")

                if record is None:
                    log.info(f"New contact: {callsign} {band} {mode}")
                    log.debug(f"ADIF: {adif}")
                    results = send_to_services(cfg, adif, enabled)
                    if all(results.values()):
                        records[key] = results
                    else:
                        ts = _now_iso()
                        records[key] = {**results, "first_attempt": ts, "last_attempt": ts}
                    save_state(state_file, records)
                    continue

                # Previously attempted but incomplete — retry hourly, give up after 5 days
                if now - _parse_iso(record["last_attempt"]) < RETRY_INTERVAL:
                    continue

                failed = {name for name in SERVICE_LABELS if record.get(name) is False}
                log.info(f"Retrying contact: {callsign} {band} {mode} (pending: {', '.join(sorted(failed))})")
                record.update(send_to_services(cfg, adif, enabled, only=failed))

                if all(record.get(name, True) for name in SERVICE_LABELS):
                    record.pop("first_attempt", None)
                    record.pop("last_attempt", None)
                elif now - _parse_iso(record["first_attempt"]) >= RETRY_GIVE_UP_AFTER:
                    still = sorted(name for name in SERVICE_LABELS if record.get(name) is False)
                    log.warning(f"Giving up on {callsign} {band} {mode} after 5 days — never reached: {', '.join(still)}")
                    record["gave_up"] = True
                else:
                    record["last_attempt"] = _now_iso()

                records[key] = record
                save_state(state_file, records)

            pruned = prune_records(records, current_keys)
            if len(pruned) != len(records):
                records = pruned
                save_state(state_file, records)

            if stop_event is not None:
                if stop_event.wait(poll_interval):
                    break
            else:
                time.sleep(poll_interval)

        if stop_event is not None:
            log.info("Bridge stopped.")
    finally:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


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

    try:
        run(config_file)
    except KeyboardInterrupt:
        log.info("Stopped by user.")