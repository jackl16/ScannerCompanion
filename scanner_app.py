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

CURRENT_VERSION = "1.0.0"
MANIFEST_URL = "https://raw.githubusercontent.com/jackl16/ScannerCompanion/refs/heads/main/version.json"

import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox,filedialog
from datetime import datetime
import os           
import requests
import subprocess
import sys


import winsound

import serial
import serial.tools.list_ports

from scanner_db import ScannerDB

FONT = "Segoe UI"

# Clean blue & white palette -- kept as one dict so the whole look can be
# retuned from a single place later without hunting through every screen.
COLORS = {
    "bg": "#121214",          # app background (soft slate)
    "card": "#1a1a1e",         # panels/cards
    "border": "#27272a",  
    "text": "#f4f4f5", 
    "text_muted": "#a1a1aa",
    "primary": "#4f46e5",  
    "primary_dark": "#4338ca",
    "primary_light": "#DBEAFE",
    "on_primary": "#FFFFFF",
    "success": "#10b981",  
    "error": "#ef4444",    
    "warning": "#f59e0b" 
}


class SerialReaderThread(threading.Thread):
    """Reads one scanner's COM port in the background and pushes each
    scanned line onto a shared queue for the main thread to process."""

    def __init__(self, port: str, baud_rate: int, output_queue: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.port = port
        self.baud_rate = baud_rate
        self.output_queue = output_queue
        self.stop_event = stop_event
    

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


class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Scanner Companion")
        self.geometry("560x560")
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])

        self.sound_enabled = tk.BooleanVar(value=False) 

        self._setup_style()


        try:
            self.db = ScannerDB()
        except FileNotFoundError as e:
            messagebox.showerror("Missing config", str(e))
            self.destroy()
            return

        self.scan_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.reader_threads = []

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
        self.after(150, self._load_custom_icon)
    # --- VISUAL STYLE -----------------------------------------------------
    def _load_custom_icon(self):
        """Loads the window icon asynchronously after the main GUI has finished drawing."""
        try:
            from PIL import Image, ImageTk
            icon_raw = Image.open("app_icon.ico")
            self.app_icon_photo = ImageTk.PhotoImage(icon_raw)
            self.iconphoto(False, self.app_icon_photo)
        except Exception:
            # If the asset file vanishes or fails to decode, fall back cleanly
            pass

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

    def _make_menu(self) -> tk.Menu:
        """Plain tk.Menu -- used both for the native top menu bar's dropdowns
        and, later, for any popup menus. tk.Menu doesn't take ttk styling,
        so it renders with the OS's native menu look, which is the point."""
        return tk.Menu(self, tearoff=0)

    def _build_menu_bar(self):
        """The real File/Edit/View-style menu bar along the top of the
        window (not part of the blue header -- this is a separate Tk
        concept, set via self.config(menu=...))."""
        menu_bar = tk.Menu(self, tearoff=0)

        account_menu = self._make_menu()
        account_menu.add_command(label="Export Scan Log", command=self._export_scan_log) 
        account_menu.add_command(label="Clear Scan Log", command=self._clear_scan_log)
        account_menu.add_separator()
        account_menu.add_command(label="Logout", command=self._logout)
        account_menu.add_command(label="Exit", command=self.destroy)
        menu_bar.add_cascade(label="File", menu=account_menu)


        settings_menu = self._make_menu()
        settings_menu.add_command(label="🔎 Detect Scanner", command=self._show_scanner_setup_screen)
        settings_menu.add_separator()
        settings_menu.add_checkbutton(
            label="Enable Audio Feedback", 
            variable=self.sound_enabled
        )        
        menu_bar.add_cascade(label="Settings", menu=settings_menu)

        # Status -- one entry per port, updated live as scans/errors come in.
        self.status_menu = self._make_menu()
        self.status_menu_indices = {}
        for i, port in enumerate(self.db.serial_ports):
            self.status_menu.add_command(label=f"{port}  …", state="disabled")
            self.status_menu_indices[port] = i
            
        # FIXED: Removed the "Status: …" placeholder from the top text title
        menu_bar.add_cascade(label="Status", menu=self.status_menu)
        self.port_statuses = {port: "connecting" for port in self.db.serial_ports}



        help_menu = self._make_menu()
        help_menu.add_command(label="Check for Updates...", command=lambda: self.check_for_updates(silent=False))
        help_menu.add_separator()
        help_menu.add_command(label="📖 View Setup Guide", command=self._open_setup_guide)
        help_menu.add_command(label="🔌 Troubleshoot Connection...", command=self._show_troubleshooting_wizard) # New wizard
        help_menu.add_separator()
        help_menu.add_command(label="ℹ️ About Scanner Companion", command=self._show_about_popup)
        menu_bar.add_cascade(label="Help", menu=help_menu)

        self.menu_bar = menu_bar
        self.config(menu=menu_bar)

    def _clear_menu_bar(self):
        """Removes the top menu bar -- used on screens (login, setup) that
        shouldn't show it. Needed because the menu bar is attached to the
        window itself, not the content area, so _clear_window() alone
        doesn't touch it."""
        self.config(menu=tk.Menu(self))

    def _style_listbox(self, listbox: tk.Listbox):
        """tk.Listbox isn't a ttk widget, so it needs its colors set
        directly rather than through the style system above."""
        listbox.configure(
            bg=COLORS["card"], fg=COLORS["text"],
            selectbackground="#0056b3", selectforeground=COLORS["on_primary"], # Fixed selection color here
            highlightthickness=0,bd=0,
            relief="flat", borderwidth=0, font=("Consolas", 10),
        )

    def _header(self, text: str):
        """Full-width blue banner used at the top of every screen."""
        header = ttk.Frame(self, style="Header.TFrame", padding=(16, 14))
        header.pack(fill="x", pady=(0, 0))
        ttk.Label(header, text=text, style="Header.TLabel").pack(side="left")
        return header

    # --- LOGIN SCREEN ---------------------------------------------------

    def _show_login_screen(self):
        self._clear_window()
        self._clear_menu_bar()
        self._header("Scanner Companion")

        # --- 1. SETUP THE CUSTOM DARK ENTRY STYLE PARAMETERS ---
        # We build a unique sub-style specifically for dark text entries
        style = ttk.Style()
        style.configure(
            "DarkEntry.TEntry",
            fieldbackground=COLORS["card"],  
            foreground="#ffffff",        
            insertcolor="#ffffff",        
            bordercolor=COLORS["border"],  
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"]
        )
        # -------------------------------------------------------

        outer = ttk.Frame(self, padding=40)
        outer.pack(expand=True)

        card = ttk.Frame(outer, style="Card.TFrame", padding=28)
        card.pack()

        ttk.Label(card, text="🔒 Staff Login", style="CardTitle.TLabel").pack(pady=(0, 20))

        ttk.Label(card, text="Email", style="Card.TLabel").pack(anchor="w")
        email_var = tk.StringVar()
        
        email_entry = ttk.Entry(card, textvariable=email_var, width=32, style="DarkEntry.TEntry")
        email_entry.pack(pady=(2, 10))
        email_entry.focus()

        ttk.Label(card, text="Password", style="Card.TLabel").pack(anchor="w")
        pw_var = tk.StringVar()
        
        pw_entry = ttk.Entry(card, textvariable=pw_var, show="*", width=32, style="DarkEntry.TEntry")
        pw_entry.pack(pady=(2, 10))

        remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(card, text="Remember me for 30 days", variable=remember_var).pack(pady=(0, 12), anchor="w")

        error_label = ttk.Label(card, text="", style="Card.TLabel", foreground=COLORS["error"])
        error_label.pack()

        def try_login(event=None):
            if self.db.sign_in(email_var.get(), pw_var.get()):
                if remember_var.get():
                    self.db.remember_session(self.db._last_refresh_token)
                if self.db.serial_ports:
                    self._show_scanner_screen()
                else:
                    self._show_scanner_setup_screen()
            else:
                error_label.config(text=self.db.last_auth_error or "Login failed.")
                pw_var.set("")

        pw_entry.bind("<Return>", try_login)
        ttk.Button(card, text="Login", command=try_login).pack(pady=(8, 0), fill="x")


    # --- SCANNER DETECTION / SETUP SCREEN --------------------------------

    def _show_scanner_setup_screen(self):
        """Lets the user pick candidate COM ports (or just select them all)
        and scan a barcode once. Whichever port the scan actually arrives
        on gets saved as the configured scanner -- no need to know in
        advance which port is the real one."""
        self._clear_window()
        self._clear_menu_bar()
        self._stop_reader_threads()  # release any ports the main scanner screen was holding open
        self.probe_stop_event.clear()
        self.probe_port_vars = {}
        self.confirmed_scanner_ports = []

        self._header("🔎 Detect Scanner(s)")

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            style="Muted.TLabel",
            text="Select the port(s) to check (or leave all checked if unsure),\n"
                 "then scan a barcode on each scanner you want to enable.\n"
                 "Each port that receives a scan gets added below -- scan on\n"
                 "as many devices as you have, then hit Done.",
        ).pack(anchor="w", pady=(0, 12))

        ports_card = ttk.Frame(frame, style="Card.TFrame", padding=14)
        ports_card.pack(fill="x", pady=(0, 12))

        available_ports = serial.tools.list_ports.comports()
        if not available_ports:
            ttk.Label(
                ports_card, style="Card.TLabel",
                text="No serial ports found. Plug in the scanner and reopen this screen.",
                foreground=COLORS["error"],
            ).pack(anchor="w")
        for port in available_ports:
            var = tk.BooleanVar(value=True)  # default: all candidates selected
            label = f"{port.device} — {port.description}"
            ttk.Checkbutton(ports_card, text=label, variable=var).pack(anchor="w", pady=2)
            self.probe_port_vars[port.device] = var

        self.probe_status_label = ttk.Label(frame, text="", style="Muted.TLabel")
        self.probe_status_label.pack(anchor="w", pady=(4, 4))

        ttk.Label(frame, text="Confirmed scanners:", font=(FONT, 9, "bold")).pack(anchor="w")
        self.confirmed_label = ttk.Label(frame, text="(none yet)", style="Muted.TLabel")
        self.confirmed_label.pack(anchor="w", pady=(0, 8))

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(4, 0))

        self.probe_start_button = ttk.Button(
            button_row, text="Start Detection", command=self._start_detection
        )
        self.probe_start_button.pack(side="left")

        self.probe_done_button = ttk.Button(
            button_row, text="Done", command=self._finish_detection, state="disabled"
        )
        self.probe_done_button.pack(side="left", padx=(8, 0))

        if self.db.serial_ports:
            ttk.Button(
                button_row, text="Cancel", style="Secondary.TButton", command=self._cancel_detection
            ).pack(side="left", padx=(8, 0))

        self.probe_log_list = tk.Listbox(frame, height=8)
        self._style_listbox(self.probe_log_list)
        self.probe_log_list.pack(fill="both", expand=True, pady=(12, 0))

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
        self.probe_done_button.config(state="normal")

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
        self.probe_log_list.insert(0, f"[{timestamp}] {message}")

    # --- MAIN SCANNER SCREEN --------------------------------------------

    def _show_scanner_screen(self):
        self._clear_window()

        if not self.db.serial_ports:
            self._show_scanner_setup_screen()
            return

        self._header("Scanner Companion")
        self._build_menu_bar()

        # --- STATUS BAR COMPONENT ---
        self.status_bar = tk.Frame(self, bd=0, relief=tk.SUNKEN, bg=COLORS["card"])
        self.status_bar.pack(side=tk.BOTTOM, fill="x")

        # Dynamic connection icon indicator 
        self.status_icon = tk.Label(self.status_bar, text="…", font=(FONT, 10, "bold"), bg=COLORS["card"])
        self.status_icon.pack(side=tk.LEFT, padx=(10, 2), pady=4)

        # Dynamic action and feedback message label
        self.status_label = tk.Label(self.status_bar, text="Waiting for scan...", font=(FONT, 10), bg=COLORS["card"], fg=COLORS["text_muted"])
        self.status_label.pack(side=tk.LEFT, padx=4, pady=4)
        # ---------------------------------------

        log_frame = ttk.Frame(self, padding=20)
        log_frame.pack(fill="both", expand=True, padx=20, pady=20)
        ttk.Label(log_frame, text="Scan Log", font=(FONT, 10, "bold")).pack(anchor="w")

        list_container = tk.Frame(log_frame, bg=COLORS["card"])
        list_container.pack(fill="both", expand=True)

        self.log_list = tk.Listbox(list_container)
        self._style_listbox(self.log_list)
        self.log_list.pack(fill="both", expand=True) # Fills the entire log card container
        

        def _on_mouse_wheel(event):
            # Windows sends mouse delta movements in factors of 120
            # Dividing by -120 converts it into a uniform single line scroll step
            self.log_list.yview_scroll(int(-1 * (event.delta / 120)), "units")
            
        self.log_list.bind("<MouseWheel>", _on_mouse_wheel)
        
        self._start_reader_threads()
        self._log(f"Signed in. Listening on: {', '.join(self.db.serial_ports)}")
        self.after(100, self._poll_queue)


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
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def _update_port_status(self, port: str, status: str):
        self.port_statuses[port] = status
        idx = self.status_menu_indices.get(port)
        if idx is not None:
            icon = "✅" if status == "connected" else "❌"
            self.status_menu.entryconfig(idx, label=f"{port}   {icon} {status}")


        # 🔴 CASE 1: ANY port has failed or disconnected completely
        if any(s not in ("connected", "connecting") for s in self.port_statuses.values()):
            if hasattr(self, 'status_icon'):
                self.status_icon.config(text="🔴", fg=COLORS["error"])
            if hasattr(self, 'status_label'):
                self.status_label.config(text="Error with a scanner...", fg=COLORS["error"])
                
        # 🟢 CASE 2: ALL configured ports are perfectly connected
        elif all(s == "connected" for s in self.port_statuses.values()):
            if hasattr(self, 'status_icon'):
                self.status_icon.config(text="🟢", fg=COLORS["success"])
            if hasattr(self, 'status_label'):
                # Reset back to standard message if it was previously erroring
                self.status_label.config(text="Waiting for scan...", fg=COLORS["text_muted"])
                
        # 🟡 CASE 3: Ports are currently attempting a hand-shake/reconnection loop
        else:
            if hasattr(self, 'status_icon'):
                self.status_icon.config(text="…", fg=COLORS["warning"])
            if hasattr(self, 'status_label'):
                self.status_label.config(text="Connecting to hardware...", fg=COLORS["text_muted"])


    def _clear_scan_log(self):
        """Deletes all entries inside the main scan log view."""
        if hasattr(self, 'log_list'):
            self.log_list.delete(0, tk.END)

    def _logout(self):
        if not messagebox.askyesno("Log out", "Are you sure you want to log out?"):
            return
        self._stop_reader_threads()
        self.db.sign_out()
        self._show_login_screen()


    def _handle_scan(self, raw_scan: str):
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

        if status in error_states:
            self._play_feedback_sound(is_success=False) # Play warning buzz
        elif status != "debounced":
            self._play_feedback_sound(is_success=True)  # Play success chirp

        colors = {
            "checked_in": COLORS["success"],
            "checked_out": COLORS["primary_dark"], # A slightly darker blue for contrast text readability
            "already_done": COLORS["text_muted"],
            "not_found": COLORS["error"],
            "invalid": COLORS["warning"],
            "debounced": COLORS["text_muted"],
            "error": COLORS["error"],
        }
        # Change foreground to fg here
        self.status_label.config(text=message, fg=colors.get(status, COLORS["text"]))

        if status != "debounced":
            self._log(message)

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
        """Automatically launches the documentation file or a web link in the user's browser."""



        if os.path.exists("README.txt"):
            os.startfile("README.txt")
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
            "If the bottom health status bar continues to display a red '🔴', "
            "please contact jackli6140@gmail.com with the details."
        )
        messagebox.showinfo("Hardware Troubleshooting Guide", tips)

    def _log(self, message: str):
        timestamp = datetime.now().strftime("%I:%M:%S %p")
        self.log_list.insert(0, f"[{timestamp}] {message}")

        if self.log_list.size() > 200:
            self.log_list.delete(tk.END)

        self.log_list.see(0)

    def _export_scan_log(self):
        """Saves the current live scan log items into a clean text file."""

        
        # Check if there is actually anything to export
        if not hasattr(self, 'log_list') or self.log_list.size() == 0:
            messagebox.showwarning("Export Failed", "The scan log is currently empty.")
            return
            
        # Open a standard Windows "Save As" popup window
        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
            title="Export Scan Log",
            initialfile=f"scan_log_{datetime.now().strftime('%Y-%m-%d')}.txt"
        )
        
        # Write the listbox contents to the selected file
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as file:
                    # Get all items from the listbox (from index 0 to the very end)
                    log_items = self.log_list.get(0, tk.END)
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
                if remote_version > CURRENT_VERSION:
                    # Prompt the user with a standard confirmation popup dialog box
                    msg = f"A new software update (v{remote_version}) is available!\n\nWhat's New:\n{data.get('changelog')}\n\nWould you like to download and apply the update now?"
                    if messagebox.askyesno("Update Available", msg):
                        self._apply_background_update(download_url)
                else:
                    if not silent:
                        messagebox.showinfo("Up to Date", "You are running the latest version of Scanner Companion.")
        except Exception as e:
            if not silent:
                messagebox.showerror("Update Error", f"Failed to connect to the update server:\n{e}")

    def _apply_background_update(self, download_url):
        """Downloads the new binary executable into a temp folder and triggers the file-swap batch handler."""

        
        try:
            # Update footer display state visually so the user doesn't close it mid-stream
            self.status_label.config(text="📥 Downloading software update... Please wait.", fg=COLORS["warning"])
            self.update()

            r = requests.get(download_url, stream=True, timeout=30)
            if r.status_code == 200:
                # Path configurations
                exe_path = os.path.abspath(sys.argv[0])          # Path to current running .exe
                current_dir = os.path.dirname(exe_path)
                temp_new_exe = os.path.join(current_dir, "new_version.tmp")
                bat_script_path = os.path.join(current_dir, "update_process.bat")
                
                with open(temp_new_exe, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        
                # Create the automated Windows Batch background loop to overwrite the locked exe file
                # The loop tries to delete the old application until it closes, then renames the temp file
                bat_content = f"""
                @echo off
                :loop
                taskkill /F /PID {os.getpid()} >nul 2>&1
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
                    
                # Launch the script completely decoupled from the main process pipeline, then kill the app
                subprocess.Popen([bat_script_path], shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)
                self.destroy()
                sys.exit()
                
        except Exception as e:
            messagebox.showerror("Update Interrupted", f"Could not complete installation:\n{e}")
            self.status_label.config(text="Waiting for scan...", fg=COLORS["text_muted"])

    def _play_feedback_sound(self, is_success=True):
        """Plays a native Windows beep pattern if audio feedback is enabled."""
        if not self.sound_enabled.get():
            return  # Exit instantly if the user toggled sounds off
            
        
        def run_sound():
            try:
                if is_success:
                    # High pitched, quick chirp (Frequency 1000Hz, Duration 150ms)
                    winsound.Beep(1000, 150)
                else:
                    # Lower pitched, longer warning buzzer sound (Frequency 400Hz, Duration 400ms)
                    winsound.Beep(400, 400)
            except Exception:
                pass # Fail silently if system audio drivers are locked down
                
        # Spawn the audio execution in a micro-thread branch so the main UI loop 
        # doesn't freeze for a split second while the speaker card activates
        threading.Thread(target=run_sound, daemon=True).start()

    def _clear_window(self):
        for widget in self.winfo_children():
            widget.destroy()

    def _on_close(self):
        self.stop_event.set()
        self.probe_stop_event.set()
        self.db.stop()  # stop the token-refresh background thread too
        self.destroy()


if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()