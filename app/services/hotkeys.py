"""Global hotkeys through the Win32 RegisterHotKey API.

Windows delivers WM_HOTKEY to the registering thread's queue. A native event
filter forwards those messages through the Qt event loop.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "cmd": MOD_WIN,
    "super": MOD_WIN,
}


def _vk(key: str) -> int | None:
    key = key.strip().lower()
    if len(key) == 1:
        ch = key.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
    if key.startswith("f") and key[1:].isdigit():
        number = int(key[1:])
        if 1 <= number <= 24:
            return 0x6F + number
    named = {
        "space": 0x20,
        "tab": 0x09,
        "enter": 0x0D,
        "esc": 0x1B,
        "escape": 0x1B,
        "insert": 0x2D,
        "delete": 0x2E,
        "home": 0x24,
        "end": 0x23,
        "pageup": 0x21,
        "pagedown": 0x22,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
    }
    return named.get(key)


def parse(combo: str) -> tuple[int, int] | None:
    """'alt+s' -> (MOD_ALT|MOD_NOREPEAT, VK_S)."""
    combo = (combo or "").strip().lower()
    if not combo:
        return None
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    modifiers = MOD_NOREPEAT
    vk = None
    for part in parts:
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        else:
            if vk is not None:
                return None
            vk = _vk(part)
            if vk is None:
                return None
    if vk is None:
        return None
    return modifiers, vk


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, manager: "HotkeyManager") -> None:
        super().__init__()
        self._manager = manager

    def nativeEventFilter(self, eventType, message):  # noqa: N802 (Qt naming)
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY:
            callback = self._manager._callbacks.get(int(msg.wParam))
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
                return True, 0
        return False, 0


class HotkeyManager:
    def __init__(self) -> None:
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._registered: list[int] = []
        self._filter: _Filter | None = None
        self._next_id = 0xB000
        self._by_combo = {}

    def install(self, app: QApplication) -> None:
        if self._filter is None:
            self._filter = _Filter(self)
            app.installNativeEventFilter(self._filter)

    def register(self, bindings: dict[str, Callable[[], None]]) -> list[str]:
        """Register every combo; returns human-readable problems (empty = all good)."""
        parsed_bindings = [parse(combo) for combo in bindings]
        if any(p is None for p in parsed_bindings):
            return ["快捷键格式无效，保留原绑定"]
        if len(set(parsed_bindings)) != len(parsed_bindings):
            return ["快捷键重复，保留原绑定"]
        problems: list[str] = []
        desired = {}
        callbacks = {}
        newly_registered = []
        for combo, callback in bindings.items():
            parsed = parse(combo)
            hotkey_id = self._by_combo.get(parsed)
            if hotkey_id is None:
                while self._next_id in self._registered or self._next_id in newly_registered:
                    self._next_id = 0xB000 + (self._next_id - 0xB000 + 1) % 0x1000
                hotkey_id = self._next_id
                self._next_id = 0xB000 + (self._next_id - 0xB000 + 1) % 0x1000
                if not user32.RegisterHotKey(None, hotkey_id, *parsed):
                    problems.append(f"注册失败（可能被占用）：{combo}")
                    continue
                newly_registered.append(hotkey_id)
            desired[parsed] = hotkey_id
            callbacks[hotkey_id] = callback
        if problems:
            for hotkey_id in newly_registered:
                user32.UnregisterHotKey(None, hotkey_id)
            return problems
        for hotkey_id in self._registered:
            if hotkey_id not in callbacks:
                user32.UnregisterHotKey(None, hotkey_id)
        self._by_combo = desired
        self._callbacks = callbacks
        self._registered = list(callbacks)
        return problems

    def unregister(self) -> None:
        for hotkey_id in self._registered:
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass
        self._registered.clear()
        self._callbacks.clear()
        self._by_combo.clear()


HOTKEY_HELP = "快捷键格式：alt+s、ctrl+alt+d、f9（修饰键支持 ctrl/alt/shift/win）"
