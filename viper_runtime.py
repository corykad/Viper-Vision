import logging
import threading
from concurrent.futures import ThreadPoolExecutor


executor = ThreadPoolExecutor(max_workers=12)
is_shutting_down = threading.Event()


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
