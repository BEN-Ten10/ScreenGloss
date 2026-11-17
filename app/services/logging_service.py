"""Bounded, asynchronous local logs. Never log user text or credentials."""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import queue
import re
import threading
from collections import deque

from app import config

_listener = None
_queue_handler = None
_recent = deque(maxlen=80)
_lock = threading.Lock()


class SafeFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        text = re.sub(r"(?i)(bearer\s+|api[_ -]?key[=: ]+)[^\s,;]+", r"\1[redacted]", text)
        return re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)


class BoundedQueueHandler(logging.handlers.QueueHandler):
    """Drop overflow without printing unredacted records to stderr."""

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


class DrainingQueueListener(logging.handlers.QueueListener):
    def enqueue_sentinel(self):
        # QueueListener's put_nowait can raise when shutdown meets a full queue.
        self.queue.put(self._sentinel)


def setup(console=False):
    global _listener, _queue_handler
    if _listener:
        return
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("screengloss")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.handlers.RotatingFileHandler(
        config.DATA_DIR / "app.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    outputs = [handler]
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(handler.formatter)
        outputs.append(stream)
    q = queue.Queue(maxsize=1000)
    _queue_handler = BoundedQueueHandler(q)
    _queue_handler.setFormatter(SafeFormatter("%(message)s"))
    logger.addHandler(_queue_handler)
    _listener = DrainingQueueListener(q, *outputs, respect_handler_level=True)
    _listener.start()
    atexit.register(shutdown)


def event(name, *, level=logging.INFO, **fields):
    allowed = {
        "task",
        "stage",
        "duration_ms",
        "task_duration_ms",
        "cache_hit",
        "model",
        "status",
        "count",
        "category",
        "threads",
        "language",
    }
    safe = {k: v for k, v in fields.items() if k in allowed}
    item = {"event": name, **safe}
    with _lock:
        _recent.append(item)
    logging.getLogger("screengloss").log(level, json.dumps(item, ensure_ascii=False))


def recent():
    with _lock:
        return list(_recent)


def shutdown():
    global _listener, _queue_handler
    if _listener:
        logging.getLogger("screengloss").removeHandler(_queue_handler)
        _listener.stop()
        for handler in _listener.handlers:
            handler.close()
        _queue_handler.close()
        _listener = None
        _queue_handler = None
