"""RGB-in OCR with shared detection, lazy recognizers and isolated cached results."""

from __future__ import annotations

import copy
import hashlib
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path

import numpy as np

from app import config

from .logging_service import event

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_MODEL_FILES = {"ch": "ch", "en": "ch", "ja": "japan", "ko": "korean", "ru": "cyrillic"}
_engines = OrderedDict()
_cache = OrderedDict()
_lock = threading.RLock()
_base = None
_base_threads = None
_base_arena = None
_recent_lang = "ch"


def _tune_session(wrapper, threads, arena=True):
    """Disable busy-waiting between sequential OCR stages and reuse CPU allocations."""
    import onnxruntime as ort

    previous = wrapper.session
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    options.enable_cpu_mem_arena = arena
    # Adapter is pinned to rapidocr-onnxruntime 1.4.4 and verified by runtime tests.
    wrapper.session = ort.InferenceSession(
        previous._model_path, sess_options=options, providers=["CPUExecutionProvider"]
    )


def _group_tokens(words, text):
    """Older RapidOCR returns character boxes even for Latin words; normalize both forms."""
    grouped = []
    cursor = 0
    for word in words:
        token = word["text"]
        index = text.find(token, cursor)
        if index < 0:
            index = cursor
        end = index + len(token)
        cursor = end
        word = {**word, "index": index, "end": end}
        if not token.strip():
            continue
        cjk = bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff]", token))
        lexical = all(_word_char(c) for c in token)
        if (
            grouped
            and not cjk
            and lexical
            and grouped[-1].get("lexical")
            and index == grouped[-1]["end"]
        ):
            previous = grouped[-1]
            gap = word["bbox"][0] - previous["bbox"][2]
            height = word["bbox"][3] - word["bbox"][1]
            if not re.search(r"[\uac00-\ud7af]", token) or gap <= max(3, height * 0.20):
                previous["text"] += token
                previous["end"] = end
                previous["bbox"] = (
                    previous["bbox"][0],
                    min(previous["bbox"][1], word["bbox"][1]),
                    word["bbox"][2],
                    max(previous["bbox"][3], word["bbox"][3]),
                )
                x0, y0, x1, y1 = previous["bbox"]
                previous["quad"] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
                continue
        word["lexical"] = lexical and not cjk
        grouped.append(word)
    return grouped


def _bbox(box):
    return (
        min(p[0] for p in box),
        min(p[1] for p in box),
        max(p[0] for p in box),
        max(p[1] for p in box),
    )


def _get_engine(lang="ch"):
    global _base, _base_threads, _base_arena
    lang = "ch" if lang in ("auto", "en", None) else lang
    cfg = config.load()["ocr"]
    threads = min(int(cfg.get("threads", 4)), __import__("os").cpu_count() or 1)
    arena = bool(cfg.get("memory_arena", True))
    model = config.DATA_DIR / "ocr" / f"{_MODEL_FILES.get(lang, lang)}.onnx"
    stamp = (model.stat().st_size, model.stat().st_mtime_ns) if model.exists() else None
    key = (lang, threads, stamp, arena)
    with _lock:
        if key in _engines:
            _engines.move_to_end(key)
            return _engines[key]
        from rapidocr_onnxruntime import RapidOCR

        if _base is None or _base_threads != threads or _base_arena != arena:
            _base = RapidOCR(
                det_limit_side_len=960,
                det_limit_type="max",
                rec_batch_num=16,
                intra_op_num_threads=threads,
                inter_op_num_threads=1,
            )
            _tune_session(_base.text_det.infer, threads, arena)
            _tune_session(_base.text_cls.infer, 1, arena)
            _tune_session(_base.text_rec.session, threads, arena)
            _base_threads = threads
            _base_arena = arena
            _engines.clear()
            _cache.clear()
            _engines[("ch", threads, None, arena)] = _base
        if lang == "ch":
            engine = _base
        else:
            if not model.exists():
                raise ValueError(f"未安装 OCR 模型：{lang}")
            import rapidocr_onnxruntime
            from rapidocr_onnxruntime.ch_ppocr_rec import TextRecognizer
            from rapidocr_onnxruntime.utils import UpdateParameters, read_yaml, update_model_path

            cfg = update_model_path(
                read_yaml(Path(rapidocr_onnxruntime.__file__).parent / "config.yaml")
            )
            cfg = UpdateParameters()(
                cfg,
                rec_model_path=str(model),
                intra_op_num_threads=threads,
                inter_op_num_threads=1,
                rec_batch_num=16,
            )
            engine = copy.copy(_base)
            engine.text_rec = TextRecognizer(cfg["Rec"])
            _tune_session(engine.text_rec.session, threads, arena)
            # Actual forward pass validates input shape, character metadata and decoder.
            engine.text_rec([np.full((48, 160, 3), 255, dtype=np.uint8)], False)
        _engines[key] = engine
        while len(_engines) > 2:
            oldest = next(k for k in _engines if k[0] != "ch" and k != key)
            del _engines[oldest]
        return engine


