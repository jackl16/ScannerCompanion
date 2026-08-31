"""
Standalone Supabase data access for the barcode scanner app.
This mirrors the relevant parts of the Streamlit app's data/connection.py,
but has no Streamlit dependency -- this runs as its own desktop process.
"""
import base64
import json
import sys
import threading
import time
import copy
try:
    import tomllib  # stdlib on Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # pip install tomli  (for Python < 3.11)
import tomli_w  # pip install tomli_w  (writing TOML isn't in the stdlib)
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
import re



from supabase import create_client, Client

if getattr(sys, "frozen", False):
    # running as a PyInstaller-built .exe -- look next to the exe itself,
    # not inside the temp folder PyInstaller extracts to at runtime
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_PATH = BASE_DIR / "scanner_config.toml"
SESSION_FILE = BASE_DIR / "scanner_session.json"  # holds the remember-me refresh token
PENDING_SCANS_FILE = BASE_DIR / "pending_scans.json"  # offline buffer -- scans that
                                                        # couldn't reach Supabase yet
LOCAL_TZ = ZoneInfo("America/Edmonton")  # match the Streamlit app's timezone

TOKEN_REFRESH_INTERVAL_SECONDS = 30 * 60  # refresh well before Supabase's ~1hr token expiry

STAFF_CODE_PATTERN = re.compile(r"^S\d{5}$")

# Substrings that show up in exceptions caused by *connectivity* problems
# (no internet, DNS hiccup, request timeout, etc.) as opposed to a genuine
# application error. Used to decide whether a failed scan should be queued
# for retry rather than surfaced as a hard error, and whether a failed
# session refresh should be treated as "try again later" instead of
# "this token is dead, forget it".
NETWORK_ERROR_MARKERS = (
    "network", "connection", "timeout", "timed out", "unreachable",
    "getaddrinfo", "max retries", "temporary failure", "name resolution",
    "connection reset", "connection aborted",
)

# Substrings that indicate Supabase itself rejected the refresh token
# (expired, revoked, already used, malformed) -- these are the *only*
# cases where it's safe to delete the remembered session file. Anything
# else (a network blip while refreshing) should NOT delete it, or "remember
# me" quietly stops working the next time the app is opened with no wifi.
INVALID_TOKEN_MARKERS = (
    "invalid_grant", "invalid refresh token", "refresh_token_not_found",
    "already used", "revoked", "expired", "invalid token", "invalid_token",
    "session_not_found", "user_not_found",
)


class AuthError(Exception):
    """Raised when the configured scanner account can't sign in."""
    pass


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CONFIG_PATH}. Copy scanner_config.example.toml to "
            "scanner_config.toml and fill in your real values."
        )
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)
    # --- AUTOMATED CREDENTIALS OBFUSCATION ---    
    if "scanner" in config and "password" in config["scanner"]:
        try:
            encoded_pw = config["scanner"]["password"]
            # Decode the base64 string back into standard human-readable text
            decoded_bytes = base64.b64decode(encoded_pw.encode("utf-8"))
            config["scanner"]["password"] = decoded_bytes.decode("utf-8")
        except Exception:
            # Fall back gracefully if the password string isn't encoded yet
            pass
    return config

def save_config(config: dict) -> None:
    export_config = copy.deepcopy(config)
    
    if "scanner" in export_config and "password" in export_config["scanner"]:
        raw_password = export_config["scanner"]["password"]
        # Encode the raw password string into a scrambled base64 block
        encoded_bytes = base64.b64encode(raw_password.encode("utf-8"))
        export_config["scanner"]["password"] = encoded_bytes.decode("utf-8")
        
    with open(CONFIG_PATH, "wb") as f:
        tomli_w.dump(export_config, f)


