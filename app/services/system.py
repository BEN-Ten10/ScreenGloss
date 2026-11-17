"""Per-user desktop shortcut and Windows autostart management.

Both features are implemented as ordinary ``.lnk`` files: one on the user's
Desktop, one in the user's Startup folder.  No registry Run key and no admin
rights are required, and removing the file fully disables the feature.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pythoncom
from win32com.client import Dispatch
from win32com.shell import shell, shellcon

from .. import config

APP_NAME = "ScreenGloss"

_KINDS = {
    "desktop": shellcon.CSIDL_DESKTOP,
    "startup": shellcon.CSIDL_STARTUP,
}
_LABELS = {"desktop": "desktop shortcut", "startup": "autostart"}


def folder(kind: str) -> Path:
    return Path(shell.SHGetFolderPath(0, _KINDS[kind], None, 0))


def shortcut_path(kind: str) -> Path:
    return folder(kind) / f"{APP_NAME}.lnk"


def _pythonw() -> Path:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def _create(kind: str) -> Path:
    target = shortcut_path(kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()
    shell_obj = None
    link = None
    try:
        shell_obj = Dispatch("WScript.Shell")
        link = shell_obj.CreateShortCut(str(target))
        link.TargetPath = str(_pythonw())
        link.Arguments = f'-B "{config.APP_DIR / "launcher.py"}"'
        link.WorkingDirectory = str(config.BASE_DIR)
        link.IconLocation = f"{_pythonw()},0"
        link.Description = "ScreenGloss - screen translator & dictionary"
        link.Save()
    finally:
        # Release the COM objects before uninitialising this thread, otherwise
        # pywin32 logs "Win32 exception occurred releasing IUnknown".
        del link
        del shell_obj
        pythoncom.CoUninitialize()
    return target


def _remove(kind: str) -> None:
    target = shortcut_path(kind)
    if target.exists():
        target.unlink()


def exists(kind: str) -> bool:
    return shortcut_path(kind).exists()


def apply(desktop: bool | None, autostart: bool | None) -> list[str]:
    """Create/remove the requested shortcuts; ``None`` leaves one untouched."""
    errors: list[str] = []
    for kind, wanted in (("desktop", desktop), ("startup", autostart)):
        if wanted is None:
            continue
        try:
            if wanted:
                _create(kind)
            else:
                _remove(kind)
        except Exception as exc:  # noqa: BLE001 - reported to the tray
            errors.append(f"{_LABELS[kind]}: {exc}")
    return errors


def ensure(desktop: bool, autostart: bool) -> list[str]:
    """Create enabled shortcuts if missing; never removes anything."""
    errors: list[str] = []
    for kind, wanted in (("desktop", desktop), ("startup", autostart)):
        if not wanted:
            continue
        try:
            if not exists(kind):
                _create(kind)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{_LABELS[kind]}: {exc}")
    return errors
