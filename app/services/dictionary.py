"""Offline dictionary management: streaming StarDict indexes and read-only JMdict."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from .. import config
from .stardict import StarDict

_instances: list[StarDict] | None = None
_scan_lock = threading.Lock()


def _scan() -> list[StarDict]:
    global _instances
    with _scan_lock:
        if _instances is not None:
            return _instances
        root = config.DATA_DIR / "dictionaries"
        found: list[StarDict] = []
        if root.exists():
            for ifo in sorted(root.glob("**/*.ifo")):
                if any(part.endswith(".importing") for part in ifo.parts):
                    continue
                dictionary = StarDict(ifo)
                if dictionary.valid:
                    found.append(dictionary)
        _instances = found
        return found


def refresh() -> None:
    global _instances
    with _scan_lock:
        _instances = None
        _word_cache.clear()


_word_cache: dict[str, bool] = {}


def _known(word: str) -> bool:
    if len(word) < 2:
        return False
    if word in _word_cache:
        return _word_cache[word]
    found = any(dictionary.contains(word) for dictionary in _scan())
    _word_cache[word] = found
    return found


_COMMON_TWO = {
    "it",
    "is",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "be",
    "we",
    "he",
    "as",
    "an",
    "or",
    "if",
    "do",
    "go",
    "my",
    "no",
    "so",
    "up",
    "us",
    "am",
    "me",
    "hi",
    "ok",
}


def segment_words(text: str) -> list[str]:
    """Return the best dictionary segmentation of a space-less Latin run."""
    raw = (text or "").strip()
    count = len(raw)
    if count < 6 or " " in raw or not raw.isascii():
        return [raw]
    best = [10**6] * (count + 1)
    previous = [-1] * (count + 1)
    best[0] = 0
    for start in range(count):
        if best[start] >= 10**6:
            continue
        for end in range(start + 2, min(count, start + 15) + 1):
            word = raw[start:end].lower()
            if not _known(word):
                continue
            cost = 1.0 + (0.5 if len(word) == 2 and word not in _COMMON_TWO else 0.0)
            total = best[start] + cost
            if total < best[end] - 1e-9:
                best[end] = total
                previous[end] = start
    if best[count] >= 10**6:
        return [raw]
    segments: list[str] = []
    position = count
    while position > 0:
        start = previous[position]
        if start < 0:
            return [raw]
        segments.append(raw[start:position])
        position = start
    segments.reverse()
    return segments if len(segments) > 1 else [raw]


def segment_latin(text: str, index: int = -1) -> str:
    """Return the segmented word that contains ``index`` (or remains whole)."""
    raw = (text or "").strip()
    words = segment_words(raw)
    if len(words) < 2:
        return raw
    if 0 <= index < len(raw):
        cursor = 0
        for word in words:
            if cursor <= index < cursor + len(word):
                return word
            cursor += len(word)
    return words[0]


def preload(cancel=None, progress=None) -> int:
    """Load every installed dictionary index once (used at app startup)."""
    count = 0
    for dictionary in _scan():
        try:
            dictionary._load(cancel, progress)
            count += 1
        except Exception:
            if cancel:
                cancel.check()
            raise
    return count


def installed() -> list[str]:
    return [dictionary.name for dictionary in _scan()]


def installed_info() -> list[dict]:
    info = []
    for dictionary in _scan():
        folder = dictionary.path.parent
        size = 0
        try:
            size = sum(file.stat().st_size for file in folder.glob("*") if file.is_file())
        except Exception:
            pass
        info.append(
            {
                "name": dictionary.name,
                "path": str(folder),
                "size_mb": round(size / (1024 * 1024), 1),
                "wordcount": dictionary.wordcount,
                "kind": "stardict",
            }
        )
    for name, path in _sqlite_sources():
        count = 0
        try:
            import sqlite3

            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            count = int(con.execute("SELECT COUNT(*) FROM jmdict").fetchone()[0])
            con.close()
        except Exception:
            count = 0
        info.append(
            {
                "name": path.stem,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "wordcount": count,
                "kind": "sqlite",
            }
        )
    return info


def root() -> Path:
    return config.DATA_DIR / "dictionaries"


def add_dictionary(source: Path, cancel=None, progress=None) -> str:
    """Copy a StarDict folder (or a single .ifo's folder) into data/dictionaries."""
    import shutil

    source = Path(source)
    if source.is_file() and source.suffix.lower() == ".ifo":
        source = source.parent
    if not source.is_dir():
        raise ValueError("请选择包含 .ifo/.idx/.dict 的词典文件夹")
    if not any(source.glob("*.ifo")):
        raise ValueError("该文件夹里没有 .ifo 文件")
    target = root() / source.name
    if target.exists():
        raise ValueError(f"词典 {source.name} 已存在，请先删除")
    ifo_stem = next(iter(source.glob("*.ifo"))).stem
    if any(dictionary.name == ifo_stem for dictionary in _scan()):
        raise ValueError(f"词典 {ifo_stem} 已安装，请先删除")
    staging = root() / (source.name + ".importing")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        shutil.copytree(source, staging)
        candidates = [StarDict(path) for path in staging.glob("*.ifo")]
        if not candidates or not all(item.valid for item in candidates):
            raise ValueError("词典文件不完整或格式不支持")
        for candidate in candidates:
            candidate._load(cancel, progress)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    refresh()
    ifos = list(target.glob("*.ifo"))
    return ifos[0].stem if ifos else source.name


def remove_dictionary(name: str) -> None:
    import shutil

    for db_name, path in _sqlite_sources():
        if db_name == name or path.stem == name:
            path.unlink()
            refresh()
            return
    target = None
    for dictionary in _scan():
        if dictionary.name == name:
            target = dictionary.path.parent
            break
    if target is None:
        target = root() / name
    if not target.exists():
        raise ValueError("词典不存在")
    shutil.rmtree(target)
    refresh()


_POS_MARK = re.compile(
    r"(?:(?<=\s)|^)(n|vt|vi|v|adj|a|ad|adv|prep|conj|pron|num|art|int|interj|aux)\.\s*",
    re.IGNORECASE,
)


def _parse_senses(article: str) -> tuple[str, str, str, list[str]]:
    """Split an ECDICT-style article into tidy fields.

    The article keeps all of its information; the layout is what gets tidied:
    inflected forms move to their own field, every part of speech becomes its
    own line, and technical senses are kept (not dropped) at the end.
    """
    forms: list[str] = []
    tail = re.search(r"\s+([A-Za-z][A-Za-z'\-]*(?:,\s*[A-Za-z][A-Za-z'\-]*)+)\s*$", article)
    if tail:
        candidate = [item.strip() for item in tail.group(1).split(",") if item.strip()]
        if candidate and all(len(item) <= 20 for item in candidate):
            forms = candidate
            article = article[: tail.start()].strip()

    markers = list(_POS_MARK.finditer(article))
    if not markers:
        text = article.strip()
        return text[:200], text[:600], "", forms
    senses: list[tuple[str, str]] = []
    for index, match in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(article)
        gloss = re.sub(r"\s+", " ", article[match.end() : end]).strip(" ,;")
        if gloss:
            senses.append((match.group(1).lower(), gloss))
    if not senses:
        return article[:200], article[:600], "", forms

    chinese: list[str] = []
    for _pos, gloss in senses:
        if re.search(r"[\u3400-\u9fff]", gloss):
            cleaned = gloss[:48]
            if cleaned not in chinese:
                chinese.append(cleaned)
    translation = "；".join(chinese[:2]) or senses[0][1][:80]
    definition = "\n".join(f"{pos}. {gloss[:80]}" for pos, gloss in senses[:6])
    pos_text = ",".join(dict.fromkeys(pos for pos, _gloss in senses[:6]))
    return translation, definition, pos_text, forms


def _clean_article(text: str) -> str:
    text = re.sub(r"\*\[[^\]]*\]", " ", text)
    text = re.sub(r"\[[^\]]{1,40}\]", " ", text)
    text = re.sub(r"-[A-Z]\d{1,2}\b", " ", text)
    text = re.sub(r"\(宝[^)]*\)", " ", text)
    text = re.sub(r"\([^)]*\d+\s*/\s*\d+[^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _sqlite_sources() -> list[tuple[str, Path]]:
    sources = []
    root = config.DATA_DIR / "dictionaries"
    if root.exists():
        for db in sorted(root.glob("**/*.db")):
            sources.append((db.parent.name, db))
    return sources


def _lookup_sqlite(word: str) -> dict | None:
    import sqlite3

    for name, path in _sqlite_sources():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT word, reading, gloss, pos FROM jmdict WHERE key=? LIMIT 30",
                (word,),
            ).fetchall()
            if not rows:
                rows = con.execute(
                    "SELECT word, reading, gloss, pos FROM jmdict WHERE key=? LIMIT 30",
                    (word.lower(),),
                ).fetchall()
            con.close()
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0]
        gloss = "\n".join(dict.fromkeys(r["gloss"] for r in rows if r["gloss"]))
        return {
            "word": row["word"] or word,
            "reading": row["reading"] or "",
            "translation": gloss,
            "definition": gloss,
            "definition_language": "en",
            "part_of_speech": row["pos"] or "",
            "source": f"jmdict:{name}",
        }
    return None


def lookup(word: str, cancel=None) -> dict | None:
    """Return {'word', 'translation', 'definition', 'source'} from the first hit."""
    for dictionary in _scan():
        hit = dictionary.lookup(word, cancel)
        if not hit:
            continue
        article = _clean_article(hit["article"])
        ipa_match = re.search(r"\[([^\]\n]{2,40})\]", hit["article"])
        ipa = ipa_match.group(1).strip() if ipa_match else ""
        translation, definition, pos_text, forms = _parse_senses(article)
        return {
            "word": hit["word"],
            "translation": translation,
            "definition": definition,
            "ipa": ipa,
            "reading": "",
            "part_of_speech": pos_text,
            "forms": forms,
            "source": f"stardict:{hit['dictionary']}",
            "definition_language": "zh" if re.search(r"[\u3400-\u9fff]", article) else "unknown",
        }
    return _lookup_sqlite(word)