def _rows(image, lang, rotated, word_boxes):
    engine = _get_engine(lang)
    result, parts = engine(
        np.ascontiguousarray(image[:, :, ::-1]), use_cls=rotated, return_word_box=word_boxes
    )
    if parts:
        event("ocr.stages", stage="det/cls/rec", duration_ms=[round(v * 1000, 1) for v in parts])
    return result or []


def _items(rows, scale=1):
    result = []
    for row in rows:
        quad = np.asarray(row[0], dtype=float) / scale
        item = {
            "quad": quad.tolist(),
            "bbox": _bbox(quad),
            "text": str(row[1]),
            "score": float(row[2]),
            "words": [],
        }
        if len(row) >= 6:
            for box, text, score in zip(row[3], row[4], row[5]):
                box = np.asarray(box, dtype=float) / scale
                item["words"].append(
                    {"bbox": _bbox(box), "quad": box.tolist(), "text": text, "score": float(score)}
                )
            item["characters"] = copy.deepcopy(item["words"])
            item["words"] = _group_tokens(item["words"], item["text"])
        if item["text"].strip():
            result.append(item)
    return result


def _restore_hangul_spacing(image, items):
    """Some Korean rec models omit spaces; recover visible gaps without a model call."""
    for item in items:
        if not re.search(r"[\uac00-\ud7af]", item["text"]) or " " in item["text"]:
            continue
        tokens = item.get("characters", [])
        if len(tokens) < 2:
            continue
        x0, y0, x1, y1 = (round(float(v)) for v in item["bbox"])
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(image.shape[1], x1)
        y1 = min(image.shape[0], y1)
        roi = image[y0:y1, x0:x1]
        if not roi.size:
            continue
        border = image[
            max(0, y0 - 3) : min(image.shape[0], y1 + 3),
            max(0, x0 - 3) : min(image.shape[1], x1 + 3),
        ]
        background = np.median(
            np.concatenate((border[0], border[-1], border[:, 0], border[:, -1])), axis=0
        )
        ink = (np.max(np.abs(roi.astype(float) - background), axis=2) > 40).mean(axis=0) > 0.05
        cuts = []
        start = None
        for column, value in enumerate([*ink, True]):
            if not value and start is None:
                start = column
            elif value and start is not None:
                if start > 0 and column < len(ink) and column - start > max(4, (y1 - y0) * 0.38):
                    cuts.append(x0 + (start + column) / 2)
                start = None
        if not cuts:
            continue
        groups = [[] for _ in range(len(cuts) + 1)]
        for token in tokens:
            center = (token["bbox"][0] + token["bbox"][2]) / 2
            groups[sum(center > cut for cut in cuts)].append(token)
        words = []
        cursor = 0
        for group in groups:
            if not group:
                continue
            text = "".join(t["text"] for t in group).strip()
            if not text:
                continue
            box = (
                min(t["bbox"][0] for t in group),
                min(t["bbox"][1] for t in group),
                max(t["bbox"][2] for t in group),
                max(t["bbox"][3] for t in group),
            )
            words.append(
                {
                    "text": text,
                    "bbox": box,
                    "quad": [
                        [box[0], box[1]],
                        [box[2], box[1]],
                        [box[2], box[3]],
                        [box[0], box[3]],
                    ],
                    "score": float(np.mean([t["score"] for t in group])),
                    "index": cursor,
                }
            )
            cursor += len(text) + 1
        if len(words) > 1:
            item["text"] = " ".join(w["text"] for w in words)
            item["words"] = words


def _reread(image, items, lang, word_boxes):
    """Reuse detected quads; only run another recognizer on the same crops."""
    import cv2

    engine = _get_engine(lang)
    bgr = np.ascontiguousarray(image[:, :, ::-1])
    crops = []
    valid = []
    for item in items:
        quad = np.asarray(item["quad"], dtype=np.float32)
        w = max(1, int(np.linalg.norm(quad[1] - quad[0])))
        h = max(1, int(np.linalg.norm(quad[3] - quad[0])))
        matrix = cv2.getPerspectiveTransform(
            quad, np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
        )
        crops.append(cv2.warpPerspective(bgr, matrix, (w, h)))
        valid.append(item)
    if not crops:
        return []
    rec, _ = engine.text_rec(crops, word_boxes)
    if word_boxes:
        rec = engine.cal_rec_boxes(crops, [np.asarray(i["quad"]) for i in valid], rec)
    rows = []
    for old, new in zip(valid, rec):
        rows.append([old["quad"], new[0], new[1], *new[2:]])
    return _items(rows)


def _script(items):
    text = "".join(i["text"] for i in items)
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if len(re.findall(r"[\u0400-\u04ff]", text)) >= 2:
        return "ru"
    return "ch"


