"""OCR layout analysis and model management."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from .. import config
from .ocr_engine import _CJK, _get_engine, ocr, word_at

__all__ = [
    "ocr",
    "word_at",
    "_get_engine",
    "merge_lines",
    "layout_blocks",
    "split_units",
    "sentence_at",
    "installed_models",
    "add_model",
    "remove_model",
    "model_dir",
]


def merge_lines(items: list[dict], y_tolerance: int = 8) -> list[dict]:
    """Group OCR fragments into reading lines (left-to-right, top-to-bottom)."""
    if not items:
        return []
    rows: list[list[dict]] = []
    for item in sorted(items, key=lambda i: (i["bbox"][1], i["bbox"][0])):
        placed = False
        for row in rows:
            if abs(row[0]["bbox"][1] - item["bbox"][1]) <= y_tolerance:
                row.append(item)
                placed = True
                break
        if not placed:
            rows.append([item])
    lines = []
    for row in rows:
        row.sort(key=lambda i: i["bbox"][0])
        text = " ".join(i["text"] for i in row)
        x0 = min(i["bbox"][0] for i in row)
        y0 = min(i["bbox"][1] for i in row)
        x1 = max(i["bbox"][2] for i in row)
        y1 = max(i["bbox"][3] for i in row)
        lines.append({"bbox": (x0, y0, x1, y1), "text": text, "items": row})
    return lines


def group_paragraphs(lines: list[dict], gap_factor: float = 1.35) -> list[dict]:
    """Merge consecutive lines that belong to one paragraph.

    Screen translation draws one box per paragraph instead of one per line, so
    long wrapped sentences no longer produce a stack of overlapping boxes.
    """
    if not lines:
        return []
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    heights = sorted(line["bbox"][3] - line["bbox"][1] for line in ordered)
    median = heights[len(heights) // 2] if heights else 1.0
    max_gap = max(6.0, median * gap_factor)

    def union(group: list[dict]) -> tuple[float, float, float, float]:
        return (
            min(line["bbox"][0] for line in group),
            min(line["bbox"][1] for line in group),
            max(line["bbox"][2] for line in group),
            max(line["bbox"][3] for line in group),
        )

    def text_of(group: list[dict]) -> str:
        parts = [line["text"].strip() for line in group if line["text"].strip()]
        if not parts:
            return ""
        joined = ""
        for index, part in enumerate(parts):
            if index and not (_CJK.search(part) and _CJK.search(parts[index - 1])):
                joined += " "
            joined += part
        return joined

    paragraphs: list[dict] = []
    current = [ordered[0]]
    for line in ordered[1:]:
        px0, _py0, px1, py1 = union(current)
        x0, y0, x1, _y1 = line["bbox"]
        gap = y0 - py1
        overlap = min(px1, x1) - max(px0, x0)
        same_paragraph = gap <= max_gap and overlap >= -0.25 * max(1.0, x1 - x0)
        if same_paragraph:
            current.append(line)
        else:
            bbox = union(current)
            paragraphs.append({"bbox": bbox, "text": text_of(current), "lines": list(current)})
            current = [line]
    bbox = union(current)
    paragraphs.append({"bbox": bbox, "text": text_of(current), "lines": list(current)})
    return paragraphs


_SENTENCE_END = ".!?。！？"
_LEADING_MARK = re.compile(r"^\s*(?:\d+[.)]|[•·\-])\s*")


def model_dir() -> "Path":

    folder = config.DATA_DIR / "ocr"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def installed_models() -> list[dict]:
    folder = model_dir()
    models = []
    for path in sorted(folder.glob("*.onnx")):
        models.append(
            {
                "code": path.stem,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
            }
        )
    return models


def add_model(source: "Path") -> str:
    import shutil
    from pathlib import Path

    source = Path(source)
    if source.suffix.lower() != ".onnx":
        raise ValueError("请选择 .onnx 识别模型")
    if source.stem.lower() in ("ch", "en", "auto", "ja", "ko", "ru"):
        raise ValueError("该模型名称为内置语言保留，请重命名后导入")
    target = model_dir() / source.name
    if target.exists():
        raise ValueError(f"模型 {source.stem} 已存在")
    staging = target.with_suffix(".partial")
    # Validate on a temporary model path without exposing a broken installed model.
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        str(source), sess_options=options, providers=["CPUExecutionProvider"]
    )
    inputs = session.get_inputs()
    meta = session.get_modelmeta().custom_metadata_map
    if len(inputs) != 1 or len(inputs[0].shape) != 4 or "character" not in meta:
        raise ValueError("需要含 character 字符表的 PP-OCR CTC 识别模型（NCHW 输入）")
    shape = inputs[0].shape
    height = shape[2] if isinstance(shape[2], int) else 48
    if height != 48 or (isinstance(shape[1], int) and shape[1] != 3):
        raise ValueError("当前支持 3×48×W 识别模型")
    width = shape[3] if isinstance(shape[3], int) else 160
    output = session.run(None, {inputs[0].name: np.ones((1, 3, height, width), dtype=np.float32)})
    if not output or len(output[0].shape) != 3:
        raise ValueError("模型输出不是 CTC 识别结果")
    del session
    try:
        shutil.copy2(source, staging)
        staging.replace(target)
        _get_engine(source.stem)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        staging.unlink(missing_ok=True)
    return source.stem


def remove_model(code: str) -> None:

    if code == "ch":
        raise ValueError("内置中英模型不能删除")
    target = model_dir() / f"{code}.onnx"
    if not target.exists():
        raise ValueError("模型不存在")
    target.unlink()
    from .ocr_engine import _cache, _engines, _lock

    with _lock:
        _engines.clear()
        _cache.clear()


def sentence_at(lines: list[dict], word: str, x: float, y: float) -> str:
    """Rebuild the sentence around a word, but stay inside its visual block.

    Joining every captured line over-reaches: nearby menu items, buttons and
    unrelated paragraphs end up in the sentence ("... the same question he rule
    Copy Example prompt · Calculator"). Blocks from :func:`layout_blocks` keep
    separate UI elements apart, so expansion to sentence punctuation happens
    only inside the block that contains the pointer.
    """
    if not lines:
        return ""

    def line_distance(line: dict) -> float:
        _x0, y0, _x1, y1 = line["bbox"]
        if y0 - 4 <= y <= y1 + 4:
            return 0.0
        return min(abs(y - y0), abs(y - y1))

    fallback = min(lines, key=line_distance)
    fallback_text = _LEADING_MARK.sub("", fallback["text"].strip()).strip()

    blocks = layout_blocks(lines)
    point_blocks = [
        block
        for block in blocks
        if block["bbox"][0] - 2 <= x <= block["bbox"][2] + 2
        and block["bbox"][1] - 2 <= y <= block["bbox"][3] + 2
    ]
    word_blocks = [block for block in blocks if word and word.lower() in block["text"].lower()]
    block = None
    if point_blocks:
        block = min(
            point_blocks,
            key=lambda item: (
                (item["bbox"][2] - item["bbox"][0]) * (item["bbox"][3] - item["bbox"][1])
            ),
        )
    elif word_blocks:
        block = word_blocks[0]
    if block is None:
        return fallback_text

    block_lines = sorted(
        block.get("lines") or [], key=lambda line: (line["bbox"][1], line["bbox"][0])
    )
    if not block_lines:
        return fallback_text
    target = min(block_lines, key=line_distance)
    target_index = block_lines.index(target)

    def height(line: dict) -> float:
        return line["bbox"][3] - line["bbox"][1]

    def compatible(previous: dict, following: dict) -> bool:
        hp, hf = height(previous), height(following)
        if not hp or not hf:
            return False
        # A tiny eyebrow line above a display heading is a different text run.
        if max(hp, hf) / min(hp, hf) > 1.35:
            return False
        gap = following["bbox"][1] - previous["bbox"][3]
        if gap > max(6.0, 0.75 * min(hp, hf)):
            return False
        px0, _py0, px1, _py1 = previous["bbox"]
        fx0, _fy0, fx1, _fy1 = following["bbox"]
        overlap = min(px1, fx1) - max(px0, fx0)
        return overlap > -0.4 * min(px1 - px0, fx1 - fx0)

    def ends_sentence(line: dict) -> bool:
        return line["text"].strip().endswith(tuple(_SENTENCE_END))

    start = end = target_index
    block_right = max(line["bbox"][2] for line in block_lines)
    block_width = max(1.0, block_right - min(line["bbox"][0] for line in block_lines))
    while (
        start > 0
        and compatible(block_lines[start - 1], block_lines[start])
        and not ends_sentence(block_lines[start - 1])
    ):
        start -= 1
    while (
        end + 1 < len(block_lines)
        and compatible(block_lines[end], block_lines[end + 1])
        and not ends_sentence(block_lines[end])
        # A short line that stops well before the block's right margin is a
        # paragraph end, not a wrapped continuation.
        and block_lines[end]["bbox"][2] >= block_right - max(24.0, 0.12 * block_width)
    ):
        end += 1
    run = block_lines[start : end + 1]
    parts = [line["text"].strip() for line in run if line["text"].strip()]
    text = ""
    for index, part in enumerate(parts):
        if index and not (_CJK.search(part) and _CJK.search(parts[index - 1])):
            text += " "
        text += part
    text = text.strip()
    position = text.lower().find(word.lower()) if word else -1
    if position < 0:
        return _LEADING_MARK.sub("", target["text"].strip()).strip()

    left = position
    while left > 0 and text[left - 1] not in _SENTENCE_END:
        left -= 1
    right = position + len(word)
    while right < len(text) and text[right] not in _SENTENCE_END:
        right += 1
    if right < len(text):
        right += 1
    sentence = text[left:right].strip()
    sentence = _LEADING_MARK.sub("", sentence).strip("\"'“”‘’ ")
    return sentence or fallback_text


def layout_blocks(items: list[dict]) -> list[dict]:
    """Split OCR fragments into visual blocks with a recursive XY-cut.

    Cutting happens on fragments (not pre-merged lines), because a line merge
    that only looks at y would glue the three columns of a feature list into a
    single pseudo-line. After cutting, each block's own fragments are merged
    into reading lines and translated as one unit: a wrapped sentence keeps its
    flow, separate columns/sections keep their own boxes.
    """
    if not items:
        return []
    heights = sorted(item["bbox"][3] - item["bbox"][1] for item in items)
    median = heights[len(heights) // 2] if heights else 12.0
    min_y_gap = max(8.0, median * 0.85)
    min_x_gap = max(20.0, median * 1.1)

    def biggest_gap(group: list[dict], axis: int) -> tuple[float, int | None]:
        ordered = sorted(group, key=lambda item: item["bbox"][axis])
        reach = ordered[0]["bbox"][axis + 2]
        best = (0.0, None)
        for index in range(1, len(ordered)):
            start = ordered[index]["bbox"][axis]
            gap = start - reach
            if gap > best[0]:
                best = (gap, index)
            reach = max(reach, ordered[index]["bbox"][axis + 2])
        return best

    def cut(group: list[dict]) -> list[list[dict]]:
        if len(group) <= 1:
            return [group]
        gap_y, split_y = biggest_gap(group, 1)
        gap_x, split_x = biggest_gap(group, 0)
        if gap_y >= min_y_gap and gap_y >= gap_x * 0.8 and split_y is not None:
            ordered = sorted(group, key=lambda item: item["bbox"][1])
            return cut(ordered[:split_y]) + cut(ordered[split_y:])
        if gap_x >= min_x_gap and split_x is not None:
            ordered = sorted(group, key=lambda item: item["bbox"][0])
            return cut(ordered[:split_x]) + cut(ordered[split_x:])
        return [group]

    blocks: list[dict] = []
    for group in cut(list(items)):
        lines = merge_lines(group)
        x0 = min(item["bbox"][0] for item in group)
        y0 = min(item["bbox"][1] for item in group)
        x1 = max(item["bbox"][2] for item in group)
        y1 = max(item["bbox"][3] for item in group)
        parts = [line["text"].strip() for line in lines if line["text"].strip()]
        text = ""
        for index, part in enumerate(parts):
            if index and not (_CJK.search(part) and _CJK.search(parts[index - 1])):
                text += " "
            text += part
        blocks.append({"bbox": (x0, y0, x1, y1), "text": text, "lines": lines})
    blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    return blocks


def heading_score(block: dict) -> tuple[float, dict]:
    """Typographic heading score with its supporting features.

    Document-layout tools classify headings from typography rather than from a
    word list: relative font size, whitespace below, line width, list markers
    and capitalisation (e.g. pdfstructx and kreuzberg's PDF structure
    classifier). OCR gives no bold flag, so line height is the font-size proxy
    and the gap after the first line is the whitespace proxy.
    """
    import statistics

    lines = block.get("lines") or []
    if len(lines) < 2:
        return 0.0, {}
    first = lines[0]
    text = first["text"].strip()
    heights = [line["bbox"][3] - line["bbox"][1] for line in lines]
    median_height = statistics.median(heights) or 1.0
    first_height = first["bbox"][3] - first["bbox"][1]
    gaps = [
        lines[index + 1]["bbox"][1] - lines[index]["bbox"][3] for index in range(len(lines) - 1)
    ]
    median_gap = statistics.median(gaps) if gaps else 1.0
    gap_after = gaps[0] if gaps else 0.0
    block_width = max(1.0, block["bbox"][2] - block["bbox"][0])
    width_ratio = (first["bbox"][2] - first["bbox"][0]) / block_width
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", text)
    title_case = bool(words) and all(word[0].isupper() for word in words)
    list_heading = bool(re.match(r"^\s*(\d+[.)]|[•·\-])\s+", text))
    ends_punct = text.endswith((".", "!", "?", "。", "！", "？"))

    size_ratio = first_height / median_height
    gap_ratio = gap_after / max(1.0, abs(median_gap)) if median_gap > 0 else 1.0
    score = 0.0
    if list_heading:
        score += 0.9
    if size_ratio >= 1.15:
        score += 0.5
    if gap_ratio >= 1.25:
        score += 0.35
    if width_ratio <= 0.6:
        score += 0.35
    if title_case:
        score += 0.3
    score += -0.5 if ends_punct else 0.2
    if len(text) > 40:
        score -= 0.4
    # Short, clearly separated labels are headings even without title case.
    if width_ratio <= 0.35 and gap_ratio >= 1.5:
        score += 0.25
    features = {
        "size_ratio": round(size_ratio, 2),
        "gap_ratio": round(gap_ratio, 2),
        "width_ratio": round(width_ratio, 2),
        "title_case": title_case,
        "list": list_heading,
        "punct": ends_punct,
    }
    return score, features


def is_heading(block: dict) -> bool:
    return heading_score(block)[0] >= 0.7


def split_units(blocks: list[dict]) -> list[tuple[tuple[float, float, float, float], str]]:
    """Turn blocks into (bbox, text) translation units.

    A block whose first line looks like a heading (short, title case or a list
    marker, no sentence punctuation) yields two units: the heading anchored to
    its own bbox, and the body anchored to the remaining lines. This keeps a
    card's title over the title instead of over the artwork, while a wrapped
    paragraph still stays one unit.
    """
    units: list[tuple[tuple[float, float, float, float], str]] = []
    for block in blocks:
        lines = block.get("lines") or []
        if not lines:
            continue
        first = lines[0]
        text = first["text"].strip()
        if is_heading(block):
            units.append((first["bbox"], text))
            rest = lines[1:]
            body_bbox = (
                min(line["bbox"][0] for line in rest),
                min(line["bbox"][1] for line in rest),
                max(line["bbox"][2] for line in rest),
                max(line["bbox"][3] for line in rest),
            )
            units.append((body_bbox, " ".join(line["text"].strip() for line in rest)))
        else:
            units.append((block["bbox"], block["text"]))
    return units
