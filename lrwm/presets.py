"""Lightroom presets: carried between the PC and the SSD at Leave and Arrive.

The same preset often lives in different folders on the two computers, so
presets are matched three ways:
  1. Same path: replaced only by a newer file with different content.
  2. Same content in any folder (ignoring line endings): already there.
  3. Same preset ID in another folder, different content: replaced only if the
     source was edited since the last sync. Otherwise they are two versions
     from before syncing began, and both are left alone.

Replaced PC files are backed up first. Nothing is ever deleted. The PC
remembers the presets it has had (presets-seen.json in the backup folder), so
a preset deleted there isn't brought back by a copy still on the SSD.

On the Mac, 'Sync Presets.command' (placed on the SSD at Leave) follows the
same rules. Change the two together.
"""

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import fsops

SSD_DIR = "Presets"
FOLDERS = ("CameraRaw/Settings", "CameraRaw/ImportedSettings", "Lightroom/Local Adjustment Presets")
EXTENSIONS = (".xmp", ".lrtemplate")  # leaves out each machine's own Index.dat
SEEN_FILE = "presets-seen.json"
BACKUP_DIR = "PresetBackups"
MAC_SCRIPT = "Sync Presets.command"
_SCRIPT_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_presets.command")
_XMP_ID = re.compile(rb'crs:UUID="([^"]+)"')
_TEMPLATE_ID = re.compile(rb'^\s*id = "([^"]+)"', re.M)


def key(rel: str) -> str:
    """Identity of a file on both systems: ignores case, slashes and Unicode form (NFC/NFD)."""
    return unicodedata.normalize("NFC", rel.replace("\\", "/")).casefold()


@dataclass
class Item:
    src: str
    dst: str
    rel: str       # where it is at the destination
    size: int
    mtime: float
    replaces: bool = False


@dataclass
class Plan:
    dst_root: str = ""
    items: List[Item] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)   # same path: destination is newer or the same age
    deleted: List[str] = field(default_factory=list)     # deleted on the PC, so not brought back
    elsewhere: List[str] = field(default_factory=list)   # same content is there in another folder
    versions: List[str] = field(default_factory=list)    # another version, same ID, in another folder
    same: int = 0

    @property
    def bytes_to_copy(self) -> int:
        return sum(i.size for i in self.items)

    def describe(self) -> str:
        new = sum(not i.replaces for i in self.items)
        parts = [f"{new} new", f"{len(self.items) - new} updated", f"{self.same} unchanged"]
        if self.elsewhere:
            parts.append(f"{len(self.elsewhere)} already there in another folder")
        if self.versions:
            parts.append(f"{len(self.versions)} older variants left alone")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} left alone (destination is newer)")
        if self.deleted:
            parts.append(f"{len(self.deleted)} not restored (deleted on the PC)")
        return ", ".join(parts)


class Preset(NamedTuple):
    rel: str
    size: int
    mtime: float
    digest: str               # of the content, ignoring line endings
    preset_id: Optional[str]


def _read(root: str, rel: str, size: int, mtime: float) -> Preset:
    with open(os.path.join(root, rel), "rb") as f:
        body = f.read().replace(b"\r\n", b"\n").rstrip()
    found = (_XMP_ID if rel.lower().endswith(".xmp") else _TEMPLATE_ID).search(body)
    return Preset(rel, size, mtime, hashlib.sha256(body).hexdigest(),
                  found.group(1).decode("ascii", "replace").upper() if found else None)


def scan(root: str) -> Dict[str, Preset]:
    """{key: preset} for the preset files under an Adobe-style root."""
    found = {}
    for folder in FOLDERS:
        base = os.path.join(root, *folder.split("/"))
        if not os.path.isdir(base):
            continue
        for rel, size, mtime in fsops.walk_files(base):
            low = rel.lower()
            if not low.endswith(EXTENSIONS):
                continue
            if os.path.basename(low).startswith("~") and low.endswith(".lrtemplate"):
                continue  # what Lightroom 7.3 left behind after converting old templates
            full = os.path.join(*folder.split("/"), rel)
            found[key(full)] = _read(root, full, size, mtime)
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


def _remember(pc_root: str, backup_dir: str) -> None:
    """Record every preset on the PC by path, content ('#') and ID ('@')."""
    seen = read_seen(backup_dir)
    for k, p in scan(pc_root).items():
        seen[k] = p.mtime
        seen["#" + p.digest] = p.mtime
        if p.preset_id:
            seen["@" + p.preset_id] = max(seen.get("@" + p.preset_id, 0.0), p.mtime)
    write_seen(backup_dir, seen)


