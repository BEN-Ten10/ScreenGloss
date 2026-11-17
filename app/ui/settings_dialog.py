"""Settings dialog: API/model, languages, hotkeys, dictionary and OCR sources."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..config import load
from ..services import dictionary, ocr, translate
from ..services.hotkeys import parse
from .async_jobs import Cancellation, Jobs

LANGS = [
    ("auto", "自动检测"),
    ("zh", "中文"),
    ("en", "英语"),
    ("ja", "日语"),
    ("ko", "韩语"),
    ("ru", "俄语"),
    ("de", "德语"),
    ("fr", "法语"),
    ("es", "西班牙语"),
    ("pt", "葡萄牙语"),
    ("it", "意大利语"),
]


class SettingsDialog(QDialog):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowTitle("ScreenGloss 设置")
        self.setMinimumWidth(600)
        self.resize(660, 580)
        self._cfg = load()
        self.jobs = Jobs(self)
        self.jobs.progress.connect(
            lambda count: self.add_dict_button.setText(
                i18n.tr(f"已索引 {count:,}", f"Indexed {count:,}")
            )
        )

        tabs = QTabWidget()
        tabs.addTab(self._build_connection_tab(), "连接与翻译")
        tabs.addTab(self._build_input_tab(), "取词与快捷键")
        tabs.addTab(self._build_dictionary_tab(), "词典")
        tabs.addTab(self._build_ocr_tab(), "OCR 与外观")
        tabs.addTab(self._build_system_tab(), "系统")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)
        for standard, text in ((QDialogButtonBox.Save, "保存"), (QDialogButtonBox.Cancel, "取消")):
            try:
                buttons.button(standard).setText(text)
            except Exception:
                pass
        i18n.translate_widgets(self)
        self.setWindowTitle(i18n.tr("ScreenGloss 设置", "ScreenGloss Settings"))

    # -- tabs ------------------------------------------------------------
    def _build_connection_tab(self) -> QWidget:
        api = self._cfg["api"]
        form = QFormLayout()
        self.ui_language = QComboBox()
        for code, label in i18n.LANGUAGES:
            self.ui_language.addItem(label, code)
        language_index = self.ui_language.findData(
            str(self._cfg.get("ui", {}).get("language", "en"))
        )
        self.ui_language.setCurrentIndex(
            language_index if language_index >= 0 else self.ui_language.findData("en")
        )
        form.addRow("界面语言", self.ui_language)
        self.base_url = QLineEdit(str(api.get("base_url", "")))
        self.api_key = QLineEdit(str(api.get("api_key", "")))
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("留空则使用环境变量 DEEPSEEK_API_KEY")
        self._initial_key = self.api_key.text().strip()
        self.model = QComboBox()
        self.model.setEditable(True)
        current_model = str(api.get("model", "deepseek-chat"))
        self.model.addItem(current_model)
        self.model.setCurrentText(current_model)
        form.addRow("API Base URL", self.base_url)
        form.addRow("API Key", self.api_key)
        self.remember_key = QCheckBox(
            i18n.tr("记住密钥（明文保存在本目录）", "Remember key (plain text in this folder)")
        )
        self.remember_key.setChecked(bool(load(resolve_key=False)["api"].get("api_key")))
        form.addRow("", self.remember_key)
        clear = QPushButton(i18n.tr("清除已保存密钥", "Clear saved key"))
        clear.clicked.connect(lambda: (self.api_key.clear(), self.remember_key.setChecked(False)))
        form.addRow("", clear)
        form.addRow("模型", self.model)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接并获取模型")
        self.api_status = QLabel("")
        self.api_status.setWordWrap(True)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.api_status, 1)
        form.addRow("", test_row)
        self.test_button.clicked.connect(self._test_api)

        self.source = QComboBox()
        self.target = QComboBox()
        for code, label in LANGS:
            self.source.addItem(f"{label} ({code})", code)
        for code, label in LANGS[1:]:
            self.target.addItem(f"{label} ({code})", code)
        self._select(self.source, str(self._cfg["languages"].get("source", "auto")))
        self._select(self.target, str(self._cfg["languages"].get("target", "zh")))
        form.addRow("源语言", self.source)
        form.addRow("目标语言", self.target)

        self.tts_engine = QComboBox()
        self.tts_engine.addItem("Windows SAPI（离线）", "sapi")
        self.tts_engine.addItem("Edge-TTS（在线，音质更好）", "edge")
        current_tts = str(self._cfg.get("tts", {}).get("engine", "sapi"))
        index = self.tts_engine.findData(current_tts)
        self.tts_engine.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("发音引擎", self.tts_engine)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(form)
        layout.addStretch(1)
        return tab

    def _build_input_tab(self) -> QWidget:
        hot = self._cfg["hotkeys"]
        form = QFormLayout()
        self.hk_screen = QLineEdit(str(hot.get("screen_translate", "alt+s")))
        self.hk_dict = QLineEdit(str(hot.get("dictionary", "alt+d")))
        self.hk_text = QLineEdit(str(hot.get("translate_clipboard", "alt+t")))
        self.hk_hover = QLineEdit(str(hot.get("hover_toggle", "alt+h")))
        form.addRow("屏幕翻译", self.hk_screen)
        form.addRow("弹出词典", self.hk_dict)
        form.addRow("翻译剪贴板", self.hk_text)
        form.addRow("悬浮取词开关", self.hk_hover)

        self.hover_enabled = QCheckBox("开启悬浮取词（鼠标停留自动查词）")
        self.hover_enabled.setChecked(bool(self._cfg.get("hover", {}).get("enabled", False)))
        form.addRow("", self.hover_enabled)
        self.hover_delay = QSpinBox()
        self.hover_delay.setRange(200, 5000)
        self.hover_delay.setSingleStep(100)
        self.hover_delay.setSuffix(" ms")
        self.hover_delay.setValue(int(self._cfg.get("hover", {}).get("delay_ms", 700)))
        form.addRow("悬浮停留", self.hover_delay)

        self.screen_freeze = QCheckBox("按 Alt+S 后先冻结画面，再框选")
        self.screen_freeze.setChecked(
            bool(self._cfg.get("screen", {}).get("freeze_selection", True))
        )
        form.addRow("", self.screen_freeze)

        dict_cfg = self._cfg.get("dictionary", {})
        self.capture_width = QSpinBox()
        self.capture_width.setRange(240, 1800)
        self.capture_width.setSingleStep(50)
        self.capture_width.setSuffix(" px")
        self.capture_width.setValue(int(dict_cfg.get("capture_width", 900)))
        self.capture_width.setToolTip("以光标为中心截取的宽度（逻辑像素）")
        form.addRow("取词范围宽", self.capture_width)
        self.capture_height = QSpinBox()
        self.capture_height.setRange(60, 500)
        self.capture_height.setSingleStep(10)
        self.capture_height.setSuffix(" px")
        self.capture_height.setValue(int(dict_cfg.get("capture_height", 120)))
        self.capture_height.setToolTip("以光标为中心截取的高度（逻辑像素）")
        form.addRow("取词范围高", self.capture_height)
        capture_hint = QLabel("以光标为中心截取；默认 900×120，足够 3 行上下文，比大范围更快。")
        capture_hint.setWordWrap(True)
        capture_hint.setStyleSheet("color: #777; font-size: 11px;")
        form.addRow("", capture_hint)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(form)
        layout.addStretch(1)
        return tab

    def _build_dictionary_tab(self) -> QWidget:
        form = QFormLayout()
        self.dict_mode = QComboBox()
        self.dict_mode.addItem("离线优先（未收录再用模型）", "offline_first")
        self.dict_mode.addItem("模型优先（模型失败再用离线）", "llm_first")
        self.dict_mode.addItem("仅用离线词典", "offline_only")
        self.dict_mode.addItem("仅用模型", "llm_only")
        current_mode = str(self._cfg.get("dictionary", {}).get("mode", "offline_first"))
        index = self.dict_mode.findData(current_mode)
        self.dict_mode.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("查词来源优先级", self.dict_mode)
        hint = QLabel(
            i18n.tr(
                "离线命中后不自动联网；点击词卡的“解释语境”获取语境义和句译。",
                "Offline hits stay local. Click Explain context for model explanations.",
            )
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        self.dict_context = QCheckBox("启用上下文解释（附带所在句子与语境义）")
        self.dict_context.setChecked(
            bool(self._cfg.get("dictionary", {}).get("context_enabled", True))
        )
        form.addRow("", self.dict_context)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(form)
        layout.addWidget(QLabel("已安装词典（StarDict / SQLite）"))
        self.dict_list = QListWidget()
        self.dict_list.setMinimumHeight(110)
        layout.addWidget(self.dict_list)
        buttons = QHBoxLayout()
        self.add_dict_button = QPushButton("载入词典…")
        cancel_import = QPushButton(i18n.tr("取消导入", "Cancel import"))
        cancel_import.clicked.connect(lambda: self.jobs.token.cancelled.set())
        self.remove_dict_button = QPushButton("删除选中")
        self.open_dict_button = QPushButton("打开词典目录")
        buttons.addWidget(self.add_dict_button)
        buttons.addWidget(self.remove_dict_button)
        buttons.addWidget(self.open_dict_button)
        buttons.addWidget(cancel_import)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.add_dict_button.clicked.connect(self._add_dictionary)
        self.remove_dict_button.clicked.connect(self._remove_dictionary)
        self.open_dict_button.clicked.connect(self._open_dictionary_dir)
        self._refresh_dictionaries()
        return tab

    def _build_ocr_tab(self) -> QWidget:
        form = QFormLayout()
        self.min_score = QDoubleSpinBox()
        self.min_score.setRange(0.0, 1.0)
        self.min_score.setSingleStep(0.05)
        self.min_score.setValue(float(self._cfg["ocr"].get("min_score", 0.5)))
        form.addRow("OCR 最低置信度", self.min_score)

        self.ocr_lang = QComboBox()
        form.addRow("OCR 语言", self.ocr_lang)
        self.ocr_threads = QSpinBox()
        self.ocr_threads.setRange(1, min(16, os.cpu_count() or 1))
        self.ocr_threads.setValue(int(self._cfg["ocr"].get("threads", 4)))
        form.addRow(i18n.tr("OCR 线程数", "OCR threads"), self.ocr_threads)
        self.ocr_rotated = QCheckBox(
            i18n.tr("识别旋转文字（较慢）", "Recognize rotated text (slower)")
        )
        self.ocr_rotated.setChecked(bool(self._cfg["ocr"].get("rotated", False)))
        form.addRow("", self.ocr_rotated)
        self.memory_arena = QCheckBox(
            i18n.tr(
                "复用推理内存（更快，内存占用较高）", "Reuse inference memory (faster, more RAM)"
            )
        )
        self.memory_arena.setChecked(bool(self._cfg["ocr"].get("memory_arena", True)))
        form.addRow("", self.memory_arena)

        self.font_pt = QSpinBox()
        self.font_pt.setRange(8, 28)
        self.font_pt.setValue(int(self._cfg["ui"].get("font_pt", 11)))
        form.addRow("覆盖层字号", self.font_pt)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(form)
        layout.addWidget(QLabel("OCR 识别模型（.onnx，可载入自定义模型）"))
        self.ocr_model_list = QListWidget()
        self.ocr_model_list.setMinimumHeight(96)
        layout.addWidget(self.ocr_model_list)
        buttons = QHBoxLayout()
        self.add_ocr_button = QPushButton("载入 OCR 模型…")
        self.remove_ocr_button = QPushButton("删除选中")
        self.open_ocr_button = QPushButton("打开模型目录")
        buttons.addWidget(self.add_ocr_button)
        buttons.addWidget(self.remove_ocr_button)
        buttons.addWidget(self.open_ocr_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.add_ocr_button.clicked.connect(self._add_ocr_model)
        self.remove_ocr_button.clicked.connect(self._remove_ocr_model)
        self.open_ocr_button.clicked.connect(self._open_ocr_dir)
        self._refresh_ocr_models()
        return tab

    def key_was_edited(self) -> bool:
        """True only when the user typed a different key than the loaded one."""
        return self.api_key.text().strip() != self._initial_key

    def _build_system_tab(self) -> QWidget:
        system_cfg = self._cfg.get("system", {})
        self.desktop_shortcut = QCheckBox("创建桌面快捷方式")
        self.desktop_shortcut.setChecked(bool(system_cfg.get("desktop_shortcut", False)))
        self.autostart = QCheckBox("开机自动启动")
        self.autostart.setChecked(bool(system_cfg.get("autostart", False)))
        hint = QLabel("保存设置后立即应用；快捷方式指向本目录的 pythonw。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777; font-size: 11px;")

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self.desktop_shortcut)
        layout.addWidget(self.autostart)
        self.debug_capture = QCheckBox(
            i18n.tr(
                "保存调试截图（包含屏幕内容，保留最近10张）",
                "Save debug screenshots (screen content; last 10 only)",
            )
        )
        self.debug_capture.setChecked(bool(self._cfg["diagnostics"]["capture_images"]))
        layout.addWidget(self.debug_capture)
        layout.addWidget(hint)
        layout.addStretch(1)
        return tab

    # -- API -------------------------------------------------------------
    def _test_api(self) -> None:
        base = self.base_url.text().strip()
        key = (
            self.api_key.text().strip()
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        model = self.model.currentText().strip()
        if not base or not key:
            self._set_status("请先填写 Base URL 和 API Key", ok=False)
            return
        self.test_button.setEnabled(False)
        self._set_status(i18n.tr("测试中…", "Testing…"), ok=True)

        def done(result, error):
            self.test_button.setEnabled(True)
            if error:
                self._set_status(str(error), ok=False)
                return
            ok, message, models = result
            self._set_status(message, ok=ok)
            if models and self.base_url.text().strip() == base:
                current = self.model.currentText()
                self.model.blockSignals(True)
                self.model.clear()
                self.model.addItems(models)
                self.model.setCurrentText(current)
                self.model.blockSignals(False)

        token = self.jobs.token
        self.jobs.run(
            lambda: translate.test_connection(base, key, model, timeout=15, cancel=token), done
        )

    def _set_status(self, text: str, *, ok: bool) -> None:
        self.api_status.setText(text)
        self.api_status.setStyleSheet("color:#2e7d32" if ok else "color:#c62828")

    # -- dictionaries ----------------------------------------------------
    def _refresh_dictionaries(self) -> None:
        self.dict_list.clear()
        try:
            infos = dictionary.installed_info()
        except Exception:
            infos = []
        for info in infos:
            item = QListWidgetItem(
                f"{info['name']}  ·  {info['size_mb']} MB  ·  {info['wordcount']} 词条"
            )
            item.setData(Qt.UserRole, info["name"])
            self.dict_list.addItem(item)
        if not infos:
            self.dict_list.addItem("（未安装词典）")

    def _add_dictionary(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 StarDict 词典文件夹")
        if not folder:
            return
        self.jobs.token = Cancellation()
        self.add_dict_button.setEnabled(False)
        self.add_dict_button.setText(i18n.tr("导入与建索引中…", "Importing and indexing…"))

        def done(name, error):
            self.add_dict_button.setEnabled(True)
            self.add_dict_button.setText(i18n.tr("载入词典…", "Load dictionary…"))
            if error:
                QMessageBox.warning(self, "ScreenGloss", str(error))
            self._refresh_dictionaries()

        self.jobs.run(
            lambda: dictionary.add_dictionary(
                Path(folder), self.jobs.token, self.jobs.progress.emit
            ),
            done,
        )

    def _remove_dictionary(self) -> None:
        item = self.dict_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        answer = QMessageBox.question(
            self, "删除词典", f"确定删除词典 {name}？\n文件会从 data/dictionaries 移除。"
        )
        if answer != QMessageBox.Yes:
            return
        try:
            dictionary.remove_dictionary(str(name))
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self._refresh_dictionaries()

    def _open_dictionary_dir(self) -> None:
        folder = dictionary.root()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # -- OCR models ------------------------------------------------------
    _OCR_LABELS = {
        "japan": "日语（PP-OCRv4）",
        "korean": "韩语（PP-OCRv4）",
        "cyrillic": "俄语（PP-OCRv5）",
    }
    _OCR_CODES = {"japan": "ja", "korean": "ko", "cyrillic": "ru"}

    def _refresh_ocr_models(self) -> None:
        current = self.ocr_lang.currentData() if self.ocr_lang.count() else None
        current = current or str(self._cfg["ocr"].get("lang", "ch"))
        self.ocr_lang.blockSignals(True)
        self.ocr_lang.clear()
        self.ocr_lang.addItem("自动检测", "auto")
        self.ocr_lang.addItem("中英（内置）", "ch")
        self.ocr_model_list.clear()
        for info in ocr.installed_models():
            stem = info["code"]
            code = self._OCR_CODES.get(stem, stem)
            label = self._OCR_LABELS.get(stem, f"{stem}（自定义）")
            self.ocr_lang.addItem(label, code)
            item = QListWidgetItem(f"{stem}.onnx  ·  {info['size_mb']} MB")
            item.setData(Qt.UserRole, stem)
            self.ocr_model_list.addItem(item)
        index = self.ocr_lang.findData(current)
        self.ocr_lang.setCurrentIndex(index if index >= 0 else 1)
        self.ocr_lang.blockSignals(False)

    def _add_ocr_model(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "选择 OCR 模型", "", "ONNX 模型 (*.onnx)")
        if not path:
            return
        self.add_ocr_button.setEnabled(False)

        def done(code, error):
            self.add_ocr_button.setEnabled(True)
            if error:
                QMessageBox.warning(self, "ScreenGloss", str(error))
                return
            self._refresh_ocr_models()
            index = self.ocr_lang.findData(self._OCR_CODES.get(code, code))
            if index >= 0:
                self.ocr_lang.setCurrentIndex(index)

        self.jobs.run(lambda: ocr.add_model(Path(path)), done)

    def _remove_ocr_model(self) -> None:
        item = self.ocr_model_list.currentItem()
        if item is None:
            return
        stem = item.data(Qt.UserRole)
        if not stem:
            return
        answer = QMessageBox.question(self, "删除模型", f"确定删除 OCR 模型 {stem}.onnx？")
        if answer != QMessageBox.Yes:
            return
        try:
            ocr.remove_model(str(stem))
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self._refresh_ocr_models()

    def _open_ocr_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ocr.model_dir())))

    @staticmethod
    def _select(combo: QComboBox, code: str) -> None:
        index = combo.findData(code)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def result_config(self) -> dict:
        cfg = load()
        cfg["api"]["base_url"] = self.base_url.text().strip() or "https://api.deepseek.com"
        cfg["api"]["model"] = self.model.currentText().strip() or "deepseek-chat"
        cfg["api"]["api_key"] = self.api_key.text().strip()
        cfg["languages"]["source"] = self.source.currentData()
        cfg["languages"]["target"] = self.target.currentData()
        cfg["hotkeys"]["screen_translate"] = self.hk_screen.text().strip() or "alt+s"
        cfg["hotkeys"]["dictionary"] = self.hk_dict.text().strip() or "alt+d"
        cfg["hotkeys"]["translate_clipboard"] = self.hk_text.text().strip() or "alt+t"
        cfg["hotkeys"]["hover_toggle"] = self.hk_hover.text().strip() or "alt+h"
        cfg.setdefault("hover", {})["enabled"] = bool(self.hover_enabled.isChecked())
        cfg["hover"]["delay_ms"] = int(self.hover_delay.value())
        cfg.setdefault("screen", {})["freeze_selection"] = bool(self.screen_freeze.isChecked())
        cfg.setdefault("tts", {})["engine"] = str(self.tts_engine.currentData() or "sapi")
        cfg.setdefault("dictionary", {})["mode"] = str(
            self.dict_mode.currentData() or "offline_first"
        )
        cfg["dictionary"]["context_enabled"] = bool(self.dict_context.isChecked())
        cfg["dictionary"]["capture_width"] = int(self.capture_width.value())
        cfg["dictionary"]["capture_height"] = int(self.capture_height.value())
        cfg["ocr"]["min_score"] = float(self.min_score.value())
        cfg["ocr"]["lang"] = str(self.ocr_lang.currentData() or "ch")
        cfg["ocr"]["threads"] = self.ocr_threads.value()
        cfg["ocr"]["rotated"] = self.ocr_rotated.isChecked()
        cfg["ocr"]["memory_arena"] = self.memory_arena.isChecked()
        cfg["ui"]["font_pt"] = int(self.font_pt.value())
        cfg["ui"]["language"] = str(self.ui_language.currentData() or "en")
        cfg.setdefault("system", {})["desktop_shortcut"] = bool(self.desktop_shortcut.isChecked())
        cfg["system"]["autostart"] = bool(self.autostart.isChecked())
        cfg["diagnostics"]["capture_images"] = self.debug_capture.isChecked()
        return cfg

    def accept(self):
        combinations = [
            editor.text().strip()
            for editor in (self.hk_screen, self.hk_dict, self.hk_text, self.hk_hover)
        ]
        parsed = [parse(value) for value in combinations]
        if any(value is None for value in parsed) or len(set(parsed)) != len(parsed):
            QMessageBox.warning(
                self,
                "ScreenGloss",
                i18n.tr(
                    "快捷键格式无效或重复，请修改后保存。", "Hotkeys are invalid or duplicated."
                ),
            )
            return
        try:
            translate._endpoint(self.base_url.text())
        except ValueError as error:
            QMessageBox.warning(self, "ScreenGloss", str(error))
            return
        except translate.ApiError as error:
            QMessageBox.warning(self, "ScreenGloss", str(error))
            return
        super().accept()

    def done(self, result):
        self.jobs.enabled = False
        self.jobs.token.cancelled.set()
        super().done(result)
