"""Portable configuration for ScreenGloss.

Everything lives beside this project so a copy of the folder is a complete
"green" install: no registry, no %APPDATA%, no installer.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent  # ...\ScreenGloss
APP_DIR = BASE_DIR / "app"
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "version": 2,
    "api": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
        "timeout_s": 60,
    },
    "languages": {"source": "en", "target": "zh"},
    "hotkeys": {
        "screen_translate": "alt+s",
        "dictionary": "alt+d",
        "translate_clipboard": "alt+t",
        "hover_toggle": "alt+h",
    },
    "ocr": {
        "min_score": 0.5,
        "max_side": 1600,
        "lang": "ch",
        "threads": 4,
        "rotated": False,
        "memory_arena": True,
    },
    "dictionary": {
        "enabled": True,
        "mode": "offline_first",
        "context_enabled": True,
        "capture_width": 900,
        "capture_height": 120,
    },
    "screen": {"freeze_selection": True},
    "tts": {"engine": "sapi"},
    "hover": {"enabled": False, "delay_ms": 700},
    "system": {"desktop_shortcut": False, "autostart": False},
    "ui": {"popup_width": 520, "overlay_alpha": 245, "font_pt": 11, "language": "en"},
    "cache": {"max_mb": 100},
    "diagnostics": {"capture_images": False},
}

_write_lock = threading.RLock()
_session_key: str | None = None


def set_session_key(key: str | None) -> None:
    global _session_key
    _session_key = key.strip() if key else None


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(*, resolve_key: bool = True) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError(
                f"无法读取配置 {CONFIG_PATH.name}，请修复 JSON 或重命名该文件后重试"
            ) from exc
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    version = data.get("version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError("不支持该配置版本，请使用匹配版本的 ScreenGloss")
    cfg = _merge(DEFAULTS, data)
    for section, default in DEFAULTS.items():
        if isinstance(default, dict) and not isinstance(cfg.get(section), dict):
            raise ValueError(f"配置节 {section} 必须是对象")
    cfg["hotkeys"].pop("quit", None)
    for section, name, low, high in (
        ("api", "timeout_s", 5, 180),
        ("ocr", "min_score", 0, 1),
        ("ocr", "max_side", 640, 4000),
        ("ocr", "threads", 1, 16),
        ("dictionary", "capture_width", 240, 1800),
        ("dictionary", "capture_height", 60, 500),
        ("hover", "delay_ms", 200, 5000),
        ("ui", "font_pt", 8, 28),
        ("ui", "popup_width", 400, 900),
        ("ui", "overlay_alpha", 80, 255),
        ("cache", "max_mb", 5, 1000),
    ):
        value = cfg[section][name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not low <= value <= high
        ):
            raise ValueError(f"配置 {section}.{name} 应在 {low}–{high} 之间")
        if name != "min_score" and type(value) is not int:
            raise ValueError(f"配置 {section}.{name} 必须是整数")
    for section, default in DEFAULTS.items():
        if isinstance(default, dict):
            for name, value in default.items():
                if isinstance(value, bool) and not isinstance(cfg[section][name], bool):
                    raise ValueError(f"配置 {section}.{name} 必须是布尔值")
                if isinstance(value, str) and not isinstance(cfg[section][name], str):
                    raise ValueError(f"配置 {section}.{name} 必须是文本")
    cfg["version"] = 2
    # An API key kept in the environment still works without being written to disk.
    if resolve_key and _session_key:
        cfg["api"]["api_key"] = _session_key
    elif resolve_key and not cfg["api"].get("api_key"):
        env_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if env_key:
            cfg["api"]["api_key"] = env_key
    return cfg


def save(cfg: dict[str, Any], *, persist_key: bool | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = json.loads(json.dumps(cfg))
    with _write_lock:
        if persist_key is None:
            # A routine save must neither erase a saved key nor persist an env key.
            stored = load(resolve_key=False) if CONFIG_PATH.exists() else DEFAULTS
            out["api"]["api_key"] = stored["api"].get("api_key", "")
        elif not persist_key:
            out["api"]["api_key"] = ""
        out["hotkeys"].pop("quit", None)
        out["version"] = 2
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


def db_path() -> Path:
    return DATA_DIR / "cards.db"


def update(section: str, values: dict) -> dict:
    """Persist a small change without overwriting other windows' recent settings."""
    with _write_lock:
        cfg = load(resolve_key=False)
        cfg[section] = _merge(cfg.get(section, {}), values)
        save(cfg)
        return load()
