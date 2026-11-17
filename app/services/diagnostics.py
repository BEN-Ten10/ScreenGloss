"""Opt-in diagnostic snapshots contain no config credentials or captured text."""

import json
import platform
from importlib.metadata import PackageNotFoundError, version

from app import config

from .logging_service import recent


def snapshot():
    dependencies = {}
    for name in (
        "PySide6_Essentials",
        "onnxruntime",
        "rapidocr-onnxruntime",
        "Pillow",
        "requests",
        "mss",
        "pywin32",
    ):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "missing"
    from . import dictionary

    try:
        edge_tts = version("edge-tts")
    except PackageNotFoundError:
        edge_tts = "not installed (optional)"

    return {
        "app": "ScreenGloss 1.1",
        "python": platform.python_version(),
        "platform": platform.system() + " " + platform.release(),
        "dependencies": dependencies,
        "optional_dependencies": {"edge-tts": edge_tts},
        "models": [
            {"file": p.name, "bytes": p.stat().st_size}
            for p in sorted((config.DATA_DIR / "ocr").glob("*.onnx"))
        ],
        "dictionaries": [
            {k: row[k] for k in ("name", "wordcount", "size_mb", "kind")}
            for row in dictionary.installed_info()
        ],
        "recent": recent(),
    }


def export(path):
    path.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def clear_debug():
    count = 0
    for path in (config.DATA_DIR / "debug").glob("sg_*.png"):
        path.unlink(missing_ok=True)
        count += 1
    return count
