"""Floating dictionary / translation card window."""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..config import load
from ..services import tts


class InfoPopup(QWidget):
    save_requested = Signal(dict)
    context_requested = Signal(dict)
    closed = Signal()

    def __init__(self, title: str, body_html: str, entry: dict[str, Any] | None = None) -> None:
        super().__init__(None)
        self._entry = entry or {}
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._cfg = load()
        self._drag_offset = None
        width = int(self._cfg.get("ui", {}).get("popup_width", 460))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(self)
        frame.setObjectName("card")
        frame.setStyleSheet(
            """
            QFrame#card { background: #1f2329; border: 1px solid #3a4250; border-radius: 10px; }
            QLabel { color: #e8edf5; }
            QPushButton { background: #2c3542; color: #dfe7f3; border: 1px solid #445062;
                          border-radius: 7px; padding: 6px 10px; }
            QPushButton:hover { background: #37455a; }
            QPushButton:pressed { background: #24303f; }
            QPushButton:disabled { color: #718096; background: #252b34; border-color: #343d4b; }
            QPushButton#stopSpeech, QPushButton#closePopup {
                background: transparent; color: #abb8ca; border-color: #3a4250;
            }
            QPushButton#stopSpeech:hover, QPushButton#closePopup:hover { background: #2c3542; }
            QPushButton#explainContext {
                background: #2d6cdf; color: #ffffff; border-color: #4c80df; font-weight: 600;
            }
            QPushButton#explainContext:hover { background: #3779ed; }
            QPushButton#explainContext:pressed { background: #245bc1; }
            QPushButton#explainContext:disabled {
                background: #293b56; border-color: #3a5275; color: #94aac8;
            }
            """
        )
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.setSpacing(10)

        self._title = QLabel(html.unescape(title))
        self._title.setTextFormat(Qt.PlainText)
        self._title.setStyleSheet("font-size: 15px; font-weight: 700; color: #ffffff;")
        self._title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        inner.addWidget(self._title)

        body = QLabel(body_html)
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setMaximumWidth(width - 28)
        self._body_label = body
        scroll = QScrollArea()
        scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: #1f2329; border: 0; }"
        )
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        scroll.setMinimumHeight(min(360, max(100, body.heightForWidth(width - 60) + 8)))
        scroll.setMaximumHeight(420)
        inner.addWidget(scroll)

        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background: #343d4b; border: 0;")
        inner.addWidget(separator)

        controls = QVBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._speak_word = QPushButton(i18n.tr("🔊 单词", "🔊 Word"))
        self._speak_word.setToolTip(
            i18n.tr("朗读词条（Windows 离线语音）", "Speak the headword (offline Windows voice)")
        )
        buttons.addWidget(self._speak_word, 1)
        self._speak_context = QPushButton(i18n.tr("🔊 语境", "🔊 Context"))
        self._speak_context.setToolTip(
            i18n.tr("朗读包含该词的句子", "Speak the sentence containing the word")
        )
        self._speak_context.setEnabled(bool(self._entry.get("sentence")))
        buttons.addWidget(self._speak_context, 1)
        stop = QPushButton(i18n.tr("停止朗读", "Stop speech"))
        stop.setObjectName("stopSpeech")
        stop.clicked.connect(tts.stop)
        buttons.addWidget(stop, 1)
        controls.addLayout(buttons)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._copy = QPushButton(i18n.tr("复制", "Copy"))
        self._save = QPushButton(i18n.tr("保存卡片", "Save card"))
        close = QPushButton(i18n.tr("关闭", "Close"))
        close.setObjectName("closePopup")
        buttons.addWidget(self._copy, 1)
        buttons.addWidget(self._save, 1)
        buttons.addWidget(close, 1)
        controls.addLayout(buttons)
        self._save.setVisible(bool(self._entry.get("word")))
        self._explain = QPushButton(i18n.tr("解释语境（联网）", "Explain context (online)"))
        self._explain.setObjectName("explainContext")
        self._explain.setVisible(bool(self._entry.get("context_allowed")))
        self._explain.clicked.connect(lambda: self.context_requested.emit(dict(self._entry)))
        controls.addWidget(self._explain)
        inner.addLayout(controls)

        action_buttons = (
            self._speak_word,
            self._speak_context,
            stop,
            self._copy,
            self._save,
            close,
        )
        for button in action_buttons:
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(0)
        button_height = max(34, *(button.sizeHint().height() for button in action_buttons))
        for button in action_buttons:
            button.setFixedHeight(button_height)
        self._explain.setFixedHeight(button_height + 2)

        root.addWidget(frame)
        self._speak_word.clicked.connect(self._on_speak_word)
        if self._speak_context is not None:
            self._speak_context.clicked.connect(self._on_speak_context)
        self._copy.clicked.connect(self._on_copy)
        self._save.clicked.connect(self._on_save)
        close.clicked.connect(self.close)
        self.setFixedWidth(width)
        self.adjustSize()

    def context_busy(self, busy):
        self._explain.setEnabled(not busy)
        self._explain.setText(
            i18n.tr("解释中…", "Explaining…")
            if busy
            else i18n.tr("解释语境（联网）", "Explain context (online)")
        )

    # -- helpers ---------------------------------------------------------
    def word(self) -> str:
        return str((self._entry or {}).get("word") or "")

    def update_entry(self, entry: dict[str, Any]) -> None:
        """Merge new content into the card (used for async context translation)."""
        if not entry:
            return
        self._entry = {**(self._entry or {}), **entry}
        self._speak_context.setEnabled(bool(self._entry.get("sentence")))
        self.context_busy(False)
        title, body = dictionary_html(
            self._entry,
            source=str(self._entry.get("source_lang") or self._entry.get("word") or ""),
            target=str(self._entry.get("target_lang") or "zh"),
        )
        self._title.setText(html.unescape(title))
        self._body_label.setText(body)
        self.adjustSize()

    def _position_near(self, pos: QPoint | None) -> None:
        screen = QGuiApplication.screenAt(pos) if pos else QGuiApplication.primaryScreen()
        geo = (
            screen.availableGeometry()
            if screen
            else QGuiApplication.primaryScreen().availableGeometry()
        )
        w, h = self.width(), self.height()
        if pos is None:
            pos = geo.center()
        x = pos.x() + 18
        y = pos.y() + 20
        if x + w > geo.right():
            x = max(geo.left(), pos.x() - w - 12)
        if y + h > geo.bottom():
            y = max(geo.top(), pos.y() - h - 12)
        self.move(x, y)

    def show_at(self, pos: QPoint | None = None) -> None:
        self._position_near(pos)
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_copy(self) -> None:
        data = self._entry or {}
        text = data.get("copy_text") or self._title.text()
        QGuiApplication.clipboard().setText(text)

    @staticmethod
    def _lang_for(text: str) -> str:
        import re

        if re.search(r"[\u3040-\u30ff]", text):
            return "ja"
        if re.search(r"[\uac00-\ud7af]", text):
            return "ko"
        if re.search(r"[\u0400-\u04ff]", text):
            return "ru"
        if re.search(r"[\u4e00-\u9fff]", text):
            return "zh"
        return "en"

    def _on_speak_word(self) -> None:
        data = self._entry or {}
        text = str(data.get("copy_text") or data.get("word") or self._title.text())
        tts.speak(text, self._speech_language(text))

    def _speech_language(self, text):
        data = self._entry or {}
        if str(data.get("source", "")).startswith("jmdict:"):
            return "ja"
        chosen = data.get("source_lang") if data.get("word") else data.get("target_lang")
        if chosen and chosen != "auto":
            return str(chosen)
        return self._lang_for(text)

    def _on_speak_context(self) -> None:
        text = str((self._entry or {}).get("sentence") or "")
        if text:
            tts.speak(text, self._speech_language(text))

    def _on_save(self) -> None:
        if self._entry:
            self.save_requested.emit(dict(self._entry))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


