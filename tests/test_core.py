"""Offline regression tests. All databases/config writes use temporary directories."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import gzip
import json
import logging
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QRect
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app import config
from app.services import cards, dictionary, translate, workflow
from app.services.capture import Monitor
from app.services.hotkeys import parse
from app.services.logging_service import SafeFormatter
from app.services.ocr_engine import _group_tokens, word_at
from app.services.stardict import StarDict
from app.services.tasks import Cancelled, Task, TaskRunner
from app.ui.overlay import OverlayWindow, TranslationPanel

APP = QApplication.instance() or QApplication([])
QFontDatabase.addApplicationFont(
    str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc")
)
APP.setFont(QFont("Microsoft YaHei", 10))


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_patch = patch.object(config, "DATA_DIR", self.root)
        self.data_patch.start()
        self.path_patch = patch.object(config, "CONFIG_PATH", self.root / "config.json")
        self.path_patch.start()
        config.set_session_key(None)
        dictionary.refresh()

    def tearDown(self):
        # close() only hides parentless Qt widgets. Destroy them while the
        # QApplication and its font engine are still alive, not at Python exit.
        for widget in APP.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        with translate._cache_lock:
            if translate._cache:
                translate._cache.close()
            translate._cache = None
            translate._cache_path = None
        dictionary.refresh()
        config.set_session_key(None)
        self.path_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()


class ConfigTests(Isolated):
    def test_default_language_is_english_and_saved_choice_is_preserved(self):
        self.assertEqual(config.load()["ui"]["language"], "en")
        config.CONFIG_PATH.write_text(json.dumps({"ui": {"font_pt": 12}}), encoding="utf-8")
        self.assertEqual(config.load()["ui"]["language"], "en")
        config.update("ui", {"language": "zh"})
        self.assertEqual(config.load()["ui"]["language"], "zh")
        self.assertEqual(config.DEFAULTS["ui"]["language"], "en")

    def test_default_settings_and_invalid_language_fallback_are_english(self):
        from app import i18n
        from app.ui.settings_dialog import SettingsDialog

        previous = i18n.language()
        try:
            i18n.set_language(None)
            self.assertEqual(i18n.language(), "en")
            dialog = SettingsDialog()
            self.assertEqual(dialog.ui_language.currentData(), "en")
            self.assertEqual(dialog.windowTitle(), "ScreenGloss Settings")
            self.assertEqual(dialog.result_config()["ui"]["language"], "en")
            dialog.close()
            i18n.set_language("unsupported")
            self.assertEqual(i18n.tr("关闭", "Close"), "Close")
            i18n.set_language("zh")
            self.assertEqual(i18n.tr("关闭", "Close"), "关闭")
        finally:
            i18n.set_language(previous)

    def test_partial_updates_preserve_other_sections(self):
        config.update("cache", {"max_mb": 25})
        config.update("hover", {"enabled": True})
        self.assertEqual(config.load()["cache"]["max_mb"], 25)
        self.assertTrue(config.load()["hover"]["enabled"])

    def test_defaults_not_mutated(self):
        cfg = config.load()
        cfg["hotkeys"]["dictionary"] = "f8"
        self.assertEqual(config.load()["hotkeys"]["dictionary"], "alt+d")

    def test_saved_key_survives_routine_save(self):
        cfg = config.load()
        cfg["api"]["api_key"] = "saved-secret"
        config.save(cfg, persist_key=True)
        cfg = config.load()
        cfg["hover"]["enabled"] = True
        config.save(cfg)
        self.assertEqual(config.load(resolve_key=False)["api"]["api_key"], "saved-secret")

    def test_environment_not_persisted(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-secret"}):
            cfg = config.load()
            self.assertEqual(cfg["api"]["api_key"], "environment-secret")
            config.save(cfg)
        self.assertEqual(config.load(resolve_key=False)["api"]["api_key"], "")

    def test_explicit_clear_and_session(self):
        cfg = config.load()
        cfg["api"]["api_key"] = "saved"
        config.save(cfg, persist_key=True)
        config.save(cfg, persist_key=False)
        config.set_session_key("session")
        self.assertEqual(config.load()["api"]["api_key"], "session")
        self.assertEqual(config.load(resolve_key=False)["api"]["api_key"], "")

    def test_corrupt_config_not_overwritten(self):
        config.CONFIG_PATH.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            config.load()
        self.assertEqual(config.CONFIG_PATH.read_text(), "broken")

    def test_invalid_section_and_range(self):
        for value in ({"ocr": []}, {"ocr": {"min_score": 2}}, {"hover": {"enabled": "false"}}):
            config.CONFIG_PATH.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                config.load()


def make_dictionary(folder, *, bits=32, compressed=False, sequence="m", articles=None):
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "tiny"
    articles = articles or [("Hello", b"greeting"), ("world", b"planet")]
    data = b""
    index = b""
    for word, article in articles:
        index += (
            word.encode()
            + b"\0"
            + struct.pack(">QI" if bits == 64 else ">II", len(data), len(article))
        )
        data += article
    prefix.with_suffix(".ifo").write_text(
        f"StarDict's dict ifo file\nversion=3.0.0\nwordcount={len(articles)}\nidxoffsetbits={bits}\nsametypesequence={sequence}\n",
        encoding="utf-8",
    )
    if compressed:
        prefix.with_suffix(".idx.gz").write_bytes(gzip.compress(index))
        prefix.with_suffix(".dict.dz").write_bytes(gzip.compress(data))
    else:
        prefix.with_suffix(".idx").write_bytes(index)
        prefix.with_suffix(".dict").write_bytes(data)
    return prefix.with_suffix(".ifo")


class DictionaryTests(Isolated):
    def test_plain_and_casefold(self):
        d = StarDict(make_dictionary(self.root / "dict"))
        self.assertEqual(d.lookup("hello")["article"], "greeting")
        self.assertFalse(d.contains("missing"))
        self.assertFalse(hasattr(d, "_entries"))
        self.assertEqual(d._data.suffix, ".dict")

    def test_64bit_compressed_and_synonym(self):
        path = make_dictionary(self.root / "dict", bits=64, compressed=True)
        path.with_suffix(".syn").write_bytes(b"hi\0" + struct.pack(">I", 0))
        d = StarDict(path)
        self.assertEqual(d.lookup("hi")["word"], "Hello")

    def test_multiple_fields(self):
        path = make_dictionary(
            self.root / "dict", sequence="mt", articles=[("word", b"meaning\0pronunciation")]
        )
        self.assertEqual(StarDict(path).lookup("word")["article"], "meaning\npronunciation")

    def test_truncated_index_not_enabled(self):
        path = make_dictionary(self.root / "dict")
        path.with_suffix(".idx").write_bytes(b"bad\0\0")
        d = StarDict(path)
        with self.assertRaises(ValueError):
            d._load()
        self.assertFalse(d._ready)
        self.assertFalse(list((self.root / "indexes").glob("*.partial")))

    def test_out_of_bounds_rejected(self):
        path = make_dictionary(self.root / "dict")
        path.with_suffix(".dict").write_bytes(b"")
        with self.assertRaises(ValueError):
            StarDict(path)._load()

    def test_cancel_index(self):
        path = make_dictionary(
            self.root / "dict", articles=[(str(i), b"entry") for i in range(10001)]
        )
        task = Task(1, "index", config.load())
        task.cancelled.set()
        with self.assertRaises(Cancelled):
            StarDict(path)._load(task)
        self.assertFalse(list((self.root / "indexes").glob("*.sqlite")))

    def test_japanese_combines_senses(self):
        folder = self.root / "dictionaries" / "jmdict"
        folder.mkdir(parents=True)
        with closing(sqlite3.connect(folder / "jmdict.db")) as con, con:
            con.execute("CREATE TABLE jmdict(key,word,reading,gloss,pos)")
            con.executemany(
                "INSERT INTO jmdict VALUES (?,?,?,?,?)",
                [("猫", "猫", "ねこ", "cat", "noun"), ("猫", "猫", "ねこ", "feline", "noun")],
            )
        result = dictionary.lookup("猫")
        self.assertEqual(result["definition"], "cat\nfeline")
        self.assertEqual(result["definition_language"], "en")


class CardTests(Isolated):
    def test_concurrent_duplicate_save_is_serialized(self):
        db = self.root / "cards.db"
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: cards.add_card(db, self.entry()), range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(cards.count_cards(db), 1)

    def entry(self):
        return dict(
            word="apply",
            source_lang="en",
            target_lang="zh",
            translation="适用",
            context="Special rules apply.",
            sentence="Special rules apply.",
            context_meaning="生效",
            sentence_translation="特殊规则适用。",
            example="Rules apply.",
        )

    def test_save_preserves_context(self):
        db = self.root / "cards.db"
        cards.add_card(db, self.entry())
        row = cards.list_cards(db)[0]
        self.assertEqual(row["ContextMeaning"], "生效")
        self.assertEqual(row["ContextTranslation"], "特殊规则适用。")
        self.assertEqual(row["Explanation"], "Rules apply.")

    def test_duplicate_updates_and_distinct_sentence_retained(self):
        db = self.root / "cards.db"
        entry = self.entry()
        first = cards.add_card(db, entry)
        entry["translation"] = "有效"
        self.assertEqual(cards.add_card(db, entry), first)
        entry["sentence"] = "Apply for a job."
        cards.add_card(db, entry)
        self.assertEqual(cards.count_cards(db), 2)

    def test_legacy_migration_does_not_drop_rows(self):
        db = self.root / "cards.db"
        with closing(sqlite3.connect(db)) as con, con:
            con.executescript(cards.SCHEMA)
            con.execute(
                "INSERT INTO Cards(Guid,CreatedAt,UpdatedAt,OriginalWord) VALUES ('g','now','now','legacy')"
            )
        self.assertEqual(cards.count_cards(db), 1)
        self.assertEqual(cards.list_cards(db)[0]["OriginalWord"], "legacy")

    def test_export_escaping_tsv_and_complete_long_text(self):
        db = self.root / "cards.db"
        entry = self.entry()
        entry.update(word='<word>\t"quoted"', definition="<script>&\n" + "long" * 500)
        cards.add_card(db, entry)
        out = cards.export_tsv(db, self.root / "cards.tsv")
        rows = list(
            csv.reader(
                [
                    line
                    for line in out.read_text(encoding="utf-8").splitlines()
                    if not line.startswith("#")
                ],
                delimiter="\t",
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 3)
        self.assertIn("&lt;word&gt;", rows[0][0])
        self.assertIn("long" * 500, rows[0][1])
        self.assertIn("生效", rows[0][1])

    def test_edit_delete_selected_export(self):
        db = self.root / "cards.db"
        first = cards.add_card(db, self.entry())
        cards.update_card(db, first, {"word": "updated"})
        self.assertEqual(cards.list_cards(db, search="updated")[0]["Id"], first)
        cards.export_tsv(db, self.root / "empty.tsv", [])
        cards.delete_cards(db, [first])
        self.assertEqual(cards.count_cards(db), 0)

    def test_unicode_identity_survives_edit(self):
        db = self.root / "cards.db"
        for word, variant in (("Привет", "привет"), ("Straße", "STRASSE"), ("Café", "CAFE\u0301")):
            entry = dict(word=word, source_lang="auto", target_lang="zh", sentence="same")
            first = cards.add_card(db, entry)
            cards.update_card(db, first, {"translation": "edited"})
            self.assertEqual(cards.add_card(db, entry | {"word": variant}), first)
        self.assertEqual(cards.count_cards(db), 3)

    def test_legacy_unicode_identity_is_recovered(self):
        db = self.root / "cards.db"
        first = cards.add_card(db, dict(word="Привет", source_lang="ru", target_lang="zh"))
        with closing(cards.connect(db)) as con, con:
            con.execute("UPDATE Cards SET IdentityKey=NULL")
        self.assertEqual(
            cards.add_card(db, dict(word="привет", source_lang="ru", target_lang="zh")), first
        )

    def test_edited_language_and_sentence_update_identity(self):
        db = self.root / "cards.db"
        first = cards.add_card(db, dict(word="hello", source_lang="en", target_lang="zh"))
        cards.update_card(db, first, dict(target_lang="ja", sentence="hello there"))
        self.assertEqual(
            cards.add_card(
                db, dict(word="HELLO", source_lang="en", target_lang="ja", sentence="hello there")
            ),
            first,
        )
        self.assertEqual(cards.count_cards(db), 1)


class FakeServer(BaseHTTPRequestHandler):
    mode = "ok"
    calls = 0
    request_started = threading.Event()

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).request_started.set()
        if self.mode in ("slow_headers", "stall_headers"):
            time.sleep(10 if self.mode == "stall_headers" else 1)
        if self.mode == "drip":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        self.reply(200, {"data": [{"id": "test-model"}]})

    def reply(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "0")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data).encode())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        type(self).calls += 1
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.mode == "auth":
            self.reply(401, {"error": "private server message"})
            return
        if self.mode == "rate" and self.calls < 3:
            self.reply(429, {})
            return
        if self.mode == "empty":
            content = " "
        elif self.mode == "invalid":
            content = "broken"
        elif payload.get("response_format"):
            items = json.loads(payload["messages"][-1]["content"])
            if isinstance(items, list):
                rows = [{"id": row["id"], "text": "译文 " + row["text"]} for row in reversed(items)]
                if self.mode == "missing":
                    rows = rows[:-1]
                content = json.dumps({"translations": rows})
            else:
                content = json.dumps(
                    dict(
                        word=items["word"],
                        translation="词义",
                        definition="释义",
                        synonyms=[],
                        forms=[],
                    )
                )
        else:
            content = "OK"
        self.reply(
            200,
            {
                "choices": [
                    {
                        "finish_reason": "length" if self.mode == "truncated" else "stop",
                        "message": {"content": content},
                    }
                ]
            },
        )


class TranslationTests(Isolated):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        FakeServer.mode = "ok"
        FakeServer.calls = 0
        FakeServer.request_started.clear()
        self.args = dict(
            src="en",
            tgt="zh",
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            model="test-model",
            api_key="test-key",
            timeout=5,
        )

    def test_endpoint_normalization(self):
        for base in ("https://host", "https://host/v1/", "https://host/v1/chat/completions"):
            self.assertEqual(translate._endpoint(base), "https://host/v1/chat/completions")
        self.assertEqual(
            translate._endpoint("https://host/custom/v2", "/models"),
            "https://host/custom/v2/models",
        )

    def test_reject_url_credentials(self):
        with self.assertRaises(translate.ApiError):
            translate._endpoint("https://secret@host/v1")

    def test_reordered_ids_and_success_cache(self):
        result = translate.translate_lines(["one", "two"], **self.args)
        self.assertEqual(result, ["译文 one", "译文 two"])
        translate.translate_lines(["one", "two"], **self.args)
        self.assertEqual(FakeServer.calls, 1)

    def test_failure_never_cached(self):
        FakeServer.mode = "auth"
        with self.assertRaises(translate.ApiError):
            translate.translate_lines(["one"], **self.args)
        self.assertEqual(translate.cache_size(), 0)
        self.assertEqual(FakeServer.calls, 1)
        FakeServer.mode = "ok"
        self.assertEqual(translate.translate_lines(["one"], **self.args), ["译文 one"])

    def test_semantic_failure_bounded(self):
        for mode in ("empty", "invalid", "missing", "truncated"):
            FakeServer.mode = mode
            FakeServer.calls = 0
            result = translate.translate_units([mode], **self.args)
            self.assertTrue(result[0]["error"])
            self.assertEqual(FakeServer.calls, 2)
        self.assertEqual(translate.cache_size(), 0)

    def test_rate_retry_bounded(self):
        FakeServer.mode = "rate"
        translate.translate_lines(["one"], **self.args)
        self.assertEqual(FakeServer.calls, 3)

    def test_model_and_endpoint_isolate_cache(self):
        translate.translate_lines(["one"], **self.args)
        translate.translate_lines(["one"], **{**self.args, "model": "other"})
        self.assertEqual(FakeServer.calls, 2)

    def test_long_text_split_and_reassembled(self):
        value = "word. " * 900
        result = translate.translate_lines([value], **self.args)
        self.assertTrue(result[0].startswith("译文"))
        self.assertGreater(FakeServer.calls, 1)

    def test_partial_results_preserved(self):
        count = [0]
        original = translate.chat

        def mixed(*args, **kwargs):
            count[0] += 1
            if count[0] > 1:
                raise translate.ApiError("failed", category="http")
            return original(*args, **kwargs)

        with patch.object(translate, "chat", mixed):
            result = translate.translate_units([f"line{i}" for i in range(9)], **self.args)
        self.assertTrue(all(r["translation"] for r in result[:8]))
        self.assertTrue(result[-1]["error"])

    def test_cancel_before_request(self):
        task = Task(1, "test", config.load())
        task.cancelled.set()
        with self.assertRaises(Cancelled):
            translate.translate_lines(["one"], **self.args, cancel=task)
        self.assertEqual(FakeServer.calls, 0)

    def test_connection_keeps_list_separate_from_generation(self):
        FakeServer.mode = "auth"
        ok, message, models = translate.test_connection(
            self.args["base_url"], "test-key", "test-model"
        )
        self.assertFalse(ok)
        self.assertEqual(models, ["test-model"])

    def test_total_timeout_bounds_trickling_body(self):
        FakeServer.mode = "drip"
        started = time.monotonic()
        with self.assertRaises(translate.ApiError) as caught:
            translate._request("GET", self.args["base_url"], api_key="test", timeout=0.25)
        self.assertEqual(caught.exception.category, "timeout")
        self.assertLess(time.monotonic() - started, 0.8)

    def test_total_timeout_bounds_delayed_headers(self):
        FakeServer.mode = "slow_headers"
        started = time.monotonic()
        with self.assertRaises(translate.ApiError):
            translate._request("GET", self.args["base_url"], api_key="test", timeout=0.2)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_cancel_while_waiting_for_response(self):
        FakeServer.mode = "slow_headers"
        task = Task(1, "test", config.load())

        def cancel_after_headers():
            FakeServer.request_started.wait(2)
            task.cancelled.set()

        thread = threading.Thread(target=cancel_after_headers)
        thread.start()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            translate._request("GET", self.args["base_url"], api_key="test", timeout=5, cancel=task)
        thread.join(2)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_invalid_request_limits_rejected(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(translate.ApiError):
                translate._request("GET", self.args["base_url"], api_key="test", timeout=timeout)

    def test_response_size_is_bounded(self):
        with patch.object(translate, "_MAX_RESPONSE_BYTES", 4):
            with self.assertRaises(translate.ApiError) as caught:
                translate.list_models(self.args["base_url"], "test")
        self.assertEqual(caught.exception.category, "response_size")

    def test_close_runner_releases_waiting_network_task(self):
        FakeServer.mode = "slow_headers"
        runner = TaskRunner()
        task = runner.begin("test", config.load())
        runner.submit(
            task,
            lambda task: translate._request(
                "GET", self.args["base_url"], api_key="test", timeout=5, cancel=task
            ),
            network=True,
        )
        self.assertTrue(FakeServer.request_started.wait(2))
        future = runner._futures[0]
        runner.close()
        future.result(timeout=0.8)
        self.assertFalse(runner.active(task))

    def test_process_exits_with_network_request_in_flight(self):
        import subprocess
        import sys

        FakeServer.mode = "stall_headers"
        script = """
