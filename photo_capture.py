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
from datetime import datetime
from tkinter import messagebox

from scanner_db import BASE_DIR as BASE

import cv2
from PIL import Image, ImageTk

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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


class DriveStore:
    def __init__(self, keep_copies=KEEP_COPIES):
        self.keep = keep_copies
        self._cache = {}  # key -> (folder_id, name): saves Drive round trips
        self.svc = build("drive", "v3", credentials=self._creds(), cache_discovery=False)
        self.root_id = self._find_or_create("__root__", ROOT_NAME, None)

    @staticmethod
    def _creds():
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
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

    def upload(self, student_id, last, first, subject, jpeg, barcode=None):
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


def _jpeg(frame):
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


class PhotoDialog(tk.Toplevel):
    """Live camera preview inside Tk. Calls on_done(jpeg_bytes or None) when finished.
    Keys: Space = capture, Enter = keep, R = retake, Esc = cancel."""

    def __init__(self, parent, label, on_done, confirm=CONFIRM_PHOTO):
        super().__init__(parent)
        self.title("Photo")
        self.on_done, self.confirm = on_done, confirm
        self.frame, self.frozen, self.closed = None, False, False
        self.cam = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)  # CAP_DSHOW opens faster on Windows
        if not self.cam.isOpened():
            messagebox.showerror("Camera", "Camera not found", parent=parent)
            self.finish(None)
            return

        tk.Label(self, text=label, font=("Segoe UI", 14, "bold")).pack(pady=4)
        self.view = tk.Label(self)
        self.view.pack(padx=8)
        self.bar = tk.Frame(self)
        self.bar.pack(pady=8)
        mk = lambda text, cmd: tk.Button(self.bar, text=text, width=16, command=cmd, takefocus=0)
        self.buttons = {
            "capture": mk("Capture (Space)", self.capture),
            "keep": mk("Keep (Enter)", self.keep),
            "retake": mk("Retake (R)", self.retake),
            "cancel": mk("Cancel (Esc)", lambda: self.finish(None)),
        }
        self._layout(["capture", "cancel"])

        self.bind("<space>", lambda e: self.capture())
        self.bind("<Return>", lambda e: self.keep())
        self.bind("<r>", lambda e: self.retake())
        self.bind("<Escape>", lambda e: self.finish(None))
        self.protocol("WM_DELETE_WINDOW", lambda: self.finish(None))
        self.focus_force()
        self._tick()

    def _layout(self, names):
        for b in self.buttons.values():
            b.pack_forget()
        for n in names:
            self.buttons[n].pack(side="left", padx=4)

    def _tick(self):
        if self.closed:
            return
        if not self.frozen:
            ok, frame = self.cam.read()
            if ok:
                self.frame = frame
                small = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))
                self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
                self.view.configure(image=self.photo)  # keep a reference or Tk drops the image
        self.after(30, self._tick)

    def capture(self):
        if self.frame is None or self.frozen:
            return
        if not self.confirm:
            self.finish(_jpeg(self.frame))
        else:
            self.frozen = True  # freeze the preview so the user can check the shot
            self._layout(["keep", "retake", "cancel"])

    def keep(self):
        if self.frozen:
            self.finish(_jpeg(self.frame))

    def retake(self):
        if self.frozen:
            self.frozen = False
            self._layout(["capture", "cancel"])

    def finish(self, result):
        if self.closed:
            return
        self.closed = True
        self.cam.release()
        self.destroy()
        self.on_done(result)


class UploadWorker(threading.Thread):
    """Uploads in the background so a slow connection never blocks the next scan."""

    def __init__(self, store):
        super().__init__(daemon=True)
        self.store = store
        self.q = queue.Queue()
        self.start()

    def submit(self, student_id, last, first, subject, jpeg, barcode=None):
        self.q.put((student_id, last, first, subject, jpeg, barcode))

    def run(self):
        while True:
            job = self.q.get()
            for attempt in range(3):
                try:
                    self.store.upload(*job)
                    print(f"uploaded {job[3]} photo for {job[0]}")
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