def dictionary_html(result: dict, *, source: str, target: str) -> tuple[str, str]:
    """Return (title, html body) for a dictionary lookup result."""
    word = html.escape(str(result.get("word") or source))
    reading = result.get("reading")
    ipa = result.get("ipa")
    pos = result.get("part_of_speech")
    translation = html.escape(str(result.get("translation") or ""))
    definition = html.escape(str(result.get("definition") or ""))
    synonyms = result.get("synonyms") or []
    forms = result.get("forms") or []
    cefr = result.get("cefr")
    example = result.get("example") or result.get("explanation")

    bits: list[str] = []
    if reading:
        bits.append(f"<span style='color:#9fb3cc'>{html.escape(str(reading))}</span>")
    if ipa:
        bits.append(f"<span style='color:#9fb3cc'>/{html.escape(str(ipa).strip('/'))}/</span>")
    if pos:
        bits.append(f"<span style='color:#7fb2ff'>{html.escape(str(pos))}</span>")
    if cefr:
        bits.append(f"<span style='color:#c9a227'>{html.escape(str(cefr))}</span>")
    header = " &nbsp;·&nbsp; ".join(bits)
    body = []
    if result.get("source"):
        body.append(
            "<div style='color:#9fb3cc'>"
            + html.escape(str(result["source"]))
            + " · "
            + html.escape(str(result.get("definition_language") or ""))
            + "</div><br>"
        )
    if header:
        body.append(header)
    if translation:
        body.append(f"<div style='font-size:13px;color:#cfeee0;margin-top:4px'>{translation}</div>")
    if definition:
        body.append(f"<div style='margin-top:4px'>{definition.replace(chr(10), '<br>')}</div>")
    if synonyms:
        body.append(
            "<div style='margin-top:4px;color:#9fb3cc'>"
            + i18n.tr("近义：", "Synonyms: ")
            + "，".join(html.escape(str(s)) for s in synonyms[:8])
            + "</div>"
        )
    if forms:
        body.append(
            "<div style='color:#9fb3cc'>"
            + i18n.tr("变形：", "Forms: ")
            + "，".join(html.escape(str(s)) for s in forms[:8])
            + "</div>"
        )
    if example:
        body.append(
            f"<div style='margin-top:4px;font-style:italic'>{html.escape(str(example))}</div>"
        )
    context_meaning = result.get("context_meaning")
    sentence = result.get("sentence")
    sentence_translation = result.get("sentence_translation")
    if context_meaning:
        body.append(
            "<div style='margin-top:6px;color:#9fb3cc'>"
            + i18n.tr("语境义：", "In context: ")
            + html.escape(str(context_meaning))
            + "</div>"
        )
    if sentence:
        body.append(
            "<div style='margin-top:6px;border-left:3px solid #3a4250;padding-left:8px;"
            "font-style:italic'>“" + html.escape(str(sentence)) + "”</div>"
        )
    if sentence_translation:
        body.append(
            "<div style='color:#cfeee0;padding-left:11px'>"
            + html.escape(str(sentence_translation))
            + "</div>"
        )
    if not body:
        body.append("<div>" + i18n.tr("（没有查到结果）", "(no result)") + "</div>")
    return word, "".join(body)
