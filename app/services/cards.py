"""Transactional local word cards and HTML-safe Anki TSV export."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

_write_lock = threading.RLock()


def _word_key(word):
    return unicodedata.normalize("NFC", str(word or "").strip().casefold())


def _identity(word, source, target, sentence):
    return hashlib.sha256(
        json.dumps(
            [_word_key(word), source or "", target or "", sentence or ""], ensure_ascii=False
        ).encode()
    ).hexdigest()


def _serialized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)

    return wrapped


SCHEMA = """
CREATE TABLE IF NOT EXISTS Cards (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    Guid TEXT NOT NULL UNIQUE,
    CreatedAt TEXT NOT NULL,
    UpdatedAt TEXT NOT NULL,
    OriginalWord TEXT NOT NULL,
    Reading TEXT,
    Ipa TEXT,
    PartOfSpeech TEXT,
    Definition TEXT,
    DefinitionLanguageTag TEXT,
    WordTranslation TEXT,
    TranslationLanguageTag TEXT,
    LanguageTag TEXT,
    CefrLevel TEXT,
    Synonyms TEXT,
    Forms TEXT,
    Explanation TEXT,
    ContextTranslation TEXT,
    LongContextHtml TEXT,
    WindowTitle TEXT,
    ExecutableName TEXT,
    ScreenshotPath TEXT,
    ExtraJson TEXT
);
CREATE INDEX IF NOT EXISTS IX_Cards_Word ON Cards(OriginalWord);
CREATE TABLE IF NOT EXISTS Resources (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    CardId INTEGER NOT NULL,
    Filename TEXT NOT NULL,
    ResourceType INTEGER NOT NULL,
    LanguageTag TEXT,
    CropRect TEXT,
    ContextRect TEXT,
    FOREIGN KEY (CardId) REFERENCES Cards(Id) ON DELETE CASCADE
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@_serialized
def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.create_function("sg_word_key", 1, _word_key, deterministic=True)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    columns = {r[1] for r in con.execute("PRAGMA table_info(Cards)")}
    with con:
        con.execute("BEGIN IMMEDIATE")
        for name in ("ContextMeaning", "Sentence", "Source", "IdentityKey"):
            if name not in columns:
                con.execute(f"ALTER TABLE Cards ADD COLUMN {name} TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS IX_Cards_Identity ON Cards(IdentityKey)")
        con.execute("PRAGMA user_version=2")
    return con


@_serialized
def add_card(db: Path, entry: dict[str, Any]) -> int:
    con = connect(db)
    try:
        if not str(entry.get("word", "")).strip():
            raise ValueError("不能保存空词条")
        sentence = entry.get("sentence") or entry.get("context") or ""
        identity = _identity(
            entry["word"], entry.get("source_lang"), entry.get("target_lang"), sentence
        )
        existing = con.execute(
            "SELECT Id FROM Cards WHERE IdentityKey=? LIMIT 1", (identity,)
        ).fetchone()
        if existing is None:
            existing = con.execute(
                "SELECT Id FROM Cards WHERE sg_word_key(OriginalWord)=? AND COALESCE(LanguageTag,'')=? AND COALESCE(TranslationLanguageTag,'')=? AND COALESCE(NULLIF(Sentence,''),LongContextHtml,'')=? LIMIT 1",
                (
                    _word_key(entry["word"]),
                    entry.get("source_lang") or "",
                    entry.get("target_lang") or "",
                    sentence,
                ),
            ).fetchone()
        if existing:
            update_card(db, int(existing[0]), entry)
            con.execute("UPDATE Cards SET IdentityKey=? WHERE Id=?", (identity, existing[0]))
            con.commit()
            return int(existing[0])
        cur = con.execute(
            """
            INSERT INTO Cards (Guid, CreatedAt, UpdatedAt, OriginalWord, Reading, Ipa,
                PartOfSpeech, Definition, DefinitionLanguageTag, WordTranslation,
                TranslationLanguageTag, LanguageTag, CefrLevel, Synonyms, Forms,
                Explanation, ContextTranslation, LongContextHtml, WindowTitle,
                ExecutableName, ScreenshotPath, ExtraJson)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                _now(),
                _now(),
                entry.get("word", ""),
                entry.get("reading"),
                entry.get("ipa"),
                entry.get("part_of_speech"),
                entry.get("definition"),
                entry.get("definition_language") or entry.get("target_lang"),
                entry.get("translation"),
                entry.get("target_lang"),
                entry.get("source_lang"),
                entry.get("cefr"),
                json.dumps(entry.get("synonyms") or [], ensure_ascii=False),
                json.dumps(entry.get("forms") or [], ensure_ascii=False),
                entry.get("example") or entry.get("explanation"),
                entry.get("sentence_translation") or entry.get("context_translation"),
                entry.get("context"),
                entry.get("window_title"),
                entry.get("executable"),
                entry.get("screenshot_path"),
                json.dumps(entry.get("extra") or {}, ensure_ascii=False),
            ),
        )
        con.execute(
            "UPDATE Cards SET ContextMeaning=?,Sentence=?,Source=?,IdentityKey=? WHERE Id=?",
            (entry.get("context_meaning"), sentence, entry.get("source"), identity, cur.lastrowid),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def list_cards(db: Path, limit: int = 200, offset: int = 0, search: str = "") -> list[dict]:
    con = connect(db)
    try:
        rows = con.execute(
            "SELECT * FROM Cards WHERE OriginalWord LIKE ? OR WordTranslation LIKE ? ORDER BY Id DESC LIMIT ? OFFSET ?",
            (f"%{search}%", f"%{search}%", limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def count_cards(db: Path, search: str = "") -> int:
    con = connect(db)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM Cards WHERE OriginalWord LIKE ? OR WordTranslation LIKE ?",
                (f"%{search}%", f"%{search}%"),
            ).fetchone()[0]
        )
    finally:
        con.close()


def _json_list(value: str | None) -> list[str]:
    try:
        data = json.loads(value or "[]")
        return [str(item) for item in data] if isinstance(data, list) else []
    except Exception:
        return []


def _html_field(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return html.escape(text).replace("\r", "").replace("\n", "<br>")


def export_tsv(db: Path, out: Path, ids: list[int] | None = None) -> Path:
    """Anki-importable TSV: front, back (HTML), tags.

    The back keeps the fields a learner needs for review: part of speech,
    translation, definition (multi-line), inflected forms, synonyms, CEFR,
    the example, and the captured context plus its translation.
    """
    con = connect(db)
    try:
        if ids is None:
            rows = con.execute("SELECT * FROM Cards ORDER BY Id").fetchall()
        else:
            rows = (
                con.execute(
                    "SELECT * FROM Cards WHERE Id IN ("
                    + ",".join("?" for _ in ids)
                    + ") ORDER BY Id",
                    ids,
                ).fetchall()
                if ids
                else []
            )
    finally:
        con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        fh.write("#separator:Tab\n#html:true\n#tags column:3\n")
        for row in rows:
            word = html.escape(row["OriginalWord"])
            reading = html.escape(row["Reading"] or "")
            ipa = html.escape(row["Ipa"] or "")
            pos = html.escape(row["PartOfSpeech"] or "")
            trans = _html_field(row["WordTranslation"])
            definition = _html_field(row["Definition"])
            forms = [html.escape(v) for v in _json_list(row["Forms"])]
            synonyms = [html.escape(v) for v in _json_list(row["Synonyms"])]
            cefr = html.escape(row["CefrLevel"] or "")
            example = _html_field(row["Explanation"])
            context = _html_field(row["LongContextHtml"])
            context_translation = _html_field(row["ContextTranslation"])

            front = f"<b>{word}</b>"
            if reading:
                front += f"<br><i>{reading}</i>"
            if ipa:
                front += f"<br>{ipa}"

            parts: list[str] = []
            if pos:
                parts.append(f"<i>{pos}</i>")
            if trans:
                parts.append(trans)
            if definition:
                parts.append(definition)
            if forms:
                parts.append("<b>变形</b>：" + "，".join(forms[:10]))
            if synonyms:
                parts.append("<b>近义</b>：" + "，".join(synonyms[:10]))
            if cefr:
                parts.append(f"<b>CEFR</b>：{cefr}")
            if example:
                parts.append(f"<hr><i>{example}</i>")
            if context:
                parts.append(f"<hr>{context}")
            if context_translation:
                parts.append(f"<i>{context_translation}</i>")
            if row["ContextMeaning"]:
                parts.append("<b>语境义</b>：" + _html_field(row["ContextMeaning"]))
            back = "<br>".join(parts)

            tags = "screengloss"
            language = (row["LanguageTag"] or "").strip()
            if language and language != "auto":
                tags += " " + language
            writer.writerow([front, back, tags])
    temporary.replace(out)
    return out


@_serialized
def update_card(db: Path, card_id: int, entry: dict) -> None:
    fields = {
        "word": "OriginalWord",
        "translation": "WordTranslation",
        "definition": "Definition",
        "ipa": "Ipa",
        "reading": "Reading",
        "part_of_speech": "PartOfSpeech",
        "context_meaning": "ContextMeaning",
        "sentence": "Sentence",
        "context": "LongContextHtml",
        "sentence_translation": "ContextTranslation",
        "context_translation": "ContextTranslation",
        "example": "Explanation",
        "explanation": "Explanation",
        "source": "Source",
        "definition_language": "DefinitionLanguageTag",
        "cefr": "CefrLevel",
        "source_lang": "LanguageTag",
        "target_lang": "TranslationLanguageTag",
    }
    values = {column: entry[key] for key, column in fields.items() if key in entry}
    for key, column in (("synonyms", "Synonyms"), ("forms", "Forms")):
        if key in entry:
            values[column] = json.dumps(entry[key] or [], ensure_ascii=False)
    if "OriginalWord" in values and not str(values["OriginalWord"]).strip():
        raise ValueError("词条不能为空")
    values["UpdatedAt"] = _now()
    con = connect(db)
    try:
        with con:
            row = con.execute("SELECT * FROM Cards WHERE Id=?", (card_id,)).fetchone()
            if row is None:
                return
            merged = dict(row) | values
            values["IdentityKey"] = _identity(
                merged["OriginalWord"],
                merged["LanguageTag"],
                merged["TranslationLanguageTag"],
                merged["Sentence"] or merged["LongContextHtml"],
            )
            con.execute(
                "UPDATE Cards SET " + ",".join(f"{key}=?" for key in values) + " WHERE Id=?",
                [*values.values(), card_id],
            )
    finally:
        con.close()


@_serialized
def delete_cards(db: Path, ids: list[int]) -> None:
    con = connect(db)
    try:
        with con:
            con.executemany("DELETE FROM Cards WHERE Id=?", [(i,) for i in ids])
    finally:
        con.close()
