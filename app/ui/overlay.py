"""Screen-bounded translation overlays and a complete, scrollable bilingual view."""

from __future__ import annotations

import html

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from .. import config, i18n


def text_layout(text, font, width):
    layout = QTextLayout(text.replace("\n", "\u2028"), font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    y = 0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(20, width))
        line.setPosition(QPointF(0, y))
        y += line.height()
    layout.endLayout()
    return layout, y


class TranslationPanel(QWidget):
    retry_requested = Signal()
    locate_requested = Signal(str)

    def __init__(self, items):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(i18n.tr("双语译文", "Bilingual translation"))
        self.resize(650, 650)
        root = QVBoxLayout(self)
        row = QHBoxLayout()
        retry = QPushButton(i18n.tr("重试失败项", "Retry failed"))
        close = QPushButton(i18n.tr("关闭", "Close"))
        row.addWidget(retry)
        row.addStretch()
        row.addWidget(close)
        root.addLayout(row)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(lambda url: self.locate_requested.emit(url.toString()))
        root.addWidget(self.browser)
        retry.clicked.connect(self.retry_requested)
        close.clicked.connect(self.close)
        self.set_items(items)

    def set_items(self, items):
        scroll = self.browser.verticalScrollBar().value()
        out = []
        for item in items:
            source = html.escape(str(item["source"])).replace("\n", "<br>")
            translated = html.escape(
                str(
                    item.get("translation")
                    or item.get("error")
                    or i18n.tr("翻译中…", "Translating…")
                )
            ).replace("\n", "<br>")
            color = "#b64040" if item.get("error") else "#215d48"
            out.append(
                f'<p><a href="{item["id"]}">#{int(item["id"]) + 1}</a> {source}</p><p style="color:{color}">{translated}</p><hr>'
            )
        self.browser.setHtml("".join(out))
        self.browser.verticalScrollBar().setValue(scroll)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


class OverlayWindow(QWidget):
    dismissed = Signal()
    side_requested = Signal()

    def __init__(self, region, items, geometry=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._region = QRect(region)
        self._screen = QRect(geometry or QGuiApplication.primaryScreen().geometry())
        self.setGeometry(self._screen)
        self._cfg = config.load()
        self.overflow = False
        self._focus = None
        self._items = items
        self._boxes = []
        self.side = QPushButton(i18n.tr("双语侧栏", "Bilingual view"), self)
        self.side.clicked.connect(self.side_requested)
        self.side.setGeometry(max(8, self.width() - 220), 8, 120, 30)
        close = QPushButton("×", self)
        close.setGeometry(max(140, self.width() - 90), 8, 60, 30)
        close.clicked.connect(self.dismissed)
        self.set_items(items)

    def set_items(self, items):
        self._items = items
        self._boxes = []
        self.overflow = False
        units = []
        screen = QRectF(self._screen)
        for item in items:
            x0, y0, x1, y1 = item["bbox"]
            source = QRectF(
                self._region.x() + x0, self._region.y() + y0, max(1, x1 - x0), max(1, y1 - y0)
            )
            if screen.intersects(source):
                units.append((item, source.intersected(screen), not screen.contains(source)))
        placed = []
        for item, source, cross_screen in sorted(units, key=lambda r: (r[1].top(), r[1].left())):
            # Very small screens/fragments cannot hold a readable label. The
            # complete item remains available in the bilingual panel.
            if self.width() < 36 or self.height() < 80:
                self.overflow = True
                continue
            x = max(4.0, source.x() - self._screen.x() - 4)
            y = min(self.height() - 30.0, max(46.0, source.y() - self._screen.y() - 2))
            width = min(max(80, source.width() + 8), self.width() - x - 4)
            if width < 28:
                self.overflow = True
                continue
            next_y = float(self.height() - 4)
            for other, box, _ in units:
                if (
                    box.top() > source.top() + 3
                    and box.left() < source.right()
                    and box.right() > source.left()
                ):
                    next_y = min(next_y, box.top() - self._screen.y() - 3)
            room = max(0, next_y - y)
            if room < 14:
                self.overflow = True
                continue
            text = str(
                item.get("translation") or item.get("error") or i18n.tr("翻译中…", "Translating…")
            )
            font = QFont()
            point = int(self._cfg["ui"]["font_pt"])
            font.setPointSize(point)
            layout, height = text_layout(text, font, width - 8)
            while height + 8 > room and point > 9:
                point -= 1
                font.setPointSize(point)
                layout, height = text_layout(text, font, width - 8)
            rect = QRectF(x, y, width, max(height + 8, source.height() + 4))
            if (
                cross_screen
                or rect.bottom() > self.height() - 4
                or rect.height() > room
                or any(rect.intersects(r) for r in placed)
            ):
                self.overflow = True
                text = (
                    i18n.tr("↗ 侧栏", "↗ View")
                    if width < 150
                    else i18n.tr("完整译文见侧栏", "Full translation in bilingual view")
                )
                font.setPointSize(9)
                layout, height = text_layout(text, font, width - 8)
                rect.setHeight(min(room, height + 8))
            if any(rect.intersects(r) for r in placed):
                self.overflow = True
                continue
            placed.append(rect)
            self._boxes.append((str(item["id"]), rect, layout, bool(item.get("error"))))
        self.update()

    def highlight(self, unit):
        self._focus = unit
        self.update()
        self.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for unit, rect, layout, error in self._boxes:
            painter.setPen(
                QPen(
                    QColor("#ffcf45" if self._focus == unit else "#50aaff"),
                    2 if self._focus == unit else 1,
                )
            )
            painter.setBrush(QColor(24, 26, 30, int(self._cfg["ui"]["overlay_alpha"])))
            painter.drawRoundedRect(rect, 4, 4)
            painter.save()
            painter.setClipRect(rect.adjusted(3, 2, -3, -2))
            painter.setPen(QColor("#ffb3b3" if error else "#f0f4fa"))
            layout.draw(painter, rect.topLeft() + QPointF(4, 4))
            painter.restore()
        painter.end()

    def mousePressEvent(self, event):
        self.dismissed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.dismissed.emit()

    def show_centered(self):
        self.show()
        self.raise_()
        self.activateWindow()