class ScannerDB:
    def __init__(self):
        config = load_config()
        self.client: Client = create_client(config["supabase"]["url"], config["supabase"]["key"])
        self.center_id = None

        self._students_cache = []
        self._students_cache_time = 0
        self._cache_ttl_seconds = 60  # refresh student list at most once a minute

        scanner_cfg = config.get("scanner", {})
        self.serial_ports = scanner_cfg.get("ports", [])
        self.baud_rate = scanner_cfg.get("baud_rate", 9600)
        self.debounce_seconds = scanner_cfg.get("debounce_seconds", 3)

        self._last_scan_id = None
        self._last_scan_time = 0
        self.last_refresh_ok = True
        self.last_auth_error = None

        self._stop_event = threading.Event()
        self._pending_lock = threading.Lock()  # guards pending_scans.json read/write

        # No auto sign-in here anymore -- the app calls sign_in(email, password)
        # or try_remembered_login() explicitly after showing/skipping the login screen.

    # --- auth ------------------------------------------------------------

    def sign_in(self, email: str, password: str) -> bool:
        """Manual login from the on-screen form. Returns True/False;
        check self.last_auth_error for the reason on failure."""
        self.last_auth_error = None
        try:
            auth_resp = self.client.auth.sign_in_with_password({
                "email": email,
                "password": password,
            })
        except Exception as e:
            self.last_auth_error = "Incorrect email or password."
            return False

        if not auth_resp or not auth_resp.session:
            self.last_auth_error = "Incorrect email or password."
            return False

        self._last_refresh_token = auth_resp.session.refresh_token
        return self._finish_login(auth_resp.session)

    def try_remembered_login(self) -> bool:
        """Attempts to restore a session from the local remember-me file.
        Returns True if successful.

        IMPORTANT: only deletes the remembered session file when Supabase
        has *confirmed* the refresh token is actually dead (expired,
        revoked, already used). A transient error (no wifi yet on boot, a
        DNS hiccup, a request timeout) must NOT delete it -- that was the
        cause of "remember me" appearing to randomly stop working: any
        exception at all was wiping the file, even ones that had nothing
        to do with the token itself.
        """
        if not SESSION_FILE.exists():
            return False

        try:
            saved = json.loads(SESSION_FILE.read_text())
            refresh_token = saved["refresh_token"]
        except Exception:
            # the file itself is corrupt/unreadable -- nothing to restore
            self.forget_remembered_session()
            return False

        try:
            auth_resp = self.client.auth.refresh_session(refresh_token)
        except Exception as e:
            if self._is_invalid_token_error(e):
                self.forget_remembered_session()
            else:
                # probably offline or Supabase is briefly unreachable --
                # leave the remembered session alone and just fall back to
                # the login screen this launch; it'll work next time
                self.last_auth_error = (
                    "Couldn't reach the server to restore your saved login "
                    "(check your internet connection)."
                )
            return False

        if not auth_resp or not auth_resp.session:
            # Supabase responded but didn't hand back a session -- treat
            # this as a genuinely dead token, not a connectivity issue
            self.forget_remembered_session()
            return False

        success = self._finish_login(auth_resp.session)
        if success:
            self.remember_session(auth_resp.session.refresh_token)  # token may have rotated
        else:
            self.forget_remembered_session()
        return success

    @staticmethod
    def _is_invalid_token_error(e: Exception) -> bool:
        msg = str(e).lower()
        return any(marker in msg for marker in INVALID_TOKEN_MARKERS)

    @staticmethod
    def _looks_like_network_error(e: Exception) -> bool:
        msg = str(e).lower()
        return any(marker in msg for marker in NETWORK_ERROR_MARKERS)

    def remember_session(self, refresh_token: str):
        SESSION_FILE.write_text(json.dumps({"refresh_token": refresh_token}))

    def forget_remembered_session(self):
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()

    def _finish_login(self, session) -> bool:
        center_resp = (
            self.client.table("user_centers")
            .select("center_id")
            .eq("user_id", session.user.id)
            .execute()
        )
        if not center_resp.data:
            self.last_auth_error = "This account isn't linked to a center. Contact your administrator."
            return False

        self.center_id = center_resp.data[0]["center_id"]
        self._start_token_refresh_thread()
        return True

    def _start_token_refresh_thread(self):
        thread = threading.Thread(target=self._token_refresh_loop, daemon=True)
        thread.start()

    def _token_refresh_loop(self):
        # Supabase access tokens expire (typically ~1hr); this keeps the
        # session alive for a full shift without needing a restart.
        while not self._stop_event.wait(TOKEN_REFRESH_INTERVAL_SECONDS):
            try:
                self.client.auth.refresh_session()
                self.last_refresh_ok = True
            except Exception:
                self.last_refresh_ok = False  # next data call will surface the real error

    def stop(self):
        self._stop_event.set()

    def sign_out(self):
        """Signs out of Supabase, stops the token-refresh thread, and
        forgets the remembered session so the login screen shows again."""
        self._stop_event.set()
        try:
            self.client.auth.sign_out()
        except Exception:
            pass  # best-effort -- still proceed with local cleanup either way
        self.forget_remembered_session()
        self.center_id = None
        self._stop_event.clear()  # ready for a fresh token-refresh thread on next login

    # --- scanner config ---

    def save_serial_ports(self, ports: list) -> None:
        """Persists the detected scanner port(s) into scanner_config.toml,
        preserving every other existing setting in the file."""
        config = load_config()
        config.setdefault("scanner", {})["ports"] = ports
        save_config(config)
        self.serial_ports = ports  # keep the in-memory copy in sync too

    # --- local time helpers ---

    def local_now(self) -> datetime:
        return datetime.now(LOCAL_TZ)

    def local_date(self) -> date:
        return self.local_now().date()

    # --- students (cached locally so keystrokes don't hit the network every time) ---

    def get_students(self) -> list:
        now = time.monotonic()
        if now - self._students_cache_time > self._cache_ttl_seconds:
            resp = (
                self.client.table("students")
                .select("*")
                .eq("center_id", self.center_id)
                .execute()
            )
            self._students_cache = resp.data
            self._students_cache_time = now
        return self._students_cache

    def find_student(self, code: str) -> dict | None:
        for s in self.get_students():
            if str(s.get("barcode_code", "")).strip() == code:
                return s
        return None

    # --- attendance ---

    def get_today_attendance(self, s_id: int) -> dict | None:
        today = self.local_date().isoformat()
        resp = (
            self.client.table("attendance_logs")
            .select("*")
            .eq("center_id", self.center_id)
            .eq("s_id", s_id)
            .eq("date", today)
            .execute()
        )
        return resp.data[0] if resp.data else None


    def set_attendance(self, s_id: int, **fields):
        today = self.local_date().isoformat()
        existing = self.get_today_attendance(s_id)

        if existing:
            self.client.table("attendance_logs").update(fields).eq("log_id", existing["log_id"]).execute()
        else:
            self.client.table("attendance_logs").insert({
                **fields,
                "center_id": self.center_id,
                "s_id": s_id,
                "date": today,
            }).execute()

    # --- staff ---

    def get_staff(self) -> list:
        now = time.monotonic()
        if not hasattr(self, "_staff_cache_time") or now - self._staff_cache_time > self._cache_ttl_seconds:
            resp = (
                self.client.table("staff")
                .select("*")
                .eq("center_id", self.center_id)
                .eq("status", "active")
                .execute()
            )
            self._staff_cache = resp.data
            self._staff_cache_time = now
        return self._staff_cache

    def find_staff(self, staff_code: str) -> dict | None:
        for s in self.get_staff():
            if str(s.get("staff_code", "")).strip() == staff_code:
                return s
        return None

    # --- staff attendance  ---

    def get_today_staff_attendance(self, staff_id: str) -> dict | None:
        today = self.local_date().isoformat()
        resp = (
            self.client.table("staff_time_logs")
            .select("*")
            .eq("center_id", self.center_id)
            .eq("staff_id", staff_id)
            .eq("date", today)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def set_staff_attendance(self, staff_id: str, **fields):
        today = self.local_date().isoformat()
        existing = self.get_today_staff_attendance(staff_id)

        if existing:
            self.client.table("staff_time_logs").update(fields).eq("log_id", existing["log_id"]).execute()
        else:
            self.client.table("staff_time_logs").insert({
                **fields,
                "center_id": self.center_id,
                "staff_id": staff_id,
                "date": today,
            }).execute()

    # --- offline buffer (JSON) -------------------------------------------
    # If Supabase can't be reached mid-scan, the raw scan gets appended to
    # pending_scans.json instead of being silently lost. Every subsequent
    # scan -- and a periodic timer in the app -- tries to drain this queue
    # in order before/alongside processing new scans.

    def _load_pending(self) -> list:
        if not PENDING_SCANS_FILE.exists():
            return []
        try:
            return json.loads(PENDING_SCANS_FILE.read_text())
        except Exception:
            return []  # corrupt file -- don't crash the app over a queue file

    def _save_pending(self, pending: list) -> None:
        PENDING_SCANS_FILE.write_text(json.dumps(pending, indent=2))

    def pending_count(self) -> int:
        with self._pending_lock:
            return len(self._load_pending())

    def _queue_pending_scan(self, clean_scan: str) -> None:
        with self._pending_lock:
            pending = self._load_pending()
            pending.append({
                "code": clean_scan,
                "queued_at": self.local_now().isoformat(),
            })
            self._save_pending(pending)

    def flush_pending_scans(self) -> int:
        """Replays queued offline scans against Supabase, oldest first.
        Stops at the first one that still fails for network reasons (the
        rest are almost certainly still offline too -- no point hammering
        a dead connection) but drops any entry that fails for a *non*
        network reason (e.g. bad/corrupt data) so the queue can't jam
        forever on one bad entry. Returns how many were flushed."""
        with self._pending_lock:
            pending = self._load_pending()
            if not pending:
                return 0

            remaining = list(pending)
            flushed = 0
            for entry in pending:
                code = str(entry.get("code", ""))
                try:
                    if STAFF_CODE_PATTERN.match(code):
                        self._process_staff_scan(code)
                    elif code.isdigit():
                        self._process_student_scan(code)
                    # anything else was already invalid at queue time and
                    # would've been rejected before ever reaching the
                    # queue -- but drop it defensively just in case
                    remaining.pop(0)
                    flushed += 1
                except Exception as e:
                    if self._looks_like_network_error(e):
                        break  # still offline -- leave this and the rest queued
                    remaining.pop(0)  # a real (non-network) failure -- don't retry forever

            self._save_pending(remaining)
            return flushed

    def process_scan(self, raw_scan: str) -> tuple[str, str]:
        """
        Looks up the scanned code, toggles check-in/check-out, and returns
        (status, message). Routes to staff or student handling based on
        the scan's format: 'S' + 5 digits is a staff code, plain digits
        is a student ID. Status is one of:
        'checked_in', 'checked_out', 'already_done', 'not_found', 'invalid',
        'queued' (Supabase unreachable -- scan saved locally for retry)
        """
        clean_scan = raw_scan.strip()

        now = time.monotonic()
        if clean_scan == self._last_scan_id and (now - self._last_scan_time) < self.debounce_seconds:
            return "debounced", f"Ignored repeat scan of {clean_scan} (within {self.debounce_seconds}s)"
        self._last_scan_id = clean_scan
        self._last_scan_time = now

        if not (STAFF_CODE_PATTERN.match(clean_scan) or clean_scan.isdigit()):
            return "invalid", f"Ignored unrecognized scan format: '{raw_scan}'"

        # try to clear any backlog first so scans replay in the order they
        # actually happened, then process this scan the normal way
        self.flush_pending_scans()

        try:
            if STAFF_CODE_PATTERN.match(clean_scan):
                return self._process_staff_scan(clean_scan)
            else:
                return self._process_student_scan(clean_scan)
        except Exception as e:
            if self._looks_like_network_error(e):
                self._queue_pending_scan(clean_scan)
                return "queued", f"⏳ Offline -- saved scan '{clean_scan}' to retry automatically"
            raise  # a genuine bug/data issue -- let the caller's generic handler surface it

    def _process_student_scan(self, clean_code: str) -> tuple[str, str]:
        student = self.find_student(clean_code)
        if student is None:
            return "not_found", f"Scanned code {clean_code} not found in student database."

        s_id = student["s_id"]
        display_name = student.get("first_name") or clean_code
        existing = self.get_today_attendance(s_id)
        current_status = existing["attendance"] if existing else "No Show"
        now_str = self.local_now().strftime("%H:%M:%S")

        if current_status in ("No Show", ""):
            self.set_attendance(s_id, attendance="In", time_in=now_str)
            return "checked_in", f"✅ Checked IN: {display_name}"
        elif current_status == "In":
            self.set_attendance(s_id, attendance="Out", time_out=now_str)
            return "checked_out", f"👋 Checked OUT: {display_name}"
        else:
            return "already_done", f"ℹ️ {display_name} already logged as '{current_status}'"

    def _process_staff_scan(self, staff_code: str) -> tuple[str, str]:
        staff = self.find_staff(staff_code)
        if staff is None:
            return "not_found", f"Scanned staff code {staff_code} not found."

        display_name = staff.get("first_name") or staff_code
        existing = self.get_today_staff_attendance(staff["staff_id"])
        current_status = existing["attendance"] if existing else "Not Clocked In"
        now_str = self.local_now().strftime("%H:%M:%S")

        if current_status == "Not Clocked In":
            self.set_staff_attendance(staff["staff_id"], attendance="In", time_in=now_str)
            return "checked_in", f"👔 Staff Clocked IN: {display_name}"
        elif current_status == "In":
            self.set_staff_attendance(staff["staff_id"], attendance="Out", time_out=now_str)
            return "checked_out", f"👋 Staff Clocked OUT: {display_name}"
        else:
            return "already_done", f"ℹ️ {display_name} already logged as '{current_status}'"