import time
from PySide6.QtCore import QCoreApplication
from app import config
from app.services.tasks import TaskRunner
from app.services.translate import _request
app = QCoreApplication([])
runner = TaskRunner()
task = runner.begin('test', config.DEFAULTS)
runner.submit(task, lambda task: _request('GET', %r, api_key='test', timeout=30,
                                        cancel=task), network=True)
time.sleep(.2)
runner.close()
print('RUNNER_CLOSED', flush=True)
""" % self.args["base_url"]
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=3
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RUNNER_CLOSED", result.stdout)
        self.assertTrue(FakeServer.request_started.is_set())


class GeometryAndTaskTests(Isolated):
    def test_hotkey_changes_preserve_previous_bindings_on_conflict(self):
        from app.services import hotkeys

        manager = hotkeys.HotkeyManager()

        def original():
            return None

        with (
            patch.object(hotkeys.user32, "RegisterHotKey", return_value=True),
            patch.object(hotkeys.user32, "UnregisterHotKey") as unregister,
        ):
            self.assertEqual(manager.register({"alt+s": original}), [])
            previous = dict(manager._callbacks)
            with patch.object(hotkeys.user32, "RegisterHotKey", return_value=False):
                self.assertTrue(manager.register({"alt+s": original, "alt+d": lambda: None}))
            self.assertEqual(manager._callbacks, previous)
            unregister.assert_not_called()
            manager.unregister()

    def test_debug_images_opt_in_and_bounded(self):
        import numpy as np

        from app.services import diagnostics

        image = np.zeros((16, 24, 3), dtype=np.uint8)
        cfg = config.load()
        task = Task(1, "ocr", cfg)
        with patch.object(workflow.ocr, "ocr", return_value=[]):
            workflow.recognize(task, image)
            self.assertFalse((self.root / "debug").exists())
            task.cfg["diagnostics"]["capture_images"] = True
            for _ in range(12):
                workflow.recognize(task, image)
        self.assertEqual(len(list((self.root / "debug").glob("sg_*.png"))), 10)
        self.assertEqual(diagnostics.clear_debug(), 10)

    def test_hangul_spacing_uses_only_large_visual_gaps(self):
        import numpy as np

        from app.services.ocr_engine import _restore_hangul_spacing

        image = np.full((40, 140, 3), 255, dtype=np.uint8)
        tokens = []
        for char, x in zip("한국어번역", [5, 25, 45, 85, 105]):
            image[5:30, x : x + 15] = 0
            tokens.append(dict(text=char, bbox=(x, 5, x + 15, 30), score=1))
        items = [dict(text="한국어번역", bbox=(5, 5, 120, 30), characters=tokens, words=[])]
        _restore_hangul_spacing(image, items)
        self.assertEqual(items[0]["text"], "한국어 번역")

    def test_speech_uses_dictionary_and_selected_language(self):
        from app.ui.popup import InfoPopup

        popup = InfoPopup("学校", "", dict(word="学校", source="jmdict:jmdict", source_lang="auto"))
        self.assertEqual(popup._speech_language("学校"), "ja")
        popup._entry = dict(word="bonjour", source_lang="fr")
        self.assertEqual(popup._speech_language("bonjour"), "fr")
        popup.close()

    def test_character_boxes_group_into_complete_latin_words(self):
        text = "Special rules apply"
        words = [
            {"text": char, "bbox": (i * 14, 0, i * 14 + 8, 20), "quad": [], "score": 1}
            for i, char in enumerate(text)
        ]
        grouped = _group_tokens(words, text)
        self.assertEqual([w["text"] for w in grouped], ["Special", "rules", "apply"])

    def test_ocr_cache_returns_independent_objects(self):
        import numpy as np

        from app.services import ocr_engine

        image = np.zeros((20, 80, 3), dtype=np.uint8)
        rows = [[[[0, 0], [80, 0], [80, 20], [0, 20]], "hello", 1.0]]
        with patch.object(ocr_engine, "_rows", return_value=rows):
            ocr_engine._cache.clear()
            first = ocr_engine.ocr(image)
            first[0]["text"] = "changed"
            second = ocr_engine.ocr(image)
            self.assertEqual(second[0]["text"], "hello")
        ocr_engine._cache.clear()

    def test_mixed_dpi_origin(self):
        monitor = Monitor("right", (1920, 0, 1280, 720), (1920, 0, 3840, 1080))
        self.assertEqual(monitor.physical_rect(QRect(2020, 100, 200, 80)), (2070, 150, 300, 120))
        left = Monitor("left", (-1280, 0, 1280, 720), (-1920, 0, 0, 1080))
        self.assertEqual(left.physical_rect(QRect(-1180, 100, 200, 80)), (-1770, 150, 300, 120))

    def test_unicode_word_selection(self):
        for word in ("hello", "Привет", "한국어"):
            result = word_at([dict(bbox=(0, 0, 120, 20), text=word)], 60, 10)
            self.assertEqual(result["word"], word)

    def test_word_box_beats_equal_width(self):
        line = dict(
            bbox=(0, 0, 200, 20),
            text="iiii WWWW",
            words=[
                dict(bbox=(0, 0, 30, 20), text="iiii", score=1),
                dict(bbox=(40, 0, 200, 20), text="WWWW", score=1),
            ],
        )
        self.assertEqual(word_at([line], 50, 10)["word"], "WWWW")

    def test_latest_task_wins(self):
        runner = TaskRunner()
        one = runner.begin("one", config.load())
        two = runner.begin("two", config.load())
        self.assertFalse(runner.active(one))
        self.assertTrue(runner.active(two))
        runner.close()
        self.assertFalse(runner.active(two))

    def test_task_has_snapshot(self):
        cfg = config.load()
        runner = TaskRunner()
        task = runner.begin("one", cfg)
        cfg["languages"]["target"] = "ja"
        self.assertEqual(task.cfg["languages"]["target"], "zh")
        runner.close()

    def test_offline_mode_never_calls_model(self):
        cfg = config.load()
        cfg["dictionary"]["mode"] = "offline_only"
        task = Task(1, "lookup", cfg)
        with (
            patch.object(
                dictionary, "lookup", return_value={"word": "word", "translation": "词义"}
            ),
            patch.object(translate, "lookup_word") as model,
        ):
            result = workflow.lookup_entry(task, {"word": "word", "sentence": "a word"})
            model.assert_not_called()
            self.assertFalse(result["context_allowed"])

    def test_offline_first_hit_stays_local(self):
        task = Task(1, "lookup", config.load())
        with (
            patch.object(
                dictionary, "lookup", return_value={"word": "word", "translation": "词义"}
            ),
            patch.object(translate, "lookup_word") as model,
        ):
            result = workflow.lookup_entry(task, {"word": "word", "sentence": "a word"})
            model.assert_not_called()
            self.assertTrue(result["context_allowed"])

    def test_hotkey_parser_strict(self):
        self.assertIsNotNone(parse("ctrl+alt+f9"))
        self.assertIsNone(parse("alt+a+b"))
        self.assertIsNone(parse("bad+f8"))

    def test_overlay_bounds_and_sidebar_completeness(self):
        items = [
            dict(
                id="0",
                bbox=(10, 10, 200, 40),
                source="source",
                translation="long " * 200,
                error=None,
            )
        ]
        overlay = OverlayWindow(QRect(0, 0, 400, 250), items, QRect(0, 0, 400, 250))
        self.assertTrue(overlay.overflow)
        for _, rect, _, _ in overlay._boxes:
            self.assertLessEqual(rect.bottom(), 250)
        panel = TranslationPanel(items)
        self.assertIn("long " * 150, panel.browser.toPlainText())
        overlay.close()
        panel.close()

    def test_log_redacts_credentials(self):
        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "Bearer top-secret sk-private123", (), None
        )
        formatted = SafeFormatter().format(record)
        self.assertNotIn("top-secret", formatted)
        self.assertNotIn("sk-private123", formatted)

    def test_log_restart_has_one_handler_and_closes_files(self):
        from app.services import logging_service

        logger = logging.getLogger("screengloss")
        original = len(logger.handlers)
        try:
            for _ in range(2):
                logging_service.setup()
                self.assertEqual(len(logger.handlers), original + 1)
                logger.info("Bearer top-secret sk-private123")
                logging_service.shutdown()
                self.assertEqual(len(logger.handlers), original)
            text = (self.root / "app.log").read_text(encoding="utf-8")
            self.assertEqual(text.count("[redacted]"), 4)
            self.assertNotIn("top-secret", text)
            self.assertNotIn("sk-private123", text)
        finally:
            logging_service.shutdown()

    def test_full_log_queue_does_not_expose_records(self):
        import io
        import queue
        from contextlib import redirect_stderr

        from app.services.logging_service import BoundedQueueHandler

        q = queue.Queue(maxsize=1)
        handler = BoundedQueueHandler(q)
        record = logging.LogRecord("test", logging.INFO, "", 0, "sk-private123", (), None)
        errors = io.StringIO()
        with redirect_stderr(errors):
            handler.handle(record)
            handler.handle(record)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(q.qsize(), 1)

    def test_overlay_edge_boxes_remain_inside_screen(self):
        for bbox in ((10, 245, 200, 249), (398, 60, 440, 90), (-20, -20, 20, 20)):
            overlay = OverlayWindow(
                QRect(0, 0, 400, 250),
                [dict(id="0", bbox=bbox, source="source", translation="long " * 100)],
                QRect(0, 0, 400, 250),
            )
            for _, rect, _, _ in overlay._boxes:
                self.assertTrue(overlay.rect().contains(rect.toAlignedRect()))
                self.assertGreater(rect.width(), 0)
                self.assertGreater(rect.height(), 0)
            self.assertTrue(overlay.overflow)
            overlay.close()

    def test_cross_screen_blocks_use_bilingual_fallback(self):
        items = [dict(id="0", bbox=(0, 60, 80, 90), source="source", translation="text")]
        for screen in (QRect(0, 0, 800, 600), QRect(800, 0, 800, 600)):
            overlay = OverlayWindow(QRect(760, 0, 100, 200), items, screen)
            self.assertTrue(overlay.overflow)
            self.assertEqual(len(overlay._boxes), 1)
            self.assertTrue(overlay.rect().contains(overlay._boxes[0][1].toAlignedRect()))
            overlay.close()

    def test_overlay_does_not_place_overlapping_boxes(self):
        items = [
            dict(id=str(i), bbox=(10, 100, 200, 120), source="source", translation="long " * 50)
            for i in range(3)
        ]
        overlay = OverlayWindow(QRect(0, 0, 400, 250), items, QRect(0, 0, 400, 250))
        boxes = [row[1] for row in overlay._boxes]
        self.assertTrue(overlay.overflow)
        self.assertFalse(any(a.intersects(b) for i, a in enumerate(boxes) for b in boxes[i + 1 :]))
        overlay.close()

    def test_debug_write_failure_does_not_break_ocr(self):
        import numpy as np

        cfg = config.load()
        cfg["diagnostics"]["capture_images"] = True
        task = Task(1, "test", cfg)
        with (
            patch("PIL.Image.Image.save", side_effect=OSError("disk full")),
            patch.object(workflow.ocr, "ocr", return_value=[{"text": "ok"}]),
        ):
            self.assertEqual(
                workflow.recognize(task, np.zeros((10, 10, 3), dtype=np.uint8)), [{"text": "ok"}]
            )


class ControllerTests(Isolated):
    def setUp(self):
        super().setUp()
        from app.main import ScreenGlossApp

        self.registration = patch.object(ScreenGlossApp, "_register_hotkeys")
        self.registration.start()
        self.warm = patch.object(ScreenGlossApp, "_warm")
        self.warm.start()
        self.tray = patch("app.main.QSystemTrayIcon.show")
        self.tray.start()
        self.controller = ScreenGlossApp(APP)
        APP.processEvents()

    def tearDown(self):
        self.controller.quit()
        APP.processEvents()
        self.tray.stop()
        self.warm.stop()
        self.registration.stop()
        super().tearDown()

    def until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_clipboard_pipeline_and_empty_word_save_hidden(self):
        APP.clipboard().setText("Synthetic input")
        with patch.object(translate, "translate_lines", return_value=["人工测试译文"]):
            self.controller.start_clipboard_translate()
            self.until(lambda: self.controller._popup is not None)
        self.assertFalse(self.controller._popup._save.isVisible())
        self.assertEqual(self.controller._popup._entry["copy_text"], "人工测试译文")

    def test_screen_pipeline_partial_failure_and_retry(self):
        monitor = Monitor("test", (0, 0, 800, 600), (0, 0, 800, 600))
        units = [
            dict(
                id=str(i),
                bbox=(20, 60 + i * 80, 300, 100 + i * 80),
                source=f"line{i}",
                translation="",
                error=None,
            )
            for i in range(2)
        ]
        task = self.controller._begin("screen")
        with patch.object(
            translate,
            "translate_units",
            return_value=[
                dict(translation="译文", error=None),
                dict(translation="", error="failure"),
            ],
        ):
            self.controller._result(
                (
                    task,
                    dict(
                        stage="recognized",
                        units=units,
                        rect=QRect(0, 0, 800, 600),
                        screens=[monitor],
                    ),
                    None,
                )
            )
            self.until(lambda: self.controller._panel is not None)
        self.assertEqual(self.controller._screen_state["units"][0]["translation"], "译文")
        with patch.object(
            translate, "translate_units", return_value=[dict(translation="重试译文", error=None)]
        ) as mock:
            self.controller.retry_screen()
            self.until(
                lambda: self.controller._screen_state["units"][1]["translation"] == "重试译文"
            )
            self.assertEqual(mock.call_args[0][0], ["line1"])
        self.assertEqual(self.controller._screen_state["units"][0]["translation"], "译文")

    def test_stale_result_ignored(self):
        one = self.controller._begin("clipboard")
        self.controller._begin("dictionary")
        self.controller._result((one, dict(stage="clipboard", text="stale", pos=None), None))
        self.assertIsNone(self.controller._popup)

    def test_dismiss_screen_ignores_late_progress_and_failure(self):
        task = self.controller._begin("screen")
        units = [dict(id="0", bbox=(10, 60, 200, 90), source="source", translation="")]
        self.controller._screen_state = dict(units=units, rect=QRect(0, 0, 800, 600), screens=[])
        self.controller._show_screen(
            units, QRect(0, 0, 800, 600), [Monitor("test", (0, 0, 800, 600), (0, 0, 800, 600))]
        )
        self.controller._overlays[0].dismissed.emit()
        self.assertFalse(self.controller.runner.active(task))
        self.controller._progress((task, {"items": units}))
        self.controller._result(
            (task, dict(stage="translated", units=[dict(error="failed")]), None)
        )
        self.assertEqual(self.controller._overlays, [])
        self.assertIsNone(self.controller._screen_state)
        self.assertIsNone(self.controller._panel)

    def test_card_library_search_async(self):
        from app.ui.cards_dialog import CardsDialog

        cards.add_card(config.db_path(), dict(word="hello", translation="问候"))
        dialog = CardsDialog()
        dialog.show()
        self.until(lambda: len(dialog.rows) == 1)
        dialog.search.setText("missing")
        self.until(lambda: len(dialog.rows) == 0)
        dialog.close()
        APP.processEvents()

    def test_connection_uses_openai_environment_fallback(self):
        from app.ui.settings_dialog import SettingsDialog

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": "test-openai"}),
            patch.object(translate, "test_connection", return_value=(True, "ok", [])) as probe,
        ):
            dialog = SettingsDialog()
            dialog.api_key.clear()
            dialog._test_api()
            self.until(lambda: probe.called and dialog.test_button.isEnabled())
            self.assertEqual(probe.call_args.args[1], "test-openai")
            dialog.close()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
