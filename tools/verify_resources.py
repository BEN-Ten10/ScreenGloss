"""Check optional local resources without downloading or uploading anything."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent


def load_manifest(path=ROOT / "resources/manifest.json"):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] != 1
    ):
        raise ValueError("Unsupported resource manifest version")
    return validate_entries(document.get("resources"))


def validate_entries(resources):
    if not isinstance(resources, list) or not resources:
        raise ValueError("Resource manifest must contain a non-empty resource list")
    seen = set()
    for entry in resources:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("Invalid resource entry")
        relative = PurePosixPath(entry["path"])
        parts = relative.parts
        if (
            relative.is_absolute()
            or ".." in parts
            or "\\" in entry["path"]
            or len(parts) < 3
            or parts[0] != "data"
            or parts[1] not in ("ocr", "dictionaries")
            or ":" in entry["path"]
            or entry["path"].casefold() in seen
        ):
            raise ValueError("Invalid or duplicate resource path")
        if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            raise ValueError("Invalid resource size")
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", entry["sha256"]
        ):
            raise ValueError("Invalid resource hash")
        seen.add(entry["path"].casefold())
    return resources


def resource_path(root, entry):
    root = Path(root).resolve()
    path = (root / entry["path"]).resolve()
    if not path.is_relative_to(root / "data"):
        raise ValueError("Resource resolves outside the data directory")
    return path


def check_file(path, entry):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != entry["bytes"]:
        raise ValueError(f"Missing resource or size mismatch: {entry['path']}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != entry["sha256"]:
        raise ValueError(f"Resource hash mismatch: {entry['path']}")


def main():
    resources = load_manifest()
    for entry in resources:
        check_file(resource_path(ROOT, entry), entry)
    print(f"All {len(resources)} resources match the manifest.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
