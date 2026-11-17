"""Full-screen rubber-band region selector used by screen translation."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget


class RegionSelector(QWidget):
    selected = Signal(object)  # QRect in global screen coordinates, or None

    def __init__(self, background: QPixmap | None = None) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        geometry = QRect()
        for screen in QGuiApplication.screens():
            geometry = geometry.united(screen.geometry())
        self._virtual = geometry
        self.setGeometry(geometry)
        self._background = background
        self._origin: QPoint | None = None
        self._current: QPoint | None = None

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        if self._background is not None:
            painter.drawPixmap(self.rect(), self._background)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._origin and self._current:
            rect = QRect(self._origin, self._current).normalized()
            if self._background is not None:
                # Reveal the frozen pixels inside the rubber band.
                painter.drawPixmap(rect, self._background_region(rect))
            else:
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.fillRect(rect, Qt.transparent)
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(80, 170, 255), 2))
            painter.drawRect(rect)

    def _background_region(self, rect: QRect) -> QPixmap:
        if self._background is None:
            return QPixmap()
        sx = self._background.width() / max(1, self._virtual.width())
        sy = self._background.height() / max(1, self._virtual.height())
        source = QRect(
            int(rect.x() * sx),
            int(rect.y() * sy),
            max(1, int(rect.width() * sx)),
            max(1, int(rect.height() * sy)),
        )
        return self._background.copy(source)

    # -- input ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._origin is None:
            return
        rect = QRect(self._origin, event.position().toPoint()).normalized()
        self.close()
        if rect.width() < 6 or rect.height() < 6:
            self.selected.emit(None)
            return
        global_rect = QRect(rect.topLeft() + self._virtual.topLeft(), rect.size())
        self.selected.emit(global_rect)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            self.selected.emit(None)
