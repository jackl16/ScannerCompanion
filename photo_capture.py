"""
photo_capture.py - prototype: scan a student -> photograph book pages -> per-student Google Drive folder.

Setup
  pip install opencv-python pillow supabase tomli google-api-python-client google-auth-oauthlib
  Google Cloud console: create a project, enable the Google Drive API, configure the OAuth consent
  screen (add your Google account as a test user), create an OAuth client of type "Desktop app",
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
from collections import OrderedDict

from datetime import datetime
from tkinter import ttk
from ui_kit import messagebox

from scanner_db import BASE_DIR as BASE
import customtkinter as ctk
from ui_kit import COLORS, FONT, font, StatusText, center_over

import cv2
from PIL import Image, ImageTk

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.auth.exceptions import RefreshError

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

# students table columns (adjust if yours differ)
BARCODE_COL, FIRST_COL, LAST_COL = "barcode_code", "first_name", "last_name"
MATH_COL, READING_COL = "math", "reading"  # a Y (or true) means the student does that subject

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

    def _upload(self, student_id, last, first, subject, jpeg, barcode=None):
        """Saves to <root>/<First, Last (ID)>/<subject>/ and keeps only the newest N photos per subject."""
        try:
            label = student_id
            student = self._find_or_create(f"student:{student_id}", f"{first}, {last} ({label})", self.root_id)
            folder = self._find_or_create(f"student:{student_id}:{subject}", subject, student)
            name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"
            media = MediaIoBaseUpload(io.BytesIO(jpeg), mimetype="image/jpeg")
            self.svc.files().create(body={"name": name, "parents": [folder]}, media_body=media, fields="id").execute()
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

def _jpeg(frame):
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


class PhotoDialog(ctk.CTkToplevel):
    """Live camera preview. Calls on_done(jpeg_bytes or None) when finished.
    Keys: Space = capture, Enter = keep, R = retake, Esc = cancel."""

    PREVIEW_W = 640  # raise to ~800 for a bigger preview if the screen allows

    def __init__(self, parent, label, on_done, confirm=CONFIRM_PHOTO, subtitle="", theme=None, camera_index=CAMERA_INDEX):
        super().__init__(parent)
        self.withdraw()
        self.configure(fg_color=COLORS["bg"])
        self.title("Take Photo")
        self.resizable(False, False)
        self.on_done, self.confirm = on_done, confirm
        self.shot, self.frozen, self.closed = None, False, False
        self.zoom = 1.0
        self._tick_job = self._act_job = None
        self.cam = _open_camera(camera_index)

        if not self.cam.isOpened():
            messagebox.showerror("Camera", "Camera not found", parent=parent)
            self.finish(None)
            return

        self.cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self.cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

        card = ctk.CTkFrame(self, fg_color=COLORS["card"], corner_radius=16,
                            border_width=1, border_color=COLORS["border"])
        card.pack(fill="both", expand=True, padx=12, pady=12)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 10))
        names = ctk.CTkFrame(head, fg_color="transparent")
        names.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(names, text=label, font=font(20, "bold"), anchor="w",
                     text_color=COLORS["text"]).pack(fill="x")
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
        cam_w = int(self.cam.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
        cam_h = int(self.cam.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
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
        self.zoom_slider.set(1.0)
        self.zoom_slider.pack(side="left")
        self.zoom_label = ctk.CTkLabel(zrow, text="1.0×", width=44, font=font(12, "bold"),
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
            "retake": btn("Retake", self.retake, "secondary"),
            "cancel": btn("Cancel", lambda: self.finish(None), "secondary"),
        }
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
            self.hint.configure(text="Enter = keep      R = retake      Esc = cancel")
            self._layout(["keep", "retake", "cancel"])
        else:
            self.box.configure(border_color=COLORS["success"])
            self.state_label.configure(text="●  LIVE", fg_color=COLORS["success"], text_color="#052e16")
            self.hint.configure(text="Space = capture      Esc = cancel")
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
            ok, frame = self.cam.read()
            if ok:
                frame = self._crop(frame)
                self.shot = frame
                small = cv2.resize(frame, (self.preview_w, self._pv_h))
                self._photo_img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
                self.view.configure(image=self._photo_img)  # keep a reference or Tk drops the image
        self._tick_job = self.after(30, self._tick)

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

    def finish(self, result):
        if self.closed:
            return
        self.closed = True
        for job in (self._tick_job, self._act_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self.cam.release()
        self.destroy()
        self.on_done(result)

    def _fit_preview_width(self, aspect):
        """Keeps the preview modest: at most ~35% of the screen height, and never taller than what fits."""
        scale = max(self.winfo_fpixels("1i") / 96, 1.0)           # Windows display scaling
        screen_h = self.winfo_screenheight()
        room = screen_h - 120 - int(210 * scale)                   # minus taskbar/title bar and the controls
        height = max(180, min(room, int(screen_h * 0.35), int(self.PREVIEW_W / aspect)))
        return int(height * aspect)

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
        self.start()

    def submit(self, student_id, last, first, subject, jpeg, barcode=None):
        self.q.put((student_id, last, first, subject, jpeg, barcode))

    def stop(self):
        self.q.put(None)   # sentinel: finish anything already queued, then exit

    def run(self):
        while True:
            job = self.q.get()
            if job is None:
                self.q.task_done()
                return
            for attempt in range(3):
                try:
                    self.store.upload(*job)
                    print(f"uploaded {job[3]} photo for {job[0]}")
                    if self.on_uploaded:
                        try:
                            self.on_uploaded(job[0], job[3])  # student id, subject
                        except Exception:
                            pass
                    break
                except Exception as e:
                    print(f"upload failed ({e}); retry {attempt + 1}/3")
                    time.sleep(2 ** attempt * 2)
            else:  # all retries failed: keep the photo on disk instead of losing it
                os.makedirs(PENDING_DIR, exist_ok=True)
                path = os.path.join(PENDING_DIR, f"{job[0]}_{job[3]}_{int(time.time())}.jpg")
                with open(path, "wb") as f:
                    f.write(job[4])
                print("saved locally:", path)
            self.q.task_done()


def _is_yes(v):
    return v is True or (isinstance(v, str) and v.strip().lower() in ("y", "yes", "true", "1"))

def student_photo_info(row: dict) -> dict:
    """Turns a students-table row (from ScannerDB.find_student) into what the photo flow needs."""
    cols = {k.lower(): k for k in row}
    if FIRST_COL not in row or LAST_COL not in row:
        raise KeyError(f"Name columns not found; students table has: {sorted(row)}")
    if MATH_COL not in cols and READING_COL not in cols:
        raise KeyError(f"Math/Reading columns not found; students table has: {sorted(row)}")
    subjects = [name for name, col in (("Math", MATH_COL), ("Reading", READING_COL))
                if col in cols and _is_yes(row[cols[col]])]
    return {
        "id": str(row["s_id"]),
        "barcode": str(row.get(BARCODE_COL, "")),
        "first": row[FIRST_COL],
        "last": row[LAST_COL],
        "subjects": subjects,
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

    def __init__(self, parent, get_store, get_visible_students):
        super().__init__(parent, fg_color="transparent")
        self.get_store = get_store                          # -> DriveStore or None
        self.get_visible_students = get_visible_students    # -> {s_id: "First Last (barcode)"}
        self.meta = {}                  # tree item id -> {"kind", "id", ...}
        self._q = queue.Queue()
        self._cache = OrderedDict()     # photo file id -> bytes (last 30)
        self._want = None               # photo currently selected for preview
        self._selected = None
        self._loading = set()
        self._job = None
        self._img = None
        self._pil = None                 # full-size photo currently previewed
        self._zoom = 1.0                 # 1.0 = fit to the box
        self._pan_x = self._pan_y = 0.0
        self._drag = None
        
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
        tk.Frame(left, height=1, bg="#3f3f46").pack(fill="x", padx=12, pady=(10, 0))  # divider

        # footer status line: always present, so changing its text never moves anything.
        # Packed from the bottom up: status first (lowest), then its divider, then the tree.
        self.status = StatusText(left, text="Click Refresh to load", size=11, anchor="w",
                                 wraplength=290)
        self.status.pack(side="bottom", fill="x", padx=14, pady=(4, 10))
        tk.Frame(left, height=1, bg="#3f3f46").pack(side="bottom", fill="x", padx=12)

        box = tk.Frame(left, bg=COLORS["card"])  # plain frame hosts the ttk tree + scrollbar
        box.pack(fill="both", expand=True, padx=(8, 4), pady=(6, 4))
        self.tree = ttk.Treeview(box, show="tree", selectmode="browse")
        self.tree.column("#0", width=290, stretch=True)
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
        self.open_btn = ctk.CTkButton(tools, text="Open full size", text_color_disabled=COLORS["text_muted"],
                                      state="disabled", command=self._open_selected, **{
                                          **btn_style, "text_color": COLORS["text"]})
        self.open_btn.pack(side="right")
        ctk.CTkButton(tools, text="Fit", width=52, command=self._fit, **btn_style).pack(side="right", padx=(0, 8))
        self.zoom_slider = ctk.CTkSlider(tools, from_=1.0, to=6.0, number_of_steps=50, width=160,
                                         progress_color=COLORS["primary"],
                                         command=lambda v: self._zoom_at(float(v)))
        self.zoom_slider.set(1.0)
        self.zoom_slider.pack(side="left")
        self.zoom_label = ctk.CTkLabel(tools, text="1.0×", width=44, font=font(12, "bold"),
                                       text_color=COLORS["text"])
        self.zoom_label.pack(side="left", padx=(8, 0))

        self.canvas.bind("<Configure>", lambda e: self._render())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Double-Button-1>", lambda e: self._fit())

        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
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
        store = self.get_store()
        if store is None:
            self.status.config(text="Drive not connected")
            return
        try:
            visible = self.get_visible_students()
        except Exception as e:
            self.status.config(text=f"Student list failed: {e}")
            return
        self.tree.delete(*self.tree.get_children())
        self.meta.clear()
        self._loading.clear()
        self.status.config(text="Loading...")
        self._run(self._fetch_root, store, visible)

    def _fetch_root(self, store, visible):
        rows = []
        for f in store.list_children(store.root_id, folders_only=True):
            key = (f.get("appProperties") or {}).get("kt_key", "").split(":")
            if len(key) == 2 and key[0] == "student" and key[1] in visible:
                rows.append((visible[key[1]], f["id"], key[1]))
        rows.sort(key=lambda r: r[0].lower())
        return ("root", rows)

    def _on_open(self, event):
        iid = self.tree.focus()
        meta = self.meta.get(iid)
        if not meta or meta["kind"] == "photo" or iid in self._loading:
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
        subject_iid = self.tree.parent(sel[0])
        student_iid = self.tree.parent(subject_iid)
        self.caption.configure(text=f"{self.tree.item(student_iid, 'text')}   •   "
                                    f"{self.tree.item(subject_iid, 'text')}   •   "
                                    f"{self.tree.item(sel[0], 'text')}")
        self._selected = meta["id"]
        self.open_btn.configure(state="normal")
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
        kind = msg[0]
        if kind == "root":
            for label, fid, sid in msg[1]:
                iid = self.tree.insert("", "end", text=label)
                self.meta[iid] = {"kind": "student", "id": fid, "student_id": sid}
                self.tree.insert(iid, "end", text="Loading...")
            self.status.config(text=f"{len(msg[1])} students with photos")
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
            self._fit()

    def _is_open(self, iid):
        return str(self.tree.item(iid, "open")).lower() in ("1", "true")

    def on_uploaded(self, student_id, subject):
        """A photo finished uploading: refresh only the folder the user could be looking at."""
        student_iid = next((i for i, m in self.meta.items()
                            if m["kind"] == "student" and m.get("student_id") == str(student_id)), None)
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
        iw, ih = self._pil.size
        scale = min(cw / iw, ch / ih) * self._zoom
        dw, dh = iw * scale, ih * scale
        mx, my = max(0.0, (dw - cw) / 2), max(0.0, (dh - ch) / 2)   # can't drag the photo off-screen
        self._pan_x = max(-mx, min(mx, self._pan_x))
        self._pan_y = max(-my, min(my, self._pan_y))
        ox = cw / 2 + self._pan_x - dw / 2      # top-left of the photo on the canvas
        oy = ch / 2 + self._pan_y - dh / 2
        vx0, vy0 = max(0.0, -ox), max(0.0, -oy)  # visible part, in display pixels
        vx1, vy1 = min(dw, cw - ox), min(dh, ch - oy)
        box = (max(0.0, vx0 / scale), max(0.0, vy0 / scale), min(iw, vx1 / scale), min(ih, vy1 / scale))
        size = (max(1, round(vx1 - vx0)), max(1, round(vy1 - vy0)))
        # only the visible part is resized, so dragging stays smooth even at high zoom
        self._img = ImageTk.PhotoImage(self._pil.resize(size, Image.BILINEAR, box=box))
        c.create_image(round(ox + vx0), round(oy + vy0), image=self._img, anchor="nw")

    def _zoom_at(self, new_zoom, px=None, py=None):
        """Zoom toward a point (the cursor), or the centre if none is given."""
        if self._pil is None:
            return
        new_zoom = max(1.0, min(6.0, new_zoom))
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        px = cw / 2 if px is None else px
        py = ch / 2 if py is None else py
        iw, ih = self._pil.size
        fit = min(cw / iw, ch / ih)
        old, new = fit * self._zoom, fit * new_zoom
        ox = cw / 2 + self._pan_x - iw * old / 2
        oy = ch / 2 + self._pan_y - ih * old / 2
        u, v = (px - ox) / old, (py - oy) / old          # photo point under the cursor
        self._pan_x = px - u * new - cw / 2 + iw * new / 2
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