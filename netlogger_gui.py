#!/usr/bin/env python3
"""
NetLogger Bridge GUI

Tkinter front-end for editing config.ini and starting/stopping
netlogger_bridge's poll loop, with a live log view.

Cross-platform: Windows, macOS, Linux (requires Tk, included with most
Python installs; on some Linux distros install the 'python3-tk' package).
"""

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

import netlogger_bridge as bridge

CONFIG_PATH = "config.ini"
CONFIG_ABS_PATH = bridge.resolve_path(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Autostart (Task Scheduler / launchd / systemd) — runs the headless CLI
# bridge in the background at login, pointed at this GUI's config.ini.
# ---------------------------------------------------------------------------
TASK_NAME = "NetLoggerBridge"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.netloggerbridge.bridge.plist"
PLIST_LABEL = "com.netloggerbridge.bridge"
UNIT_NAME = "netlogger-bridge.service"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
WRAPPER_PATH = bridge.APP_DIR / "netlogger_bridge_autostart.vbs"
TASK_XML_PATH = bridge.APP_DIR / "netlogger_bridge_task.xml"


def _cli_command() -> list[str]:
    if getattr(sys, "frozen", False):
        exe_name = "netlogger_bridge.exe" if sys.platform == "win32" else "netlogger_bridge"
        return [str(bridge.APP_DIR / exe_name), str(CONFIG_ABS_PATH)]
    return [sys.executable, str(bridge.APP_DIR / "netlogger_bridge.py"), str(CONFIG_ABS_PATH)]


def is_autostart_enabled() -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["schtasks", "/query", "/tn", TASK_NAME],
                capture_output=True,
            )
            return result.returncode == 0
        if sys.platform == "darwin":
            return PLIST_PATH.exists()
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", UNIT_NAME],
            capture_output=True,
        )
        return result.returncode == 0
    except OSError:
        return False


def _run_elevated(args: list[str]) -> int:
    """Run `schtasks <args>` elevated via a UAC prompt. Returns the exit code."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "schtasks"
    sei.lpParameters = subprocess.list2cmdline(args)
    sei.nShow = SW_HIDE

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.GetLastError()
        if err == 1223:  # ERROR_CANCELLED
            raise OSError("Administrator approval was declined.")
        raise OSError(f"Elevation request failed (error {err}).")

    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 30000)
    exit_code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return exit_code.value


def _run_schtasks(args: list[str]):
    """Run schtasks, escalating to a UAC prompt if access is denied."""
    result = subprocess.run(["schtasks", *args], capture_output=True, text=True)
    if result.returncode == 0:
        return
    if "denied" not in result.stderr.lower():
        raise OSError(result.stderr.strip() or f"schtasks exited with {result.returncode}")
    exit_code = _run_elevated(args)
    if exit_code != 0:
        raise OSError(f"schtasks (elevated) exited with code {exit_code}")


def enable_autostart():
    cmd = _cli_command()
    if sys.platform == "win32":
        # schtasks' /tr value is limited to 261 characters, which the full
        # python.exe + script + config paths can easily exceed, so the real
        # command lives in a short VBScript wrapper instead. The wrapper runs
        # it hidden (window style 0) and *waits* for it (bWaitOnReturn=True),
        # propagating its exit code via WScript.Quit — this keeps the task
        # "running" for as long as the bridge is alive, so Task Scheduler can
        # detect a crash/kill and apply RestartOnFailure below.
        cmd_line = " ".join(f'"{c}"' for c in cmd)
        vbs_cmd = cmd_line.replace('"', '""')
        vbs = (
            f'exitCode = CreateObject("WScript.Shell").Run("{vbs_cmd}", 0, True)\n'
            f'WScript.Quit(exitCode)\n'
        )
        WRAPPER_PATH.write_text(vbs, encoding="utf-8")

        task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"{WRAPPER_PATH}"</Arguments>
    </Exec>
  </Actions>
</Task>
"""
        TASK_XML_PATH.write_text(task_xml, encoding="utf-16")
        _run_schtasks(["/create", "/tn", TASK_NAME, "/xml", str(TASK_XML_PATH), "/f"])
    elif sys.platform == "darwin":
        args_xml = "\n".join(f"        <string>{c}</string>" for c in cmd)
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLIST_PATH.write_text(plist, encoding="utf-8")
        subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)], check=True)
    else:
        exec_start = " ".join(f'"{c}"' for c in cmd)
        unit = f"""[Unit]
Description=NetLogger Bridge

[Service]
ExecStart={exec_start}
Restart=always

[Install]
WantedBy=default.target
"""
        UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)


