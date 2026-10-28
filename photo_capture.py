"""
photo_capture.py - prototype: scan a student -> photograph book pages -> per-student Google Drive folder.

Setup
  pip install opencv-python pillow supabase tomli google-api-python-client google-auth-oauthlib
  Google Cloud console: create a project, enable the Google Drive API, configure the OAuth consent
  screen (add Google account as a test user), create an OAuth client of type "Desktop app",
  and save it as credentials.json next to this file.
  First run opens a browser to log in as the Google account that should own the photos.

Run the demo:  python photo_capture.py
Integrate:     import DriveStore, UploadWorker, PhotoDialog; open a PhotoDialog from the Tk main thread
               when a scan arrives (see the demo at the bottom for the queue hand-off).
"""
import io
import os
import queue
import threading
import time
import tkinter as tk
import subprocess
import sys
import tempfile
import webbrowser
from collections import OrderedDict

from datetime import datetime
from tkinter import ttk
from ui_kit import messagebox

from scanner_db import BASE_DIR as BASE, DAY_NAMES
import customtkinter as ctk
from ui_kit import COLORS, FONT, font, StatusText, center_over

import cv2
from PIL import Image, ImageTk, ImageDraw, ImageFont

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.auth.exceptions import RefreshError

import logging
log = logging.getLogger("photos")

DEFAULT_THEME = {
    "colors": {"bg": "#121214", "card": "#1a1a1e", "border": "#27272a", "text": "#f4f4f5",
               "text_muted": "#a1a1aa", "primary": "#4f46e5", "primary_light": "#DBEAFE",
               "on_primary": "#FFFFFF", "success": "#10b981", "warning": "#f59e0b"},
    "font": "Segoe UI",
}

SCOPES = ["https://www.googleapis.com/auth/drive.file"]  # app can only touch files it created
ROOT_NAME = "Student Book Photos"
KEEP_COPIES = 1000          # per student; 
CONFIRM_PHOTO = True     # toggle: preview + keep/retake before saving
CAMERA_INDEX = 0

FOLDER_MIME = "application/vnd.google-apps.folder"

PENDING_DIR = os.path.join(BASE, "pending_photos")
CREDS_FILE = os.path.join(BASE, "credentials.json")
TOKEN_FILE = os.path.join(BASE, "token.json")

# students table columns 
BARCODE_COL, FIRST_COL, LAST_COL = "barcode_code", "first_name", "last_name"
MATH_COL, READING_COL = "math", "reading"  # a Y (or true) means the student does that subject
SHOT_TYPES = [("Planning", False), ("Scores", True)]   # (folder suffix, allow several photos)

LAST_KEY = "last_photo"   # appProperties key on each student folder, e.g. 2026-10-21T14:03:11
SORT_OPTIONS = ["A–Z", "Longest ago", "Most recent"]
STALE_WARN_DAYS = 4     # amber from here
STALE_BAD_DAYS = 14     # red from here (and for "Never")

MAX_DAYS = 2    # selecting a third day switches off the oldest selected one
MIN_PILLS = 2   # rows with fewer days are padded with "n/a" pills

# day -> (fill, text, outline or None)
PILL_COLORS = {
    "Mon": ("#facc15", "#422006", None),
    "Tue": ("#ffffff", "#18181b", "#d4d4d8"),
    "Wed": ("#000000", "#ffffff", "#71717a"),
    "Thu": ("#22c55e", "#052e16", None),
    "Sat": ("#dc2626", "#ffffff", None),
    "n/a": ("#3f3f46", "#a1a1aa", None),
}
PILL_FALLBACK = ("#a1a1aa", "#18181b", None)   # for Fri/Sun if you bring them back

SUBJECT_ORDER = ["Math", "Reading"]
SUBJECT_LETTER = {"Math": "M", "Reading": "R"}
SUBJECT_COLORS = {"Math": ("#3b82f6", "#ffffff", None), "Reading": ("#a855f7", "#ffffff", None)}
KIND_COLORS = {"Planning": ("#06b6d4", "#083344", None), "Scores": ("#ec4899", "#ffffff", None)}

