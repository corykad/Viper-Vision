import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


executor = ThreadPoolExecutor(max_workers=12)
is_shutting_down = threading.Event()
_startup_started = time.perf_counter()
_startup_phases = []
_event_journal = []
_runtime_lock = threading.Lock()
_MAX_EVENTS = 40


def mark_startup_phase(name, detail=""):
    """Record a lightweight startup milestone without touching the UI thread."""
    entry = {
        "name": str(name or "startup"),
        "detail": str(detail or ""),
        "elapsed_seconds": round(time.perf_counter() - _startup_started, 3),
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    with _runtime_lock:
        _startup_phases.append(entry)
    logging.info("[STARTUP] %.3fs %s %s", entry["elapsed_seconds"], entry["name"], entry["detail"])
    return entry


def startup_summary_lines(limit=12):
    with _runtime_lock:
        phases = list(_startup_phases)[-int(limit or 12):]
    if not phases:
        return ["Startup timing has not recorded any phases yet."]
    lines = ["Startup timing:"]
    for phase in phases:
        detail = f" - {phase['detail']}" if phase.get("detail") else ""
        lines.append(f"{phase['elapsed_seconds']:.3f}s: {phase['name']}{detail}")
    return lines


def record_event(kind, message, **details):
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": str(kind or "event"),
        "message": str(message or ""),
        "details": {str(k): v for k, v in (details or {}).items()},
    }
    with _runtime_lock:
        _event_journal.insert(0, entry)
        del _event_journal[_MAX_EVENTS:]
    logging.info("[EVENT] %s %s %s", entry["kind"], entry["message"], entry["details"])
    return entry


def recent_events(limit=12):
    with _runtime_lock:
        return list(_event_journal)[:int(limit or 12)]


def format_recent_events(limit=8):
    events = recent_events(limit)
    if not events:
        return ["No recent Viper events recorded yet."]
    lines = ["Recent Viper events:"]
    for event in events:
        lines.append(f"{event['time']}: {event['kind']}: {event['message']}")
    return lines


def safe_submit(fn, *args, **kwargs):
    """
    Centralized, thread-safe task submitter.
    Prevents the app from throwing RuntimeErrors if a webhook, UI button,
    or background loop tries to fire while the app is closing.
    """
    if is_shutting_down.is_set():
        logging.debug("Ignored task %s: System is shutting down.", getattr(fn, "__name__", fn))
        return None
    try:
        return executor.submit(fn, *args, **kwargs)
    except RuntimeError as e:
        logging.warning("Executor rejected task %s during shutdown: %s", getattr(fn, "__name__", fn), e)
        return None