def start_bridge_now():
    """Launch the registered autostart task/service immediately, instead of
    waiting for the next login. macOS (RunAtLoad) and Linux (enable --now)
    already start on registration; only Windows Task Scheduler needs this
    extra nudge since its LogonTrigger otherwise only fires at next logon."""
    if sys.platform == "win32":
        _run_schtasks(["/run", "/tn", TASK_NAME])


def disable_autostart():
    if sys.platform == "win32":
        _run_schtasks(["/delete", "/tn", TASK_NAME, "/f"])
        WRAPPER_PATH.unlink(missing_ok=True)
        TASK_XML_PATH.unlink(missing_ok=True)
    elif sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", "-w", str(PLIST_PATH)], check=False)
        PLIST_PATH.unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
        UNIT_PATH.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


# ---------------------------------------------------------------------------
# Bridge process detection — works for any instance (GUI-launched or
# autostart-launched) via the PID file the bridge writes on startup.
# ---------------------------------------------------------------------------
def _read_bridge_pid() -> int | None:
    try:
        return int(bridge.PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_running(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
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


def get_running_bridge_pid() -> int | None:
    """Return the PID of a running bridge process, or None if not running."""
    pid = _read_bridge_pid()
    if pid is not None and _pid_running(pid):
        return pid
    return None


class QueueHandler(logging.Handler):
    """Logging handler that pushes formatted records onto a queue for the GUI thread."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NetLogger Bridge")
        self.geometry("640x560")

        self.cfg = bridge.load_config_for_gui(CONFIG_ABS_PATH)
        self.stop_event = None
        self.worker = None
        self.log_queue = queue.Queue()
        self.vars = {}

        self._build_widgets()
        self._load_values()

        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll_log_queue)
        self.after(0, self._poll_process_status)

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------
    def _build_widgets(self):
        general = ttk.LabelFrame(self, text="General")
        general.pack(fill="x", padx=10, pady=5)
        self._add_entry(general, "poll_interval", "Poll interval (seconds)")
        self._add_entry(general, "contacts_adi", "Contacts.adi path (blank = auto-detect)")
        self._add_entry(general, "state_file", "State file")

        wavelog = ttk.LabelFrame(self, text="WaveLog")
        wavelog.pack(fill="x", padx=10, pady=5)
        self._add_checkbox(wavelog, "wavelog_enabled", "Enable WaveLog")
        self._add_entry(wavelog, "wavelog_url", "WaveLog URL")
        self._add_entry(wavelog, "wavelog_api_key", "API key")
        self._add_entry(wavelog, "wavelog_station_id", "Station ID")

        n3fjp = ttk.LabelFrame(self, text="N3FJP AC Log")
        n3fjp.pack(fill="x", padx=10, pady=5)
        self._add_checkbox(n3fjp, "n3fjp_enabled", "Enable N3FJP")
        self._add_entry(n3fjp, "n3fjp_host", "Host")
        self._add_entry(n3fjp, "n3fjp_port", "Port")

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10, pady=5)
        ttk.Button(buttons, text="Save Config", command=self._save_config).pack(side="left")
        self.start_button = ttk.Button(buttons, text="Start", command=self._toggle_run)
        self.start_button.pack(side="left", padx=5)
        self.status_label = ttk.Label(buttons, text="Stopped")
        self.status_label.pack(side="left", padx=10)

        self.process_label = ttk.Label(buttons, text="Bridge process: checking...")
        self.process_label.pack(side="left", padx=10)

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        ttk.Checkbutton(
            buttons,
            text="Run automatically at login (background)",
            variable=self.autostart_var,
            command=self._toggle_autostart,
        ).pack(side="left", padx=10)

        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, state="disabled", height=16)
        self.log_text.pack(fill="both", expand=True)

    def _add_entry(self, parent, key, label):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=5, pady=2)
        ttk.Label(row, text=label, width=32).pack(side="left")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        self.vars[key] = var

    def _add_checkbox(self, parent, key, label):
        var = tk.BooleanVar()
        ttk.Checkbutton(parent, text=label, variable=var).pack(anchor="w", padx=5, pady=2)
        self.vars[key] = var

    # ------------------------------------------------------------------
    # Config <-> widgets
    # ------------------------------------------------------------------
    def _load_values(self):
        general = self.cfg["general"]
        self.vars["poll_interval"].set(general.get("poll_interval", "10"))
        self.vars["contacts_adi"].set(general.get("contacts_adi", ""))
        self.vars["state_file"].set(general.get("state_file", "last_offset.txt"))

        wavelog = self.cfg["wavelog"]
        self.vars["wavelog_enabled"].set(wavelog.getboolean("enabled", fallback=False))
        self.vars["wavelog_url"].set(wavelog.get("url", ""))
        self.vars["wavelog_api_key"].set(wavelog.get("api_key", ""))
        self.vars["wavelog_station_id"].set(wavelog.get("station_id", "1"))

        n3fjp = self.cfg["n3fjp"]
        self.vars["n3fjp_enabled"].set(n3fjp.getboolean("enabled", fallback=False))
        self.vars["n3fjp_host"].set(n3fjp.get("host", "127.0.0.1"))
        self.vars["n3fjp_port"].set(n3fjp.get("port", "1100"))

    def _save_config(self):
        general = self.cfg["general"]
        general["poll_interval"] = self.vars["poll_interval"].get()
        general["contacts_adi"] = self.vars["contacts_adi"].get()
        general["state_file"] = self.vars["state_file"].get()

        wavelog = self.cfg["wavelog"]
        wavelog["enabled"] = "true" if self.vars["wavelog_enabled"].get() else "false"
        wavelog["url"] = self.vars["wavelog_url"].get()
        wavelog["api_key"] = self.vars["wavelog_api_key"].get()
        wavelog["station_id"] = self.vars["wavelog_station_id"].get()

        n3fjp = self.cfg["n3fjp"]
        n3fjp["enabled"] = "true" if self.vars["n3fjp_enabled"].get() else "false"
        n3fjp["host"] = self.vars["n3fjp_host"].get()
        n3fjp["port"] = self.vars["n3fjp_port"].get()

        with open(CONFIG_ABS_PATH, "w", encoding="utf-8") as f:
            self.cfg.write(f)

    # ------------------------------------------------------------------
    # Bridge control
    # ------------------------------------------------------------------
    def _toggle_run(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.start_button.config(state="disabled")
            self.status_label.config(text="Stopping...")
        else:
            if not self.vars["wavelog_enabled"].get() and not self.vars["n3fjp_enabled"].get():
                messagebox.showerror("NetLogger Bridge", "Enable WaveLog and/or N3FJP first.")
                return

            pid = get_running_bridge_pid()
            if pid is not None:
                if not messagebox.askyesno(
                    "NetLogger Bridge",
                    f"The bridge already appears to be running (PID {pid}), "
                    "possibly started automatically at login. Running a second "
                    "instance can cause duplicate or conflicting uploads.\n\n"
                    "Start another instance anyway?",
                ):
                    return

            self._save_config()
            self.stop_event = threading.Event()
            self.worker = threading.Thread(
                target=bridge.run, args=(CONFIG_ABS_PATH, self.stop_event), daemon=True
            )
            self.worker.start()
            self.start_button.config(text="Stop")
            self.status_label.config(text="Running")
            self.after(500, self._watch_worker)

    def _toggle_autostart(self):
        try:
            if self.autostart_var.get():
                self._save_config()
                enable_autostart()
                if get_running_bridge_pid() is None:
                    start_bridge_now()
            else:
                disable_autostart()
        except (OSError, subprocess.CalledProcessError) as e:
            self.autostart_var.set(not self.autostart_var.get())
            messagebox.showerror("NetLogger Bridge", f"Could not update autostart: {e}")

    def _watch_worker(self):
        if self.worker and self.worker.is_alive():
            self.after(500, self._watch_worker)
        else:
            self.start_button.config(text="Start", state="normal")
            self.status_label.config(text="Stopped")

    def _poll_process_status(self):
        pid = get_running_bridge_pid()
        if pid is not None:
            self.process_label.config(text=f"Bridge process: running (PID {pid})")
        else:
            self.process_label.config(text="Bridge process: not running")
        self.after(2000, self._poll_process_status)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.after(200, self._poll_log_queue)

    def _on_close(self):
        if self.stop_event:
            self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
