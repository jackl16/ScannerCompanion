"""
Standalone barcode scanner app -- COM (serial) port mode.

Run this on the shared center computer. Each USB barcode scanner, set to
COM/serial mode, gets read on its own dedicated background thread -- no
dependency on which window has keyboard focus, and no risk of two
scanners' input interleaving (each has its own separate connection).

Run:
    python scanner_app.py
"""

import requests
from tkinter import messagebox

CURRENT_VERSION = "1.0.4"
MANIFEST_URL = "https://raw.githubusercontent.com/jackl16/ScannerCompanion/refs/heads/main/version.json"
NORMAL_SIZE = "680x640"

import math
import struct
import tempfile
import wave
import csv
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from datetime import datetime
import os           
import requests
import subprocess
import sys
from pathlib import Path
import customtkinter as ctk
from ui_kit import (COLORS, FONT, MONO, font, TopBar, StatusText,
                    LastScanCard, ScanLog, StatusPanel, ActionButton, MenuPanel,
                    messagebox,simpledialog, ListPicker, set_log_style)

try:
    import winsound
except ImportError:
    winsound = None

import serial
import serial.tools.list_ports

from scanner_db import ScannerDB, BASE_DIR
from features import check_feature_code

import hashlib
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

UPDATE_PUBLIC_KEY = "paste the PUBLIC KEY hex here"

def _update_is_authentic(version, sha_hex, sig_hex) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(UPDATE_PUBLIC_KEY)).verify(
            bytes.fromhex(sig_hex), f"{version}:{sha_hex}".encode())
        return True
    except Exception:
        return False

def _ver(v) -> tuple:
    """'1.0.10' -> (1, 0, 10), so versions compare numerically."""
    try:
        return tuple(int(p) for p in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)

def resource_path(name: str) -> Path:
    """Where a bundled data file lives: PyInstaller's extraction folder
    when frozen, otherwise next to this script."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name

def open_path(path) -> None:
    """Open a file or folder with the OS default app."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])

SAMPLE_RATE = 44100

def _render_notes(notes, volume=0.35):
    """notes: list of (freq_hz, start_sec, length_sec). Returns 16-bit PCM bytes.
    Each note is a sine plus soft overtones with a fast attack and exponential
    decay, which gives a bell/chime feel instead of a harsh beep."""
    total = max(start + length for _, start, length in notes) + 0.05
    buf = [0.0] * int(SAMPLE_RATE * total)
    for freq, start, length in notes:
        offset = int(start * SAMPLE_RATE)
        for i in range(int(length * SAMPLE_RATE)):
            t = i / SAMPLE_RATE
            env = min(1.0, t / 0.005) * math.exp(-5.0 * t / length)
            w = (math.sin(2 * math.pi * freq * t)
                 + 0.30 * math.sin(4 * math.pi * freq * t)
                 + 0.10 * math.sin(6 * math.pi * freq * t)) / 1.4
            buf[offset + i] += volume * env * w
    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in buf)

def build_feedback_sounds() -> dict:
    """Generates the success/error sounds once, as WAV files in a temp folder."""
    sounds = {
        "success": [(1047, 0.00, 0.16), (1568, 0.08, 0.28)],  # C6 -> G6, rising
        "error":   [(330,  0.00, 0.18), (262,  0.12, 0.30)],  # E4 -> C4, falling
    }
    folder = Path(tempfile.mkdtemp(prefix="scanner_sounds_"))
    paths = {}
    for name, notes in sounds.items():
        path = folder / f"{name}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(_render_notes(notes))
        paths[name] = str(path)
    return paths


def list_scanner_ports():
    """Serial ports worth probing as barcode scanners.

    Windows/Linux: everything pyserial reports, same as before.
    macOS: only the /dev/cu.* callout devices, minus the built-in
    Bluetooth and debug entries. macOS lists each device twice
    (tty.* and cu.*), and the tty.* version can block on open.
    """
    ports = serial.tools.list_ports.comports()
    if sys.platform == "darwin":
        ports = [
            p for p in ports
            if p.device.startswith("/dev/cu.")
            and "bluetooth" not in p.device.lower()
            and "debug-console" not in p.device.lower()
        ]
    return sorted(ports, key=lambda p: p.device)        


# Minimum time between two accepted reads on the *same physical port*.
# Cheap USB barcode scanners occasionally double-fire on a single swipe
# (a bit of movement, a bounce on the trigger, etc.), which reads as two
# separate lines a few milliseconds apart. 
MIN_SCAN_INTERVAL_SECONDS = 1.0

# COLORS = {
#     "bg": "#121214",          # app background (soft slate)
#     "card": "#1a1a1e",         # panels/cards
#     "border": "#27272a",  
#     "text": "#f4f4f5", 
#     "text_muted": "#a1a1aa",
#     "primary": "#4f46e5",  
#     "primary_dark": "#4338ca",
#     "primary_light": "#DBEAFE",
#     "on_primary": "#FFFFFF",
#     "success": "#10b981",  
#     "error": "#ef4444",    
#     "warning": "#f59e0b",
#     "log_text": "#39ff14",     # matrix-green scan log text
# }