def ocr(
    image,
    min_score=0.5,
    max_side=1600,
    lang="ch",
    auto_fallback=True,
    *,
    word_boxes=True,
    source="auto",
):
    global _recent_lang
    if image is None or not image.size:
        return []
    cfg = config.load()["ocr"]
    models = tuple(
        (p.name, p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted((config.DATA_DIR / "ocr").glob("*.onnx"))
    )
    key = (
        hashlib.sha256(np.ascontiguousarray(image).tobytes()).digest(),
        image.shape,
        min_score,
        max_side,
        lang,
        auto_fallback,
        word_boxes,
        source,
        cfg.get("threads"),
        cfg.get("rotated"),
        cfg.get("memory_arena", True),
        models,
    )
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            event("ocr.cache", cache_hit=True)
            return copy.deepcopy(_cache[key])
        started = time.perf_counter()
        original = image
        scale = 1.0
        if max(image.shape[:2]) > max_side:
            from PIL import Image

            scale = max_side / max(image.shape[:2])
            image = np.array(
                Image.fromarray(image).resize(
                    (round(image.shape[1] * scale), round(image.shape[0] * scale))
                )
            )
        chosen = lang
        if lang == "auto":
            chosen = source if source in ("ja", "ko", "ru", "en", "zh") else _recent_lang
        if chosen in ("en", "zh"):
            chosen = "ch"
        items = _items(_rows(image, chosen, bool(cfg.get("rotated", False)), word_boxes), scale)
        if lang == "auto":
            detected = _script(items)
            candidates = [detected] if detected != chosen else []

            def score(rows):
                return sum(r["score"] for r in rows) / max(1, len(rows))

            if not items and auto_fallback:
                candidates = [c for c in ("ch", "ja", "ko", "ru") if c != chosen]
            elif auto_fallback and score(items) < 0.8:
                candidates += [c for c in ("ja", "ko", "ru") if c not in (chosen, *candidates)]
            for candidate in candidates:
                try:
                    other = (
                        _reread(original, items, candidate, word_boxes)
                        if items
                        else _items(_rows(image, candidate, False, word_boxes), scale)
                    )
                    if other and score(other) > score(items) + 0.02:
                        items = other
                        chosen = candidate
                except (ValueError, RuntimeError):
                    event("ocr.language.unavailable", language=candidate)
            _recent_lang = chosen
        result = [r for r in items if r["score"] >= min_score]
        _restore_hangul_spacing(original, result)
        _cache[key] = copy.deepcopy(result)
        while len(_cache) > 12:
            _cache.popitem(last=False)
        event(
            "ocr.completed",
            language=chosen,
            count=len(result),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return result


def _word_char(char):
    return unicodedata.category(char)[0] in "LNM" or char in "'’-"


def word_at(lines, x, y, cancel=None):
    if not lines:
        return None
    fragments = [part for line in lines for part in line.get("items", [line])]
    hits = []
    for line in fragments:
        for token in line.get("words", []):
            x0, y0, x1, y1 = token["bbox"]
            if x0 - 2 <= x <= x1 + 2 and y0 - 3 <= y <= y1 + 3:
                hits.append((line, token))
    if hits:
        line, token = min(
            hits,
            key=lambda hit: (
                (hit[1]["bbox"][2] - hit[1]["bbox"][0]) * (hit[1]["bbox"][3] - hit[1]["bbox"][1])
            ),
        )
        text = line["text"]
        word = token["text"].strip("'’- ")
        index = token.get("index", text.find(word))
        # CJK character boxes need dictionary segmentation around the pointer.
        if word and re.search(r"[\u3400-\u9fff\u3040-\u30ff]", word):
            from . import dictionary

            center = max(0, index)
            candidates = []
            for start in range(max(0, center - 15), center + 1):
                for end in range(center + 1, min(len(text), start + 16) + 1):
                    candidate = text[start:end]
                    if cancel:
                        cancel.check()
                    if (
                        candidate.strip() == candidate
                        and not any(c.isspace() for c in candidate)
                        and dictionary.lookup(candidate, cancel)
                    ):
                        candidates.append((len(candidate), candidate, start))
            if candidates:
                _, word, index = max(candidates)
        if word:
            return {
                "word": word,
                "line": line,
                "index": max(0, index),
                "offset": 0,
                "bbox": token["bbox"],
            }
    # Fallback for models without word boxes, using Unicode letters and marks.
    nearby = []
    for line in fragments:
        x0, y0, x1, y1 = line["bbox"]
        if x0 - 8 <= x <= x1 + 8 and y0 - 6 <= y <= y1 + 6:
            nearby.append(line)
    if not nearby:
        return None
    line = min(nearby, key=lambda r: abs(y - (r["bbox"][1] + r["bbox"][3]) / 2))
    text = line["text"]
    if not text:
        return None
    x0, _, x1, _ = line["bbox"]
    index = max(0, min(len(text) - 1, int((x - x0) / max(1, x1 - x0) * len(text))))
    if not _word_char(text[index]):
        return None
    left = right = index
    while left > 0 and _word_char(text[left - 1]):
        left -= 1
    while right + 1 < len(text) and _word_char(text[right + 1]):
        right += 1
    word = text[left : right + 1].strip("'’-")
    return (
        {"word": word, "line": line, "index": index, "offset": index - left, "bbox": line["bbox"]}
        if word
        else None
    )
