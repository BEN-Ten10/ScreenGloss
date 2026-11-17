"""Run syntax and offline regression checks, writing UTF-8 evidence without shell redirection."""

import ast
import json
import sys
import time
import unittest
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
out = root / "tests" / "artifacts"
out.mkdir(parents=True, exist_ok=True)
checked = 0
for folder in ("app", "tests", "tools"):
    for path in (root / folder).rglob("*.py"):
        if any(part in {".devtools", "__pycache__", "artifacts"} for part in path.parts):
            continue
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path.relative_to(root)))
        checked += 1
start = time.perf_counter()
suite = unittest.TestLoader().discover(str(root / "tests"))
with (out / "unittest.log").open("w", encoding="utf-8") as stream:
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
report = {
    "syntax_files": checked,
    "tests": result.testsRun,
    "failures": len(result.failures),
    "errors": len(result.errors),
    "skipped": len(result.skipped),
    "duration_s": round(time.perf_counter() - start, 2),
    "success": result.wasSuccessful(),
}
(out / "checks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
raise SystemExit(0 if result.wasSuccessful() else 1)
