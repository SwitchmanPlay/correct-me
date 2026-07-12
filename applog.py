"""Single-file event log for correct-me.

Every event is appended to correct-me.log (next to config.json / the .exe)
as one JSON object per line, and also printed to the console when there is
one. Attach correct-me.log when reporting a bug - it contains the whole
story: hotkey presses, what was grabbed, model timings, rejections, errors.
"""

import json
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

LOG_PATH = BASE_DIR / "correct-me.log"
MAX_BYTES = 2_000_000  # when exceeded, the oldest half of the log is dropped

_lock = threading.Lock()


def short(text: str, limit: int = 300) -> str:
    """Truncate long text for log lines; newlines become visible \\n."""
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."


def log(event: str, **fields) -> None:
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    rec.update(fields)
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except (TypeError, ValueError):
        line = json.dumps({"ts": rec["ts"], "event": event, "note": "unserializable fields"})
    with _lock:
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
                lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                LOG_PATH.write_text(
                    "\n".join(lines[len(lines) // 2 :]) + "\n", encoding="utf-8"
                )
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # logging must never break the app
    extras = ", ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{event}]" + (f" {extras}" if extras else ""), flush=True)
