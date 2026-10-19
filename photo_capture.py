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
from tkinter import messagebox, ttk

from scanner_db import BASE_DIR as BASE

import cv2
from PIL import Image, ImageTk

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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


class PhotoDialog(tk.Toplevel):
    """Live camera preview inside Tk. Calls on_done(jpeg_bytes or None) when finished.
    Keys: Space = capture, Enter = keep, R = retake, Esc = cancel."""

    PREVIEW_W = 640  # raise to ~800 for a bigger preview if the screen allows

    def __init__(self, parent, label, on_done, confirm=CONFIRM_PHOTO, subtitle="", theme=None):
        super().__init__(parent)
        theme = theme or DEFAULT_THEME
        self.c, self.font = theme["colors"], theme["font"]
        self.title("Take Photo")
        self.configure(bg=self.c["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self.on_done, self.confirm = on_done, confirm
        self.frame, self.frozen, self.closed = None, False, False

        self.cam = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)  # CAP_DSHOW opens faster on Windows
        if not self.cam.isOpened():
            messagebox.showerror("Camera", "Camera not found", parent=parent)
            self.finish(None)
            return

        # header banner: student name, then subject / step
        head = tk.Frame(self, bg=self.c["primary"], padx=16, pady=10)
        head.pack(fill="x")
        tk.Label(head, text=label, font=(self.font, 16, "bold"), anchor="w",
                 bg=self.c["primary"], fg=self.c["on_primary"]).pack(fill="x")
        if subtitle:
            tk.Label(head, text=subtitle, font=(self.font, 10), anchor="w",
                     bg=self.c["primary"], fg=self.c.get("primary_light", "#DBEAFE")).pack(fill="x")

        self.state_label = tk.Label(self, font=(self.font, 10, "bold"), bg=self.c["bg"])
        self.state_label.pack(pady=(10, 4))

        # coloured frame around the preview (green = live, amber = reviewing)
        self.box = tk.Frame(self, bg=self.c["success"], padx=3, pady=3)
        self.box.pack(padx=16)
        cam_w = int(self.cam.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
        cam_h = int(self.cam.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
        self._blank = tk.PhotoImage(width=self.PREVIEW_W, height=int(cam_h * self.PREVIEW_W / cam_w))
        self.view = tk.Label(self.box, image=self._blank, bg=self.c["card"], bd=0)
        self.view.pack()

        self.bar = tk.Frame(self, bg=self.c["bg"])
        self.bar.pack(pady=(12, 4))
        mk = lambda text, cmd, style="TButton": ttk.Button(
            self.bar, text=text, width=16, command=cmd, style=style, takefocus=0)
        self.buttons = {
            "capture": mk("Capture (Space)", self.capture),
            "keep": mk("Keep (Enter)", self.keep),
            "retake": mk("Retake (R)", self.retake, "Secondary.TButton"),
            "cancel": mk("Cancel (Esc)", lambda: self.finish(None), "Secondary.TButton"),
        }

        self.hint = tk.Label(self, font=(self.font, 9), bg=self.c["bg"], fg=self.c["text_muted"])
        self.hint.pack(pady=(0, 12))
        self._set_mode(reviewing=False)

        self.bind("<space>", lambda e: self.capture())
        self.bind("<Return>", lambda e: self.keep())
        self.bind("<r>", lambda e: self.retake())
        self.bind("<Escape>", lambda e: self.finish(None))
        self.protocol("WM_DELETE_WINDOW", lambda: self.finish(None))

        # centre over the main window
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        self.focus_force()
        self._tick()

    def _layout(self, names):
        for b in self.buttons.values():
            b.pack_forget()
        for n in names:
            self.buttons[n].pack(side="left", padx=4)

    def _set_mode(self, reviewing):
        if reviewing:
            self.box.configure(bg=self.c["warning"])
            self.state_label.config(text="Review - keep this photo?", fg=self.c["warning"])
            self.hint.config(text="Enter = keep     R = retake     Esc = cancel")
            self._layout(["keep", "retake", "cancel"])
        else:
            self.box.configure(bg=self.c["success"])
            self.state_label.config(text="●  Live preview", fg=self.c["success"])
            self.hint.config(text="Space = capture     Esc = cancel")
            self._layout(["capture", "cancel"])

    def _tick(self):
        if self.closed:
            return
        if not self.frozen:
            ok, frame = self.cam.read()
            if ok:
                self.frame = frame
                small = cv2.resize(frame, (self.PREVIEW_W, int(frame.shape[0] * self.PREVIEW_W / frame.shape[1])))
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
            self._set_mode(reviewing=True)

    def keep(self):
        if self.frozen:
            self.finish(_jpeg(self.frame))

    def retake(self):
        if self.frozen:
            self.frozen = False
            self._set_mode(reviewing=False)

    def finish(self, result):
        if self.closed:
            return
        self.closed = True
        self.cam.release()
        self.destroy()
        self.on_done(result)


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

    def run(self):
        while True:
            job = self.q.get()
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


class PhotoBrowser(ttk.Frame):
    """Lazy Drive browser: students -> subject folders -> photos.
    One API call per folder expanded and per photo opened. All Drive work runs on
    background threads and reports back through a queue polled on the Tk thread."""

    def __init__(self, parent, get_store, get_visible_students):
        super().__init__(parent)
        self.get_store = get_store                          # -> DriveStore or None
        self.get_visible_students = get_visible_students    # -> {s_id: "First Last (barcode)"}
        self.meta = {}                  # tree item id -> {"kind", "id", "name"}
        self._q = queue.Queue()
        self._cache = OrderedDict()     # photo file id -> bytes (last 30)
        self._want = None               # photo currently selected for preview
        self._loading = set()
        self._job = None
        self._img = None

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        self.status = ttk.Label(bar, text="Click Refresh to load", style="Muted.TLabel")
        self.status.pack(side="left", padx=8)

        box = ttk.Frame(left)
        box.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(box, show="tree", selectmode="browse")
        self.tree.column("#0", width=280, stretch=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.preview = ttk.Label(self, text="Select a photo", style="Muted.TLabel", anchor="center")
        self.preview.grid(row=0, column=1, sticky="nsew")

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
        if meta and meta["kind"] == "photo":
            self._show_photo(meta["id"], external=False)

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
            img = Image.open(io.BytesIO(data))
            img.thumbnail((max(self.preview.winfo_width() - 10, 200),
                           max(self.preview.winfo_height() - 10, 200)))
            self._img = ImageTk.PhotoImage(img)  # keep a reference or Tk drops it
            self.preview.config(image=self._img, text="")

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