class SerialReaderThread(threading.Thread):
    """Reads one scanner's COM port in the background and pushes each
    scanned line onto a shared queue for the main thread to process."""

    def __init__(self, port: str, baud_rate: int, output_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.port = port
        self.baud_rate = baud_rate
        self.output_queue = output_queue
        self.stop_event = stop_event
        self._last_read_time = 0.0  # monotonic clock -- for the per-port rate limit
    

    def run(self):
        while not self.stop_event.is_set():
            try:
                with serial.Serial(self.port, self.baud_rate, timeout=1) as ser:
                    self.output_queue.put(("status", self.port, "connected"))
                    while not self.stop_event.is_set():
                        line = ser.readline()
                        if line:
                            decoded = line.decode(errors="ignore").strip()
                            if decoded:
                                now = time.monotonic()
                                if now - self._last_read_time < MIN_SCAN_INTERVAL_SECONDS:
                                    # almost certainly a physical double-read on this
                                    # exact scanner (jitter/bounce), not a second,
                                    # genuine scan -- silently drop it
                                    continue
                                self._last_read_time = now
                                self.output_queue.put(("scan", self.port, decoded))
            except serial.SerialException as e:
                self.output_queue.put(("status", self.port, f"error: {e}"))
                # port not available yet (e.g. scanner unplugged) -- wait and retry
                self.stop_event.wait(3)


class PortProbeThread(threading.Thread):
    """Like SerialReaderThread, but for the one-time detection screen:
    listens on a single candidate port and reports the first line it
    reads back to the main thread, then stops itself. Used to figure out
    *which* port a scanner is plugged into, without knowing in advance."""

    def __init__(self, port: str, baud_rate: int, output_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.port = port
        self.baud_rate = baud_rate
        self.output_queue = output_queue
        self.stop_event = stop_event

    def run(self):
        try:
            with serial.Serial(self.port, self.baud_rate, timeout=1) as ser:
                self.output_queue.put(("probe_status", self.port, "listening"))
                while not self.stop_event.is_set():
                    line = ser.readline()
                    if line:
                        decoded = line.decode(errors="ignore").strip()
                        if decoded:
                            self.output_queue.put(("probe_hit", self.port, decoded))
                            return
        except serial.SerialException as e:
            self.output_queue.put(("probe_status", self.port, f"error: {e}"))


class ScannerApp(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=COLORS["bg"])
        self.title("Scanner Companion")
        self.geometry(NORMAL_SIZE)
        self.resizable(False, False)

        self._setup_style()


        try:
            self.db = ScannerDB()
        except FileNotFoundError as e:
            messagebox.showerror("Missing config", str(e))
            self.destroy()
            return

        # loaded from scanner_config.toml, so the toggle survives restarts
        # instead of silently resetting to off every launch
        self.sound_enabled = tk.BooleanVar(value=self.db.sound_enabled)
        try:
            self._sound_files = build_feedback_sounds()
        except Exception:
            self._sound_files = {}  # sound just stays silent if generation fails

        self.log_pills_var = tk.BooleanVar(value=self.db.log_pills)
        self.log_terminal_var = tk.BooleanVar(value=self.db.log_terminal)
        set_log_style(self.db.log_pills, self.db.log_terminal)

        self.photo_mode_var = tk.BooleanVar(value=self.db.photo_mode)
        self._photo_toggle_added = False
        self._photo_busy = False
        self.photo_worker = None


        self.scan_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.reader_threads = []
        self._poll_job = None
        self._flush_job = None

        # separate queue/stop-event for the detection screen, so probing
        # never gets tangled up with the live scanner listeners
        self.probe_queue = queue.Queue()
        self.probe_stop_event = threading.Event()
        self.probe_threads = []
        self.probe_port_vars = {}
        self.confirmed_scanner_ports = []

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.db.try_remembered_login():
            self._show_scanner_screen()
        else:
            self._show_login_screen()

        self.after(2000, lambda: self.check_for_updates(silent=True))
        self.after(250, self._load_custom_icon)
    # --- VISUAL STYLE -----------------------------------------------------
    def _load_custom_icon(self):
        try:
            self.iconbitmap(str(resource_path("app_icon.ico")))
        except Exception:
            pass  # missing or unsupported on this OS: keep the default

    def _setup_style(self):
        """One place for the whole app's look. 'clam' is the only built-in
        ttk theme that reliably respects custom colors on Windows -- the
        native theme ignores most of this."""
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["card"])
        style.configure("Header.TFrame", background=COLORS["primary"])

        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(FONT, 10))
        style.configure("Card.TLabel", background=COLORS["card"], foreground=COLORS["text"], font=(FONT, 10))
        style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["text_muted"], font=(FONT, 9))
        style.configure("CardMuted.TLabel", background=COLORS["card"], foreground=COLORS["text_muted"], font=(FONT, 9))
        style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(FONT, 14, "bold"))
        style.configure("CardTitle.TLabel", background=COLORS["card"], foreground=COLORS["text"], font=(FONT, 13, "bold"))
        style.configure("Header.TLabel", background=COLORS["primary"], foreground=COLORS["on_primary"], font=(FONT, 15, "bold"))

        style.configure(
            "TButton", background=COLORS["primary"], foreground=COLORS["on_primary"],
            font=(FONT, 10, "bold"), padding=(14, 9), borderwidth=0, focuscolor=COLORS["bg"],
        )
        style.map(
            "TButton",
            background=[("active", COLORS["primary_dark"]), ("disabled", COLORS["border"])],
            foreground=[("disabled", COLORS["text_muted"])],
        )
        style.configure(
            "Secondary.TButton", background=COLORS["card"], foreground=COLORS["primary"],
            font=(FONT, 10, "bold"), padding=(14, 9), borderwidth=1, focuscolor=COLORS["bg"],
        )
        style.map(
            "Secondary.TButton",
            background=[("active", COLORS["primary_light"])],
            bordercolor=[("!disabled", COLORS["primary"])],
        )

        style.configure("TCheckbutton", background=COLORS["card"], foreground=COLORS["text"], font=(FONT, 10))
        style.map("TCheckbutton", background=[("active", COLORS["card"])])

        style.configure(
            "TEntry", fieldbackground=COLORS["card"], bordercolor=COLORS["border"],
            lightcolor=COLORS["border"], darkcolor=COLORS["border"], padding=8, relief="flat",
        )

        style.configure("TSeparator", background=COLORS["border"])

        style.configure(
            "DarkEntry.TEntry", fieldbackground=COLORS["card"], foreground="#ffffff",
            insertcolor="#ffffff", bordercolor=COLORS["border"],
            lightcolor=COLORS["border"], darkcolor=COLORS["border"],
        )

        scale = max(self.winfo_fpixels("1i") / 96, 1.0)
        style.configure("Treeview", background=COLORS["card"], fieldbackground=COLORS["card"],
                        foreground=COLORS["text"], borderwidth=0, relief="flat",
                        rowheight=int(32 * scale), font=(FONT, 11))
        style.map("Treeview", background=[("selected", COLORS["primary"])],
                  foreground=[("selected", COLORS["on_primary"])])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no border

    def _make_menu(self):
        return MenuPanel(self)

    def _header(self, text: str):
        self.topbar = TopBar(self, text, CURRENT_VERSION)
        self.topbar.pack(fill="x")
        return self.topbar

    def _clear_menu_bar(self):
        pass  # no native menu bar any more; menus live in the top bar

    def _build_menu_bar(self, with_status=True):
        """Top-bar menus. with_status=False is used on the Detect Scanner screen, where
        there are no live scanner connections or scan log to act on."""
        account_menu = self._make_menu()
        if with_status:
            account_menu.add_command(label="Export Scan Log", command=self._export_scan_log)
            account_menu.add_command(label="Clear Scan Log", command=self._clear_scan_log)
            account_menu.add_separator()
        account_menu.add_command(label="Logout", command=self._logout)
        account_menu.add_command(label="Exit", command=self.destroy)

        settings_menu = self._make_menu()
        self.settings_menu = settings_menu
        if with_status:
            settings_menu.add_command(label="🔎 Detect Scanner", command=self._show_scanner_setup_screen)
            settings_menu.add_separator()
        settings_menu.add_checkbutton(
            label="Enable Audio Feedback",
            variable=self.sound_enabled,
            command=lambda: self.db.save_sound_enabled(self.sound_enabled.get()),
        )
        settings_menu.add_checkbutton(
            label="Pill tags in scan log",
            variable=self.log_pills_var,
            command=self._on_log_style_changed,
        )
        settings_menu.add_checkbutton(
            label="Timestamp-first scan log",
            variable=self.log_terminal_var,
            command=self._on_log_style_changed,
        )        
        settings_menu.add_separator()
        settings_menu.add_command(label="🔑 Add Feature...", command=self._prompt_feature_code)
        self._photo_toggle_added = False
        if self.db.photo_unlocked:
            self._add_photo_toggle()

        help_menu = self._make_menu()
        help_menu.add_command(label="Check for Updates...", command=lambda: self.check_for_updates(silent=False))
        help_menu.add_separator()
        help_menu.add_command(label="📖 View Setup Guide", command=self._open_setup_guide)
        help_menu.add_command(label="🔌 Troubleshoot Connection...", command=self._show_troubleshooting_wizard)
        help_menu.add_separator()
        help_menu.add_command(label="ℹ️ About Scanner Companion", command=self._show_about_popup)

        if with_status:
            self.port_statuses = {port: "connecting" for port in self.db.serial_ports}
            self.status_panel = StatusPanel(self, self.db.serial_ports,
                                            on_detect=self._show_scanner_setup_screen)
            self.status_pill = self.topbar.add_pill(
                lambda: self.status_panel.toggle(self.status_pill))
        self.topbar.add_menu("Account", account_menu)
        self.topbar.add_menu("Settings", settings_menu)
        self.topbar.add_menu("Help", help_menu, width=66)

    def _style_listbox(self, listbox: tk.Listbox):
        listbox.configure(
            bg=COLORS["card"], fg=COLORS["log_text"],
            selectbackground="#0056b3", selectforeground=COLORS["on_primary"], # Fixed selection color here
            highlightthickness=0,bd=0,
            relief="flat", borderwidth=0, font=(MONO, 10),
        )

    # def _header(self, text: str):
    #     """Full-width blue banner used at the top of every screen."""
    #     header = ttk.Frame(self, style="Header.TFrame", padding=(16, 14))
    #     header.pack(fill="x", pady=(0, 0))
    #     ttk.Label(header, text=text, style="Header.TLabel").pack(side="left")
    #     return header

    # --- LOGIN SCREEN ---------------------------------------------------

    def _show_login_screen(self):
        self._clear_window()
        self._clear_menu_bar()
        self._use_normal_window()
        self._header("Scanner Companion")

        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(expand=True)
        card = ctk.CTkFrame(outer, fg_color=COLORS["card"], corner_radius=16,
                            border_width=1, border_color=COLORS["border"])
        card.pack(padx=40, pady=20)

        ctk.CTkLabel(card, text="Staff login", font=font(22, "bold"),
                     text_color=COLORS["text"]).pack(pady=(24, 2))
        ctk.CTkLabel(card, text="Sign in to start scanning", font=font(12),
                     text_color=COLORS["text_muted"]).pack(pady=(0, 18))

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.pack(padx=32, pady=(0, 24))

        def field_label(text):
            ctk.CTkLabel(form, text=text, font=font(12), anchor="w",
                         text_color=COLORS["text_muted"]).pack(fill="x", pady=(0, 4))

        entry_opts = dict(width=300, height=40, corner_radius=10, font=font(13),
                          fg_color=COLORS["bg"], border_color=COLORS["border"])
        field_label("Email")
        email_entry = ctk.CTkEntry(form, **entry_opts)
        email_entry.pack(fill="x", pady=(0, 12))
        field_label("Password")
        pw_entry = ctk.CTkEntry(form, show="*", **entry_opts)
        pw_entry.pack(fill="x", pady=(0, 12))

        remember_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(form, text="Remember me", variable=remember_var, font=font(12),
                        checkbox_width=20, checkbox_height=20, corner_radius=5, border_width=2,
                        border_color=COLORS["text_muted"], fg_color=COLORS["primary"],
                        hover_color=COLORS["primary_dark"]).pack(anchor="w", pady=(0, 8))

        error_label = ctk.CTkLabel(form, text="", font=font(12), anchor="w", justify="left",
                                   wraplength=300, text_color=COLORS["error"])
        error_label.pack(fill="x")

        def try_login(event=None):
            self.db._remember = remember_var.get()  # must be set BEFORE sign_in
            if self.db.sign_in(email_entry.get(), pw_entry.get()):
                if self.db.serial_ports:
                    self._show_scanner_screen()
                else:
                    self._show_scanner_setup_screen()
            else:
                error_label.configure(text=self.db.last_auth_error or "Login failed.")
                pw_entry.delete(0, "end")

        email_entry.bind("<Return>", lambda e: pw_entry.focus())
        pw_entry.bind("<Return>", try_login)
        ctk.CTkButton(form, text="Log in", command=try_login, height=40, corner_radius=10,
                      font=font(14, "bold"), fg_color=COLORS["primary"],
                      hover_color=COLORS["primary_dark"]).pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(outer, text=f"Scanner Companion v{CURRENT_VERSION}", font=font(11),
                     text_color=COLORS["text_muted"]).pack(pady=(4, 0))
        self.after(150, email_entry.focus)

    # --- SCANNER DETECTION / SETUP SCREEN --------------------------------

    def _show_scanner_setup_screen(self):
        """Pick candidate COM ports, then scan a barcode on each scanner. Every port that
        receives a scan is saved as a configured scanner."""
        self._clear_window()
        self._clear_menu_bar()
        self._stop_reader_threads()  # release any ports the main scanner screen was holding open
        self.probe_stop_event.clear()
        self.probe_port_vars = {}
        self.confirmed_scanner_ports = []
        self._use_normal_window()

        self._header("Detect Scanners")
        self._build_menu_bar(with_status=False)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(16, 18))

        ctk.CTkLabel(body, text="Find your scanners", font=font(20, "bold"), anchor="w",
                     text_color=COLORS["text"]).pack(fill="x")
        ctk.CTkLabel(body, anchor="w", justify="left", font=font(12), wraplength=620,
                     text_color=COLORS["text_muted"],
                     text="Tick the ports to check (leave all ticked if unsure), press Start, then "
                          "scan any barcode on each scanner. Each port that receives a scan is "
                          "added below.").pack(fill="x", pady=(2, 12))

        ports_card = ctk.CTkScrollableFrame(body, fg_color=COLORS["card"], corner_radius=12,
                                            border_width=1, border_color=COLORS["border"],
                                            height=120)
        ports_card.pack(fill="x", pady=(0, 10))

        available_ports = list_scanner_ports()
        if not available_ports:
            ctk.CTkLabel(ports_card, text="No serial ports found. Plug in the scanner, then "
                                          "reopen this screen.", font=font(12), wraplength=560,
                         justify="left", text_color=COLORS["error"]).pack(anchor="w", padx=8, pady=8)
        for port in available_ports:
            var = tk.BooleanVar(value=True)  # default: all candidates selected
            ctk.CTkCheckBox(ports_card, text=f"{port.device}  —  {port.description}", variable=var,
                            font=font(12), checkbox_width=20, checkbox_height=20, corner_radius=5,
                            border_width=2, border_color=COLORS["text_muted"],
                            fg_color=COLORS["primary"],
                            hover_color=COLORS["primary_dark"]).pack(anchor="w", padx=10, pady=5)
            self.probe_port_vars[port.device] = var

        self.probe_status_label = StatusText(body, text="Press Start detection when you're ready.",
                                             anchor="w")
        self.probe_status_label.pack(fill="x")

        confirmed_row = ctk.CTkFrame(body, fg_color="transparent")
        confirmed_row.pack(fill="x", pady=(6, 12))
        ctk.CTkLabel(confirmed_row, text="CONFIRMED", font=font(11, "bold"),
                     text_color=COLORS["text_muted"]).pack(side="left")
        self.confirmed_label = StatusText(confirmed_row, text="(none yet)", size=13)
        self.confirmed_label.pack(side="left", padx=(10, 0))

        button_row = ctk.CTkFrame(body, fg_color="transparent")
        button_row.pack(fill="x")
        self.probe_start_button = ActionButton(
            button_row, text="Start detection", height=38, corner_radius=10, font=font(13, "bold"),
            fg_color=COLORS["primary"], hover_color=COLORS["primary_dark"],
            command=self._start_detection)
        self.probe_start_button.pack(side="left")
        self.probe_done_button = ActionButton(
            button_row, text="Done", height=38, corner_radius=10, font=font(13, "bold"),
            fg_color=COLORS["card_alt"], text_color_disabled=COLORS["text_muted"],
            state="disabled", command=self._finish_detection)
        self.probe_done_button.pack(side="left", padx=(8, 0))

        if self.db.serial_ports:
            ctk.CTkButton(button_row, text="Cancel", command=self._cancel_detection, height=38,
                          corner_radius=10, font=font(13), fg_color=COLORS["card_alt"],
                          hover_color=COLORS["border"], text_color=COLORS["text"]).pack(side="right")

        ctk.CTkLabel(body, text="ACTIVITY", font=font(11, "bold"),
                     text_color=COLORS["text_muted"]).pack(anchor="w", pady=(16, 6))
        self.probe_log_list = ScanLog(body, height=130)
        self.probe_log_list.pack(fill="both", expand=True)

    def _start_detection(self):
        selected_ports = [p for p, var in self.probe_port_vars.items() if var.get()]
        if not selected_ports:
            messagebox.showwarning("No ports selected", "Check at least one port to search.")
            return

        self.probe_start_button.config(state="disabled")
        self.probe_status_label.config(
            text=f"Listening on {len(selected_ports)} port(s)... scan a barcode on each scanner."
        )
        self._probe_log(f"Probing: {', '.join(selected_ports)}")

        self.probe_stop_event.clear()
        self.probe_threads = []
        for port in selected_ports:
            thread = PortProbeThread(port, self.db.baud_rate, self.probe_queue, self.probe_stop_event)
            thread.start()
            self.probe_threads.append(thread)

        self.after(100, self._poll_probe_queue)

    def _poll_probe_queue(self):
        try:
            while True:
                kind, port, payload = self.probe_queue.get_nowait()
                if kind == "probe_status":
                    self._probe_log(f"{port}: {payload}")
                elif kind == "probe_hit":
                    self._on_scanner_detected(port, payload)
        except queue.Empty:
            pass
        # each PortProbeThread ends itself after one hit -- keep polling
        # so other still-running threads (other scanners) can report in too,
        # until the user hits Done or every thread has finished/errored
        if not self.probe_stop_event.is_set():
            self.after(100, self._poll_probe_queue)

    def _on_scanner_detected(self, port: str, sample_scan: str):
        # a given port's own PortProbeThread already stops itself after one
        # hit -- other selected ports keep listening so multiple scanners
        # can be confirmed in the same pass
        if port in self.confirmed_scanner_ports:
            return  # duplicate hit from the same port, ignore
        self.confirmed_scanner_ports.append(port)
        self._probe_log(f"✅ Scanner found on {port} (read: '{sample_scan}')")
        self.confirmed_label.config(
            text=", ".join(self.confirmed_scanner_ports), foreground=COLORS["success"]
        )
        self.probe_status_label.config(
            text=f"{len(self.confirmed_scanner_ports)} scanner(s) confirmed. "
                 f"Scan another, or hit Done."
        )
        self.probe_done_button.configure(state="normal", fg_color=COLORS["success"],
                                         hover_color="#16a34a")

    def _finish_detection(self):
        self._stop_probe_threads()
        if not self.confirmed_scanner_ports:
            messagebox.showwarning("No scanners confirmed", "Scan at least one barcode before finishing.")
            return
        self.db.save_serial_ports(self.confirmed_scanner_ports)
        self._show_scanner_screen()

    def _stop_probe_threads(self):
        """Signals every probe thread to stop and waits for each one to
        actually release its port -- mirrors _stop_reader_threads, and
        matters for the same reason: the next screen may try to reopen
        these exact same ports immediately."""
        self.probe_stop_event.set()
        for thread in self.probe_threads:
            thread.join(timeout=2)
        self.probe_threads = []

    def _cancel_detection(self):
        self._stop_probe_threads()
        self._show_scanner_screen()

    def _probe_log(self, message: str):
        timestamp = datetime.now().strftime("%I:%M:%S %p")
        if not self._widget_alive("probe_log_list"):
            return
        self.probe_log_list.insert(0, f"[{timestamp}] {message}")

    # --- MAIN SCANNER SCREEN --------------------------------------------

    def _show_scanner_screen(self):
        self._clear_window()

        if not self.db.serial_ports:
            self._show_scanner_setup_screen()
            return

        self._header("Scanner Companion")
        self._build_menu_bar()

        self.hero = LastScanCard(self)
        self.hero.pack(fill="x", padx=20, pady=(16, 8))

        self.info_row = ctk.CTkFrame(self, fg_color="transparent")
        self.info_row.pack(fill="x", padx=26)
        self.status_label = StatusText(self.info_row, text="Waiting for scan...")
        self.status_label.pack(side="left")
        self.mode_label = StatusText(self.info_row, text="")
        self.mode_label.pack(side="right")
        self.pending_label = StatusText(self.info_row, text="")
        self.pending_label.pack(side="right", padx=(0, 12))

        self.log_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.log_frame.pack(fill="both", expand=True, padx=20, pady=(8, 16))
        ctk.CTkLabel(self.log_frame, text="RECENT SCANS", font=font(11, "bold"),
                     text_color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 6))
        self.log_list = ScanLog(self.log_frame, height=220)
        self.log_list.pack(fill="both", expand=True)

        self._start_reader_threads()
        self._log(f"Signed in. Listening on: {', '.join(self.db.serial_ports)}")
        self._poll_job = self.after(100, self._poll_queue)
        self._update_pending_label()
        self._flush_job = self.after(20000, self._periodic_flush)
        self._refresh_mode_indicator()

    def _start_reader_threads(self):
        self.stop_event.clear()
        for port in self.db.serial_ports:
            thread = SerialReaderThread(port, self.db.baud_rate, self.scan_queue, self.stop_event)
            thread.start()
            self.reader_threads.append(thread)

    def _stop_reader_threads(self):
        """Signals every live scanner thread to stop and waits for each one
        to actually release its port before returning -- callers must not
        try to reopen the same ports until this finishes, or they'll hit
        spurious 'could not open port' errors."""
        if not self.reader_threads:
            return
        self.stop_event.set()
        for thread in self.reader_threads:
            thread.join(timeout=2)  # readline() has a 1s timeout, so this is generous
        self.reader_threads = []
        self.stop_event.clear()

    def _poll_queue(self):
        try:
            while True:
                kind, port, payload = self.scan_queue.get_nowait()
                if kind == "status":
                    self._update_port_status(port, payload)
                elif kind == "scan":
                    self._handle_scan(payload)
                elif kind == "photo_uploaded":
                    if self._widget_alive("photo_browser"):
                        self.photo_browser.on_uploaded(port, payload)
        except queue.Empty:
            pass
        finally:
            self._poll_job = self.after(100, self._poll_queue)

    def _periodic_flush(self):
        """Every 20s, try to drain the offline scan queue -- catches the
        case where connectivity comes back but nobody scans anything for
        a while, so queued check-ins/outs don't just sit there untouched."""
        try:
            flushed = self.db.flush_pending_scans()
            if flushed:
                self._log(f"✅ Synced {flushed} queued scan(s) that were waiting on a connection.")
        except Exception:
            pass  # still offline, or a transient issue -- just try again next cycle
        self._update_pending_label()
        self._flush_job = self.after(20000, self._periodic_flush)

    def _update_pending_label(self):
        if not hasattr(self, "pending_label") or not self.pending_label.winfo_exists():
            return
        count = self.db.pending_count()
        if count:
            self.pending_label.config(text=f"⏳ {count} queued (offline)", fg=COLORS["warning"])
        else:
            self.pending_label.config(text="", fg=COLORS["text_muted"])

    def _update_port_status(self, port: str, status: str):
        self.port_statuses[port] = status
        if self._widget_alive("status_panel"):
            self.status_panel.set_status(port, status)

        if any(s not in ("connected", "connecting") for s in self.port_statuses.values()):
            state, text, color = "error", "Error with a scanner...", COLORS["error"]
        elif all(s == "connected" for s in self.port_statuses.values()):
            state, text, color = "ok", "Waiting for scan...", COLORS["text_muted"]
        else:
            state, text, color = "connecting", "Connecting to hardware...", COLORS["text_muted"]

        if self._widget_alive("status_pill"):
            self.status_pill.set_state(state)
        if self._widget_alive("status_label"):
            self.status_label.config(text=text, fg=color)

    def _clear_scan_log(self):
        """Deletes all entries inside the main scan log view."""
        if hasattr(self, 'log_list'):
            self.log_list.delete(0, tk.END)

    def _on_log_style_changed(self):
        pills, terminal = self.log_pills_var.get(), self.log_terminal_var.get()
        set_log_style(pills, terminal)
        self.db.save_log_style(pills, terminal)
        for name in ("log_list", "probe_log_list"):   # main log and the Detect Scanners log
            if self._widget_alive(name):
                getattr(self, name).restyle()

    # --- PHOTO CAPTURE FEATURE ------------------------------------------

    def _prompt_feature_code(self):
        code = simpledialog.askstring("Add Feature", "Enter your feature code:", parent=self, show="*")
        if not code:
            return
        if check_feature_code(code) == "drive_photos":
            self.db.unlock_photo_feature()
            self._add_photo_toggle()
            messagebox.showinfo("Feature added",
                                "Photo Capture is now available under Settings. Turn it on there when you're ready.")
        else:
            messagebox.showerror("Add Feature", "That code isn't valid.")

    def _add_photo_toggle(self):
        if self._photo_toggle_added:
            return
        self.settings_menu.add_checkbutton(
            label="📷 Photo Capture Mode",
            variable=self.photo_mode_var,
            command=self._on_photo_mode_toggled,
        )
        self.settings_menu.add_command(label="Choose Camera...", command=self._choose_camera)
        self.settings_menu.add_command(label="Disconnect Google Drive...", command=self._disconnect_google)
        self._photo_toggle_added = True

    def _on_photo_mode_toggled(self):
        enabled = self.photo_mode_var.get()
        if enabled:
            if not messagebox.askyesno(
                "Photo Capture Mode",
                "While this is on, scans will NOT check students in or out.\n"
                "They will only open the camera for that student.\n\nTurn it on?"
            ) or not self._ensure_photo_backend():
                self.photo_mode_var.set(False)
                enabled = False
        self.db.save_photo_mode(enabled)
        self._refresh_mode_indicator()
        self._log("📷 Photo mode ON - scans find students for photos, no check-in/out" if enabled
                  else "Photo mode OFF - scans check students in/out")

    def _widget_alive(self, name):
        w = getattr(self, name, None)
        return w is not None and w.winfo_exists()

    def _refresh_mode_indicator(self):
        if not self._widget_alive("mode_label"):
            return
        if self.photo_mode_var.get():
            self.mode_label.config(text="📷 PHOTO MODE (no check-in)", fg=COLORS["warning"])
            self.hero.pack_forget()
            self._ensure_photo_widgets()
            self.photo_search_frame.pack(fill="x", padx=20, pady=(12, 0), before=self.log_frame)
            self.photo_browser.pack(fill="both", expand=True, padx=20, pady=(8, 0), before=self.log_frame)
            self.log_frame.pack(fill="both", expand=False, padx=20, pady=(8, 12))
            self.log_list.configure(height=130)
            self.resizable(True, True)
            self.minsize(760, 640)
            self.geometry("900x760")
        else:
            self.mode_label.config(text="")
            for name in ("photo_search_frame", "photo_browser"):
                if self._widget_alive(name):
                    getattr(self, name).pack_forget()
            self.hero.pack(fill="x", padx=20, pady=(16, 8), before=self.info_row)
            self.log_frame.pack(fill="both", expand=True, padx=20, pady=(8, 16))
            self.log_list.configure(height=220)
            self.minsize(1, 1)
            self.resizable(False, False)
            self.geometry(NORMAL_SIZE)

    def _ensure_photo_widgets(self):
        if not self._widget_alive("photo_search_frame"):
            self._build_photo_search_bar()
        if not self._widget_alive("photo_browser"):
            self._build_photo_browser()

    def _build_photo_browser(self):
        from photo_capture import PhotoBrowser
        self.photo_browser = PhotoBrowser(
            self,
            get_store=lambda: self.photo_worker.store if self._ensure_photo_backend() else None,
            get_visible_students=self._visible_photo_students,
        )

    def _visible_photo_students(self) -> dict:
        """s_id -> display label, for active students only (withdrawn are never listed)."""
        out = {}
        for s in self.db.get_students():
            if self.db.is_withdrawn(s):
                continue
            out[str(s["s_id"])] = (f"{s.get('first_name', '')} {s.get('last_name', '')} "
                                   f"({s.get('barcode_code', '')})")
        return out

    def _ensure_photo_backend(self) -> bool:
        """Connects to Google Drive the first time it's needed (may open a browser to sign in)."""
        if self.photo_worker is not None:
            return True
        try:
            from photo_capture import DriveStore, UploadWorker, has_saved_login
            if not has_saved_login():
                messagebox.showinfo("Photo Capture",
                                    "A browser window will open. Sign in to the Google account that should store the photos.")
            self.config(cursor="watch")
            self.update_idletasks()
            self.photo_worker = UploadWorker(
                DriveStore(),
                on_uploaded=lambda sid, subj: self.scan_queue.put(("photo_uploaded", sid, subj)),
            )
            return True
        except Exception as e:
            messagebox.showerror("Photo Capture", f"Couldn't connect to Google Drive:\n{e}")
            return False
        finally:
            self.config(cursor="")

    def _handle_photo_scan(self, raw_scan: str):
        code = raw_scan.strip()

        if self._photo_busy:
            self.status_label.config(text="Finish the current photo first - scan ignored.", fg=COLORS["warning"])
            return
        if not code.isdigit():
            self.status_label.config(text="Photo mode: staff and unknown codes are ignored.", fg=COLORS["warning"])
            return

        try:
            student = self.db.find_student_fresh(code)
        except Exception as e:
            self._play_feedback_sound(is_success=False)
            self.status_label.config(text=f"Lookup failed: {e}", fg=COLORS["error"])
            return

        if student is None:
            self._play_feedback_sound(is_success=False)
            msg = f"Student code {code} not found."
            self.status_label.config(text=msg, fg=COLORS["error"])
            self._log(msg)
            return

        self._start_photo_for_student(student)

    def _start_photo_for_student(self, student: dict):
        """Shared by scans, typed barcodes and name search: opens the camera for each subject."""
        if self._photo_busy:
            self.status_label.config(text="Finish the current photo first.", fg=COLORS["warning"])
            return
        if self.db.is_withdrawn(student):
            self._play_feedback_sound(is_success=False)
            msg = f"{student.get('first_name', '')} {student.get('last_name', '')} is withdrawn - no photo taken."
            self.status_label.config(text=msg, fg=COLORS["error"])
            self._log(msg)
            return
        if not self._ensure_photo_backend():
            return

        from photo_capture import PhotoDialog, student_photo_info

        try:
            info = student_photo_info(student)
        except Exception as e:
            self._play_feedback_sound(is_success=False)
            self.status_label.config(text=f"Student data problem: {e}", fg=COLORS["error"])
            return

        label = f"{info['first']}, {info['last']}"
        subjects = list(info["subjects"])
        if not subjects:
            self._play_feedback_sound(is_success=False)
            msg = f"{label}: no Math or Reading marked"
            self.status_label.config(text=msg, fg=COLORS["warning"])
            self._log(msg)
            return

        self._play_feedback_sound(is_success=True)
        self.status_label.config(text=f"Found: {label} ({' + '.join(subjects)})", fg=COLORS["success"])
        self._photo_busy = True
        total = len(subjects)


        def next_subject():
            if not subjects:
                self._photo_busy = False
                self.status_label.config(text="Waiting for scan...", fg=COLORS["text_muted"])
                if hasattr(self, "photo_search_entry") and self.photo_search_entry.winfo_exists():
                    self.photo_search_entry.focus_set()
                return
            subject = subjects.pop(0)

            def done(jpeg):
                if jpeg:
                    self.photo_worker.submit(info["id"], info["last"], info["first"], subject, jpeg, info["barcode"])
                    self._log(f"📷 {label}: {subject} photo captured, uploading")
                else:
                    self._log(f"{label}: {subject} photo skipped")
                next_subject()

            try:
                step = total - len(subjects)  # subject was just popped, so this is 1-based
                PhotoDialog(
                    self, label, done,
                    subtitle=subject if total == 1 else f"{subject}  •  photo {step} of {total}",
                    theme={"colors": COLORS, "font": FONT},
                    camera_index=self.db.camera_index,
                )
            except Exception as e:
                self._photo_busy = False
                self._log(f"Camera error: {e}")

        next_subject()

    def _choose_camera(self):
        if self._photo_busy:
            messagebox.showinfo("Choose Camera", "Finish the current photo first.")
            return
        from photo_capture import CameraPicker

        def picked(index):
            self.db.save_camera_index(index)
            self._log(f"Camera set to Camera {index + 1}")

        CameraPicker(self, self.db.camera_index, picked)

    def _disconnect_google(self):
        if self._photo_busy:
            messagebox.showinfo("Disconnect Google Drive", "Finish the current photo first.")
            return
        from photo_capture import disconnect_google, has_saved_login
        if not has_saved_login() and self.photo_worker is None:
            messagebox.showinfo("Disconnect Google Drive", "Google Drive isn't connected on this computer.")
            return
        if not messagebox.askyesno(
            "Disconnect Google Drive",
            "This signs this computer out of Google Drive and revokes its access.\n"
            "Photos already uploaded stay in Drive.\n"
            "Photo Capture will be turned off until you reconnect.\n\nDisconnect?"
        ):
            return

        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self._finish_photo_uploads()            # let queued uploads finish first
            if self.photo_worker is not None:
                self.photo_worker.stop()
                self.photo_worker = None
            if self.photo_mode_var.get():
                self.photo_mode_var.set(False)
                self.db.save_photo_mode(False)
                self._refresh_mode_indicator()
            if self._widget_alive("photo_browser"):
                self.photo_browser.destroy()        # drops cached photos from memory
            disconnect_google()
        finally:
            self.config(cursor="")
        self._log("Google Drive disconnected")
        messagebox.showinfo("Disconnect Google Drive",
                            "Disconnected. Turn on Photo Capture Mode to sign in again.")

    def _build_photo_search_bar(self):
        """Created when photo mode first turns on; only shown while it's on."""
        self.photo_search_frame = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12,
                                               border_width=1, border_color=COLORS["border"])
        inner = ctk.CTkFrame(self.photo_search_frame, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)
        self.photo_search_entry = ctk.CTkEntry(
            inner, height=40, corner_radius=10, font=font(13),
            placeholder_text="Search by name, or type a barcode, then press Enter",
            fg_color=COLORS["bg"], border_color=COLORS["border"])
        self.photo_search_entry.pack(side="left", fill="x", expand=True)
        self.photo_search_entry.bind("<Return>", self._on_photo_search)
        ctk.CTkButton(inner, text="Find", width=84, height=40, corner_radius=10,
                      font=font(13, "bold"), fg_color=COLORS["primary"],
                      hover_color=COLORS["primary_dark"],
                      command=self._on_photo_search).pack(side="left", padx=(10, 0))

    def _on_photo_search(self, event=None):
        query = self.photo_search_entry.get().strip()
        if not query:
            return
        if self._photo_busy:
            self.status_label.config(text="Finish the current photo first.", fg=COLORS["warning"])
            return
        self.photo_search_entry.delete(0, "end")

        if query.isdigit():
            self._handle_photo_scan(query)  # typed barcode: same path as a scan
            return

        try:
            matches = self.db.search_students(query)
        except Exception as e:
            self.status_label.config(text=f"Search failed: {e}", fg=COLORS["error"])
            return

        if not matches:
            self.status_label.config(text=f"No student matching '{query}'.", fg=COLORS["error"])
        elif len(matches) == 1:
            self._start_photo_for_student(matches[0])
        else:
            self._show_student_picker(matches)

    def _show_student_picker(self, matches: list):
        items = []
        for s in matches:
            name = f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()
            bits = [f"Barcode {s.get('barcode_code', '')}"]
            if s.get("grade"):
                bits.append(f"Grade {s['grade']}")
            items.append((name, "   •   ".join(bits)))
        ListPicker(self, "Choose a student", f"{len(matches)} matches. Pick one:", items,
                   lambda i: self._start_photo_for_student(matches[i]))

    def _finish_photo_uploads(self, max_wait=20):
        """Gives queued uploads a few seconds to finish before the app closes or updates."""
        if self.photo_worker is None:
            return
        deadline = time.time() + max_wait
        while self.photo_worker.q.unfinished_tasks and time.time() < deadline:
            time.sleep(0.2)


    def _logout(self):
        if not messagebox.askyesno("Log out", "Are you sure you want to log out?"):
            return
        self._stop_probe_threads()
        self._stop_reader_threads()
        self.db.sign_out()
        self._show_login_screen()


    def _handle_scan(self, raw_scan: str):
        if self.photo_mode_var.get():
            self._handle_photo_scan(raw_scan)
            return

        # Change foreground to fg here
        self.status_label.config(text=f"Processing '{raw_scan}'...", fg=COLORS["text_muted"])

        try:
            status, message = self.db.process_scan(raw_scan)
        except Exception as e:
            # Check if it looks like a network or database server error
            error_str = str(e).lower()
            if "network" in error_str or "connection" in error_str or "timeout" in error_str:
                status, message = "error", "⚠️ Connection Lost: Check your center's internet connection."
            else:
                status, message = "error", f"⚠️ Error processing scan: {e}"

        error_states = {"not_found", "invalid", "error"}
        neutral_states = {"debounced", "queued"}  # no success chirp -- nothing was confirmed saved yet

        if status in error_states:
            self._play_feedback_sound(is_success=False) # Play warning buzz
        elif status not in neutral_states:
            self._play_feedback_sound(is_success=True)  # Play success chirp

        colors = {
            "checked_in": COLORS["success"],
            "checked_out": COLORS["primary_dark"], # A slightly darker blue for contrast text readability
            "already_done": COLORS["text_muted"],
            "not_found": COLORS["error"],
            "invalid": COLORS["warning"],
            "debounced": COLORS["text_muted"],
            "error": COLORS["error"],
            "queued": COLORS["warning"],
        }
        # Change foreground to fg here
        if status == "debounced":
            self.status_label.config(text=message, fg=COLORS["text_muted"])
        else:
            self.hero.show(status, message)
            self.status_label.config(text="Waiting for scan...", fg=COLORS["text_muted"])

        if status != "debounced":
            self._log(message)

        self._update_pending_label()

    def _show_about_popup(self):
        """Displays a clean informational dialog box with version details 
        and the explicit legal trademark disclaimer for external distribution."""
        title = "About Scanner Companion"
        
        message = (
            "Center Scanner Companion\n"
            f"Version {CURRENT_VERSION}\n\n"
            "An independent background utility designed to streamline center check-in & check-out operations.\n\n"
            "Legal Disclaimer:\n"
            "This software is an entirely independent, third-party utility tool. "
            "It is not officially affiliated with, endorsed by, authorized by, "
            "or sponsored by Kumon North America, Inc., or any of its branches."
        )
        
        messagebox.showinfo(title, message)


    def _open_setup_guide(self):
        guide = resource_path("README.txt")
        if guide.exists():
            try:
                open_path(guide)
            except Exception as e:
                messagebox.showerror("Error", f"Could not open the guide:\n{e}")
        else:
            messagebox.showerror("Error", "Documentation file 'README.txt' could not be found.")


    def _show_troubleshooting_wizard(self):
        """Launches a structural checklist pop-up to help users fix hardware connection stalls."""
        
        tips = (
            "**Please note that hardware scanners MUST have and be in 'COM' mode**\n\n"
            "Is your hardware barcode scanner failing to connect?\n\n"
            "Try these steps:\n"
            "1. Unplug the scanner's USB cable, wait 3 seconds, and reconnect it.\n"
            "2. Navigate to 'Settings -> Detect Scanner' to re-verify port parameters.\n"
            "3. Ensure no other student attendance or tracking software is open.\n"
            "4. Try moving the cable into a different physical USB port slot.\n\n"
            "If the health status bar continues to display an error, "
            "please contact jackli6140@gmail.com with the details."
        )
        messagebox.showinfo("Hardware Troubleshooting Guide", tips)

    def _log(self, message: str):
        if not self._widget_alive("log_list"):
            return
        timestamp = datetime.now().strftime("%I:%M:%S %p")
        self.log_list.insert(0, f"[{timestamp}] {message}")

        if self.log_list.size() > 200:
            self.log_list.delete(tk.END)

        self.log_list.see(0)

    def _export_scan_log(self):
        """Saves the current live scan log items into a CSV or plain text
        file, based on the extension the user picks in the save dialog."""

        
        # Check if there is actually anything to export
        if not hasattr(self, 'log_list') or self.log_list.size() == 0:
            messagebox.showwarning("Export Failed", "The scan log is currently empty.")
            return
            
        # Open a standard Windows "Save As" popup window
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv"), ("Text Files", "*.txt"), ("All Files", "*.*")],
            title="Export Scan Log",
            initialfile=f"scan_log_{datetime.now().strftime('%Y-%m-%d')}.csv"
        )
        
        if not file_path:
            return

        log_items = self.log_list.get(0, tk.END)  # oldest-last, since new entries insert at 0
        try:
            if file_path.lower().endswith(".csv"):
                with open(file_path, "w", newline="", encoding="utf-8") as file:
                    writer = csv.writer(file)
                    writer.writerow(["Timestamp", "Message"])
                    for item in log_items:
                        # entries are formatted as "[HH:MM:SS AM] message"
                        timestamp, _, message = item.strip().partition("] ")
                        writer.writerow([timestamp.lstrip("["), message])
            else:
                with open(file_path, "w", encoding="utf-8") as file:
                    for item in log_items:
                        # Strip out our internal spacing padding when writing to raw file
                        file.write(item.strip() + "\n")

            messagebox.showinfo("Success", f"Scan log successfully exported to:\n{file_path}")
        except Exception as e:
            messagebox.showerror("Export Error", f"Could not save file: {e}")

    def check_for_updates(self, silent=True):
        """Fetches the remote manifest file to see if a newer iteration exists.
        If silent=True, it runs quietly on startup. If False, it alerts the user manually."""
        
        try:
            # Query the web manifest file asynchronously
            response = requests.get(MANIFEST_URL, timeout=5)
            if response.status_code == 200:
                data = response.json()
                remote_version = data.get("version", "1.0.0")
                download_url = data.get("url")
                
                # Check if the fetched server build string is higher than our native constant
                if _ver(remote_version) > _ver(CURRENT_VERSION):
                    # Prompt the user with a standard confirmation popup dialog box
                    msg = f"A new software update (v{remote_version}) is available!\n\nWhat's New:\n{data.get('changelog')}\n\nWould you like to download and apply the update now?"
                    if messagebox.askyesno("Update Available", msg):
                        self._apply_background_update(data)
                else:
                    if not silent:
                        messagebox.showinfo("Up to Date", "You are running the latest version of Scanner Companion.")
        except Exception as e:
            if not silent:
                messagebox.showerror("Update Error", f"Failed to connect to the update server:\n{e}")

    #def _apply_background_update(self, download_url):
        # try:
        #     downloads_dir = Path.home() / "Downloads"
        #     installer_path = downloads_dir / "ScannerCompanion_Update.exe"
        #     old_exe_path = os.path.abspath(sys.argv[0])

        #     r = requests.get(download_url, stream=True, timeout=30)
        #     if r.status_code == 200:
        #         with open(installer_path, 'wb') as f:
        #             for chunk in r.iter_content(chunk_size=8192):
        #                 f.write(chunk)

        #         messagebox.showinfo(
        #             "Update Downloaded",
        #             f"The new version was saved to:\n{installer_path}\n\n"
        #             f"To finish updating:\n"
        #             f"1. Close this app\n"
        #             f"2. Delete or rename the old file at:\n   {old_exe_path}\n"
        #             f"3. Move the new file from Downloads to that same location\n"
        #             f"   (keep the same filename so your desktop shortcut still works)"
        #         )
        #         os.startfile(downloads_dir)
        # except Exception as e:
        #     messagebox.showerror("Update Failed", f"Could not download update:\n{e}")
    def _apply_background_update(self, data):
        """Downloads the new binary executable into a temp folder and triggers the file-swap batch handler."""
        
        download_url = data.get("url", "")
        if sys.platform != "win32":
            import webbrowser
            webbrowser.open(download_url)
            return
        if not download_url.startswith("https://"):
            messagebox.showerror("Update Failed", "Update link is not secure.")
            return

        try:
            # Update footer display state visually so the user doesn't close it mid-stream
            if self._widget_alive("status_label"):
                self.status_label.config(text="📥 Downloading software update... Please wait.", fg=COLORS["warning"])
                self.update()

            r = requests.get(download_url, stream=True, timeout=30)
            if r.status_code == 200:
                # Path configurations
                exe_path = os.path.abspath(sys.argv[0])          # Path to current running .exe
                current_dir = os.path.dirname(exe_path)
                temp_new_exe = os.path.join(current_dir, "new_version.tmp")
                bat_script_path = os.path.join(current_dir, "update_process.bat")
                
                h = hashlib.sha256()
                with open(temp_new_exe, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        h.update(chunk)

                if not _update_is_authentic(data.get("version", ""), h.hexdigest(),
                                            data.get("signature", "")):
                    os.remove(temp_new_exe)
                    messagebox.showerror("Update Failed",
                                         "The downloaded update failed verification and was not installed.")
                    if self._widget_alive("status_label"):
                        self.status_label.config(text="Waiting for scan...", fg=COLORS["text_muted"])
                    return
                        
                # Create the automated Windows Batch background loop to overwrite the locked exe file
                # The loop tries to delete the old application until it closes, then renames the temp file
                bat_content = f"""
                @echo off
                timeout /t 2 /nobreak >nul
                :loop
                del "{exe_path}" >nul 2>&1
                if exist "{exe_path}" (
                    timeout /t 1 /nobreak >nul
                    goto loop
                )
                move "{temp_new_exe}" "{exe_path}" >nul 2>&1
                start "" "{exe_path}"
                del "%~f0" & exit
                """

                with open(bat_script_path, "w") as bat_file:
                    bat_file.write(bat_content)

                subprocess.Popen([bat_script_path], shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)
                self._finish_photo_uploads()                
                self.stop_event.set()
                self.probe_stop_event.set()
                self.db.stop()
                self.destroy()
                sys.exit()
                
        except Exception as e:
            messagebox.showerror("Update Interrupted", f"Could not complete installation:\n{e}")
            if self._widget_alive("status_label"):
                self.status_label.config(text="Waiting for scan...", fg=COLORS["text_muted"])

    def _play_feedback_sound(self, is_success=True):
        """Plays a short chime (success) or soft low tone (error) if audio feedback is on."""
        if not self.sound_enabled.get():
            return
        path = self._sound_files.get("success" if is_success else "error")
        if not path:
            return
        try:
            if winsound:
                # SND_ASYNC returns immediately, and a new scan cuts off the previous
                # sound, so rapid scans never pile up or freeze the UI
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == "darwin":
                subprocess.Popen(["afplay", path])
        except Exception:
            pass  # fail silently if audio drivers are locked down

    def _cancel_scanner_loops(self):
        for job in (self._poll_job, self._flush_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._poll_job = None
        self._flush_job = None

    def _use_normal_window(self):
        self.minsize(1, 1)
        self.resizable(False, False)
        self.geometry(NORMAL_SIZE)

    def _clear_window(self):
        self._cancel_scanner_loops()
        for widget in self.winfo_children():
            widget.destroy()
    def _on_close(self):
        self._finish_photo_uploads()
        self.stop_event.set()
        self.probe_stop_event.set()
        self.db.stop()  # stop the token-refresh background thread too
        self.destroy()


if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()