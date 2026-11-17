"""Latest-result-wins tasks with configuration snapshots and bounded workers."""

from __future__ import annotations

import copy
import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from .logging_service import event


class Cancelled(RuntimeError):
    pass


@dataclass
class Task:
    id: int
    kind: str
    cfg: dict
    cancelled: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.perf_counter)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled("操作已取消")
        if time.perf_counter() - self.started > 180:
            raise TimeoutError("任务超过三分钟，请缩小区域后重试")


class TaskRunner(QObject):
    completed = Signal(object)
    progress = Signal(object)

    def __init__(self):
        super().__init__()
        self._ids = itertools.count(1)
        self._current = None
        self._ocr = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        self._network = ThreadPoolExecutor(max_workers=2, thread_name_prefix="network")
        self._futures = []
        self.closed = False

    def begin(self, kind, cfg):
        self.cancel()
        task = Task(next(self._ids), kind, copy.deepcopy(cfg))
        self._current = task
        event("task.started", task=task.id, stage=kind)
        return task

    def active(self, task):
        return not self.closed and self._current is task and not task.cancelled.is_set()

    def cancel(self):
        if self._current:
            self._current.cancelled.set()
        for future in self._futures:
            future.cancel()
        self._futures = [f for f in self._futures if f.running()]

    def submit(self, task, fn, *, network=False, stage=""):
        if not self.active(task):
            return None

        def run():
            stage_started = time.perf_counter()
            try:
                task.check()
                if stage:
                    self.progress.emit((task, stage))
                result = fn(task)
                task.check()
                self.completed.emit((task, result, None))
                event(
                    "task.stage.completed",
                    task=task.id,
                    stage=stage,
                    duration_ms=round((time.perf_counter() - stage_started) * 1000),
                    task_duration_ms=round((time.perf_counter() - task.started) * 1000),
                )
            except Cancelled:
                event("task.cancelled", task=task.id)
            except Exception as exc:
                import logging
                import traceback

                logging.getLogger("screengloss").error(
                    "task=%s %s\n%s",
                    task.id,
                    type(exc).__name__,
                    "".join(traceback.format_tb(exc.__traceback__)),
                )
                event("task.failed", task=task.id, category=type(exc).__name__)
                self.completed.emit((task, None, exc))

        future = (self._network if network else self._ocr).submit(run)
        self._futures = [f for f in self._futures if not f.done()]
        self._futures.append(future)

    def close(self):
        self.closed = True
        self.cancel()
        self._ocr.shutdown(wait=False, cancel_futures=True)
        self._network.shutdown(wait=False, cancel_futures=True)
