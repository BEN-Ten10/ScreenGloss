"""Exercise the documented batch entry points without starting the actual GUI."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "win32", "Windows batch launchers")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ScreenGloss space 中文"
        self.root.mkdir()
        for name in ("run.bat", "run-console.bat"):
            shutil.copyfile(ROOT / name, self.root / name)

    def invoke(self, name):
        return subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", name],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )

    def create_environment(self, exit_code):
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(self.root / ".venv")],
            capture_output=True,
            check=True,
            timeout=30,
        )
        app = self.root / "app"
        app.mkdir()
        marker = str(self.root / "marker.json")
        (app / "launcher.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            f"Path({marker!r}).write_text(json.dumps({{'cwd':str(Path.cwd()),'args':sys.argv}}))\n"
            f"raise SystemExit({exit_code})\n",
            encoding="utf-8",
        )

    def test_missing_environment_returns_failure(self):
        for name in ("run.bat", "run-console.bat"):
            with self.subTest(name=name):
                self.assertEqual(self.invoke(name).returncode, 1)

    def test_console_preserves_application_exit_and_working_directory(self):
        self.create_environment(7)
        self.assertEqual(self.invoke("run-console.bat").returncode, 7)
        marker = json.loads((self.root / "marker.json").read_text())
        self.assertEqual(Path(marker["cwd"]), self.root)
        self.assertEqual(Path(marker["args"][0]), self.root / "app/launcher.py")

    def test_windowed_launcher_starts_in_repository_directory(self):
        self.create_environment(0)
        self.assertEqual(self.invoke("run.bat").returncode, 0)
        marker_path = self.root / "marker.json"
        deadline = time.monotonic() + 5
        while not marker_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker_path.exists())
        marker = json.loads(marker_path.read_text())
        self.assertEqual(Path(marker["cwd"]), self.root)
