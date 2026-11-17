"""Per-monitor logical/physical transforms. GUI discovery, worker-safe capture."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from PySide6.QtCore import QRect


@dataclass(frozen=True)
class Monitor:
    name: str
    logical: tuple[int, int, int, int]
    physical: tuple[int, int, int, int]

    @property
    def scale(self):
        return (self.physical[2] - self.physical[0]) / self.logical[2]

    def physical_rect(self, rect):
        lx, ly, lw, lh = self.logical
        px, py, pr, pb = self.physical
        sx = (pr - px) / lw
        sy = (pb - py) / lh
        return (
            round(px + (rect.x() - lx) * sx),
            round(py + (rect.y() - ly) * sy),
            max(1, round(rect.width() * sx)),
            max(1, round(rect.height() * sy)),
        )


def monitors():
    import win32api
    from PySide6.QtGui import QGuiApplication

    native = {}
    for handle, _, bounds in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        native[info["Device"]] = bounds
    result = []
    for screen in QGuiApplication.screens():
        geo = screen.geometry()
        physical = native.get(screen.name())
        if physical is None:
            # Match a single display safely; don't silently scale a multi-monitor origin.
            if len(native) != 1:
                raise RuntimeError("无法匹配显示器坐标，请重新连接显示器后重试")
            physical = next(iter(native.values()))
        result.append(
            Monitor(screen.name(), (geo.x(), geo.y(), geo.width(), geo.height()), tuple(physical))
        )
    return result


def virtual_rect(screens):
    rect = QRect()
    for monitor in screens:
        rect = rect.united(QRect(*monitor.logical))
    return rect


def grab(rect, screens):
    """Composite physical tiles at a common density; return RGB + pixels/logical-unit."""
    import mss

    intersect = [m for m in screens if QRect(*m.logical).intersects(rect)]
    if not intersect:
        raise ValueError("选择区域不在显示器内")
    scale = max(m.scale for m in intersect)
    canvas = np.full(
        (max(1, round(rect.height() * scale)), max(1, round(rect.width() * scale)), 3),
        255,
        dtype=np.uint8,
    )
    with mss.MSS() as camera:
        for monitor in intersect:
            tile = QRect(*monitor.logical).intersected(rect)
            left, top, width, height = monitor.physical_rect(tile)
            shot = np.asarray(camera.grab(dict(left=left, top=top, width=width, height=height)))
            rgb = shot[:, :, [2, 1, 0]].copy()
            x = round((tile.x() - rect.x()) * scale)
            y = round((tile.y() - rect.y()) * scale)
            w = min(round(tile.width() * scale), canvas.shape[1] - x)
            h = min(round(tile.height() * scale), canvas.shape[0] - y)
            rgb = np.array(Image.fromarray(rgb).resize((w, h))) if rgb.shape[:2] != (h, w) else rgb
            canvas[y : y + h, x : x + w] = rgb
    return canvas, scale


def crop(snapshot, full_rect, rect, scale):
    x = round((rect.x() - full_rect.x()) * scale)
    y = round((rect.y() - full_rect.y()) * scale)
    w = round(rect.width() * scale)
    h = round(rect.height() * scale)
    return snapshot[y : y + h, x : x + w].copy()
