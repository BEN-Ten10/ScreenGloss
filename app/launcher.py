"""Catch early dependency/import failures even when started with pythonw.exe."""

import ctypes
import sys
import traceback
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
if __name__ == "__main__":
    try:
        from app.main import main

        code = main()
    except Exception:
        target = root / "data" / "startup-error.log"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(traceback.format_exc()[-16000:], encoding="utf-8")
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(
            None,
            f"ScreenGloss 无法启动。请使用 run-console.bat 或查看：\n{target}",
            "ScreenGloss",
            0x10,
        )
        code = 1
    raise SystemExit(code)