def _last_sync(backup_dir: str) -> float:
    """When presets were last synced. Never: nothing counts as edited since."""
    try:
        return os.path.getmtime(os.path.join(backup_dir, SEEN_FILE))
    except OSError:
        return float("inf")


def _was_deleted(k: str, p: Preset, seen: Dict[str, float]) -> bool:
    if "#" + p.digest in seen:
        return True
    names = [k] + (["@" + p.preset_id] if p.preset_id else [])
    known = [seen[n] for n in names if n in seen]
    return bool(known) and p.mtime <= max(known) + fsops.TIMESTAMP_TOLERANCE


def _plan(src_root: str, dst_root: str, seen: Dict[str, float], since: float) -> Plan:
    plan = Plan(dst_root)
    there = scan(dst_root)
    digests = {p.digest for p in there.values()}
    by_id: Dict[str, List[Preset]] = {}
    for p in there.values():
        if p.preset_id:
            by_id.setdefault(p.preset_id, []).append(p)

    copies: Dict[str, Item] = {}  # by destination; the newest source wins

    def copy(p: Preset, to: str, replaces: bool) -> None:
        k = key(to)
        if k not in copies or p.mtime > copies[k].mtime:
            copies[k] = Item(os.path.join(src_root, p.rel), os.path.join(dst_root, to),
                             to, p.size, p.mtime, replaces)

    tolerance = fsops.TIMESTAMP_TOLERANCE
    for k, p in sorted(scan(src_root).items()):
        if k in there:
            old = there[k]
            # Content first: installing the same presets on both machines gives different dates.
            if p.digest == old.digest:
                plan.same += 1
            elif p.mtime > old.mtime + tolerance:
                copy(p, old.rel, True)
            else:
                plan.conflicts.append(p.rel)
        elif p.digest in digests:
            plan.elsewhere.append(p.rel)
        elif p.preset_id and p.preset_id in by_id:
            others = by_id[p.preset_id]
            edited = p.mtime > since and all(p.mtime > o.mtime + tolerance for o in others)
            if edited and len({o.digest for o in others}) == 1:
                for o in others:
                    copy(p, o.rel, True)
            else:
                plan.versions.append(p.rel)
        elif _was_deleted(k, p, seen):
            plan.deleted.append(p.rel)
        else:
            copy(p, p.rel, False)
    plan.items = [copies[k] for k in sorted(copies)]
    return plan


def plan_out(pc_root: str, ssd_root: str, backup_dir: str) -> Plan:
    return _plan(pc_root, ssd_root, {}, _last_sync(backup_dir))


def plan_in(ssd_root: str, pc_root: str, backup_dir: str) -> Plan:
    # With no preset folders at all (e.g. reinstalled Windows), nothing counts as deleted.
    seen = read_seen(backup_dir) if scan(pc_root) else {}
    return _plan(ssd_root, pc_root, seen, _last_sync(backup_dir))


def copy_out(plan: Plan, pc_root: str, ssd_root: str, backup_dir: str) -> List[Tuple[str, str]]:
    """PC to SSD, plus the Mac script. Returns [(rel, error)]."""
    errors = []
    for item in plan.items:
        try:
            fsops.copy_file_atomic(item.src, item.dst)
        except OSError as e:
            errors.append((item.rel, str(e)))
    _remember(pc_root, backup_dir)
    install_mac_script(ssd_root)
    return errors


def copy_in(plan: Plan, backup_dir: str) -> List[Tuple[str, str]]:
    """SSD to PC, backing up whatever is replaced. Returns [(rel, error)]."""
    errors = []
    saved = os.path.join(backup_dir, BACKUP_DIR, datetime.now().strftime("%Y-%m-%d_%H%M%S_%f"))
    for item in plan.items:
        try:
            if item.replaces:
                fsops.copy_file_atomic(item.dst, os.path.join(saved, item.rel))
            fsops.copy_file_atomic(item.src, item.dst)
        except OSError as e:
            errors.append((item.rel, str(e)))
    _remember(plan.dst_root, backup_dir)
    return errors


def install_mac_script(ssd_root: str) -> None:
    with open(_SCRIPT_SOURCE, "rb") as f:
        body = f.read().replace(b"\r\n", b"\n")  # Git on Windows checks out CRLF; bash needs LF
    os.makedirs(ssd_root, exist_ok=True)
    path = os.path.join(ssd_root, MAC_SCRIPT)
    with open(path + fsops.TMP_SUFFIX, "wb") as f:
        f.write(body)
    os.replace(path + fsops.TMP_SUFFIX, path)
