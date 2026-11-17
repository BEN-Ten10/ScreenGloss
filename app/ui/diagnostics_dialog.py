import json
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app import config, i18n
from app.services import diagnostics, ocr, translate

from .async_jobs import Jobs


class DiagnosticsDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(i18n.tr("诊断与缓存", "Diagnostics and cache"))
        self.resize(760, 600)
        self.jobs = Jobs(self)
        root = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        root.addWidget(self.text)
        row = QHBoxLayout()
        root.addLayout(row)
        for zh, en, fn in [
            ("刷新", "Refresh", self.refresh),
            ("检查模型", "Check models", self.check_models),
            ("清理翻译缓存", "Clear cache", self.clear),
            ("清理调试截图", "Clear debug screenshots", self.clear_debug),
            ("导出脱敏诊断", "Export diagnostics", self.export),
        ]:
            b = QPushButton(i18n.tr(zh, en))
            b.clicked.connect(fn)
            row.addWidget(b)
        self.status = QLabel()
        root.addWidget(self.status)
        self.limit = QSpinBox()
        self.limit.setRange(5, 1000)
        self.limit.setSuffix(" MiB")
        self.limit.setValue(config.load()["cache"]["max_mb"])
        root.addWidget(
            QLabel(
                i18n.tr(
                    "翻译缓存上限（修改后立即保存）", "Translation cache limit (saved immediately)"
                )
            )
        )
        root.addWidget(self.limit)
        self.limit.valueChanged.connect(self.set_limit)
        self.refresh()

    def set_limit(self, value):
        config.update("cache", {"max_mb": value})

    def refresh(self):
        def done(result, error):
            if error:
                self.status.setText(str(error))
                return
            snap, count = result
            self.text.setPlainText(json.dumps(snap, ensure_ascii=False, indent=2))
            self.status.setText(i18n.tr(f"缓存 {count} 项", f"{count} cached entries"))

        self.jobs.run(lambda: (diagnostics.snapshot(), translate.cache_size()), done)

    def clear(self):
        self.jobs.run(
            translate.clear_cache,
            lambda result, error: self.status.setText(
                str(error) if error else i18n.tr("缓存已清理", "Cache cleared")
            ),
        )

    def export(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "ScreenGloss", str(config.DATA_DIR / "diagnostics.json"), "JSON (*.json)"
        )
        if filename:
            self.jobs.run(
                lambda: diagnostics.export(Path(filename)),
                lambda result, error: self.status.setText(str(error or result)),
            )

    def check_models(self):
        self.status.setText(i18n.tr("检查中…", "Checking…"))

        def check():
            result = {}
            for lang in ("ch", "ja", "ko", "ru"):
                try:
                    ocr._get_engine(lang)
                    result[lang] = "OK"
                except Exception as exc:
                    result[lang] = type(exc).__name__
            return result

        self.jobs.run(check, lambda result, error: self.status.setText(str(error or result)))

    def clear_debug(self):
        self.jobs.run(
            diagnostics.clear_debug,
            lambda result, error: self.status.setText(
                str(error) if error else i18n.tr("调试截图已清理", "Debug screenshots cleared")
            ),
        )

    def done(self, result):
        self.jobs.enabled = False
        super().done(result)
