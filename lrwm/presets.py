"""Lightroom presets: carried between the PC and the SSD at Leave and Arrive.

Only preset files are handled, in the three folders below. A file is replaced
only by a newer one with different content, and the PC's version is backed up
first. Nothing is ever deleted.

The PC remembers the presets it has had (presets-seen.json in the backup
folder), so a preset deleted on the PC isn't brought back by the copy still on
the SSD, unless that copy was edited afterwards.

On the Mac, 'Sync Presets.command' (placed on the SSD at Leave) does the same.
"""

import json
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Tuple

from . import fsops

SSD_DIR = "Presets"
FOLDERS = ("CameraRaw/Settings", "CameraRaw/ImportedSettings", "Lightroom/Local Adjustment Presets")
EXTENSIONS = (".xmp", ".lrtemplate")  # leaves out each machine's own Index.dat
SEEN_FILE = "presets-seen.json"
BACKUP_DIR = "PresetBackups"
MAC_SCRIPT = "Sync Presets.command"
_SCRIPT_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_presets.command")


def key(rel: str) -> str:
    """Identity of a file on both systems: ignores case, slashes and Unicode form (NFC/NFD)."""
    return unicodedata.normalize("NFC", rel.replace("\\", "/")).casefold()


@dataclass
class Item:
    src: str
    dst: str
    rel: str
    size: int
    mtime: float
    replaces: bool = False


@dataclass
class Plan:
    items: List[Item] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)  # destination is newer or the same age
    deleted: List[str] = field(default_factory=list)    # deleted on the PC, so not brought back
    same: int = 0

    @property
    def bytes_to_copy(self) -> int:
        return sum(i.size for i in self.items)

    def describe(self) -> str:
        new = sum(not i.replaces for i in self.items)
        parts = [f"{new} new", f"{len(self.items) - new} updated", f"{self.same} unchanged"]
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} left alone (destination is newer)")
        if self.deleted:
            parts.append(f"{len(self.deleted)} not restored (deleted on the PC)")
        return ", ".join(parts)


def scan(root: str) -> Dict[str, Tuple[str, int, float]]:
    """{key: (relative path, size, mtime)} for the preset files under an Adobe-style root."""
    found = {}
    for folder in FOLDERS:
        base = os.path.join(root, *folder.split("/"))
        if not os.path.isdir(base):
            continue
        for rel, size, mtime in fsops.walk_files(base):
            if rel.lower().endswith(EXTENSIONS):
                full = os.path.join(*folder.split("/"), rel)
                found[key(full)] = (full, size, mtime)
    return found


def read_seen(backup_dir: str) -> Dict[str, float]:
    try:
        with open(os.path.join(backup_dir, SEEN_FILE), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_seen(backup_dir: str, seen: Dict[str, float]) -> None:
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, SEEN_FILE)
    with open(path + fsops.TMP_SUFFIX, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(path + fsops.TMP_SUFFIX, path)


def _same_content(a: str, b: str) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def _plan(src_root: str, dst_root: str, seen: Dict[str, float]) -> Plan:
    plan = Plan()
    dst_files = scan(dst_root)
    for k, (rel, size, mtime) in sorted(scan(src_root).items()):
        src = os.path.join(src_root, rel)
        if k not in dst_files:
            if k in seen and mtime <= seen[k] + fsops.TIMESTAMP_TOLERANCE:
                plan.deleted.append(rel)
            else:
                plan.items.append(Item(src, os.path.join(dst_root, rel), rel, size, mtime))
            continue
        dst_rel, dst_size, dst_mtime = dst_files[k]
        dst = os.path.join(dst_root, dst_rel)
        # Content first: installing the same presets on both machines gives different dates.
        if size == dst_size and _same_content(src, dst):
            plan.same += 1
        elif mtime > dst_mtime + fsops.TIMESTAMP_TOLERANCE:
            plan.items.append(Item(src, dst, rel, size, mtime, replaces=True))
        else:
            plan.conflicts.append(rel)
    return plan


def plan_out(pc_root: str, ssd_root: str) -> Plan:
    return _plan(pc_root, ssd_root, {})


def plan_in(ssd_root: str, pc_root: str, backup_dir: str) -> Plan:
    # With no preset folders at all (e.g. reinstalled Windows), nothing counts as deleted.
    seen = read_seen(backup_dir) if scan(pc_root) else {}
    return _plan(ssd_root, pc_root, seen)


def copy_out(plan: Plan, pc_root: str, ssd_root: str, backup_dir: str) -> List[Tuple[str, str]]:
    """PC to SSD, plus the Mac script. Returns [(rel, error)]."""
    errors = []
    for item in plan.items:
        try:
            fsops.copy_file_atomic(item.src, item.dst)
        except OSError as e:
            errors.append((item.rel, str(e)))
    seen = read_seen(backup_dir)
    seen.update({k: mtime for k, (_, _, mtime) in scan(pc_root).items()})
    write_seen(backup_dir, seen)
    install_mac_script(ssd_root)
    return errors


def copy_in(plan: Plan, backup_dir: str) -> List[Tuple[str, str]]:
    """SSD to PC, backing up whatever is replaced. Returns [(rel, error)]."""
    if not plan.items:
        return []
    errors = []
    seen = read_seen(backup_dir)
    saved = os.path.join(backup_dir, BACKUP_DIR, datetime.now().strftime("%Y-%m-%d_%H%M%S_%f"))
    for item in plan.items:
        try:
            if item.replaces:
                fsops.copy_file_atomic(item.dst, os.path.join(saved, item.rel))
            fsops.copy_file_atomic(item.src, item.dst)
            seen[key(item.rel)] = item.mtime
        except OSError as e:
            errors.append((item.rel, str(e)))
    write_seen(backup_dir, seen)
    return errors


def install_mac_script(ssd_root: str) -> None:
    with open(_SCRIPT_SOURCE, "rb") as f:
        body = f.read().replace(b"\r\n", b"\n")  # Git on Windows checks out CRLF; bash needs LF
    os.makedirs(ssd_root, exist_ok=True)
    path = os.path.join(ssd_root, MAC_SCRIPT)
    with open(path + fsops.TMP_SUFFIX, "wb") as f:
        f.write(body)
    os.replace(path + fsops.TMP_SUFFIX, path)
