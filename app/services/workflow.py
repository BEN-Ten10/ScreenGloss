"""Pure task stages connecting OCR, dictionary sources and model services."""

from __future__ import annotations

from . import dictionary, ocr, translate


def api_args(cfg, task=None):
    api = cfg["api"]
    return dict(
        src=cfg["languages"]["source"],
        tgt=cfg["languages"]["target"],
        base_url=api["base_url"],
        model=api["model"],
        api_key=api["api_key"],
        timeout=int(api["timeout_s"]),
        cancel=task,
    )


def recognize(task, image, *, lookup=False):
    task.check()
    cfg = task.cfg
    if cfg.get("diagnostics", {}).get("capture_images", False):
        import uuid

        from PIL import Image

        from app import config

        try:
            folder = config.DATA_DIR / "debug"
            folder.mkdir(parents=True, exist_ok=True)
            Image.fromarray(image).save(folder / f"sg_{uuid.uuid4().hex}.png")
            for old in sorted(
                folder.glob("sg_*.png"), key=lambda p: p.stat().st_mtime_ns, reverse=True
            )[10:]:
                old.unlink(missing_ok=True)
        except OSError:
            from .logging_service import event

            event("diagnostics.capture_failed", category="io")
    options = cfg["ocr"]
    items = ocr.ocr(
        image,
        min_score=float(options["min_score"]),
        max_side=min(1200, int(options["max_side"])) if lookup else int(options["max_side"]),
        lang=options["lang"],
        auto_fallback=not lookup,
        word_boxes=lookup,
        source=cfg["languages"]["source"],
    )
    task.check()
    return items


def screen_units(task, image, scale):
    items = recognize(task, image)
    if not items:
        raise ValueError("没有识别到文字，请检查 OCR 语言或扩大文字")
    blocks = ocr.layout_blocks(items)
    return [
        {
            "id": str(i),
            "bbox": tuple(float(v) / scale for v in box),
            "source": text,
            "translation": "",
            "error": None,
        }
        for i, (box, text) in enumerate(ocr.split_units(blocks))
    ]


def lookup_capture(task, image, x, y):
    cfg = task.cfg
    items = recognize(task, image, lookup=True)
    if not items:
        raise ValueError("指针附近没有识别到文字")
    found = ocr.word_at(items, x, y, task)
    if not found:
        raise ValueError("指针没有落在可识别的单词上")
    word = found["word"]
    if word.isascii() and len(word) >= 8 and " " not in word:
        word = dictionary.segment_latin(word, found.get("offset", 0))
    sentence = ocr.sentence_at(items, word, x, y) if cfg["dictionary"]["context_enabled"] else ""
    return {
        "word": word,
        "sentence": sentence,
        "context": sentence,
        "source_lang": cfg["languages"]["source"],
        "target_lang": cfg["languages"]["target"],
    }


def lookup_entry(task, entry):
    cfg = task.cfg
    mode = cfg["dictionary"]["mode"]
    word = entry["word"]
    order = {
        "offline_only": ["offline"],
        "llm_only": ["model"],
        "llm_first": ["model", "offline"],
        "offline_first": ["offline", "model"],
    }.get(mode)
    if not order:
        raise ValueError("无效的查词来源模式")
    errors = []
    for source in order:
        task.check()
        try:
            if source == "offline":
                result = (
                    dictionary.lookup(word, task)
                    if cfg["dictionary"].get("enabled", True)
                    else None
                )
                if not result:
                    errors.append("离线词典未收录")
                    continue
            else:
                result = translate.lookup_word(
                    word, context="", include_context=False, **api_args(cfg, task)
                )
            result = {**entry, **result, "sentence": entry.get("sentence", "")}
            result["context_allowed"] = mode != "offline_only" and bool(entry.get("sentence"))
            result["copy_text"] = result["word"]
            return result
        except translate.ApiError as exc:
            errors.append(str(exc))
    raise ValueError("；".join(errors) or "没有查到该词")


def explain(task, entry):
    if task.cfg["dictionary"]["mode"] == "offline_only":
        raise ValueError("仅离线模式不调用模型")
    result = translate.lookup_word(
        entry["word"],
        context=entry.get("sentence", ""),
        include_context=True,
        **api_args(task.cfg, task),
    )
    return {
        **entry,
        **result,
        "word": entry["word"],
        "sentence": entry.get("sentence", ""),
        "context_translation": result.get("sentence_translation"),
        "context_allowed": True,
    }
