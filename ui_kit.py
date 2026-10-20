"""Modern UI components for Scanner Companion (CustomTkinter)."""
import re
import sys
import tkinter as tk
from datetime import datetime

import customtkinter as ctk

FONT = "Segoe UI" if sys.platform == "win32" else "Helvetica Neue"
MONO = "Consolas" if sys.platform == "win32" else "Menlo"

LOG_STYLE = {"pills": False, "terminal": False}

COLORS = {
    "bg": "#0f0f12",
    "card": "#18181c",
    "card_alt": "#222228",
    "border": "#2a2a31",
    "text": "#f4f4f5",
    "text_muted": "#a1a1aa",
    "primary": "#6366f1",
    "primary_dark": "#4f46e5",
    "primary_light": "#c7d2fe",
    "on_primary": "#ffffff",
    "success": "#22c55e",
    "error": "#ef4444",
    "warning": "#f59e0b",
    "info": "#818cf8",
    "log_text": "#d4d4d8",
}


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


def popup(widget, menu):
    """Drop a tk.Menu just under a widget."""
    x = widget.winfo_rootx()
    y = widget.winfo_rooty() + widget.winfo_height() + 4
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


_registry = {"panels": [], "bound": set()}


def _register(panel, host):
    _registry["panels"].append(panel)
    if id(host) not in _registry["bound"]:
        host.bind("<Button-1>", _dispatch_click, add="+")
        _registry["bound"].add(id(host))


def _dispatch_click(event):
    alive = []
    for p in _registry["panels"]:
        try:
            if not p.winfo_exists():
                continue
            alive.append(p)
            p._maybe_close(event.widget)
        except Exception:
            pass
    _registry["panels"] = alive


def set_log_style(pills: bool, terminal: bool):
    LOG_STYLE["pills"], LOG_STYLE["terminal"] = bool(pills), bool(terminal)