def _pill_font(px):
    for name in ("seguisb.ttf", "segoeui.ttf", "Arial Bold.ttf", "Arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()

import json
import keyring
from keyring.errors import PasswordDeleteError
import requests
import glob

from google_secrets import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET

CLIENT_CONFIG = {"installed": {
    "client_id": GOOGLE_CLIENT_ID,
    "client_secret": GOOGLE_CLIENT_SECRET,
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}

def has_saved_login() -> bool:
    try:
        return bool(keyring.get_password(KR_SERVICE, KR_USER))
    except Exception:
        return False

def disconnect_google() -> None:
    """Revokes this computer's Google access (best effort), then deletes the saved login
    and any full-size photos that were opened into the temp folder."""
    raw = None
    try:
        raw = keyring.get_password(KR_SERVICE, KR_USER)
    except Exception:
        pass
    if raw:
        try:
            info = json.loads(raw)
            token = info.get("refresh_token") or info.get("token")
            requests.post("https://oauth2.googleapis.com/revoke", params={"token": token},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        except Exception:
            pass  # offline or already revoked: still remove the local copy
    try:
        keyring.delete_password(KR_SERVICE, KR_USER)
    except (PasswordDeleteError, Exception):
        pass
    for p in glob.glob(os.path.join(tempfile.gettempdir(), "scanner_photo_*.jpg")):
        try:
            os.remove(p)
        except OSError:
            pass

KR_SERVICE, KR_USER = "ScannerCompanion", "google-drive"


def test_camera(index) -> bool:
    cam = _open_camera(index)
    try:
        return bool(cam.isOpened() and cam.read()[0])
    finally:
        cam.release()

def _open_camera(index):
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    return cv2.VideoCapture(index, backend)


class DriveStore:
    def __init__(self, keep_copies=KEEP_COPIES):
        self._lock = threading.Lock()
        self.keep = keep_copies
        self._cache = {}  # key -> (folder_id, name): saves Drive round trips
        self.svc = build("drive", "v3", credentials=self._creds(), cache_discovery=False)
        self.root_id = self._find_or_create("__root__", ROOT_NAME, None)

    @staticmethod
    def _creds():
        creds = None
        raw = keyring.get_password(KR_SERVICE, KR_USER)
        if raw:
            creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    creds = None
                    try:
                        keyring.delete_password(KR_SERVICE, KR_USER)
                    except PasswordDeleteError:
                        pass

            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
                creds = flow.run_local_server(port=0)

            keyring.set_password(KR_SERVICE, KR_USER, creds.to_json())
        return creds

    def _find_or_create(self, key, name, parent):
        """Folders are found by a hidden property (the StudentID), not by name, so renames are safe."""
        cached = self._cache.get(key)
        if cached and cached[1] == name:
            return cached[0]
        q = (f"appProperties has {{ key='kt_key' and value='{key}' }} "
             f"and mimeType='{FOLDER_MIME}' and trashed=false")
        if parent:
            q += f" and '{parent}' in parents"
        hits = self.svc.files().list(q=q, fields="files(id,name)").execute()["files"]
        if hits:
            f = hits[0]
            if f["name"] != name:  # name changed in the database -> rename the folder
                self.svc.files().update(fileId=f["id"], body={"name": name}).execute()
            self._cache[key] = (f["id"], name)
            return f["id"]
        body = {"name": name, "mimeType": FOLDER_MIME, "appProperties": {"kt_key": key}}
        if parent:
            body["parents"] = [parent]
        fid = self.svc.files().create(body=body, fields="id").execute()["id"]
        self._cache[key] = (fid, name)
        return fid

    def upload(self, *args, **kwargs):
        with self._lock:
            return self._upload(*args, **kwargs)

    def _upload(self, student_id, last, first, subject, jpeg, barcode=None, taken_at=None):
        """Saves to <root>/<First, Last (ID)>/<subject>/ and keeps only the newest N photos per subject."""
        try:
            label = student_id
            student = self._find_or_create(f"student:{student_id}", f"{first}, {last} ({label})", self.root_id)
            folder = self._find_or_create(f"student:{student_id}:{subject}", subject, student)
            stamp = datetime.fromtimestamp(taken_at) if taken_at else datetime.now()
            name = stamp.strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"
            media = MediaIoBaseUpload(io.BytesIO(jpeg), mimetype="image/jpeg")
            self.svc.files().create(body={"name": name, "parents": [folder]}, media_body=media, fields="id").execute()
            try:
                self._set_last_photo(student, stamp)
            except Exception as e:   # never let this trigger a re-upload of the photo
                log.warning("Couldn't stamp last-photo date: %s", e)
            files = self.svc.files().list(q=f"'{folder}' in parents and trashed=false",
                                          orderBy="createdTime desc", fields="files(id)").execute()["files"]
            for old in files[self.keep:]:
                self.svc.files().delete(fileId=old["id"]).execute()
        except Exception:
            self._cache.clear()  # a folder may have been moved or deleted in Drive: look up fresh on retry
            raise

    def list_children(self, parent_id, folders_only=False, newest_first=False):
        """One API call: everything directly inside a folder (up to 1000 items)."""
        q = f"'{parent_id}' in parents and trashed=false"
        if folders_only:
            q += f" and mimeType='{FOLDER_MIME}'"
        with self._lock:
            resp = self.svc.files().list(
                q=q, pageSize=1000,
                orderBy="createdTime desc" if newest_first else "name_natural",
                fields="files(id,name,mimeType,createdTime,appProperties)",
            ).execute(num_retries=3)
        return resp.get("files", [])

    def download(self, file_id):
        """One API call: the file's bytes."""
        with self._lock:
            return self.svc.files().get_media(fileId=file_id).execute(num_retries=3)
        
    def _set_last_photo(self, student_folder, when):
        """Keeps the newest date only, so retried older photos can't move it backwards."""
        iso = when.strftime("%Y-%m-%dT%H:%M:%S")
        cur = self.svc.files().get(fileId=student_folder, fields="appProperties").execute()
        if (cur.get("appProperties") or {}).get(LAST_KEY, "") >= iso:
            return
        self.svc.files().update(fileId=student_folder,
                                body={"appProperties": {LAST_KEY: iso}}).execute()

    def delete_photo(self, file_id):
        with self._lock:
            self.svc.files().delete(fileId=file_id).execute()

    def backfill_last_photo(self, student_folder, clear_if_empty=False):
        """One-time: finds the newest photo across a student's subject folders (by file name,
        which is the timestamp), saves it on the folder, and returns it. None if no photos."""
        newest = None
        with self._lock:
            subs = self.svc.files().list(
                q=f"'{student_folder}' in parents and trashed=false and mimeType='{FOLDER_MIME}'",
                fields="files(id)", pageSize=100).execute(num_retries=3).get("files", [])
            for sub in subs:
                hit = self.svc.files().list(
                    q=f"'{sub['id']}' in parents and trashed=false and mimeType!='{FOLDER_MIME}'",
                    orderBy="name desc", pageSize=1,
                    fields="files(name)").execute(num_retries=3).get("files", [])
                if not hit:
                    continue
                try:
                    t = datetime.strptime(hit[0]["name"].rsplit(".", 1)[0], "%Y-%m-%d_%H-%M-%S")
                except ValueError:
                    continue
                if newest is None or t > newest:
                    newest = t
            if newest:
                self.svc.files().update(
                    fileId=student_folder,
                    body={"appProperties": {LAST_KEY: newest.strftime("%Y-%m-%dT%H:%M:%S")}}
                ).execute()
            elif clear_if_empty:                  # only after a delete, not on every page load
                self.svc.files().update(
                    fileId=student_folder, body={"appProperties": {LAST_KEY: None}}
                ).execute()
        return newest


def _jpeg(frame):
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


class PhotoDialog(ctk.CTkToplevel):
    """Live camera preview. Calls on_done(jpeg_bytes or None) when finished.
    Keys: Space = capture, Enter = keep, R = retake, Esc = cancel."""

    PREVIEW_W = 640  # raise to ~800 for a bigger preview if the screen allows

    def __init__(self, parent, label, on_done, stream, confirm=CONFIRM_PHOTO, subtitle="",
                theme=None, zoom=1.0, on_zoom=None, multi=False, tags=None):
        super().__init__(parent)
        self.withdraw()
        self.configure(fg_color=COLORS["bg"])
        self.title("Take Photo")
        self.resizable(False, False)
        self.on_done, self.confirm = on_done, confirm
        self.shot, self.frozen, self.closed = None, False, False
        self.zoom, self._start_zoom, self.on_zoom = zoom, zoom, on_zoom
        self._tick_job = self._act_job = None
        self.stream = stream
        self.multi = multi

        if not stream.ok:
            messagebox.showerror("Camera", "Camera not found", parent=parent)
            self.finish(None)
            return

        card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=16,
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=12, pady=12)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 10))
        names = ctk.CTkFrame(head, fg_color="transparent")
        names.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(names, text=label, font=font(20, "bold"), anchor="w",
                     text_color=COLORS["text"]).pack(fill="x")
        if tags:
            prow = ctk.CTkFrame(names, fg_color="transparent")
            prow.pack(fill="x", pady=(6, 2))
            for text, fill, tcolor in tags:
                ctk.CTkLabel(prow, text=text, width=104, height=28, corner_radius=14,
                             font=font(13, "bold"), fg_color=fill, text_color=tcolor
                             ).pack(side="left", padx=(0, 8))
        if subtitle:
            ctk.CTkLabel(names, text=subtitle, font=font(12), anchor="w",
                         text_color=COLORS["text_muted"]).pack(fill="x")
        self.state_label = ctk.CTkLabel(head, text="", width=100, height=28, corner_radius=14,
                                        font=font(11, "bold"))
        self.state_label.pack(side="right")

        # coloured frame around the preview (green = live, amber = reviewing)
        self.box = ctk.CTkFrame(card, fg_color=COLORS["bg"], corner_radius=12,
                                border_width=3, border_color=COLORS["success"])
        self.box.pack(padx=20)
        cam_w, cam_h = stream.size()
        self.preview_w = self._fit_preview_width(cam_w / cam_h)
        self._pv_h = int(self.preview_w * cam_h / cam_w)
        self._blank = tk.PhotoImage(width=self.preview_w, height=self._pv_h)
        self.view = tk.Label(self.box, image=self._blank, bg=COLORS["bg"], bd=0)
        self.view.pack(padx=6, pady=6)

        zrow = ctk.CTkFrame(card, fg_color="transparent")
        zrow.pack(pady=(10, 0))
        ctk.CTkLabel(zrow, text="Zoom", font=font(12),
                     text_color=COLORS["text_muted"]).pack(side="left", padx=(0, 10))
        self.zoom_slider = ctk.CTkSlider(zrow, from_=1.0, to=4.0, number_of_steps=30, width=300,
                                         progress_color=COLORS["primary"], command=self._set_zoom)
        self.zoom_slider.set(self.zoom)
        self.zoom_slider.pack(side="left")
        self.zoom_label = ctk.CTkLabel(zrow, text=f"{self.zoom:.1f}×", width=44, font=font(12, "bold"),
                                       text_color=COLORS["text"])
        self.zoom_label.pack(side="left", padx=(10, 0))

        self.bar = ctk.CTkFrame(card, fg_color="transparent")
        self.bar.pack(pady=(16, 6))

        def btn(text, cmd, kind):
            fg, hover, tc = {
                "primary": (COLORS["primary"], COLORS["primary_dark"], COLORS["on_primary"]),
                "success": (COLORS["success"], "#16a34a", "#ffffff"),
                "secondary": (COLORS["card_alt"], COLORS["border"], COLORS["text"]),
            }[kind]
            return ctk.CTkButton(self.bar, text=text, command=cmd, width=150, height=44,
                                 corner_radius=12, fg_color=fg, hover_color=hover, text_color=tc,
                                 font=font(14, "normal" if kind == "secondary" else "bold"))

        self.buttons = {
            "capture": btn("Capture", self.capture, "primary"),
            "keep": btn("Keep", self.keep, "success"),
            "more": btn("Keep + more", self.keep_more, "success"),
            "retake": btn("Retake", self.retake, "secondary"),
            "cancel": btn("Skip", lambda: self.finish(None), "secondary"),
        }

        # key binding, next to the others
        self.bind("<a>", lambda e: self.keep_more())
        self.hint = ctk.CTkLabel(card, text="", font=font(11), text_color=COLORS["text_muted"])
        self.hint.pack(pady=(0, 16))
        self._set_mode(reviewing=False)

        self.bind("<space>", lambda e: self.capture())
        self.bind("<Return>", lambda e: self.keep())
        self.bind("<r>", lambda e: self.retake())
        self.bind("<Escape>", lambda e: self.finish(None))
        for key in ("<plus>", "<equal>", "<KP_Add>"):
            self.bind(key, lambda e: self._set_zoom(self.zoom + 0.5))
        for key in ("<minus>", "<KP_Subtract>"):
            self.bind(key, lambda e: self._set_zoom(self.zoom - 0.5))
        self.view.bind("<MouseWheel>", lambda e: self._set_zoom(self.zoom + (0.2 if e.delta > 0 else -0.2)))
        self.protocol("WM_DELETE_WINDOW", lambda: self.finish(None))

        center_over(self, parent)
        self.transient(parent)
        self.deiconify()
        self._act_job = self.after(100, self._activate)
        self._tick()

    def _activate(self):
        self._act_job = None
        if self.closed:
            return
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _layout(self, names):
        for b in self.buttons.values():
            b.pack_forget()
        for n in names:
            self.buttons[n].pack(side="left", padx=5)

    def _set_mode(self, reviewing):
        self.zoom_slider.configure(state="disabled" if reviewing else "normal")
        if reviewing:
            self.box.configure(border_color=COLORS["warning"])
            self.state_label.configure(text="REVIEW", fg_color=COLORS["warning"], text_color="#1c1203")
            if self.multi:
                self.hint.configure(text="Enter = keep      A = keep + take another      R = retake      Esc = skip")
                self._layout(["keep", "more", "retake", "cancel"])
            else:
                self.hint.configure(text="Enter = keep      R = retake      Esc = skip")
                self._layout(["keep", "retake", "cancel"])
        else:
            self.box.configure(border_color=COLORS["success"])
            self.state_label.configure(text="●  LIVE", fg_color=COLORS["success"], text_color="#052e16")
            self.hint.configure(text="Space = capture      Esc = skip")
            self._layout(["capture", "cancel"])

    def _crop(self, frame):
        """Digital zoom: keep the centre 1/zoom of the frame (what you see is what gets saved)."""
        if self.zoom <= 1.0:
            return frame
        h, w = frame.shape[:2]
        cw, ch = int(w / self.zoom), int(h / self.zoom)
        x, y = (w - cw) // 2, (h - ch) // 2
        return frame[y:y + ch, x:x + cw]

    def _set_zoom(self, value):
        if self.frozen:
            return
        self.zoom = round(max(1.0, min(4.0, float(value))), 1)
        self.zoom_slider.set(self.zoom)
        self.zoom_label.configure(text=f"{self.zoom:.1f}×")

    def _tick(self):
        if self.closed:
            return
        if not self.frozen:
            frame = self.stream.latest()
            if frame is not None:
                frame = self._crop(frame)
                self.shot = frame
                small = cv2.resize(frame, (self.preview_w, self._pv_h))
                self._photo_img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
                self.view.configure(image=self._photo_img)
        self._tick_job = self.after(40, self._tick)   # ~25 fps is plenty for framing a page

    def capture(self):
        if self.shot is None or self.frozen:
            return
        if not self.confirm:
            self.finish(_jpeg(self.shot))
        else:
            self.frozen = True  # freeze the preview so the user can check the shot
            self._set_mode(reviewing=True)

    def keep(self):
        if self.frozen:
            self.finish(_jpeg(self.shot))

    def retake(self):
        if self.frozen:
            self.frozen = False
            self._set_mode(reviewing=False)

    def keep_more(self):
        if self.frozen and self.multi:
            self.finish(_jpeg(self.shot), again=True)


    def finish(self, result, again=False):
        if self.closed:
            return
        self.closed = True
        for job in (self._tick_job, self._act_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        if self.on_zoom and self.zoom != self._start_zoom:
            self.on_zoom(self.zoom)
        self.destroy()
        self.on_done(result, again)

    def _fit_preview_width(self, aspect):
        """Keeps the preview modest: at most ~35% of the screen height, and never taller than what fits."""
        scale = max(self.winfo_fpixels("1i") / 96, 1.0)           # Windows display scaling
        screen_h = self.winfo_screenheight()
        room = screen_h - 120 - int(245 * scale)                   # minus taskbar/title bar and the controls
        height = max(180, min(room, int(screen_h * 0.35), int(self.PREVIEW_W / aspect)))
        return int(height * aspect)

class StaleDaysDialog(ctk.CTkToplevel):
    """Small popup to set how many days old a student's last photo gets amber / red."""

    def __init__(self, parent, warn, bad, on_save):
        super().__init__(parent)
        self.withdraw()
        self.configure(fg_color=COLORS["bg"])
        self.title("Photo Alert Days")
        self.resizable(False, False)
        self.on_save = on_save

        card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=16,
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(card, text="Photo alert days", font=font(16, "bold"),
                     text_color=COLORS["text"]).pack(padx=24, pady=(16, 2))
        ctk.CTkLabel(card, text="Highlight students whose last photo is this old.",
                     font=font(12), text_color=COLORS["text_muted"]).pack(padx=24, pady=(0, 12))

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.pack(padx=24)

        def row(label, color, value):
            r = ctk.CTkFrame(form, fg_color="transparent")
            r.pack(fill="x", pady=4)
            ctk.CTkLabel(r, text=label, font=font(13), text_color=color,
                         width=110, anchor="w").pack(side="left")
            e = ctk.CTkEntry(r, width=70, height=34, corner_radius=8, font=font(13),
                             justify="center", fg_color=COLORS["bg"], border_color=COLORS["border"])
            e.insert(0, str(value))
            e.pack(side="left", padx=(8, 8))
            ctk.CTkLabel(r, text="days", font=font(12),
                         text_color=COLORS["text_muted"]).pack(side="left")
            return e

        self.warn_entry = row("Amber after", COLORS["warning"], warn)
        self.bad_entry = row("Red after", COLORS["error"], bad)
        self.error = ctk.CTkLabel(card, text="", font=font(12), text_color=COLORS["error"])
        self.error.pack(pady=(8, 0))

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.pack(pady=(8, 16))
        ctk.CTkButton(bar, text="Save", width=100, height=36, corner_radius=10,
                      font=font(13, "bold"), fg_color=COLORS["primary"],
                      hover_color=COLORS["primary_dark"], text_color=COLORS["on_primary"],
                      command=self._save).pack(side="left", padx=5)
        ctk.CTkButton(bar, text="Cancel", width=100, height=36, corner_radius=10,
                      font=font(13), fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                      text_color=COLORS["text"], command=self.destroy).pack(side="left", padx=5)

        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        center_over(self, parent)
        self.transient(parent)
        self.deiconify()
        self.after(100, self._activate)

    def _activate(self):
        try:
            self.lift()
            self.focus_force()
            self.warn_entry.focus()
        except Exception:
            pass

    def _save(self):
        try:
            warn, bad = int(self.warn_entry.get()), int(self.bad_entry.get())
        except ValueError:
            self.error.configure(text="Enter whole numbers only.")
            return
        if warn < 1 or bad <= warn:
            self.error.configure(text="Amber must be 1 or more, and red must be later than amber.")
            return
        self.destroy()
        self.on_save(warn, bad)

class ContextMenu(ctk.CTkToplevel):
    """Small dark popup menu shown at the mouse position (for right-click)."""

    def __init__(self, parent, items, x, y):
        super().__init__(parent)
        self._parent = parent
        self.withdraw()
        self.overrideredirect(True)
        self.configure(fg_color=COLORS["border"])            # shows as a 1px outline
        inner = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=0)
        inner.pack(padx=1, pady=1)
        for label, command in items:
                    # Check if this item should have red text
                    text_color = "#ef4444" if "Delete" in label else COLORS["text"]
                    hover_color = "#450a0a" if "Delete" in label else COLORS["border"]

                    ctk.CTkButton(inner, text=label, anchor="w", width=180, height=32, corner_radius=6,
                                font=font(12), fg_color="transparent", hover_color=hover_color,
                                text_color=text_color,
                                command=lambda c=command: self._choose(c)).pack(fill="x", padx=4, pady=3)
        self.update_idletasks()
        x = max(0, min(x, self.winfo_screenwidth() - self.winfo_reqwidth() - 4))    # keep on screen
        y = max(0, min(y, self.winfo_screenheight() - self.winfo_reqheight() - 4))
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        self.bind("<Escape>", lambda e: self.close())
        for seq in ("<Button-1>", "<Button-2>", "<Button-3>"):
            self.bind(seq, self._check_outside)
        self.after(50, self._capture)

    def _capture(self):
        try:
            self.focus_force()
            self.grab_set()          # so a click anywhere else reaches us and can close the menu
        except Exception:
            pass

    def _check_outside(self, event):
        px, py = self.winfo_pointerxy()
        inside = (self.winfo_rootx() <= px < self.winfo_rootx() + self.winfo_width()
                  and self.winfo_rooty() <= py < self.winfo_rooty() + self.winfo_height())
        if not inside:
            self.close()

    def _choose(self, command):
        parent = self._parent
        self.close()
        parent.after(10, command)    # run after the menu is gone so dialogs open cleanly

    def close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class CameraStream:
    """Keeps one camera open; a background thread always holds the newest frame."""
    def __init__(self, index):
        self.index = index
        self.cap = _open_camera(index)
        self.ok = self.cap.isOpened()
        self._frame, self._lock, self._stop = None, threading.Lock(), threading.Event()
        self.count = 0
        self.last_used = time.monotonic()
        self._thread = None
        if not self.ok:
            return
        self._configure(mjpg=True)
        if not self.cap.read()[0]:           # camera didn't like MJPG: reopen and use its default
            self.cap.release()
            self.cap = _open_camera(index)
            self.ok = self.cap.isOpened()
            if not self.ok:
                return
            self._configure(mjpg=False)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _configure(self, mjpg):
        if mjpg:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # before size
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if ok:
                with self._lock:
                    self._frame = frame
                self.count += 1
            else:
                time.sleep(0.05)

    def latest(self):
        self.last_used = time.monotonic()
        with self._lock:
            return self._frame

    def size(self):
        return (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640),
                int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480))

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.cap.release()
        
