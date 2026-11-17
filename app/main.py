"""ScreenGloss portable tray application. GUI coordination only; work runs in services."""

from __future__ import annotations

import ctypes
import html
import os
import sys
from functools import partial
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QWidget,
)

from app import config, i18n
from app.services import capture, cards, dictionary, ocr, system, translate, tts, workflow
from app.services.hotkeys import HotkeyManager
from app.services.logging_service import event, setup, shutdown
from app.services.tasks import TaskRunner
from app.ui.async_jobs import Jobs
from app.ui.cards_dialog import CardsDialog
from app.ui.diagnostics_dialog import DiagnosticsDialog
from app.ui.overlay import OverlayWindow, TranslationPanel
from app.ui.popup import InfoPopup, dictionary_html
from app.ui.selector import RegionSelector
from app.ui.settings_dialog import SettingsDialog


def make_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2d6cdf"))
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QPen(QColor("white"), 5))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(14, 12, 26, 26)
    painter.drawLine(37, 36, 50, 50)
    painter.end()
    return QIcon(pixmap)


def acquire_single_instance():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, "Local\\ScreenGloss.Instance")
    if not handle:
        raise OSError("无法创建单实例锁")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None
    return handle


class Bridge(QObject):
    action = Signal(str)
    index_progress = Signal(int)


