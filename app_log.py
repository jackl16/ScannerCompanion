import logging
import sys
import tempfile
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from scanner_db import BASE_DIR

_LOG_DIR = BASE_DIR / "logs"


def log_dir() -> Path:
    return _LOG_DIR


def setup_logging():
    global _LOG_DIR
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:  # e.g. installed somewhere read-only
        _LOG_DIR = Path(tempfile.gettempdir()) / "ScannerCompanionLogs"
        _LOG_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(_LOG_DIR / "scanner_companion.log",
                                  maxBytes=500_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("urllib3", "httpx", "httpcore", "hpack", "h2", "googleapiclient",
                  "google_auth_httplib2", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    crash = logging.getLogger("crash")
    sys.excepthook = lambda t, v, tb: crash.critical("Uncaught exception", exc_info=(t, v, tb))
    threading.excepthook = lambda a: crash.error(
        "Uncaught exception in thread %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))