class CameraPicker(ctk.CTkToplevel):
    """Cycles through connected cameras with a live preview; the user picks the one to use."""

    MAX_INDEX = 6        # camera indices 0-5 are tried
    W, H = 400, 300

    def __init__(self, parent, current_index, on_pick):
        super().__init__(parent)
        self.withdraw()
        self.configure(fg_color=COLORS["bg"])
        self.title("Choose Camera")
        self.resizable(False, False)
        self.on_pick = on_pick
        self.cam, self.index, self.closed, self._job = None, None, False, None

        card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=16,
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=12, pady=12)
        self.title_label = ctk.CTkLabel(card, text="", font=font(16, "bold"), text_color=COLORS["text"])
        self.title_label.pack(padx=20, pady=(16, 2))
        self.note = ctk.CTkLabel(card, text="", font=font(12), text_color=COLORS["text_muted"])
        self.note.pack(padx=20, pady=(0, 10))
        self._blank = tk.PhotoImage(width=self.W, height=self.H)
        self.view = tk.Label(card, image=self._blank, bg=COLORS["bg"], bd=0)
        self.view.pack(padx=20)

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.pack(pady=16)
        secondary = dict(height=40, corner_radius=12, font=font(13), fg_color=COLORS["card_alt"],
                         hover_color=COLORS["border"], text_color=COLORS["text"])
        ctk.CTkButton(bar, text="Next camera", width=130, command=self._on_next,
                      **secondary).pack(side="left", padx=5)
        ctk.CTkButton(bar, text="Use this camera", width=150, height=40, corner_radius=12,
                      font=font(13, "bold"), fg_color=COLORS["primary"],
                      hover_color=COLORS["primary_dark"], text_color=COLORS["on_primary"],
                      command=self._use).pack(side="left", padx=5)
        ctk.CTkButton(bar, text="Cancel", width=90, command=self.close,
                      **secondary).pack(side="left", padx=5)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<Return>", lambda e: self._use())
        center_over(self, parent)
        self.transient(parent)
        self.deiconify()
        self.after(100, self._activate)

        if not self._open(current_index) and not self._cycle():
            self.title_label.configure(text="No camera found")
            self.note.configure(text="Plug in a camera and try again.")
        self._tick()

    def _activate(self):
        if self.closed:
            return
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _release(self):
        if self.cam is not None:
            self.cam.release()
            self.cam = None

    def _open(self, i):
        self._release()
        cam = _open_camera(i)
        if cam.isOpened() and cam.read()[0]:
            self.cam, self.index = cam, i
            w = int(cam.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cam.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            self.title_label.configure(text=f"Camera {i + 1}")
            self.note.configure(text=f"{w} × {h}" if w and h else "")
            return True
        cam.release()
        return False

    def _cycle(self):
        """Opens the next camera that works, wrapping around."""
        start = -1 if self.index is None else self.index
        for step in range(1, self.MAX_INDEX + 1):
            if self._open((start + step) % self.MAX_INDEX):
                return True
        return False

    def _on_next(self):
        prev = self.index
        self.note.configure(text="Looking...")
        self.update_idletasks()
        if not self._cycle():
            self.title_label.configure(text="No camera found")
            self.note.configure(text="")
        elif self.index == prev:
            self.note.configure(text="No other camera found")

    def _tick(self):
        if self.closed:
            return
        if self.cam is not None:
            ok, frame = self.cam.read()
            if ok:
                img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                img.thumbnail((self.W, self.H))
                bg = Image.new("RGB", (self.W, self.H), COLORS["bg"])      # letterbox on the dark background
                bg.paste(img, ((self.W - img.width) // 2, (self.H - img.height) // 2))
                self._img = ImageTk.PhotoImage(bg)
                self.view.configure(image=self._img)
        self._job = self.after(40, self._tick)

    def _use(self):
        if self.cam is None:
            return
        idx = self.index
        self.close()
        self.on_pick(idx)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
        self._release()
        self.destroy()

class UploadWorker(threading.Thread):
    """Uploads in the background so a slow connection never blocks the next scan."""

    def __init__(self, store, on_uploaded=None):
        super().__init__(daemon=True)
        self.store = store
        self.on_uploaded = on_uploaded
        self.q = queue.Queue()
        self._queued_paths = set()   # pending files already waiting in the queue
        self.start()

    def submit(self, student_id, last, first, subject, jpeg, barcode=None):
        self.q.put((student_id, last, first, subject, jpeg, barcode, time.time(), None))

    def stop(self):
        self.q.put(None)   # finish what's queued, then exit

    def retry_pending(self, lookup_names):
        """Queues photos saved by earlier failed uploads. lookup_names(student_id) -> (last, first) or None.
        Files are only deleted after a successful upload. Returns how many were queued."""
        if not os.path.isdir(PENDING_DIR):
            return 0
        n = 0
        for fn in sorted(os.listdir(PENDING_DIR)):
            path = os.path.join(PENDING_DIR, fn)
            if not fn.lower().endswith(".jpg") or path in self._queued_paths:
                continue
            try:
                sid, rest = fn[:-4].split("_", 1)           # <student id>_<subject>_<unix time>.jpg
                subject, ts = rest.rsplit("_", 1)
                names = lookup_names(sid)
                if names is None:
                    continue                                  # unknown student: leave the file alone
                last, first = names
                taken_at = int(ts)
            except Exception:
                log.warning("Skipping unrecognised pending file: %s", fn)
                continue
            self._queued_paths.add(path)
            self.q.put((sid, last, first, subject, None, None, taken_at, path))   # jpeg read in the worker
            n += 1
        if n:
            log.info("Queued %d pending photo(s) for retry", n)
        return n

    def run(self):
        while True:
            job = self.q.get()
            try:
                if job is None:
                    return
                self._handle(job)
            except Exception:
                log.exception("Photo upload job crashed")
            finally:
                self.q.task_done()

    def _handle(self, job):
        sid, last, first, subject, jpeg, barcode, taken_at, src = job
        if jpeg is None:
            with open(src, "rb") as f:
                jpeg = f.read()
        for attempt in range(3):
            try:
                self.store.upload(sid, last, first, subject, jpeg, barcode, taken_at)
                log.info("Uploaded %s photo for student %s", subject, sid)
                if src:
                    self._queued_paths.discard(src)
                    try:
                        os.remove(src)
                    except OSError as e:
                        log.warning("Couldn't delete %s: %s", src, e)
                if self.on_uploaded:
                    try:
                        self.on_uploaded(sid, subject)
                    except Exception:
                        pass
                return
            except Exception as e:
                log.warning("Upload failed (student %s, %s) attempt %d/3: %s", sid, subject, attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt * 2)

        if src:   # already on disk from an earlier failure: leave it for the next retry
            self._queued_paths.discard(src)
            log.error("Retry failed for %s; will try again later", os.path.basename(src))
        else:     # first failure: keep the photo instead of losing it
            os.makedirs(PENDING_DIR, exist_ok=True)
            path = os.path.join(PENDING_DIR, f"{sid}_{subject}_{int(taken_at)}.jpg")
            with open(path, "wb") as f:
                f.write(jpeg)
            log.error("Saved photo locally after 3 failures: %s", path)


def _is_yes(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("y", "yes", "true", "1"))

def folder_tags(folder: str) -> list:
    """'Math - Scores' -> [('MATH', fill, text), ('SCORES', fill, text)] for the photo window pills."""
    subj, _, kind = folder.partition(" - ")
    tags = []
    for text, table in ((subj, SUBJECT_COLORS), (kind, KIND_COLORS)):
        if text:
            fill, tcolor, _edge = table.get(text, PILL_FALLBACK)
            tags.append((text.upper(), fill, tcolor))
    return tags

def student_subjects(row: dict) -> list:
    cols = {k.lower(): k for k in row}
    return [name for name, col in (("Math", MATH_COL), ("Reading", READING_COL))
            if col in cols and _is_yes(row[cols[col]])]

def student_photo_info(row: dict) -> dict:
    """Turns a students-table row (from ScannerDB.find_student) into what the photo flow needs."""
    cols = {k.lower(): k for k in row}
    if FIRST_COL not in row or LAST_COL not in row:
        raise KeyError(f"Name columns not found; students table has: {sorted(row)}")
    if MATH_COL not in cols and READING_COL not in cols:
        raise KeyError(f"Math/Reading columns not found; students table has: {sorted(row)}")
    subjects = student_subjects(row)
    return {
        "id": str(row["s_id"]),
        "barcode": str(row.get(BARCODE_COL, "")),
        "first": row[FIRST_COL],
        "last": row[LAST_COL],
        "subjects": subjects,
        "steps": [(f"{s} - {kind}", multi) for s in subjects for kind, multi in SHOT_TYPES],
    }

# -------------------- External opening files functionality -----

def _open_external(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


class PhotoBrowser(ctk.CTkFrame):
    """Lazy Drive browser: students -> subject folders -> photos.
    One API call per folder expanded and per photo opened. All Drive work runs on
    background threads and reports back through a queue polled on the Tk thread."""

    def __init__(self, parent, get_store, get_visible_students,thresholds=(STALE_WARN_DAYS, STALE_BAD_DAYS), on_take_photo=None):
        super().__init__(parent, fg_color="transparent")
        self.warn_days, self.bad_days = thresholds
        self.on_take_photo = on_take_photo
        self.get_store = get_store                          # -> DriveStore or None
        self.get_visible_students = get_visible_students    # -> {s_id: "First Last (barcode)"}
        self.meta = {}                  # tree item id -> {"kind", "id", ...}
        self._q = queue.Queue()
        self._cache = OrderedDict()     # photo file id -> bytes (last 30)
        self._want = None               # photo currently selected for preview
        self._selected = None
        self._loading = set()
        self._job = None
        self._imgs = [None, None]       # PhotoImage refs (Tk drops images without one)
        self._pil = None                # full-size photo currently previewed
        self._pil_id = None             # Drive id of self._pil
        self._cmp_pil = None            # previous photo, while comparing
        self._cmp_want = None
        self._cmp_labels = ("", "")
        self._zoom = 1.0                # 1.0 = fit to the box
        self._pan_x = self._pan_y = 0.0
        self._drag = None
        self._rows = []                 # (label, folder_id or None, student_id)
        self._last = {}                 # student_id -> datetime of last photo
        self._checking = set()          # student ids whose date is still being looked up
        self._filter_text = ""
        self._days = {}                 # student_id -> set of 'Mon', 'Tue', ...
        self._search = {}               # student_id -> lowercase "name (days) barcode"
        self._day_order = []            # selected days, oldest click first
        self._pill_cache = {}           # tuple of days -> PhotoImage (also keeps Tk from dropping them)
        self._pill_slots = 1            # widest day count among students, so all strips match
        self._pill_scale = max(self.winfo_fpixels("1i") / 96, 1.0)
        self._subjects = {}
        self._day_btns = {}
        self._shown = None              # rows currently displayed (after filters), for the count
        self._refreshing = False
        self._loaded_at = None        

        self.sort_var = tk.StringVar(value=SORT_OPTIONS[0])
        self.overdue_var = tk.BooleanVar(value=False) 

        self._render_job = None

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # left card: folder tree
        left = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12,
                            border_width=1, border_color=COLORS["border"])
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        bar = ctk.CTkFrame(left, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(12, 0))
        ctk.CTkLabel(bar, text="STUDENT PHOTOS", font=font(11, "bold"),
                     text_color=COLORS["text_muted"]).pack(side="left")
        ctk.CTkButton(bar, text="Refresh", width=76, height=28, corner_radius=8, font=font(12),
                      fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                      text_color=COLORS["text"], command=self.refresh).pack(side="right")
        ctk.CTkButton(bar, text="Open in Drive", width=104, height=28, corner_radius=8,
                      font=font(12), fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                      text_color=COLORS["text"], command=self._open_in_drive
                      ).pack(side="right", padx=(0, 6))

        ctk.CTkSegmentedButton(
            left, values=SORT_OPTIONS, variable=self.sort_var, command=self._on_sort,
            height=30, font=font(12), fg_color=COLORS["bg"],
            selected_color=COLORS["primary"], selected_hover_color=COLORS["primary_dark"],
            unselected_color=COLORS["bg"], unselected_hover_color=COLORS["border"],
            text_color=COLORS["text"],
        ).pack(fill="x", padx=12, pady=(8, 0))

        frow = ctk.CTkFrame(left, fg_color="transparent")
        frow.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkCheckBox(frow, text="Overdue", variable=self.overdue_var, width=90,
                        command=self._populate_root, font=font(12),
                        checkbox_width=18, checkbox_height=18, corner_radius=5, border_width=2,
                        border_color=COLORS["text_muted"], fg_color=COLORS["primary"],
                        hover_color=COLORS["primary_dark"]).pack(side="right", padx=(10, 0))
        self.filter_entry = ctk.CTkEntry(
            frow, height=30, corner_radius=8, font=font(12),
            placeholder_text="Filter by name or barcode",
            fg_color=COLORS["bg"], border_color=COLORS["border"])
        self.filter_entry.pack(side="left", fill="x", expand=True)
        self.filter_entry.bind("<KeyRelease>", self._on_filter)

        drow = ctk.CTkFrame(left, fg_color="transparent")
        drow.pack(fill="x", padx=10, pady=(8, 0))
        for i, d in enumerate(DAY_NAMES):
            drow.columnconfigure(i, weight=1, uniform="days")
            b = ctk.CTkButton(drow, text=d, width=10, height=28, corner_radius=8, font=font(12),
                              fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
                              text_color=COLORS["text"],
                              command=lambda d=d: self._toggle_day(d))
            b.grid(row=0, column=i, sticky="ew", padx=2)
            self._day_btns[d] = b

        tk.Frame(left, height=1, bg="#3f3f46").pack(fill="x", padx=12, pady=(10, 0))  # divider

        # footer status line: always present, so changing its text never moves anything.
        self.status = StatusText(left, text="Click Refresh to load", size=11, anchor="w",
                                 wraplength=290)
        self.status.pack(side="bottom", fill="x", padx=14, pady=(4, 10))
        tk.Frame(left, height=1, bg="#3f3f46").pack(side="bottom", fill="x", padx=12)

        box = tk.Frame(left, bg=COLORS["card"])  # plain frame hosts the ttk tree + scrollbar
        box.pack(fill="both", expand=True, padx=(8, 4), pady=(6, 4))
        self.tree = ttk.Treeview(box, columns=("last",), show="tree headings",
                                 selectmode="browse", height=6)
        self.tree.heading("#0", text="Days · Subjects  /  Student", anchor="w")
        self.tree.column("#0", width=340, stretch=True)
        self.tree.heading("last", text="Last photo", anchor="e")
        scale = max(self.winfo_fpixels("1i") / 96, 1.0)
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("last", width=int(150 * scale), anchor="e", stretch=False)
        self.tree.tag_configure("warn", foreground=COLORS["warning"])
        self.tree.tag_configure("stale", foreground=COLORS["error"])
        sb = ctk.CTkScrollbar(box, command=self.tree.yview, width=12, fg_color=COLORS["card"],
                              button_color=COLORS["border"], button_hover_color=COLORS["text_muted"])
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(2, 4))

        # right card: preview
        right = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=12,
                             border_width=1, border_color=COLORS["border"])
        right.grid(row=0, column=1, sticky="nsew")
        self.caption = ctk.CTkLabel(right, text="", font=font(13, "bold"), anchor="w",
                                    text_color=COLORS["text"])
        self.caption.pack(fill="x", padx=16, pady=(14, 0))
        self.preview_box = ctk.CTkFrame(right, width=300, height=260, fg_color=COLORS["bg"],
                                        corner_radius=10, border_width=1, border_color="#3f3f46")
        self.preview_box.pack_propagate(False)
        self.preview_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.canvas = tk.Canvas(self.preview_box, bg=COLORS["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)

        tools = ctk.CTkFrame(right, fg_color="transparent")
        tools.pack(fill="x", padx=14, pady=(0, 12))
        btn_style = dict(height=34, corner_radius=8, font=font(12), fg_color=COLORS["card_alt"],
                         hover_color=COLORS["border"], text_color=COLORS["text"])
        self.open_btn = ctk.CTkButton(tools, text="Full size", width=84, state="disabled",
                                      text_color_disabled=COLORS["text_muted"],
                                      command=self._open_selected, **btn_style)
        self.open_btn.pack(side="right")
        ctk.CTkButton(tools, text="Fit", width=48, command=self._fit,
                      **btn_style).pack(side="right", padx=(0, 6))
        self.compare_btn = ctk.CTkButton(tools, text="Compare", width=80, state="disabled",
                                         text_color_disabled=COLORS["text_muted"],
                                         command=self._toggle_compare, **btn_style)
        self.compare_btn.pack(side="right", padx=(0, 6))
        self.zoom_slider = ctk.CTkSlider(tools, from_=1.0, to=6.0, number_of_steps=50, width=90,
                                         progress_color=COLORS["primary"],
                                         command=lambda v: self._zoom_at(float(v)))
        self.zoom_slider.set(1.0)
        self.zoom_slider.pack(side="left")
        self.zoom_label = ctk.CTkLabel(tools, text="1.0×", width=40, font=font(12, "bold"),
                                       text_color=COLORS["text"])
        self.zoom_label.pack(side="left", padx=(6, 0))

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Double-Button-1>", lambda e: self._fit())

        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Mac reports right-click as Button-2, and Ctrl-click counts as one too
        seqs = ("<Button-2>", "<Control-Button-1>") if sys.platform == "darwin" else ("<Button-3>",)
        for seq in seqs:
            self.tree.bind(seq, self._on_right_click)       

        self.bind("<Destroy>", self._on_destroy)
        self._poll()

    # --- background plumbing ---

    def _run(self, fn, *args):
        def worker():
            try:
                self._q.put(fn(*args))
            except Exception as e:
                self._q.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                self._handle(self._q.get_nowait())
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._job = self.after(100, self._poll)

    def _on_destroy(self, event):
        if event.widget is self and self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass

    # --- loading ---

    def refresh(self):
        if self._refreshing:
            return
        store = self.get_store()
        if store is None:
            self.status.config(text="Drive not connected")
            return
        self._refreshing = True
        self.tree.delete(*self.tree.get_children())
        self.meta.clear()
        self._loading.clear()
        self.status.config(text="Loading...")
        self._run(self._fetch_root, store)

    def refresh_if_stale(self, max_age=600):
        """Loads on first use, and again if the list is more than max_age seconds old."""
        if self._refreshing:
            return
        if self._loaded_at is None or time.monotonic() - self._loaded_at > max_age:
            self.refresh()

    def _fetch_root(self, store):
        visible = self.get_visible_students() 
        rows, last, missing, seen = [], {}, [], set()
        for f in store.list_children(store.root_id, folders_only=True):
            props = f.get("appProperties") or {}
            key = props.get("kt_key", "").split(":")
            if len(key) == 2 and key[0] == "student" and key[1] in visible:
                sid = key[1]
                seen.add(sid)
                rows.append((visible[sid][0], f["id"], sid))
                dt = self._parse_last(props.get(LAST_KEY))
                if dt:
                    last[sid] = dt
                else:
                    missing.append((sid, f["id"]))
        for sid, info in visible.items():      # no Drive folder yet = never photographed
            if sid not in seen:
                rows.append((info[0], None, sid))
        return ("root", rows, last, missing, visible)

    def _on_open(self, event):
        iid = self.tree.focus()
        meta = self.meta.get(iid)
        if not meta or meta["kind"] not in ("student", "subject") or iid in self._loading:
            return
        store = self.get_store()
        if store is None:
            return
        self._loading.add(iid)
        self.status.config(text="Loading...")
        self._run(self._fetch_children, store, iid, meta)

    def _fetch_children(self, store, iid, meta):
        if meta["kind"] == "student":
            items = store.list_children(meta["id"], folders_only=True)
        else:
            items = store.list_children(meta["id"], newest_first=True)
        return ("children", iid, items)

    def _on_select(self, event):
        sel = self.tree.selection()
        meta = self.meta.get(sel[0]) if sel else None
        if not (meta and meta["kind"] == "photo"):
            return
        self._leave_compare_state()
        subject_iid = self.tree.parent(sel[0])
        student_iid = self.tree.parent(subject_iid)
        self.caption.configure(text=f"{self.tree.item(student_iid, 'text')}   •   "
                                    f"{self.tree.item(subject_iid, 'text')}   •   "
                                    f"{self.tree.item(sel[0], 'text')}")
        self._selected = meta["id"]
        self.open_btn.configure(state="normal")
        self.compare_btn.configure(state="normal")
        self._show_photo(meta["id"], external=False)

    def _open_selected(self):
        if self._selected:
            self._show_photo(self._selected, external=True)

    def _on_double_click(self, event):
        meta = self.meta.get(self.tree.identify_row(event.y))
        if meta and meta["kind"] == "photo":
            self._show_photo(meta["id"], external=True)

    def _show_photo(self, fid, external):
        if not external:
            self._want = fid
        if fid in self._cache:
            self._cache.move_to_end(fid)
            self._deliver(fid, self._cache[fid], external)
            return
        store = self.get_store()
        if store is None:
            return
        self.status.config(text="Loading photo...")
        self._run(self._fetch_image, store, fid, external)

    def _fetch_image(self, store, fid, external):
        return ("image", fid, store.download(fid), external)

    # --- results (always on the Tk thread) ---

    def _handle(self, msg):
        if msg is None:
            return
        kind = msg[0]
        if kind == "root":
            _, self._rows, self._last, missing, visible = msg
            self._refreshing = False
            self._loaded_at = time.monotonic()
            self._days = {sid: v[2] for sid, v in visible.items()}
            self._subjects = {sid: v[3] for sid, v in visible.items()}
            self._search = {sid: f"{v[0]} {v[1]} {self._days_text(sid)} {' '.join(sorted(v[3]))}".lower()
                            for sid, v in visible.items()}
            self._pill_slots = max([MIN_PILLS] + [len(v[2]) for v in visible.values()])
            self._pill_cache.clear()
            store = self.get_store() if missing else None
            self._checking = {sid for sid, _ in missing} if store is not None else set()
            self._populate_root()
            if store is not None:
                self.status.config(text="Loading photo dates...")
                threading.Thread(target=self._backfill, args=(store, missing), daemon=True).start()
            else:
                self.status.config(text=self._count_text())
        elif kind == "last":
            _, sid, dt = msg
            self._checking.discard(sid)
            if dt:
                self._last[sid] = dt
            self._refresh_row(sid)
        elif kind == "backfill_done":
            self._checking.clear()
            self.status.config(text=self._count_text())
            if self.sort_var.get() != SORT_OPTIONS[0]:
                self._populate_root()           # dates just arrived, so re-sort
        elif kind == "children":
            _, iid, items = msg
            self._loading.discard(iid)
            if not self.tree.exists(iid):
                return
            self.tree.delete(*self.tree.get_children(iid))
            parent_kind = self.meta[iid]["kind"]
            for it in items:
                if parent_kind == "student":
                    child = self.tree.insert(iid, "end", text=it["name"])
                    self.meta[child] = {"kind": "subject", "id": it["id"]}
                    self.tree.insert(child, "end", text="Loading...")
                else:
                    child = self.tree.insert(iid, "end", text=self._pretty_time(it["name"]))
                    self.meta[child] = {"kind": "photo", "id": it["id"]}
            if not items:
                self.tree.insert(iid, "end", text="(nothing here yet)")
            self.status.config(text="")
        elif kind == "image":
            _, fid, data, external = msg
            self._cache[fid] = data
            while len(self._cache) > 30:
                self._cache.popitem(last=False)
            self._deliver(fid, data, external)
            self.status.config(text="")
        elif kind == "cmp":
            _, fid, data = msg
            self._cache[fid] = data
            while len(self._cache) > 30:
                self._cache.popitem(last=False)
            if fid == self._cmp_want:
                self._set_compare(data)
            self.status.config(text="")
        elif kind == "photo_deleted":
            _, iid, sid, newest = msg
            if iid in self.tree.selection():       
                self._leave_compare_state()
                self._pil = self._pil_id = self._selected = None
                self.canvas.delete("all")
                self.caption.configure(text="")
                self.open_btn.configure(state="disabled")
                self.compare_btn.configure(state="disabled")
            if self.tree.exists(iid):
                self.tree.delete(iid)
            if sid:
                if newest:
                    self._last[sid] = newest
                else:
                    self._last.pop(sid, None)      # no photos left: shows as Never / red
                self._refresh_row(sid)
            self.status.config(text="Photo deleted from Drive")
        elif kind == "error":
            self._loading.clear()
            self.status.config(text=f"Error: {msg[1]}")

    def _deliver(self, fid, data, external):
        if external:
            path = os.path.join(tempfile.gettempdir(), f"scanner_photo_{fid}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            _open_external(path)
        elif fid == self._want:
            self._pil = Image.open(io.BytesIO(data)).convert("RGB")
            self._pil_id = fid
            self._fit()

    def _is_open(self, iid):
        return str(self.tree.item(iid, "open")).lower() in ("1", "true")

    def on_uploaded(self, student_id, subject):
        """A photo finished uploading: refresh only the folder the user could be looking at."""
        sid = str(student_id)
        self._last[sid] = datetime.now()
        self._checking.discard(sid)
        student_iid = self._student_iid(sid)
        self._refresh_row(sid)      # updates the date text and the amber/red color

        if student_iid is None:
            if self.tree.get_children():  # list is loaded and this student's folder is brand new
                self.refresh()
            return
        if not self._is_open(student_iid):
            return  # collapsed: it loads fresh next time it's opened
        store = self.get_store()
        if store is None:
            return
        subject_iid = next((c for c in self.tree.get_children(student_iid)
                            if self.tree.item(c, "text") == subject and c in self.meta), None)
        if subject_iid is not None:
            if self._is_open(subject_iid) and subject_iid not in self._loading:
                self._loading.add(subject_iid)
                self._run(self._fetch_children, store, subject_iid, self.meta[subject_iid])
        elif student_iid not in self._loading:
            self._loading.add(student_iid)  # first photo for this subject: its folder is new
            self._run(self._fetch_children, store, student_iid, self.meta[student_iid])

    def _delete_selected_photo(self):
        sel = self.tree.selection()
        meta = self.meta.get(sel[0]) if sel else None
        if not meta or meta.get("kind") != "photo":
            self.status.config(text="Select a photo to delete")
            return
        if not messagebox.askyesno(
            "Delete Photo",
            "Are you sure you want to permanently delete this photo from Google Drive?",
            parent=self
        ):
            return
        store = self.get_store()
        if store is None:
            self.status.config(text="Drive not connected")
            return

        iid, file_id = sel[0], meta["id"]
        student_iid = self.tree.parent(self.tree.parent(iid))     # photo -> folder -> student
        smeta = self.meta.get(student_iid) or {}
        sid, student_folder = smeta.get("student_id"), smeta.get("id")

        def do_delete():
            store.delete_photo(file_id)
            newest = (store.backfill_last_photo(student_folder, clear_if_empty=True)
                      if student_folder else None)
            return ("photo_deleted", iid, sid, newest)

        self.status.config(text="Deleting photo...")
        self._run(do_delete)

    def apply_thresholds(self, warn, bad):
        self.warn_days, self.bad_days = warn, bad
        if self.overdue_var.get():
            self._populate_root()       # membership changed, so rebuild
        else:
            for iid, m in self.meta.items():
                if m["kind"] in ("student", "nophotos"):
                    self.tree.item(iid, tags=self._tag_for(m["student_id"]))
        self.status.config(text=self._count_text())

    @staticmethod
    def _pretty_time(name):
        try:
            stamp = datetime.strptime(name.rsplit(".", 1)[0], "%Y-%m-%d_%H-%M-%S")
            return stamp.strftime("%b %d, %Y  %I:%M %p")
        except ValueError:
            return name

    # --- zoom / pan viewer ---

    def _render(self):
        c = self.canvas
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw < 20 or ch < 20:
            return
        c.delete("all")
        if self._pil is None:
            c.create_text(cw // 2, ch // 2, text="Select a photo to preview it here",
                          fill=COLORS["text_muted"], font=(FONT, 12))
            return
        if self._cmp_pil is None:
            self._pan_x, self._pan_y = self._draw_pane(self._pil, 0, cw, ch, 0)
            return
        half = cw // 2
        self._draw_pane(self._cmp_pil, 0, half, ch, 1)
        self._pan_x, self._pan_y = self._draw_pane(self._pil, half, cw - half, ch, 0)
        c.create_line(half, 0, half, ch, fill="#3f3f46", width=2)
        for x, text in ((6, self._cmp_labels[0]), (half + 6, self._cmp_labels[1])):
            t = c.create_text(x + 6, 10, text=text, anchor="nw", fill="#ffffff",
                              font=(FONT, 9, "bold"))
            bb = c.bbox(t)
            r = c.create_rectangle(bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2,
                                   fill="#000000", outline="")
            c.tag_lower(r, t)

    def _draw_pane(self, pil, x0, pw, ch, slot):
        """Draws one photo into a pw-wide strip of the canvas; returns its clamped pan."""
        iw, ih = pil.size
        scale = min(pw / iw, ch / ih) * self._zoom
        dw, dh = iw * scale, ih * scale
        mx, my = max(0.0, (dw - pw) / 2), max(0.0, (dh - ch) / 2)   # can't drag off-screen
        px = max(-mx, min(mx, self._pan_x))
        py = max(-my, min(my, self._pan_y))
        ox = pw / 2 + px - dw / 2
        oy = ch / 2 + py - dh / 2
        vx0, vy0 = max(0.0, -ox), max(0.0, -oy)
        vx1, vy1 = min(dw, pw - ox), min(dh, ch - oy)
        box = (max(0.0, vx0 / scale), max(0.0, vy0 / scale),
               min(iw, vx1 / scale), min(ih, vy1 / scale))
        size = (max(1, round(vx1 - vx0)), max(1, round(vy1 - vy0)))
        self._imgs[slot] = ImageTk.PhotoImage(pil.resize(size, Image.BILINEAR, box=box))
        self.canvas.create_image(x0 + round(ox + vx0), round(oy + vy0),
                                 image=self._imgs[slot], anchor="nw")
        return px, py

    def _zoom_at(self, new_zoom, px=None, py=None):
        """Zoom toward a point (the cursor), or the centre if none is given."""
        if self._pil is None:
            return
        new_zoom = max(1.0, min(6.0, new_zoom))
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        pw = cw // 2 if self._cmp_pil is not None else cw
        px = pw / 2 if px is None else px % pw
        py = ch / 2 if py is None else py
        iw, ih = self._pil.size
        fit = min(pw / iw, ch / ih)
        old, new = fit * self._zoom, fit * new_zoom
        ox = pw / 2 + self._pan_x - iw * old / 2
        oy = ch / 2 + self._pan_y - ih * old / 2
        u, v = (px - ox) / old, (py - oy) / old
        self._pan_x = px - u * new - pw / 2 + iw * new / 2
        self._pan_y = py - v * new - ch / 2 + ih * new / 2
        self._zoom = new_zoom
        self.zoom_slider.set(new_zoom)
        self.zoom_label.configure(text=f"{new_zoom:.1f}×")
        self._render()

    def _fit(self):
        self._zoom, self._pan_x, self._pan_y = 1.0, 0.0, 0.0
        self.zoom_slider.set(1.0)
        self.zoom_label.configure(text="1.0×")
        self._render()

    def _on_wheel(self, e):
        self._zoom_at(self._zoom * (1.25 if e.delta > 0 else 0.8), e.x, e.y)

    def _drag_start(self, e):
        self._drag = (e.x, e.y, self._pan_x, self._pan_y)

    def _drag_move(self, e):
        if self._drag and self._pil is not None:
            x0, y0, px, py = self._drag
            self._pan_x, self._pan_y = px + e.x - x0, py + e.y - y0
            self._render()

    def _on_canvas_configure(self, event=None):
        """Re-render once the window stops changing size, not on every drag event."""
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except Exception:
                pass
        self._render_job = self.after(80, self._render_after_resize)

    def _render_after_resize(self):
        self._render_job = None
        if self.winfo_exists():
            self._render()            
    # dates and sort
    @staticmethod
    def _parse_last(s):
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ago(dt):
        if dt is None:
            return "—"
        d = (datetime.now().date() - dt.date()).days
        when = "today" if d <= 0 else f"{d}d ago"
        return f"{dt:%b %d}  ·  {when}"

    def _student_iid(self, sid):
        return next((i for i, m in self.meta.items()
                     if m["kind"] == "student" and m.get("student_id") == str(sid)), None)

    def _on_sort(self, _choice=None):
        self._populate_root()

    def _populate_root(self):
        mode = self.sort_var.get()
        terms = self._filter_text.split()
        rows = [r for r in self._rows
                if all(t in self._search.get(r[2], r[0].lower()) for t in terms)]
        if self._day_order:      # must have every selected day (extra days are fine)
            rows = [r for r in rows if set(self._day_order) <= self._days.get(r[2], set())]
        if self.overdue_var.get():
            rows = [r for r in rows if self._tag_for(r[2])]    # only amber or red rows
        self._shown = rows

        def last_of(r):
            return self._last.get(r[2])

        if mode == SORT_OPTIONS[1]:      # longest ago: never first, then oldest to newest
            rows.sort(key=lambda r: (last_of(r) is not None, last_of(r) or datetime.min, r[0].lower()))
        elif mode == SORT_OPTIONS[2]:    # most recent: newest first, never last
            rows.sort(key=lambda r: (last_of(r) is None,
                                     -last_of(r).timestamp() if last_of(r) else 0, r[0].lower()))
        else:
            rows.sort(key=lambda r: r[0].lower())

        self.tree.delete(*self.tree.get_children())
        self.meta.clear()
        self._loading.clear()
        for label, fid, sid in rows:
            iid = self.tree.insert("", "end", text=label, image=self._pill_img(sid),
                                   values=(self._ago_for(sid),), tags=self._tag_for(sid))
            if fid:
                self.meta[iid] = {"kind": "student", "id": fid, "student_id": sid}
                self.tree.insert(iid, "end", text="Loading...")
            else:
                self.meta[iid] = {"kind": "nophotos", "id": None, "student_id": sid}
        if not rows and self._rows:
            self.tree.insert("", "end", text="No students match")
        if not self._checking:           # don't overwrite the "Loading photo dates..." message
            self.status.config(text=self._count_text())

    def _on_filter(self, _event=None):
        text = self.filter_entry.get().strip().lower()
        if text != self._filter_text:        # ignore Shift/Tab/arrow key releases
            self._filter_text = text
            self._populate_root()

    def _days_text(self, sid):
        days = self._days.get(sid, set())
        return " ".join(d for d in DAY_NAMES if d in days)

    def _pill_img(self, sid):
        have = tuple(d for d in DAY_NAMES if d in self._days.get(sid, set()))
        days = have + ("n/a",) * max(0, MIN_PILLS - len(have))
        subs = tuple(s for s in SUBJECT_ORDER if s in self._subjects.get(sid, set()))
        key = (days, subs)
        img = self._pill_cache.get(key)
        if img is None:
            k, s = 3, self._pill_scale                     # draw 3x, then shrink for smooth edges
            pw, ph, gap = int(34 * s), int(18 * s), int(4 * s)
            sw = int(22 * s)                               # subject pills are a single letter
            slots = max(self._pill_slots, MIN_PILLS)
            pad = int(12 * s)
            days_w = slots * (pw + gap) - gap
            subs_x = days_w + int(8 * s)
            width = subs_x + len(SUBJECT_ORDER) * (sw + gap) - gap + pad
            canvas = Image.new("RGBA", (width * k, ph * k), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            fnt = _pill_font(int(ph * k * 0.58))

            def pill(x0, w, text, colors):
                fill, tcolor, edge = colors
                draw.rounded_rectangle((x0 * k, 0, (x0 + w) * k - 1, ph * k - 1),
                                       radius=ph * k // 2, fill=fill, outline=edge, width=k)
                draw.text(((x0 + w / 2) * k, ph * k / 2), text, font=fnt, fill=tcolor, anchor="mm")

            for i, d in enumerate(days):
                pill(i * (pw + gap), pw, d, PILL_COLORS.get(d, PILL_FALLBACK))
            for j, name in enumerate(SUBJECT_ORDER):
                if name in subs:                           # missing subject = blank slot, columns stay aligned
                    pill(subs_x + j * (sw + gap), sw, SUBJECT_LETTER[name], SUBJECT_COLORS[name])
            img = ImageTk.PhotoImage(canvas.resize((width, ph), Image.LANCZOS))
            self._pill_cache[key] = img
        return img

    def _toggle_day(self, day):
        if day in self._day_order:
            self._day_order.remove(day)
        else:
            self._day_order.append(day)
            if len(self._day_order) > MAX_DAYS:
                self._day_order.pop(0)          # drop the earliest-clicked day
        for d, b in self._day_btns.items():
            on = d in self._day_order
            b.configure(fg_color=COLORS["primary"] if on else COLORS["card_alt"],
                        hover_color=COLORS["primary_dark"] if on else COLORS["border"])
        self._populate_root()

    def _ago_for(self, sid):
        if sid in self._last:
            return self._ago(self._last[sid])
        return "…" if sid in self._checking else "Never"

    def _tag_for(self, sid):
        dt = self._last.get(sid)
        if dt is None:
            return () if sid in self._checking else ("stale",)
        days = (datetime.now().date() - dt.date()).days
        if days >= self.bad_days:
            return ("stale",)
        if days >= self.warn_days:
            return ("warn",)
        return ()

    def _refresh_row(self, sid):
        iid = self._student_iid(sid)
        if iid is not None:
            self.tree.item(iid, values=(self._ago_for(sid),), tags=self._tag_for(sid))

    def _count_text(self):
        rows = self._shown if self._shown is not None else self._rows
        never = sum(1 for _, _, sid in rows if sid not in self._last)
        text = f"{len(rows)} students  ·  {never} never photographed"
        if len(rows) != len(self._rows):
            text += f"  (of {len(self._rows)})"
        return text

    def _open_in_drive(self):
        sel = self.tree.selection()
        meta = self.meta.get(sel[0]) if sel else None
        if not meta or not meta.get("id"):
            self.status.config(text="Select a student, subject or photo that's in Drive")
            return
        if meta["kind"] == "photo":
            webbrowser.open(f"https://drive.google.com/file/d/{meta['id']}/view")
        else:
            webbrowser.open(f"https://drive.google.com/drive/folders/{meta['id']}")

    def _backfill(self, store, missing):
        for sid, fid in missing:
            try:
                self._q.put(("last", sid, store.backfill_last_photo(fid)))
            except Exception as e:
                log.warning("Date backfill failed for student %s: %s", sid, e)
        self._q.put(("backfill_done",))
    # Compare photos
    def _previous_photo(self):
        sel = self.tree.selection()
        if not sel or (self.meta.get(sel[0]) or {}).get("kind") != "photo":
            return None
        sibs = self.tree.get_children(self.tree.parent(sel[0]))
        for iid in sibs[sibs.index(sel[0]) + 1:]:
            m = self.meta.get(iid)
            if m and m["kind"] == "photo":
                return {**m, "iid": iid}
        return None

    def _toggle_compare(self):
        if self._cmp_pil is not None or self._cmp_want is not None:
            self._exit_compare()
            return
        sel = self.tree.selection()
        prev = self._previous_photo()
        if not sel or prev is None:
            self.status.config(text="No earlier photo to compare with")
            return
        if self._pil_id != self._selected:
            self.status.config(text="Wait for the photo to load first")
            return
        store = self.get_store()
        if store is None:
            return
        short = lambda iid: self.tree.item(iid, "text").split(",")[0]   # "Oct 22"
        self._cmp_labels = (f"PREVIOUS · {short(prev['iid'])}", f"SELECTED · {short(sel[0])}")
        self._cmp_want = prev["id"]
        if prev["id"] in self._cache:
            self._set_compare(self._cache[prev["id"]])
        else:
            self.status.config(text="Loading previous photo...")
            self._run(self._fetch_cmp, store, prev["id"])

    def _fetch_cmp(self, store, fid):
        return ("cmp", fid, store.download(fid))

    def _set_compare(self, data):
        self._cmp_pil = Image.open(io.BytesIO(data)).convert("RGB")
        self.compare_btn.configure(text="Single")
        self._fit()

    def _leave_compare_state(self):
        self._cmp_pil = None
        self._cmp_want = None
        self.compare_btn.configure(text="Compare")

    def _exit_compare(self):
        self._leave_compare_state()
        self._on_select(None)
        self._render()

    def _selected_student_id(self):
        """The student for the selected row, even if a subject or photo row is selected."""
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        while self.tree.parent(iid):
            iid = self.tree.parent(iid)
        m = self.meta.get(iid)
        return m.get("student_id") if m else None

    def _take_photo(self):
        sid = self._selected_student_id()
        if sid is None or self.on_take_photo is None:
            self.status.config(text="Select a student first")
            return
        self.on_take_photo(sid)

    def _on_right_click(self, event):
            iid = self.tree.identify_row(event.y)
            meta = self.meta.get(iid)
            if not meta:
                return
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            
            items = []
            if self.on_take_photo is not None and meta.get("kind") != "photo":
                items.append(("📷  Take photo", self._take_photo))
                
            if meta.get("id"):
                items.append(("Open in Drive", self._open_in_drive))
                
            # Add Delete option only for individual photo files
            if meta.get("kind") == "photo":
                items.append(("Delete from Drive", self._delete_selected_photo))

            if items:
                ContextMenu(self, items, event.x_root, event.y_root)
            return "break"

    