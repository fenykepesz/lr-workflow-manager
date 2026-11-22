"""Checkout record, kept as a small JSON file next to each catalog copy.

While the catalog is checked out, the PC's catalog file is renamed to
'<name>.lrcat.checked-out' so Lightroom on the PC cannot open it by accident.
After it comes back, the SSD's copy is renamed to '<name>.lrcat.returned'
so Lightroom on the Mac cannot open the stale copy by accident.
"""

import json
import os
from datetime import datetime
from typing import Optional

STATE_FILE = "lrwm-state.json"
CHECKED_OUT_SUFFIX = ".lrcat.checked-out"
RETURNED_SUFFIX = ".lrcat.returned"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read(catalog_dir: str) -> dict:
    path = os.path.join(catalog_dir, STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"status": "unreadable"}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):  # ValueError: bad JSON or bad encoding
        return {"status": "unreadable"}


def write(catalog_dir: str, data: dict) -> None:
    os.makedirs(catalog_dir, exist_ok=True)
    path = os.path.join(catalog_dir, STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def checked_out_path(catalog_dir: str, name: str) -> str:
    return os.path.join(catalog_dir, name + CHECKED_OUT_SUFFIX)


def returned_path(catalog_dir: str, name: str) -> str:
    return os.path.join(catalog_dir, name + RETURNED_SUFFIX)


def is_out(desktop_state: dict) -> bool:
    return desktop_state.get("status") == "out"


def describe_since(state: dict) -> Optional[str]:
    s = state.get("since")
    return s.replace("T", " ")[:16] if s else None
