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
from xml.sax.saxutils import escape as _xml_escape

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

# File used to store the byte offset between restarts
state_file = last_offset.txt

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

[hrd]
# Set enabled = true to forward contacts to Ham Radio Deluxe (HRD) Logbook
# In HRD: Tools > QSO Forwarding, enable N1MM source, set port to match below
enabled = false

# Hostname or IP of the machine running HRD Logbook
host = 127.0.0.1

# UDP port (must match HRD's QSO Forwarding N1MM port; default 12060)
port = 12060

# Your station callsign — included in the N1MM-compatible XML packet sent to HRD
my_call =

[log4om]
# Set enabled = true to forward contacts to Log4OM v2
# In Log4OM: Communicator > Inbound Connections > Add, type ADIF, port must match below
enabled = false

# Hostname or IP of the machine running Log4OM
host = 127.0.0.1

# UDP port (must match the Log4OM inbound ADIF connection port you configured)
port = 2237

[dxkeeper]
# Set enabled = true to forward contacts to DXLab Suite DXKeeper
# DXKeeper must be running; its TCP base port is set in DXKeeper > Config > Ports
enabled = false

# Hostname or IP of the machine running DXKeeper
host = 127.0.0.1

# TCP port (DXKeeper default: 52001, which is base port 52000 + 1)
port = 52001
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
# Ham Radio Deluxe (HRD)
# ---------------------------------------------------------------------------

# ADIF BAND → nominal lower-edge MHz string for N1MM ContactInfo XML <band>
_BAND_TO_MHZ = {
    "2190M": "0.137", "630M": "0.475", "160M": "1.8", "80M": "3.5",
    "60M": "5", "40M": "7", "30M": "10", "20M": "14", "17M": "18",
    "15M": "21", "12M": "24", "10M": "28", "6M": "50", "4M": "70",
    "2M": "144", "1.25M": "222", "70CM": "432", "33CM": "902",
    "23CM": "1240", "13CM": "2300",
}