def _tint(hex_color, base=COLORS["card"], amount=0.12):
    """Blend a color into the card background for a subtle wash."""
    c = hex_color.lstrip("#"); b = base.lstrip("#")
    mix = [round(int(b[i:i+2], 16) * (1 - amount) + int(c[i:i+2], 16) * amount) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(mix)


class Popover(ctk.CTkFrame):
    """Dark dropdown anchored under a button. Closes on any click outside it."""

    def __init__(self, host, align="left"):
        super().__init__(host, fg_color=COLORS["card"], corner_radius=12,
                         border_width=1, border_color=COLORS["border"])
        self._host = host
        self._anchor = None
        self._align = align
        _register(self, host)

    def toggle(self, anchor):
        if self.winfo_ismapped():
            self.hide()
        else:
            self.show(anchor)

    def show(self, anchor):
        self._anchor = anchor
        self.update_idletasks()
        host = self._host
        w = self.winfo_reqwidth()
        x = anchor.winfo_rootx() - host.winfo_rootx()
        if self._align == "right":
            x += anchor.winfo_width() - w
        x = max(8, min(x, host.winfo_width() - w - 8))
        y = anchor.winfo_rooty() - host.winfo_rooty() + anchor.winfo_height() + 8
        self.place_configure(x=x, y=y)  # raw Tk place: real pixels, no extra DPI scaling
        self.lift()

    def hide(self):
        self.place_forget()

    def _maybe_close(self, widget):
        if not self.winfo_ismapped():
            return
        w = widget
        while w is not None:       # clicks on the panel or its own button are handled elsewhere
            if w is self or w is self._anchor:
                return
            w = getattr(w, "master", None)
        self.hide()


class MenuPanel(Popover):
    """Drop-in for tk.Menu: add_command / add_checkbutton / add_separator."""

    def __init__(self, host, min_width=230, align="left"):
        super().__init__(host, align=align)
        self._min_width = min_width
        self._items = ctk.CTkFrame(self, fg_color="transparent")
        self._items.pack(padx=6, pady=8)

    def _run(self, command):
        self.hide()
        if command:
            # wait for the button's click animation to finish before the command
            # possibly rebuilds the whole screen
            self._host.after(120, command)

    def add_command(self, label="", command=None, **_ignored):
        ctk.CTkButton(self._items, text=label, anchor="w", width=self._min_width - 12, height=34,
                      corner_radius=8, fg_color="transparent", hover_color=COLORS["card_alt"],
                      text_color=COLORS["text"], font=font(13),
                      command=lambda: self._run(command)).pack(fill="x", pady=1)

    def add_checkbutton(self, label="", variable=None, command=None, **_ignored):
        def toggled():
            self.hide()
            if command:
                command()
        ctk.CTkSwitch(self._items, text=label, variable=variable, onvalue=True, offvalue=False,
                      command=toggled, font=font(13), text_color=COLORS["text"],
                      progress_color=COLORS["primary"], fg_color=COLORS["border"]
                      ).pack(anchor="w", padx=12, pady=9)

    def add_separator(self):
        tk.Frame(self._items, height=1, bg="#3f3f46").pack(fill="x", padx=10, pady=6)

class StatusText(ctk.CTkLabel):
    """CTkLabel that also accepts the old tk-style config(text=..., fg=...)."""

    def __init__(self, master, text="", color=None, size=12, **kw):
        super().__init__(master, text=text, text_color=color or COLORS["text_muted"],
                         font=font(size), **kw)

    def config(self, **kw):
        if "fg" in kw:
            kw["text_color"] = kw.pop("fg")
        if "foreground" in kw:
            kw["text_color"] = kw.pop("foreground")
        self.configure(**kw)

class ActionButton(ctk.CTkButton):
    """CTkButton that also accepts the old .config(...) calls."""

    def config(self, **kw):
        self.configure(**kw)

class StatusPill(ctk.CTkButton):
    STATES = {
        "ok": ("●  Ready  ▾", "success"),
        "error": ("●  Error  ▾", "error"),
        "connecting": ("●  Connecting  ▾", "warning"),
    }

    def __init__(self, master, on_click=None):
        super().__init__(master, text="", width=118, height=34, corner_radius=17,
                         border_width=1, fg_color=COLORS["bg"], hover_color=COLORS["card_alt"],
                         font=font(12, "bold"), command=on_click)
        self.set_state("connecting")

    def set_state(self, state):
        text, key = self.STATES[state]
        self.configure(text=text, text_color=COLORS[key], border_color=COLORS[key])


class TopBar(ctk.CTkFrame):
    def __init__(self, master, title, version=""):
        super().__init__(master, fg_color=COLORS["card"], corner_radius=0, height=60)
        self.pack_propagate(False)
        # menus are packed first so they always keep their space on a narrow window
        self.right = ctk.CTkFrame(self, fg_color="transparent")
        self.right.pack(side="right", padx=12)
        ctk.CTkLabel(self, text=title, font=font(16, "bold"),
                     text_color=COLORS["text"]).pack(side="left", padx=(18, 0))

    def add_pill(self, on_click):
        pill = StatusPill(self.right, on_click)
        pill.pack(side="left", padx=(0, 8))
        return pill

    def add_menu(self, text, menu, width=84):
        btn = ctk.CTkButton(self.right, text=f"{text}  ▾", width=width, height=34, corner_radius=10,
                            fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                            text_color=COLORS["text"], font=font(12))
        btn.configure(command=lambda: menu.toggle(btn))
        btn.pack(side="left", padx=3)
        return btn

class LastScanCard(ctk.CTkFrame):
    """Big card showing the most recent scan result."""

    KINDS = {
        "checked_in": ("CHECKED IN", "success", "✓"),
        "checked_out": ("CHECKED OUT", "success", "→"),
        "already_done": ("ALREADY LOGGED", "text_muted", "•"),
        "not_found": ("NOT FOUND", "error", "✕"),
        "invalid": ("NOT RECOGNIZED", "error", "!"),
        "queued": ("SAVED OFFLINE", "text_muted", "…"),
        "error": ("ERROR", "error", "!"),
    }

    def __init__(self, master):
        super().__init__(master, fg_color=COLORS["card"], corner_radius=16,
                         border_width=1, border_color=COLORS["border"])
        self.badge = ctk.CTkLabel(self, text="", width=64, height=64, corner_radius=32,
                                  font=font(28, "bold"))
        self.badge.pack(side="left", padx=(20, 16), pady=20)
        col = ctk.CTkFrame(self, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, pady=16)
        self.kind_label = ctk.CTkLabel(col, text="", font=font(11, "bold"), anchor="w")
        self.kind_label.pack(fill="x")
        self.title_label = ctk.CTkLabel(col, text="", font=font(26, "bold"), anchor="w",
                                        justify="left", wraplength=440,
                                        text_color=COLORS["text"])
        self.title_label.pack(fill="x")
        self.sub_label = ctk.CTkLabel(col, text="", font=font(12), anchor="w",
                                      text_color=COLORS["text_muted"])
        self.sub_label.pack(fill="x")
        self.reset()

    def reset(self, title="Ready to scan", subtitle="Scan a barcode to check in or out"):
        self.badge.configure(text="◌", fg_color=COLORS["card_alt"], text_color=COLORS["text_muted"])
        self.kind_label.configure(text="")
        self.title_label.configure(text=title, text_color=COLORS["text"])
        self.sub_label.configure(text=subtitle)

    def show(self, status, message):
        label, key, glyph = self.KINDS.get(status, ("DONE", "text_muted", "•"))
        color = COLORS[key]
        neutral = key == "text_muted"
        text = re.sub(r"^[^\x00-\x7F]+\s*", "", message.strip())  # drop leading emoji
        title = text
        if status in ("checked_in", "checked_out") and ": " in text:
            head, title = text.split(": ", 1)
            if head.lower().startswith("staff"):
                label = "STAFF " + label
        self.badge.configure(text=glyph,
                             fg_color=COLORS["card_alt"] if neutral else color,
                             text_color=color if neutral else "#ffffff")
        self.kind_label.configure(text=label, text_color=color)
        self.title_label.configure(text=title, text_color=color)
        self.sub_label.configure(text=datetime.now().strftime("%I:%M:%S %p"))

class ScanLog(ctk.CTkScrollableFrame):
    """Drop-in for the old Listbox log (insert/delete/size/get/see), drawn as rows."""

    def __init__(self, master, height=220):
        super().__init__(master, fg_color=COLORS["card"], corner_radius=12,
                         border_width=1, border_color=COLORS["border"], height=height)
        self._items = []  # newest first: (text, row widget)
        self._mono = ctk.CTkFont(family=MONO, size=11)

    @staticmethod
    def _parse(text):
        m = re.match(r"^\[(.*?)\]\s*(.*)$", text)
        return (m.group(1), m.group(2)) if m else ("", text)

    @staticmethod
    def _color(msg):
        low = msg.lower()
        if msg.startswith(("✅", "👔", "👋", "📷")):
            return COLORS["success"]          # check-ins / check-outs
        if msg.startswith("⚠") or any(w in low for w in (
                "not found", "error", "failed", "unrecognized", "not recognized", "withdrawn")):
            return COLORS["error"]
        return COLORS["text_muted"]           # everything else is neutral

    @staticmethod
    def _tag(msg):
        """Short label for the pill, derived from the message text."""
        low = msg.lower()
        if "staff" in low and "clocked in" in low:
            return "STAFF IN"
        if "staff" in low and "clocked out" in low:
            return "STAFF OUT"
        if "checked in" in low:
            return "IN"
        if "checked out" in low:
            return "OUT"
        if any(w in low for w in ("not found", "error", "failed", "unrecognized",
                                  "not recognized", "withdrawn")):
            return "ERROR"
        if "photo" in low:
            return "PHOTO"
        if "offline" in low or "queued" in low:
            return "QUEUED"
        if "synced" in low:
            return "SYNC"
        if "scanner found" in low:
            return "SCANNER"
        return "INFO"

    def _build_row(self, text):
        stamp, msg = self._parse(text)
        color = self._color(msg)
        neutral = color == COLORS["text_muted"]
        tag = self._tag(msg)
        msg = re.sub(r"^[^\x00-\x7F]+\s*", "", msg)   # drop leading emoji

        pills, terminal = LOG_STYLE["pills"], LOG_STYLE["terminal"]
        accent = not pills and not terminal            # bar + tint is the default look

        row = ctk.CTkFrame(self, corner_radius=8,
                           fg_color=_tint(color) if accent and not neutral else "transparent")

        if terminal:
            ctk.CTkLabel(row, text=stamp, width=88, anchor="w", font=self._mono,
                         text_color=COLORS["text_muted"]).pack(side="left", padx=(10, 6))
        if pills:
            ctk.CTkLabel(row, text=tag, width=64, height=20, corner_radius=10,
                         font=font(10, "bold"), text_color=color,
                         fg_color=COLORS["card_alt"] if neutral else _tint(color, amount=0.22)
                         ).pack(side="left", padx=(0 if terminal else 10, 10), pady=6)
        if accent:
            ctk.CTkFrame(row, width=3,height=1 ,corner_radius=2,
                         fg_color=COLORS["border"] if neutral else color
                         ).pack(side="left", fill="y", padx=(8, 10), pady=7)

        ctk.CTkLabel(row, text=msg, font=font(12, "normal" if neutral or terminal else "bold"),
                     anchor="w", justify="left", wraplength=500 - (74 if pills else 0),
                     text_color=color).pack(side="left", fill="x", expand=True, pady=6)
        if not terminal:
            ctk.CTkLabel(row, text=stamp, font=font(11),
                         text_color=COLORS["text_muted"]).pack(side="right", padx=10)
        return row

    def insert(self, index, text):
        row = self._build_row(text)
        if self._items:
            row.pack(fill="x", pady=2, padx=2, before=self._items[0][1])
        else:
            row.pack(fill="x", pady=2, padx=2)
        self._items.insert(0, (text, row))

    def restyle(self):
        """Rebuild every row with the current LOG_STYLE (called when a toggle changes)."""
        texts = [t for t, _ in reversed(self._items)]   # oldest first
        for _, row in self._items:
            row.destroy()
        self._items = []
        for t in texts:
            self.insert(0, t)

    def _remove(self, i):
        if self._items:
            _, row = self._items.pop(i)
            row.destroy()

    def delete(self, first, last=None):
        if last is not None:            # delete(0, END): clear everything
            while self._items:
                self._remove(0)
        else:                           # delete(END): drop the oldest
            self._remove(len(self._items) - 1 if first == tk.END else int(first))

    def size(self):
        return len(self._items)

    def get(self, first, last=None):
        return tuple(t for t, _ in self._items)

    def see(self, index):
        try:
            self._parent_canvas.yview_moveto(0)
        except Exception:
            pass

class StatusPanel(Popover):
    """Dark popover under the status pill, one row per scanner port."""

    def __init__(self, host, ports, on_detect):
        super().__init__(host, align="right")
        self.rows = {}
        ctk.CTkLabel(self, text="SCANNERS", font=font(11, "bold"),
                     text_color=COLORS["text_muted"]).pack(anchor="w", padx=16, pady=(12, 4))
        for port in ports:
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=3)
            dot = ctk.CTkLabel(row, text="●", width=16, font=font(13), text_color=COLORS["warning"])
            dot.pack(side="left", anchor="n")
            ctk.CTkLabel(row, text=port, font=font(13, "bold"),
                         text_color=COLORS["text"]).pack(side="left", anchor="n", padx=(4, 10))
            status = ctk.CTkLabel(row, text="connecting…", font=font(12), anchor="w", justify="left",
                                  wraplength=190, text_color=COLORS["text_muted"])
            status.pack(side="left", fill="x", expand=True)
            self.rows[port] = (dot, status)
        ctk.CTkButton(self, text="Detect scanner…", height=32, corner_radius=8,
                      fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                      text_color=COLORS["text"], font=font(12),
                      command=lambda: (self.hide(), on_detect())).pack(fill="x", padx=16, pady=(8, 14))

    def set_status(self, port, status):
        if port not in self.rows:
            return
        dot, label = self.rows[port]
        if status == "connected":
            color, text = COLORS["success"], "Connected"
        elif status == "connecting":
            color, text = COLORS["warning"], "Connecting…"
        else:
            detail = status[7:] if status.startswith("error: ") else status
            color, text = COLORS["error"], f"Not connected\n{detail[:90]}"
        dot.configure(text_color=color)
        label.configure(text=text, text_color=color)

