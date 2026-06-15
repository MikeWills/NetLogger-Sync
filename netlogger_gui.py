#!/usr/bin/env python3
"""
NetLogger Bridge GUI

Tkinter front-end for editing config.ini and starting/stopping
netlogger_bridge's poll loop, with a live log view.

Cross-platform: Windows, macOS, Linux (requires Tk, included with most
Python installs; on some Linux distros install the 'python3-tk' package).
"""

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import netlogger_bridge as bridge

CONFIG_PATH = "config.ini"


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

        self.cfg = bridge.load_config_for_gui(CONFIG_PATH)
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

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
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

            self._save_config()
            self.stop_event = threading.Event()
            self.worker = threading.Thread(
                target=bridge.run, args=(CONFIG_PATH, self.stop_event), daemon=True
            )
            self.worker.start()
            self.start_button.config(text="Stop")
            self.status_label.config(text="Running")
            self.after(500, self._watch_worker)

    def _watch_worker(self):
        if self.worker and self.worker.is_alive():
            self.after(500, self._watch_worker)
        else:
            self.start_button.config(text="Start", state="normal")
            self.status_label.config(text="Stopped")

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
