"""GUI-thread callbacks for file, database and connection operations."""

import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal


class Cancellation:
    def __init__(self):
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise RuntimeError("操作已取消")


_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="io")


class Jobs(QObject):
    delivered = Signal(object)
    progress = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = True
        self.token = Cancellation()
        self.delivered.connect(self._deliver)

    def run(self, fn, callback):
        def work():
            try:
                result, error = fn(), None
            except Exception as exc:
                result, error = None, exc
            try:
                self.delivered.emit((callback, result, error))
            except RuntimeError:
                pass

        return _pool.submit(work)

    def _deliver(self, payload):
        if self.enabled:
            callback, result, error = payload
            callback(result, error)