# --- themed replacements for tkinter's messagebox / simpledialog ---------------

class _Dialog(ctk.CTkToplevel):
    ICONS = {
        "info": ("i", "primary"),
        "question": ("?", "primary"),
        "warning": ("!", "warning"),
        "error": ("✕", "error"),
    }

    def __init__(self, parent, title, message, kind, buttons, cancel_value,
                 entry=False, secret=False):
        super().__init__(parent)
        self.configure(fg_color=COLORS["bg"])
        self.title(title)
        self.resizable(False, False)
        self._cancel_value = cancel_value
        self._default = next(v for _, v, s in buttons if s == "primary")  # Enter = the primary button
        self._entry = None
        self.result = cancel_value

        glyph, key = self.ICONS[kind]
        card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=14,
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=12, pady=12)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 6))
        ctk.CTkLabel(top, text=glyph, width=42, height=42, corner_radius=21,
                     font=font(18, "bold"), fg_color=COLORS[key],
                     text_color="#ffffff").pack(side="left", anchor="n")
        text = ctk.CTkFrame(top, fg_color="transparent")
        text.pack(side="left", fill="both", expand=True, padx=(14, 0))
        ctk.CTkLabel(text, text=title, font=font(15, "bold"), anchor="w",
                     text_color=COLORS["text"]).pack(fill="x")
        ctk.CTkLabel(text, text=message, font=font(13), anchor="w", justify="left",
                     wraplength=340, text_color=COLORS["text_muted"]).pack(fill="x", pady=(4, 0))

        if entry:
            self._entry = ctk.CTkEntry(card, show="*" if secret else "", width=340, height=38,
                                       corner_radius=10, font=font(13),
                                       fg_color=COLORS["bg"], border_color=COLORS["border"])
            self._entry.pack(padx=20, pady=(8, 4))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(14, 18))
        for label, value, style in reversed(buttons):   # last button ends up rightmost
            primary = style == "primary"
            ctk.CTkButton(row, text=label, width=96, height=36, corner_radius=10,
                          font=font(13, "bold" if primary else "normal"),
                          fg_color=COLORS["primary"] if primary else COLORS["card_alt"],
                          hover_color=COLORS["primary_dark"] if primary else COLORS["border"],
                          text_color=COLORS["on_primary"] if primary else COLORS["text"],
                          command=lambda v=value: self._finish(v)).pack(side="right", padx=(8, 0))

        self.bind("<Return>", lambda e: self._finish(self._default))
        self.bind("<Escape>", lambda e: self._finish(self._cancel_value))
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(self._cancel_value))

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        if parent.winfo_ismapped():
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        else:
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 2
        self.wm_geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")  # raw Tk call: real pixels
        self.transient(parent)
        self.after(30, self._activate)

    def _activate(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
            if self._entry is not None:
                self._entry.focus()
        except Exception:
            pass

    def _finish(self, value):
        if self._entry is not None and value is True:
            self.result = self._entry.get()
        else:
            self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def _ask(parent, title, message, kind, buttons, cancel_value, entry=False, secret=False):
    parent = parent or tk._default_root
    dlg = _Dialog(parent, title, message, kind, buttons, cancel_value, entry, secret)
    parent.wait_window(dlg)   # blocks like the old messagebox, so callers don't change
    return dlg.result


class _MessageBox:
    @staticmethod
    def showinfo(title, message, parent=None, **_):
        _ask(parent, title, message, "info", [("OK", True, "primary")], True)

    @staticmethod
    def showwarning(title, message, parent=None, **_):
        _ask(parent, title, message, "warning", [("OK", True, "primary")], True)

    @staticmethod
    def showerror(title, message, parent=None, **_):
        _ask(parent, title, message, "error", [("OK", True, "primary")], True)

    @staticmethod
    def askyesno(title, message, parent=None, **_):
        return bool(_ask(parent, title, message, "question",
                         [("Yes", True, "primary"), ("No", False, "secondary")], False))


class _SimpleDialog:
    @staticmethod
    def askstring(title, prompt, parent=None, show=None, **_):
        return _ask(parent, title, prompt, "question",
                    [("OK", True, "primary"), ("Cancel", None, "secondary")], None,
                    entry=True, secret=bool(show))

def center_over(win, parent):
    """Size a window to its content and centre it over its parent (raw Tk call: real pixels)."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    if parent.winfo_ismapped():
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
    win.wm_geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")


class ListPicker(ctk.CTkToplevel):
    """Dark picker window: a scrollable list of two-line choices."""

    def __init__(self, parent, title, subtitle, items, on_pick):
        super().__init__(parent)
        self.withdraw()
        self.configure(fg_color=COLORS["bg"])
        self.title(title)
        self.resizable(False, False)
        self._parent, self._on_pick = parent, on_pick

        card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=14,
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(card, text=title, font=font(16, "bold"), anchor="w",
                     text_color=COLORS["text"]).pack(fill="x", padx=20, pady=(18, 0))
        ctk.CTkLabel(card, text=subtitle, font=font(12), anchor="w",
                     text_color=COLORS["text_muted"]).pack(fill="x", padx=20, pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(card, fg_color="transparent", width=380,
                                        height=min(340, 62 * len(items) + 8))
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        for i, (name, sub) in enumerate(items):
            btn = ctk.CTkButton(scroll, text=f"{name}\n{sub}", anchor="w", height=54, corner_radius=10,
                                border_spacing=14,
                                fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                                text_color=COLORS["text"], font=font(13),
                                command=lambda i=i: self._choose(i))
            try:
                btn._text_label.configure(justify="left")  # left-align both lines, not center
            except Exception:
                pass
            btn.pack(fill="x", pady=3)
        ctk.CTkButton(card, text="Cancel", height=36, corner_radius=10, font=font(13),
                      fg_color="transparent", hover_color=COLORS["card_alt"],
                      text_color=COLORS["text_muted"], command=self.destroy
                      ).pack(fill="x", padx=20, pady=(4, 16))

        self.bind("<Escape>", lambda e: self.destroy())
        center_over(self, parent)
        self.transient(parent)
        self.deiconify()
        self.after(100, self._activate)

    def _activate(self):
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _choose(self, i):
        self.destroy()
        self._parent.after(60, lambda: self._on_pick(i))  # let this window close first


messagebox = _MessageBox()
simpledialog = _SimpleDialog()