def send_to_hrd(cfg: configparser.SectionProxy, adif: str) -> bool:
    """
    Send QSO to HRD Logbook via N1MM-compatible UDP XML ContactInfo packet.
    HRD listens for N1MM UDP broadcasts: Tools > QSO Forwarding > N1MM.
    Reference: https://n1mmwp.hamdocs.com/appendices/external-udp-broadcasts/
    """
    host    = cfg.get("host", "127.0.0.1")
    port    = cfg.getint("port", fallback=12060)
    my_call = cfg.get("my_call", "")

    call     = extract_field(adif, "CALL")
    band     = extract_field(adif, "BAND")
    mode     = extract_field(adif, "MODE")
    freq     = extract_field(adif, "FREQ")
    qso_date = extract_field(adif, "QSO_DATE")
    time_on  = extract_field(adif, "TIME_ON")
    rst_sent = extract_field(adif, "RST_SENT")
    rst_sent = rst_sent if rst_sent != "?" else "599"
    rst_rcvd = extract_field(adif, "RST_RCVD")
    rst_rcvd = rst_rcvd if rst_rcvd != "?" else "599"

    # Timestamp: YYYY-MM-DD HH:MM:SS
    if len(qso_date) == 8 and len(time_on) >= 6:
        ts = (f"{qso_date[:4]}-{qso_date[4:6]}-{qso_date[6:8]} "
              f"{time_on[:2]}:{time_on[2:4]}:{time_on[4:6]}")
    else:
        ts = ""

    # Band in MHz (N1MM <band> format)
    band_mhz = _BAND_TO_MHZ.get(band.upper(), "14")

    # Frequency in N1MM units (10 Hz resolution: MHz × 100 000)
    try:
        n1mm_freq = int(float(freq) * 100_000) if freq and freq != "?" else int(float(band_mhz) * 100_000)
    except ValueError:
        n1mm_freq = int(float(band_mhz) * 100_000)

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<contactinfo>'
        '<app>N1MM</app>'
        f'<timestamp>{_xml_escape(ts)}</timestamp>'
        f'<mycall>{_xml_escape(my_call)}</mycall>'
        f'<call>{_xml_escape(call)}</call>'
        f'<band>{_xml_escape(band_mhz)}</band>'
        f'<rxfreq>{n1mm_freq}</rxfreq>'
        f'<txfreq>{n1mm_freq}</txfreq>'
        f'<mode>{_xml_escape(mode)}</mode>'
        f'<snt>{_xml_escape(rst_sent)}</snt>'
        f'<rcv>{_xml_escape(rst_rcvd)}</rcv>'
        '<contestname></contestname>'
        '<contestnr>0</contestnr>'
        '<operator></operator>'
        '<sntnr>0</sntnr>'
        '<rcvnr>0</rcvnr>'
        '<gridsquare></gridsquare>'
        '<exchange1></exchange1>'
        '<section></section>'
        '<comment></comment>'
        '<qth></qth>'
        '<name></name>'
        '<power></power>'
        '<misctext></misctext>'
        '<zone>0</zone>'
        '<prec></prec>'
        '<ck>0</ck>'
        '<ismultiplier1>0</ismultiplier1>'
        '<ismultiplier2>0</ismultiplier2>'
        '<ismultiplier3>0</ismultiplier3>'
        '<points>1</points>'
        '<radionr>1</radionr>'
        '<IsOriginal>True</IsOriginal>'
        '<IsRunQSO>0</IsRunQSO>'
        '<IsClaimedQso>1</IsClaimedQso>'
        '</contactinfo>'
    )

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(xml.encode("utf-8"), (host, port))
        return True
    except (socket.error, OSError) as e:
        log.error(f"HRD UDP error ({host}:{port}): {e}")
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
      <command:11>externallog<parameters:N><ExternalLogADIF:M>[adif fields]
    Reference: https://www.dxlabsuite.com/Interoperation.htm
    """
    # DXKeeper expects ADIF fields without the trailing <EOR>
    adif_fields = adif
    if adif_fields.upper().endswith("<EOR>"):
        adif_fields = adif_fields[:-5].rstrip()

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

def run(config_path: str = "config.ini", stop_event=None):
    """
    Run the poll loop. If stop_event (a threading.Event) is given, the loop
    exits once it's set instead of running forever — used by the GUI to
    start/stop the bridge in a background thread.
    """
    cfg = load_config(config_path)
    general = cfg["general"]

    poll_interval  = general.getint("poll_interval", fallback=10)
    state_file     = str(resolve_path(general.get("state_file", "last_offset.txt")))
    adi_path       = find_adi_file(general.get("contacts_adi", ""))

    wavelog_enabled  = cfg.getboolean("wavelog",  "enabled", fallback=False)
    n3fjp_enabled    = cfg.getboolean("n3fjp",    "enabled", fallback=False)
    hrd_enabled      = cfg.getboolean("hrd",      "enabled", fallback=False)
    log4om_enabled   = cfg.getboolean("log4om",   "enabled", fallback=False)
    dxkeeper_enabled = cfg.getboolean("dxkeeper", "enabled", fallback=False)

    if not any([wavelog_enabled, n3fjp_enabled, hrd_enabled, log4om_enabled, dxkeeper_enabled]):
        log.error("No outputs enabled. Set enabled = true in at least one output section.")
        sys.exit(1)

    log.info(f"NetLogger Bridge starting — polling every {poll_interval}s")
    log.info(f"File     : {adi_path}")
    log.info(f"WaveLog  : {'enabled' if wavelog_enabled else 'disabled'}")
    log.info(f"N3FJP    : {'enabled' if n3fjp_enabled else 'disabled'}")
    log.info(f"HRD      : {'enabled' if hrd_enabled else 'disabled'}")
    log.info(f"Log4OM   : {'enabled' if log4om_enabled else 'disabled'}")
    log.info(f"DXKeeper : {'enabled' if dxkeeper_enabled else 'disabled'}")

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

    try:
        PID_FILE.write_text(str(os.getpid()))

        while stop_event is None or not stop_event.is_set():
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
                    log.info(f"  WaveLog  : {'OK' if ok else 'FAILED'}")

                if n3fjp_enabled:
                    host = cfg["n3fjp"].get("host", "127.0.0.1")
                    port = cfg["n3fjp"].getint("port", fallback=1100)
                    ok   = send_to_n3fjp(host, port, adif)
                    log.info(f"  N3FJP    : {'OK' if ok else 'FAILED'}")

                if hrd_enabled:
                    ok = send_to_hrd(cfg["hrd"], adif)
                    log.info(f"  HRD      : {'OK' if ok else 'FAILED'}")

                if log4om_enabled:
                    host = cfg["log4om"].get("host", "127.0.0.1")
                    port = cfg["log4om"].getint("port", fallback=2237)
                    ok   = send_to_log4om(host, port, adif)
                    log.info(f"  Log4OM   : {'OK' if ok else 'FAILED'}")

                if dxkeeper_enabled:
                    host = cfg["dxkeeper"].get("host", "127.0.0.1")
                    port = cfg["dxkeeper"].getint("port", fallback=52001)
                    ok   = send_to_dxkeeper(host, port, adif)
                    log.info(f"  DXKeeper : {'OK' if ok else 'FAILED'}")

                save_offset(state_file, offset)

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

    try:
        run(config_file)
    except KeyboardInterrupt:
        log.info("Stopped by user.")