"""Streaming StarDict import. Derived SQLite index; article data stays on disk."""

from __future__ import annotations

import gzip
import hashlib
import html
import re
import sqlite3
import struct
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from app import config

from .logging_service import event

_locks = {}
_registry_lock = threading.Lock()


class StarDict:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.valid = False
        with _registry_lock:
            self._lock = _locks.setdefault(str(self.path.resolve()), threading.RLock())
        self._ready = False
        meta = {}
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    meta[key.strip()] = value.strip()
            self.wordcount = int(meta.get("wordcount", 0))
            self.sametypesequence = meta.get("sametypesequence", "")
            self.offset_bits = int(meta.get("idxoffsetbits", 32))
            if self.offset_bits not in (32, 64):
                return
            self._idx_path = self.path.with_suffix(".idx")
            if not self._idx_path.exists():
                self._idx_path = self.path.with_suffix(".idx.gz")
            self._dict_path = self.path.with_suffix(".dict")
            if not self._dict_path.exists():
                self._dict_path = self.path.with_suffix(".dict.dz")
            self.valid = self._idx_path.is_file() and self._dict_path.is_file()
        except (OSError, ValueError, UnicodeError):
            self.wordcount = 0
        self._index = None
        self._data = None

    def _fingerprint(self):
        files = [self.path, self._idx_path, self._dict_path]
        syn = self.path.with_suffix(".syn")
        if syn.exists():
            files.append(syn)
        values = []
        for p in files:
            with p.open("rb") as stream:
                head = stream.read(4096)
                stream.seek(max(0, p.stat().st_size - 4096))
                tail = stream.read(4096)
            values.append(
                (
                    p.name,
                    p.stat().st_size,
                    p.stat().st_mtime_ns,
                    hashlib.sha256(head + tail).hexdigest(),
                )
            )
        return hashlib.sha256(repr(values).encode()).hexdigest()

    @staticmethod
    def _records(stream, bits=32):
        while True:
            word = bytearray()
            while True:
                ch = stream.read(1)
                if not ch:
                    if word:
                        raise ValueError("词典索引被截断")
                    return
                if ch == b"\0":
                    break
                word.extend(ch)
                if len(word) > 65536:
                    raise ValueError("词典索引词条过长")
            width = 12 if bits == 64 else 8
            raw = stream.read(width)
            if len(raw) != width:
                raise ValueError("词典索引偏移不完整")
            offset, size = struct.unpack(">QI" if bits == 64 else ">II", raw)
            yield word.decode("utf-8"), offset, size

    @contextmanager
    def _acquire(self, cancel):
        while not self._lock.acquire(timeout=0.1):
            if cancel:
                cancel.check()
        try:
            yield
        finally:
            self._lock.release()

    def _load(self, cancel=None, progress=None):
        with self._acquire(cancel):
            if self._ready:
                return
            if not self.valid:
                raise ValueError("词典文件不完整")
            root = config.DATA_DIR / "indexes"
            root.mkdir(parents=True, exist_ok=True)
            fingerprint = self._fingerprint()
            self._index = root / f"{fingerprint}.sqlite"
            self._data = self._dict_path
            if self._dict_path.suffix == ".dz":
                self._data = root / f"{fingerprint}.dict"
                if not self._data.exists():
                    tmp = self._data.with_suffix(".partial")
                    try:
                        with gzip.open(self._dict_path, "rb") as src, tmp.open("wb") as dst:
                            while chunk := src.read(1024 * 1024):
                                if cancel:
                                    cancel.check()
                                dst.write(chunk)
                        tmp.replace(self._data)
                    finally:
                        tmp.unlink(missing_ok=True)
            if not self._index.exists():
                tmp = self._index.with_suffix(".partial")
                tmp.unlink(missing_ok=True)
                con = sqlite3.connect(tmp)
                try:
                    con.execute("PRAGMA journal_mode=OFF")
                    con.execute("PRAGMA synchronous=OFF")
                    con.execute(
                        "CREATE TABLE entries (id INTEGER PRIMARY KEY,key TEXT,word TEXT,offset INTEGER,size INTEGER)"
                    )
                    batch = []
                    count = 0
                    data_size = self._data.stat().st_size
                    opener = gzip.open if self._idx_path.suffix == ".gz" else open
                    with opener(self._idx_path, "rb") as stream:
                        for number, (word, offset, size) in enumerate(
                            self._records(stream, self.offset_bits)
                        ):
                            if offset + size > data_size:
                                raise ValueError("词典条目超出正文边界")
                            batch.append((number, word.casefold(), word, offset, size))
                            count += 1
                            if len(batch) >= 10000:
                                if cancel:
                                    cancel.check()
                                con.executemany("INSERT INTO entries VALUES (?,?,?,?,?)", batch)
                                batch = []
                                event("dictionary.indexing", count=count)
                                if progress:
                                    progress(count)
                        con.executemany("INSERT INTO entries VALUES (?,?,?,?,?)", batch)
                    if count != self.wordcount:
                        raise ValueError("词典条目数量与元数据不符")
                    con.execute("CREATE INDEX entries_key ON entries(key)")
                    con.execute("CREATE TABLE synonyms (key TEXT,entry_id INTEGER)")
                    syn = self.path.with_suffix(".syn")
                    if syn.exists():
                        with syn.open("rb") as stream:
                            while True:
                                word = bytearray()
                                while (ch := stream.read(1)) not in (b"", b"\0"):
                                    word.extend(ch)
                                    if len(word) > 65536:
                                        raise ValueError("同义词索引无效")
                                if not ch:
                                    if word:
                                        raise ValueError("同义词索引被截断")
                                    break
                                raw = stream.read(4)
                                if len(raw) != 4:
                                    raise ValueError("同义词索引被截断")
                                number = struct.unpack(">I", raw)[0]
                                if number >= count:
                                    raise ValueError("同义词索引引用无效")
                                con.execute(
                                    "INSERT INTO synonyms VALUES (?,?)",
                                    (word.decode("utf-8").casefold(), number),
                                )
                    con.execute("CREATE INDEX synonyms_key ON synonyms(key)")
                    con.commit()
                    con.close()
                    con = None
                    tmp.replace(self._index)
                finally:
                    if con:
                        con.close()
                    tmp.unlink(missing_ok=True)
            self._ready = True
            event("dictionary.ready", count=self.wordcount)

    def _row(self, word, cancel=None):
        self._load(cancel)
        with closing(sqlite3.connect(self._index)) as con:
            key = word.strip().casefold()
            row = con.execute(
                "SELECT word,offset,size FROM entries WHERE key=? LIMIT 1", (key,)
            ).fetchone()
            if row is None:
                row = con.execute(
                    "SELECT e.word,e.offset,e.size FROM synonyms s JOIN entries e ON e.id=s.entry_id WHERE s.key=? LIMIT 1",
                    (key,),
                ).fetchone()
            return row

    def contains(self, word):
        return self.valid and self._row(word) is not None

    def _article(self, offset, size):
        with self._data.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(size)
        parts = []
        pos = 0
        sequence = self.sametypesequence
        while pos < len(data):
            if sequence:
                kind = sequence[0]
                sequence = sequence[1:]
                last = not sequence
            else:
                kind = chr(data[pos])
                pos += 1
                last = False
            if not kind.isalpha():
                raise ValueError("不支持的词典字段类型")
            if kind.islower():
                end = len(data) if last else data.find(b"\0", pos)
                if end < 0:
                    raise ValueError("词典文字字段不完整")
                text = data[pos:end].decode("utf-8", errors="replace")
                pos = end + 1
                if kind in "mlghxtyk":
                    parts.append(text)
            else:
                if last:
                    break
                if pos + 4 > len(data):
                    raise ValueError("词典二进制字段不完整")
                length = struct.unpack(">I", data[pos : pos + 4])[0]
                pos += 4 + length
                if pos > len(data):
                    raise ValueError("词典二进制字段被截断")
            if self.sametypesequence and not sequence:
                break
        if not parts:
            raise ValueError("该词条没有支持的文字字段")
        text = "\n".join(parts)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()

    def lookup(self, word, cancel=None):
        if not self.valid:
            return None
        row = self._row(word, cancel)
        if row:
            article = self._article(row[1], row[2])
            if article:
                return {"word": row[0], "article": article, "dictionary": self.name}
        return None
