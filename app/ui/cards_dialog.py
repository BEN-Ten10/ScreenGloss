"""Paged, asynchronous card library with edit/delete and Anki export."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import config, i18n
from app.services import cards

from .async_jobs import Jobs


class CardsDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(i18n.tr("词卡管理", "Word cards"))
        self.resize(880, 600)
        self.jobs = Jobs(self)
        self.offset = 0
        self.rows = []
        self.revision = 0
        root = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText(i18n.tr("搜索单词或译文", "Search word or translation"))
        root.addWidget(self.search)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            [
                i18n.tr("单词", "Word"),
                i18n.tr("译文", "Translation"),
                i18n.tr("语境", "Context"),
                i18n.tr("保存时间", "Saved"),
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table)
        row = QHBoxLayout()
        root.addLayout(row)
        for zh, en, fn in [
            ("上一页", "Previous", self.previous),
            ("下一页", "Next", self.next),
            ("编辑", "Edit", self.edit),
            ("删除", "Delete", self.delete),
            ("导出选中", "Export selected", lambda: self.export(True)),
            ("导出全部", "Export all", lambda: self.export(False)),
        ]:
            button = QPushButton(i18n.tr(zh, en))
            button.clicked.connect(fn)
            row.addWidget(button)
        self.status = QLabel()
        root.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.reset)
        self.search.textChanged.connect(lambda _: self.timer.start())
        self.table.doubleClicked.connect(self.edit)
        self.refresh()

    def reset(self):
        self.offset = 0
        self.refresh()

    def previous(self):
        self.offset = max(0, self.offset - 100)
        self.refresh()

    def next(self):
        if len(self.rows) == 100:
            self.offset += 100
            self.refresh()

    def refresh(self):
        self.revision += 1
        revision = self.revision
        query = self.search.text()
        offset = self.offset
        self.status.setText(i18n.tr("读取中…", "Loading…"))

        def done(result, error):
            if revision != self.revision:
                return
            if error:
                self.status.setText(str(error))
                return
            self.rows, count = result
            self.table.setRowCount(len(self.rows))
            for r, entry in enumerate(self.rows):
                for c, key in enumerate(
                    ("OriginalWord", "WordTranslation", "Sentence", "CreatedAt")
                ):
                    self.table.setItem(r, c, QTableWidgetItem(str(entry.get(key) or "")))
            self.table.resizeColumnsToContents()
            for column in (1, 2):
                self.table.setColumnWidth(column, min(280, self.table.columnWidth(column)))
            self.status.setText(f"{offset + 1 if count else 0}–{offset + len(self.rows)} / {count}")

        self.jobs.run(
            lambda: (
                cards.list_cards(config.db_path(), 100, offset, query),
                cards.count_cards(config.db_path(), query),
            ),
            done,
        )

    def selected(self):
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def edit(self, *_):
        selected = self.selected()
        if len(selected) != 1:
            return
        entry = self.rows[selected[0]]
        dialog = QDialog(self)
        dialog.setWindowTitle(i18n.tr("编辑词卡", "Edit card"))
        dialog.resize(620, 650)
        root = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        scroll.setWidget(content)
        root.addWidget(scroll)
        editors = {}
        mapping = [
            ("word", "OriginalWord", "单词", "Word"),
            ("translation", "WordTranslation", "译文", "Translation"),
            ("definition", "Definition", "释义", "Definition"),
            ("reading", "Reading", "读音", "Reading"),
            ("ipa", "Ipa", "音标", "IPA"),
            ("part_of_speech", "PartOfSpeech", "词性", "Part of speech"),
            ("context_meaning", "ContextMeaning", "语境义", "Context meaning"),
            ("sentence", "Sentence", "原句", "Sentence"),
            ("sentence_translation", "ContextTranslation", "句译", "Sentence translation"),
            ("example", "Explanation", "例句", "Example"),
        ]
        for key, column, zh, en in mapping:
            editor = QPlainTextEdit(str(entry.get(column) or ""))
            editor.setMaximumHeight(70)
            form.addRow(i18n.tr(zh, en), editor)
            editors[key] = editor
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)
        if dialog.exec():
            values = {key: editor.toPlainText() for key, editor in editors.items()}
            values["context"] = values["sentence"]
            self.jobs.run(
                lambda: cards.update_card(config.db_path(), entry["Id"], values), self._written
            )

    def _written(self, result, error):
        if error:
            QMessageBox.warning(self, "ScreenGloss", str(error))
        self.refresh()

    def delete(self):
        ids = [self.rows[r]["Id"] for r in self.selected()]
        if not ids:
            return
        if (
            QMessageBox.question(
                self,
                "ScreenGloss",
                i18n.tr(f"删除选中的 {len(ids)} 张卡片？", f"Delete {len(ids)} selected cards?"),
            )
            != QMessageBox.Yes
        ):
            return
        self.jobs.run(lambda: cards.delete_cards(config.db_path(), ids), self._written)

    def export(self, selected):
        ids = [self.rows[r]["Id"] for r in self.selected()] if selected else None
        if selected and not ids:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            i18n.tr("导出词卡", "Export cards"),
            str(config.DATA_DIR / "anki_export.tsv"),
            "TSV (*.tsv)",
        )
        if not filename:
            return
        from pathlib import Path

        self.jobs.run(
            lambda: cards.export_tsv(config.db_path(), Path(filename), ids),
            lambda result, error: self.status.setText(str(error or result)),
        )

    def done(self, result):
        self.jobs.enabled = False
        super().done(result)