class ScreenGlossApp(QObject):
    def __init__(self, qapp):
        super().__init__()
        self.qapp = qapp
        self.cfg = config.load()
        i18n.set_language(self.cfg["ui"]["language"])
        self.runner = TaskRunner()
        self.runner.completed.connect(self._result)
        self.runner.progress.connect(self._progress)
        self.jobs = Jobs(self)
        self._selector = None
        self._popup = None
        self._panel = None
        self._overlays = []
        self._screen_state = None
        self._windows = []
        self._closing = False
        self._hover_enabled = self.cfg["hover"]["enabled"]
        self._hover_pos = QCursor.pos()
        self._hover_at = 0
        self._hover_done = None
        self.bridge = Bridge()
        self.bridge.action.connect(self._action)
        self.bridge.index_progress.connect(
            lambda count: self.tray.setToolTip(
                i18n.tr(f"ScreenGloss · 词典索引 {count:,}", f"ScreenGloss · indexing {count:,}")
            )
        )
        self.hotkeys = HotkeyManager()
        self.hotkeys.install(qapp)
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("ScreenGloss 1.1")
        self.tray.activated.connect(self._tray_clicked)
        self._menu()
        self.tray.show()
        self._register_hotkeys()
        self.status = QWidget(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.status.setWindowTitle("ScreenGloss")
        row = QHBoxLayout(self.status)
        self.status_text = QLabel()
        cancel = QPushButton(i18n.tr("取消", "Cancel"))
        cancel.clicked.connect(self.cancel)
        row.addWidget(self.status_text)
        row.addWidget(cancel)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._hover)
        self.timer.start()
        tts.notifications.warning.connect(
            lambda message: self.notify(i18n.tr("朗读", "Speech"), message)
        )
        tts.cleanup()
        self._warm_task = None
        QTimer.singleShot(0, self._warm)
        options = self.cfg["system"]
        if options["desktop_shortcut"] or options["autostart"]:
            self.jobs.run(
                lambda: system.ensure(options["desktop_shortcut"], options["autostart"]),
                lambda result, error: self._system_result(result, error),
            )
        event("app.started")

    def notify(self, title, message):
        if not self._closing:
            self.tray.showMessage(title, i18n.translate_message(str(message))[:300])

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.open_settings()

    def _menu(self):
        menu = QMenu()
        for zh, en, action in [
            ("屏幕翻译（框选）", "Screen translation (select area)", "screen"),
            ("弹出词典（指针处）", "Dictionary at pointer", "dict"),
            ("翻译剪贴板", "Translate clipboard", "text"),
        ]:
            item = menu.addAction(i18n.tr(zh, en))
            item.triggered.connect(partial(self._action, action))
        hover = menu.addAction(i18n.tr("悬浮取词（自动）", "Hover lookup (auto)"))
        hover.setCheckable(True)
        hover.setChecked(self._hover_enabled)
        hover.toggled.connect(self.set_hover)
        self._hover_action = hover
        menu.addSeparator()
        menu.addAction(i18n.tr("取消词典预加载", "Cancel dictionary preload")).triggered.connect(
            lambda: self._warm_task.cancelled.set() if self._warm_task else None
        )
        for zh, en, fn in [
            ("取消当前任务", "Cancel current task", self.cancel),
            ("词卡管理", "Word cards", self.open_cards),
            ("导出 Anki TSV（复习卡片）", "Export Anki TSV (review cards)", self.export_cards),
            ("诊断与缓存", "Diagnostics and cache", self.open_diagnostics),
            (
                "打开数据目录（卡片/日志）",
                "Open data folder (cards/logs)",
                lambda: os.startfile(str(config.DATA_DIR)),
            ),
        ]:
            menu.addAction(i18n.tr(zh, en)).triggered.connect(fn)
        menu.addSeparator()
        menu.addAction(i18n.tr("设置…", "Settings…")).triggered.connect(self.open_settings)
        menu.addAction(i18n.tr("退出", "Quit")).triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu

    def _register_hotkeys(self):
        names = {
            "screen_translate": "screen",
            "dictionary": "dict",
            "translate_clipboard": "text",
            "hover_toggle": "hover",
        }
        pairs = [
            (self.cfg["hotkeys"][key], partial(self.bridge.action.emit, value))
            for key, value in names.items()
        ]
        if len({key for key, _ in pairs}) != len(pairs):
            problems = ["快捷键重复，保留原绑定"]
        else:
            problems = self.hotkeys.register(dict(pairs))
        if problems:
            self.notify("ScreenGloss", "；".join(problems))

    def _action(self, name, *_):
        if self._closing:
            return
        if name == "screen":
            self.start_screen_translate()
        elif name == "dict":
            self.start_dictionary()
        elif name == "text":
            self.start_clipboard_translate()
        elif name == "hover":
            self.set_hover(not self._hover_enabled)

    def set_hover(self, enabled):
        self._hover_enabled = bool(enabled)
        self.cfg["hover"]["enabled"] = bool(enabled)
        self._hover_done = None
        self._hover_action.blockSignals(True)
        self._hover_action.setChecked(bool(enabled))
        self._hover_action.blockSignals(False)
        try:
            config.update("hover", {"enabled": bool(enabled)})
        except Exception as error:
            self.notify("ScreenGloss", str(error))

    def _hover(self):
        import time

        if not self._hover_enabled or self._closing:
            return
        point = QCursor.pos()
        now = time.monotonic()
        if point != self._hover_pos:
            self._hover_pos = point
            self._hover_at = now
            current = self.runner._current
            if current and current.kind == "hover":
                self.runner.cancel()
                self.status.hide()
            return
        if now - self._hover_at < self.cfg["hover"]["delay_ms"] / 1000 or self._hover_done == point:
            return
        if self.status.isVisible() or self._selector or any(w.isVisible() for w in self._overlays):
            return
        if QApplication.widgetAt(point) is not None:
            return
        if any(w.isVisible() for w in self._windows):
            return
        self._hover_done = QPoint(point)
        self.start_dictionary(point, hover=True)

    def cancel(self):
        self.runner.cancel()
        self.status.hide()
        if self._selector:
            self._selector.close()
            self._selector = None
        if self._popup:
            self._popup.context_busy(False)

    def _begin(self, kind, *, close_results=True):
        self.cancel()
        if close_results:
            self.close_overlays()
            if self._panel:
                self._panel.close()
                self._panel = None
            if self._popup:
                self._popup.close()
                self._popup = None
        return self.runner.begin(kind, self.cfg)

    def _show_status(self, text):
        self.status_text.setText(text)
        self.status.adjustSize()
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.status.move(geo.right() - self.status.width() - 20, geo.top() + 12)
        self.status.show()

    def _progress(self, payload):
        task, stage = payload
        if not self.runner.active(task):
            return
        if isinstance(stage, dict):
            self._update_screen(stage["items"])
            return
        if task.kind != "hover":
            self._show_status(stage)

    def start_screen_translate(self):
        task = self._begin("screen")
        try:
            screens = capture.monitors()
            virtual = capture.virtual_rect(screens)
        except Exception as error:
            self.notify("ScreenGloss", str(error))
            return
        if task.cfg["screen"]["freeze_selection"]:
            QTimer.singleShot(
                80,
                lambda: (
                    self.runner.submit(
                        task,
                        lambda task: {
                            "stage": "frozen",
                            "capture": capture.grab(virtual, screens),
                            "virtual": virtual,
                            "screens": screens,
                        },
                    )
                    if self.runner.active(task)
                    else None
                ),
            )
        else:
            self._select(task, screens, virtual, None)

    def _select(self, task, screens, virtual, frozen):
        self.status.hide()
        background = None
        if frozen:
            image, scale = frozen
            h, w = image.shape[:2]
            background = QPixmap.fromImage(
                QImage(image.data, w, h, image.strides[0], QImage.Format_RGB888).copy()
            )
        selector = RegionSelector(background)
        self._selector = selector

        def selected(rect):
            self._selector = None
            if not self.runner.active(task):
                return
            if rect is None:
                self.cancel()
                return

            def run(task):
                if frozen:
                    image, scale = capture.crop(frozen[0], virtual, rect, frozen[1]), frozen[1]
                else:
                    image, scale = capture.grab(rect, screens)
                return {
                    "stage": "recognized",
                    "units": workflow.screen_units(task, image, scale),
                    "rect": rect,
                    "screens": screens,
                }

            QTimer.singleShot(
                80,
                lambda: (
                    self.runner.submit(task, run, stage=i18n.tr("正在识别…", "Recognizing…"))
                    if self.runner.active(task)
                    else None
                ),
            )

        selector.selected.connect(selected)
        selector.show()
        selector.raise_()
        selector.activateWindow()

    def _translate_screen(self, task, units, rect, screens):
        self._screen_state = {"units": units, "rect": rect, "screens": screens}
        missing = [i for i, unit in enumerate(units) if not unit.get("translation")]

        def work(task):
            def merged(rows):
                updated = [dict(r) for r in units]
                for original, row in zip(missing, rows):
                    updated[original].update(translation=row["translation"], error=row["error"])
                return updated

            rows = translate.translate_units(
                [units[i]["source"] for i in missing],
                **workflow.api_args(task.cfg, task),
                on_batch=lambda rows: self.runner.progress.emit((task, {"items": merged(rows)})),
            )
            return {"stage": "translated", "units": merged(rows)}

        self.runner.submit(task, work, network=True, stage=i18n.tr("正在翻译…", "Translating…"))

    def _update_screen(self, units):
        if not self._screen_state:
            return
        self._screen_state["units"] = units
        for overlay in self._overlays:
            overlay.set_items(units)
        if self._panel:
            self._panel.set_items(units)
        if any(w.overflow for w in self._overlays):
            self.open_panel()

    def close_overlays(self):
        for window in self._overlays:
            window.close()
            window.deleteLater()
        self._overlays = []

    def dismiss_screen(self):
        """User dismissal also invalidates queued results and progress updates."""
        self.cancel()
        self.close_overlays()
        self._screen_state = None
        if self._panel:
            self._panel.close()
            self._panel = None

    def _show_screen(self, units, rect, screens):
        self.close_overlays()
        for monitor in screens:
            geometry = QRect(*monitor.logical)
            if not geometry.intersects(rect):
                continue
            window = OverlayWindow(rect, units, geometry)
            window.dismissed.connect(self.dismiss_screen)
            window.side_requested.connect(self.open_panel)
            self._overlays.append(window)
            window.show_centered()

    def open_panel(self):
        if not self._screen_state:
            return
        if self._panel is None:
            self._panel = TranslationPanel(self._screen_state["units"])
            self._panel.retry_requested.connect(self.retry_screen)
            self._panel.locate_requested.connect(
                lambda unit: [window.highlight(unit) for window in self._overlays]
            )
        self._panel.show()
        self._panel.raise_()

    def retry_screen(self):
        if not self._screen_state:
            return
        state = self._screen_state
        task = self._begin("screen", close_results=False)
        self._translate_screen(task, state["units"], state["rect"], state["screens"])

    def start_dictionary(self, pos=None, hover=False):
        point = QPoint(pos) if pos is not None else QCursor.pos()
        task = self._begin("hover" if hover else "dictionary")
        try:
            screens = capture.monitors()
            monitor = next(m for m in screens if QRect(*m.logical).contains(point))
            geo = QRect(*monitor.logical)
            cfg = task.cfg["dictionary"]
            width = min(geo.width(), int(cfg["capture_width"]))
            height = min(geo.height(), int(cfg["capture_height"]))
            if not cfg["context_enabled"]:
                width = min(width, 420)
                height = min(height, 80)
            rect = QRect(
                max(geo.left(), min(point.x() - width // 2, geo.right() - width + 1)),
                max(geo.top(), min(point.y() - height // 2, geo.bottom() - height + 1)),
                width,
                height,
            )
        except Exception as error:
            self.notify("ScreenGloss", str(error))
            return

        def work(task):
            image, scale = capture.grab(rect, screens)
            entry = workflow.lookup_capture(
                task, image, (point.x() - rect.x()) * scale, (point.y() - rect.y()) * scale
            )
            return {"stage": "lookup_capture", "entry": entry, "pos": point}

        self.runner.submit(task, work, stage=i18n.tr("正在取词…", "Reading word…"))

    def _show_word(self, task, entry, point):
        title, body = dictionary_html(
            entry, source=entry["word"], target=task.cfg["languages"]["target"]
        )
        if self._popup:
            self._popup.close()
        popup = InfoPopup(title, body, entry)
        self._popup = popup
        popup.save_requested.connect(self.save_card)
        popup.context_requested.connect(self.explain_context)
        popup.closed.connect(
            lambda: (
                self.cancel()
                if self.runner._current and self.runner._current.kind == "context"
                else None
            )
        )
        popup.show_at(point)

    def explain_context(self, entry):
        if not self._popup or not self._popup.isVisible():
            return
        popup = self._popup
        task = self._begin("context", close_results=False)
        popup.context_busy(True)
        self.runner.submit(
            task,
            lambda task: {
                "stage": "context",
                "entry": workflow.explain(task, entry),
                "popup": popup,
            },
            network=True,
            stage=i18n.tr("解释语境…", "Explaining context…"),
        )

    def start_clipboard_translate(self):
        text = QGuiApplication.clipboard().text().strip()
        if not text:
            self.notify("ScreenGloss", i18n.tr("剪贴板里没有文字", "Clipboard is empty"))
            return
        task = self._begin("clipboard")
        point = QCursor.pos()
        self.runner.submit(
            task,
            lambda task: {
                "stage": "clipboard",
                "text": translate.translate_lines([text], **workflow.api_args(task.cfg, task))[0],
                "pos": point,
            },
            network=True,
            stage=i18n.tr("正在翻译…", "Translating…"),
        )

    def _result(self, payload):
        task, result, error = payload
        if not self.runner.active(task):
            return
        if error:
            self.status.hide()
            if self._popup:
                self._popup.context_busy(False)
            if task.kind != "hover":
                self.notify(i18n.tr("操作失败", "Operation failed"), str(error))
            return
        stage = result["stage"]
        if stage == "frozen":
            self._select(task, result["screens"], result["virtual"], result["capture"])
        elif stage == "recognized":
            self._show_screen(result["units"], result["rect"], result["screens"])
            self._translate_screen(task, result["units"], result["rect"], result["screens"])
        elif stage == "translated":
            self.status.hide()
            self._update_screen(result["units"])
            if any(u.get("error") for u in result["units"]):
                self.open_panel()
        elif stage == "lookup_capture":
            entry = result["entry"]
            point = result["pos"]
            self.runner.submit(
                task,
                lambda task: {
                    "stage": "lookup",
                    "entry": workflow.lookup_entry(task, entry),
                    "pos": point,
                },
                network=True,
                stage=i18n.tr("正在查词…", "Looking up…"),
            )
        elif stage == "lookup":
            self.status.hide()
            self._show_word(task, result["entry"], result["pos"])
        elif stage == "context":
            self.status.hide()
            if self._popup is result["popup"] and self._popup.isVisible():
                self._popup.update_entry(result["entry"])
        elif stage == "clipboard":
            self.status.hide()
            body = "<div>" + html.escape(result["text"]).replace("\n", "<br>") + "</div>"
            self._popup = InfoPopup(
                i18n.tr("翻译结果", "Translation"),
                body,
                {"copy_text": result["text"], "target_lang": task.cfg["languages"]["target"]},
            )
            self._popup.show_at(result["pos"])

    def save_card(self, entry):
        entry = dict(entry)
        entry["explanation"] = entry.get("example") or entry.get("explanation")
        entry["context_translation"] = entry.get("sentence_translation")
        self.jobs.run(
            lambda: cards.add_card(config.db_path(), entry),
            lambda result, error: self.notify(
                "ScreenGloss", str(error) if error else i18n.tr("词卡已保存", "Card saved")
            ),
        )

    def export_cards(self):
        self.jobs.run(
            lambda: cards.export_tsv(config.db_path(), config.DATA_DIR / "anki_export.tsv"),
            lambda result, error: self.notify("ScreenGloss", str(error or result)),
        )

    def _dialog(self, dialog):
        self.cancel()
        self._windows.append(dialog)
        dialog.finished.connect(
            lambda *_: self._windows.remove(dialog) if dialog in self._windows else None
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_cards(self):
        self._dialog(CardsDialog())

    def open_diagnostics(self):
        self._dialog(DiagnosticsDialog())

    def _system_result(self, result, error):
        if error or result:
            self.notify("ScreenGloss", str(error or "；".join(result)))

    def open_settings(self):
        self.cancel()
        dialog = SettingsDialog()
        self._windows.append(dialog)
        try:
            if dialog.exec():
                old = dict(self.cfg["system"])
                cfg = dialog.result_config()
                key = dialog.api_key.text().strip()
                config.save(cfg, persist_key=dialog.remember_key.isChecked())
                config.set_session_key(key)
                self.cfg = config.load()
                i18n.set_language(self.cfg["ui"]["language"])
                self._hover_enabled = self.cfg["hover"]["enabled"]
                self._menu()
                self._register_hotkeys()
                new = self.cfg["system"]
                self.jobs.run(
                    lambda: system.apply(
                        new["desktop_shortcut"]
                        if new["desktop_shortcut"] != old["desktop_shortcut"]
                        else None,
                        new["autostart"] if new["autostart"] != old["autostart"] else None,
                    ),
                    self._system_result,
                )
        except Exception as error:
            self.notify("ScreenGloss", str(error))
        finally:
            self._windows.remove(dialog)

    def _warm(self):
        from app.services.tasks import Task

        self._warm_task = Task(0, "startup", self.cfg)

        def work():
            ocr._get_engine(self.cfg["ocr"]["lang"])
            return dictionary.preload(self._warm_task, self.bridge.index_progress.emit)

        def done(result, error):
            event(
                "startup.warmup",
                count=result or 0,
                category=type(error).__name__ if error else "ok",
            )
            self.tray.setToolTip("ScreenGloss 1.1")

        self.jobs.run(work, done)

    def quit(self):
        self._closing = True
        self.timer.stop()
        self.jobs.enabled = False
        if self._warm_task:
            self._warm_task.cancelled.set()
        self.runner.close()
        tts.stop()
        self.hotkeys.unregister()
        self.close_overlays()
        self.status.close()
        for window in list(self._windows):
            window.close()
        if self._popup:
            self._popup.close()
        if self._panel:
            self._panel.close()
        if self._selector:
            self._selector.close()
        self.tray.hide()
        event("app.stopped")
        self.qapp.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ScreenGloss")
    app.setQuitOnLastWindowClosed(False)
    instance = None
    try:
        setup(console=sys.stdout is not None)
        cfg = config.load()
        i18n.set_language(cfg["ui"]["language"])

        def hook(kind, error, tb):
            # Traceback locations are useful, exception text may contain private input.
            import logging
            import traceback

            logging.getLogger("screengloss").error(
                "uncaught %s\n%s", kind.__name__, "".join(traceback.format_tb(tb))
            )
            QMessageBox.critical(
                None,
                "ScreenGloss",
                i18n.tr(
                    "发生意外错误，请查看诊断日志。", "Unexpected error. Please check diagnostics."
                ),
            )

        sys.excepthook = hook
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("系统托盘不可用")
        instance = acquire_single_instance()
        if instance is None:
            QMessageBox.information(
                None,
                "ScreenGloss",
                i18n.tr(
                    "ScreenGloss 已在运行，请查看系统托盘。",
                    "ScreenGloss is already running. Check the system tray.",
                ),
            )
            return 0
        controller = ScreenGlossApp(app)
        app.aboutToQuit.connect(lambda: controller.quit() if not controller._closing else None)
        return app.exec()
    except Exception as error:
        QMessageBox.critical(None, "ScreenGloss", str(error))
        return 1
    finally:
        if instance:
            kernel = ctypes.WinDLL("kernel32")
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(instance)
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
