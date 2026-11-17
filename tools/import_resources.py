"""Copy only manifest-listed resource files from an existing local installation."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.verify_resources import check_file, load_manifest, resource_path, validate_entries


def import_resources(source, destination=ROOT, entries=None):
    entries = load_manifest() if entries is None else validate_entries(entries)
    plan = []
    for entry in entries:
        origin = resource_path(source, entry)
        target = resource_path(destination, entry)
        check_file(origin, entry)
        if target.exists():
            check_file(target, entry)
        else:
            plan.append((origin, target, entry))
    # Nothing is copied until every source and existing destination is checked.
    for origin, target, entry in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".resource-", suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
        try:
            shutil.copyfile(origin, temporary)
            check_file(temporary, entry)
            if target.exists():
                check_file(target, entry)
            else:
                # Windows rename refuses to replace an existing destination.
                temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)
    return len(plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Root of an existing installation, containing data/",
    )
    args = parser.parse_args()
    try:
        print(f"Imported {import_resources(args.source)} resources. Personal data was not copied.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, str(error) + "\n")
