"""The user-facing procedures: leave, arrive, bring folders, copy photos,
status and emergency release.

Ordering principle: everything that can fail (checks, photo copies, catalog
copy) happens *before* the checkout state changes, so a failure at any point
leaves the catalog usable where it was.
"""

import logging
import os
import shutil
import uuid
from contextlib import closing
from datetime import datetime
from typing import List, Optional, Tuple

from . import drives, eject, fsops, presets, situation, state, ui
from . import lightroom as lr
from .config import Config

log = logging.getLogger("lrwm")
log.addHandler(logging.NullHandler())

REPLACED_DIR = "ReplacedFiles"  # in the backup folder: photo files as they were before being replaced
SEARCH_DEPTH = 3                # how deep to look for a folder typed by its name alone


class Abort(Exception):
    """Stop the current procedure with a message for the user. Nothing was changed
    unless the message says otherwise."""


# ================================================================ helpers

def setup_logging(cfg: Config) -> None:
    try:
        os.makedirs(cfg.backup_dir, exist_ok=True)
        handler = logging.FileHandler(os.path.join(cfg.backup_dir, "lrwm.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except OSError:
        pass


def require_ssd(cfg: Config) -> drives.SSD:
    ssd = drives.connect(cfg)
    if not ssd:
        where = f"label '{cfg.ssd_label}'" if cfg.ssd_label else cfg.ssd_root or cfg.ssd_fallback_drive
        raise Abort(f"SSD not found ({where}). Connect it and try again.")
    return ssd


def ensure_lightroom_closed(cfg: Config, ssd: Optional[drives.SSD] = None,
                            dry_run: bool = False) -> None:
    if dry_run:  # previews only read, so just warn
        try:
            _check_lightroom_closed(cfg, ssd)
        except Abort as e:
            ui.warn(f"{e}\n(Dry run continues; a real run would stop here.)")
        return
    _check_lightroom_closed(cfg, ssd)


def _check_lightroom_closed(cfg: Config, ssd: Optional[drives.SSD]) -> None:
    running = lr.lightroom_running()
    if running:
        raise Abort("Lightroom Classic is running on this PC. Quit it first.")
    if running is None:
        ui.warn("Could not check whether Lightroom is running. Make sure it is closed.")
    places = [(cfg.desktop_catalog_dir, "PC")]
    if ssd:
        places.append((ssd.catalog_dir, "SSD"))
    for folder, label in places:
        locks = lr.lock_files(folder, cfg.catalog_name)
        if locks:
            raise Abort(f"Lightroom lock files found on the {label}:\n  " + "\n  ".join(locks) +
                        "\nThe catalog seems to be open (or Lightroom crashed). "
                        "Open and properly quit Lightroom on that machine first.")


def _since_ts(record: dict) -> float:
    try:
        return datetime.fromisoformat(record["since"]).timestamp()
    except (KeyError, ValueError):
        return 0.0


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _new_dated_dir(parent: str, prefix: str) -> str:
    """Create and return '<parent>/<prefix>_<timestamp>', unique even within one second."""
    base = os.path.join(parent, f"{prefix}_{_stamp()}")
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}"
    os.makedirs(path)
    return path


def _rotate(folder: str, prefix: str, keep: int) -> None:
    if not os.path.isdir(folder) or keep < 1:
        return
    sets = sorted(d for d in os.listdir(folder)
                  if d.startswith(prefix + "_") and os.path.isdir(os.path.join(folder, d)))
    for old in sets[:-keep]:
        shutil.rmtree(os.path.join(folder, old), ignore_errors=True)
        log.info("Removed old backup %s", old)


def backup_pc_catalog(cfg: Config, lrcat: str) -> str:
    """Copy the PC catalog (+ .lrcat-data) into a dated backup folder; rotate old ones."""
    dest = _new_dated_dir(cfg.backup_dir, cfg.catalog_name)
    progress = ui.Progress("Backup  ", os.path.getsize(lrcat))
    fsops.copy_file_atomic(lrcat, os.path.join(dest, cfg.catalog_name + ".lrcat"),
                           verify=True, progress=progress)
    progress.finish()
    data = os.path.join(cfg.desktop_catalog_dir, cfg.catalog_name + ".lrcat-data")
    if os.path.isdir(data):
        shutil.copytree(data, os.path.join(dest, cfg.catalog_name + ".lrcat-data"),
                        copy_function=shutil.copy2)
    _rotate(cfg.backup_dir, cfg.catalog_name, cfg.keep_backups)
    log.info("Backed up PC catalog to %s", dest)
    return dest


def stash_ssd_catalog(cfg: Config, ssd: drives.SSD) -> Optional[str]:
    """Move any catalog already on the SSD into the SSD backup folder (never deleted outright)."""
    name = cfg.catalog_name
    candidates = [ssd.lrcat, ssd.lrcat + "-data",
                  state.returned_path(ssd.catalog_dir, name),
                  os.path.join(ssd.catalog_dir, name + ".lrcat.released")]
    present = [p for p in candidates if os.path.exists(p)]
    if not present:
        return None
    dest = _new_dated_dir(ssd.backup_dir, name)
    for p in present:
        target = os.path.basename(p)
        if p == ssd.lrcat:
            target += ".backup"  # so Lightroom on the Mac can't open a set-aside copy by mistake
        os.rename(p, os.path.join(dest, target))
    _rotate(ssd.backup_dir, name, cfg.keep_ssd_backups)
    log.info("Moved previous SSD catalog to %s", dest)
    return dest


def _plan_readonly(lrcat: str, old_prefix: str, new_prefix: str) -> lr.RetargetPlan:
    with closing(lr._ro(lrcat)) as conn:
        return lr.plan_retarget(conn, old_prefix, new_prefix)


def _show_files(title: str, items: List[fsops.FileItem], limit: int = 5) -> None:
    if not items:
        return
    ui.info(f"{title}: {len(items)}  ({ui.format_size(sum(f.size for f in items))})")
    for f in items[:limit]:
        ui.info(f"    {f.rel}")
    if len(items) > limit:
        ui.info(f"    ... and {len(items) - limit} more")


def _show_conflicts(items: List[fsops.FileItem], src_label: str, dst_label: str) -> None:
    if not items:
        return
    ui.warn(f"{len(items)} file(s) differ but the {dst_label} copy is the same age or newer.\n"
            f"These are left alone (nothing is overwritten):")
    for f in items[:10]:
        ui.info(f"    {f.rel}   {src_label}: {ui.format_size(f.size)} {ui.format_time(f.src_mtime)}"
                f"   {dst_label}: {ui.format_size(f.dst_size)} {ui.format_time(f.dst_mtime)}")
    if len(items) > 10:
        ui.info(f"    ... and {len(items) - 10} more")


def _check_space(path: str, needed: int, label: str) -> None:
    free = fsops.free_space(path)
    if needed * 1.05 > free:
        raise Abort(f"Not enough space on the {label}: need {ui.format_size(needed)}, "
                    f"{ui.format_size(free)} free.")


def _backup_drive(cfg: Config) -> str:
    return cfg.backup_dir if os.path.isdir(cfg.backup_dir) else cfg.desktop_catalog_dir


def _replaced_dir(cfg: Config) -> str:
    """Where this run puts files it replaces. Only created if it replaces any."""
    base = os.path.join(cfg.backup_dir, REPLACED_DIR, _stamp())
    path, n = base, 1
    while os.path.exists(path):
        n += 1
        path = f"{base}-{n}"
    return path


def _show_saving(cfg: Config, cmp: fsops.Comparison) -> None:
    if cmp.updated:
        ui.info(f"   The archive's {len(cmp.updated)} version(s) are saved first to "
                f"{os.path.join(cfg.backup_dir, REPLACED_DIR)}")


def _check_archive_space(cfg: Config, cmp: fsops.Comparison) -> None:
    drive = lambda p: os.path.splitdrive(os.path.abspath(p))[0].casefold()
    saving, copying = cmp.bytes_replaced, cmp.bytes_to_copy
    if saving:
        if drive(cfg.backup_dir) == drive(cfg.desktop_photos):
            copying += saving
        else:
            _check_space(_backup_drive(cfg), saving, "backup drive")
    _check_space(cfg.desktop_photos, copying, "archive drive")


def _note_replaced(folder: str) -> None:
    if os.path.isdir(folder):
        ui.info(f"The replaced versions are saved in {folder}")
        log.info("Saved replaced files to %s", folder)


def _normalize_rel(rel: str) -> str:
    return os.path.normpath(rel.strip().strip("\"'").strip("\\/")) if rel.strip() else ""


def folders_from_collection(cfg: Config, lrcat: str, root_lr: str) -> List[str]:
    """Whole folders of the photos in the 'Take along' collection, as archive-relative paths."""
    name = cfg.take_along_collection
    if not name or not os.path.exists(lrcat):
        return []
    try:
        res = lr.collection_folders(lrcat, name, root_lr)
    except Exception as e:
        ui.warn(f"Could not read the '{name}' collection: {e}")
        return []
    ui.info("")
    if res.smart_only:
        ui.info(f"'{name}' is a smart collection; please use a regular collection.")
        return []
    if not res.found:
        ui.info(f"Tip: add photos to a Lightroom collection named '{name}' and their whole "
                f"folders are offered here automatically.")
        return []
    if not (res.folders or res.outside):
        ui.info(f"The '{name}' collection is empty.")
        return []
    ui.info(f"Folders of the photos in the Lightroom collection '{name}':")
    chosen = []
    for rel, count in res.folders:
        rel_os = os.path.normpath(rel)
        path = os.path.join(cfg.desktop_photos, rel_os)
        if not os.path.isdir(path):
            ui.info(f"    {rel_os}  (not found on disk, skipped)")
            continue
        ui.info(f"    {rel_os:<45} {ui.format_size(fsops.tree_size(path)):>10}   "
                f"({count} photo(s) in collection)")
        chosen.append(rel_os)
    for full, count in res.outside:
        ui.info(f"    skipped, not inside {cfg.desktop_photos}: {full} ({count} photo(s))")
    return chosen


def ask_folders(cfg: Config, suggested: Optional[List[str]] = None) -> List[str]:
    """Pick archive folders to take along: from the collection, typed, or both."""
    if suggested:
        ui.info("")
        ui.info("[Enter] take these folders   [+] take these and add more   "
                "[M] choose manually instead   [N] take nothing")
        ui.info("Or type a folder to take as well as these.")
        while True:
            typed = ui.ask("Choice: ").strip()
            choice = typed.lower()
            if choice in ("", "y", "yes"):
                return suggested
            if choice in ("n", "none"):
                return []
            if choice == "+":
                return sorted(set(suggested) | set(_type_folders(cfg)), key=str.casefold)
            if choice == "m":
                return _type_folders(cfg)
            if choice.endswith("?"):
                ui.info("To browse, press + (add to these) or M (choose instead). ? works there.")
                continue
            extra, problems = _resolve_folders(cfg, typed)
            if extra and not problems:
                taking = list(suggested)
                for folder in extra:
                    if folder.casefold() not in [t.casefold() for t in taking]:
                        taking.append(folder)
                taking.sort(key=str.casefold)
                ui.info("Taking: " + "; ".join(taking))
                return taking
            for line in problems:
                ui.info(line)
            ui.info("Nothing was chosen. Press Enter, +, M or N, or type a folder to add.")
    return _type_folders(cfg)


def _type_folders(cfg: Config) -> List[str]:
    """Type folder paths, with ? to browse."""
    ui.info("")
    ui.info("Archive folders to take along (so you can edit their originals on the Mac).")
    ui.info(f"Paths are relative to {cfg.desktop_photos}, separated by ';'")
    ui.info(r"e.g.  Models\Ira; Abroad\25-10 Budapest")
    ui.info(r"Type ? to list top-level folders, or  Abroad\?  to list inside a folder.")
    while True:
        answer = ui.ask("Folders (empty = none): ")
        if answer.endswith("?"):
            base = os.path.join(cfg.desktop_photos, _normalize_rel(answer[:-1]))
            try:
                names = sorted(e.name for e in os.scandir(base)
                               if e.is_dir() and not fsops.skip_dir(e.name))
                for n in names:
                    ui.info(f"    {n}")
                if not names:
                    ui.info("    (no subfolders)")
            except OSError:
                ui.info(f"    Not found: {base}")
            continue
        folders, problems = _resolve_folders(cfg, answer)
        if problems:
            for line in problems:
                ui.info(line)
            continue
        return folders


def _outside_archive(rel: str) -> bool:
    return bool(os.path.splitdrive(rel)[0] or os.path.isabs(rel)
                or os.path.normpath(rel).split(os.sep)[0] == os.pardir)


def _resolve_folders(cfg: Config, answer: str) -> Tuple[List[str], List[str]]:
    """Typed folders as (archive-relative paths, what was wrong with the others).

    Takes a path in the archive, the same as a full path, or a folder's name alone.
    """
    root = cfg.desktop_photos
    found, problems = [], []
    for part in answer.split(";"):
        shown = part.strip().strip("\"'")
        if not shown:
            continue
        if os.path.splitdrive(shown)[0]:
            try:
                rel = os.path.relpath(shown, root)
            except ValueError:  # another drive
                rel = os.pardir
        else:
            rel = _normalize_rel(shown.replace("/", os.sep)) or "."
        if _outside_archive(rel):
            problems.append(f"'{shown}' isn't inside {root}")
        elif rel == ".":
            problems.append("The whole archive can't be taken along. Pick subfolders.")
        elif os.path.isdir(os.path.join(root, rel)):
            found.append(_as_on_disk(root, rel))
        else:
            matches = [] if os.sep in rel else _folders_named(root, rel)
            if len(matches) == 1:
                found.append(matches[0])
            elif matches:
                problems.append(f"'{shown}' is the name of several folders. Type one of these:")
                problems += [f"    {m}" for m in matches[:10]]
                if len(matches) > 10:
                    problems.append(f"    ... and {len(matches) - 10} more")
            else:
                problems.append(f"'{shown}' isn't a folder in {root}")
    return found, problems


def _as_on_disk(root: str, rel: str) -> str:
    """rel with the upper and lower case the folders really have."""
    parts = []
    for part in rel.split(os.sep):
        try:
            names = os.listdir(os.path.join(root, *parts))
        except OSError:
            names = []
        parts.append(next((n for n in names if n.casefold() == part.casefold()), part))
    return os.path.join(*parts)


def _folders_named(root: str, name: str) -> List[str]:
    found, level = [], [""]
    for _ in range(SEARCH_DEPTH):
        below = []
        for parent in level:
            try:
                with os.scandir(os.path.join(root, parent)) as entries:
                    subs = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for sub in subs:
                if fsops.skip_dir(sub):
                    continue
                rel = os.path.join(parent, sub)
                below.append(rel)
                if sub.casefold() == name.casefold():
                    found.append(rel)
        level = below
    return sorted(found, key=str.casefold)


def _ask_cleanup(cfg: Config, cleanup: List[fsops.CleanupGroup], dry_run: bool) -> bool:
    """Show SSD folders that aren't part of this trip; ask whether to remove the safe files."""
    if not cleanup:
        return False
    ui.info("")
    ui.info("On the SSD but not part of this trip:")
    for g in cleanup:
        label = f"{g.folder} (loose files only)" if g.files_only else g.folder
        note = f", {len(g.unsafe)} kept (not safely in the archive)" if g.unsafe else ""
        ui.info(f"    {label:<45} {len(g.safe):>5} file(s) {ui.format_size(g.safe_bytes):>10}"
                f" safe in archive{note}")
    unsafe = [f for g in cleanup for f in g.unsafe]
    if unsafe:
        ui.info("  Kept on the SSD because the archive doesn't have the same or a newer copy:")
        for f in unsafe[:5]:
            ui.info(f"    {f.rel}")
        if len(unsafe) > 5:
            ui.info(f"    ... and {len(unsafe) - 5} more")
    if not any(g.safe for g in cleanup):
        return False
    if dry_run:
        ui.info("  (A real run asks whether to remove the files that are safe in the archive.)")
        return False
    ui.info("  Removing them keeps old versions off the Mac. The archive copies are not touched.")
    return ui.confirm("Remove the files that are safe in the archive from the SSD?", default=False)


def _ask_refresh(refresh: fsops.Comparison, absent: List[str], dry_run: bool) -> bool:
    """Show what folders staying on the SSD lack; ask whether to bring them up to date."""
    if not (refresh.to_copy or absent or refresh.conflicts):
        return False
    ui.info("")
    if refresh.to_copy:
        ui.info("Folders staying on the SSD are out of date:")
        _show_files("  Older on the SSD", refresh.updated)
        _show_files("  Missing on the SSD", refresh.new)
    if absent:
        ui.info("Their subfolders that aren't on the SSD (not copied; take them along if you need them):")
        for rel in absent[:5]:
            ui.info(f"    {rel}")
        if len(absent) > 5:
            ui.info(f"    ... and {len(absent) - 5} more")
    _show_conflicts(refresh.conflicts, "Archive", "SSD")
    if not refresh.to_copy:
        return False
    if dry_run:
        ui.info("  (A real run asks whether to refresh them from the archive.)")
        return False
    if ui.confirm(f"Refresh them from the archive ({ui.format_size(refresh.bytes_to_copy)})?", default=True):
        return True
    if refresh.updated:
        ui.warn("The older versions stay on the SSD, so don't edit those on the laptop.\n"
                "An edited old version would replace the newer one at Arrive (which is saved first).")
    return False


def _plan_bring(cfg: Config, ssd: drives.SSD, folders: List[str]):
    plans = []
    for rel in folders:
        if rel in ("", "."):
            raise Abort("Taking the whole archive along is not supported; pick subfolders.")
        if _outside_archive(rel):
            raise Abort(f"Not a folder inside the archive: {rel}")
        cmp = fsops.compare_trees(os.path.join(cfg.desktop_photos, rel),
                                  os.path.join(ssd.photos, rel), cfg.extra_skip_names)
        plans.append((rel, cmp))
    return plans


def _copy_bring(cfg: Config, ssd: drives.SSD, plans) -> None:
    """Copy from the archive to the SSD. An empty folder in plans is the refresh."""
    errors = []
    saved = _replaced_dir(cfg)
    for rel, cmp in plans:
        if cmp.to_copy:
            label = rel[:24] if rel else "Refresh"
            errors += fsops.copy_files(cmp.to_copy, os.path.join(cfg.desktop_photos, rel),
                                       os.path.join(ssd.photos, rel), f"{label:<24}",
                                       os.path.join(saved, "SSD", rel))
    _note_replaced(saved)
    if errors:
        for rel, err in errors[:10]:
            ui.info(f"    ERROR {rel}: {err}")
        raise Abort(f"{len(errors)} file(s) could not be copied to the SSD. Nothing was checked out.")


def _plan_presets(cfg: Config, ssd: drives.SSD, arriving: bool) -> Optional[presets.Plan]:
    """Presets are a convenience: a problem with them is reported and never blocks the catalog."""
    if not cfg.sync_presets or not cfg.adobe_dir:
        return None
    try:
        if arriving:
            if not os.path.isdir(ssd.presets):
                return None
            return presets.plan_in(ssd.presets, cfg.adobe_dir, cfg.backup_dir)
        if not os.path.isdir(cfg.adobe_dir):
            return None
        return presets.plan_out(cfg.adobe_dir, ssd.presets, cfg.backup_dir)
    except Exception as e:
        log.exception("Could not read presets")
        ui.warn(f"Could not read the Lightroom presets, so they will be skipped: {e}")
        return None


def _copy_presets(cfg: Config, ssd: drives.SSD, plan: Optional[presets.Plan], arriving: bool) -> bool:
    """True if the presets were handled without errors."""
    if plan is None:
        return False
    try:
        if arriving:
            errors = presets.copy_in(plan, cfg.backup_dir)
        else:
            errors = presets.copy_out(plan, cfg.adobe_dir, ssd.presets, cfg.backup_dir)
    except Exception as e:
        log.exception("Could not copy presets")
        ui.warn(f"Could not copy the Lightroom presets: {e}\nThe catalog is not affected.")
        return False
    if errors:
        ui.warn(f"{len(errors)} preset(s) could not be copied. The catalog is not affected.")
        for rel, err in errors[:5]:
            ui.info(f"    ERROR {rel}: {err}")
    log.info("Presets %s: %d copied, %d errors", "in" if arriving else "out",
             len(plan.items) - len(errors), len(errors))
    return not errors


def _pending_baseline(cfg: Config, ssd: drives.SSD) -> List[str]:
    """Only used for the start screen's suggestion, so it never blocks a checkout."""
    try:
        return situation.pending_on_ssd(cfg, ssd)
    except Exception:
        log.exception("Could not record what was pending at Leave")
        return []


# ================================================================ LEAVE

def leave(cfg: Config, folders: Optional[List[str]] = None, dry_run: bool = False) -> None:
    ui.header("LEAVING: check the catalog out to the SSD" + ("  [DRY RUN]" if dry_run else ""))
    pc_state = state.read(cfg.desktop_catalog_dir)
    if state.is_out(pc_state):
        raise Abort(f"The catalog is already checked out (since {state.describe_since(pc_state)}).\n"
                    f"Use Arrive to bring it back first.")
    if os.path.exists(state.checked_out_path(cfg.desktop_catalog_dir, cfg.catalog_name)):
        raise Abort("Found a leftover '.lrcat.checked-out' file in the PC catalog folder, but the "
                    "checkout record says the catalog is at home. Please check that folder manually.")
    if not os.path.exists(cfg.desktop_lrcat):
        raise Abort(f"PC catalog not found: {cfg.desktop_lrcat}")
    ssd = require_ssd(cfg)
    ensure_lightroom_closed(cfg, ssd, dry_run)
    ui.info(f"SSD:          {ssd.root}")
    ui.info(f"PC catalog:   {cfg.desktop_lrcat}  (saved {ui.format_time(os.path.getmtime(cfg.desktop_lrcat))})")

    # 1. Photos on the SSD that never reached the archive (e.g. last trip)
    ui.info("\n  Checking the SSD for photos that are not in the archive yet...")
    pending = fsops.compare_trees(ssd.photos, cfg.desktop_photos, cfg.extra_skip_names)
    if pending.to_copy:
        ui.warn(f"{len(pending.to_copy)} file(s) on the SSD are not in the archive yet "
                f"({ui.format_size(pending.bytes_to_copy)}).")
        _show_files("Not in archive", pending.to_copy)
        ui.info("Use 'Copy photos to archive' first, so nothing exists only on the SSD.")
        if not dry_run and not ui.confirm("Continue anyway?"):
            raise Abort("Cancelled. Nothing was changed.")
    else:
        ui.info("All photos on the SSD are in the archive.")

    # 2. A catalog on the SSD from a trip that was never checked back in
    ssd_state = state.read(ssd.catalog_dir)
    if ssd_state.get("status") in ("out", "released") and os.path.exists(ssd.lrcat):
        ui.warn(f"The SSD holds a catalog from a checkout that was never brought back "
                f"(since {state.describe_since(ssd_state)}).\n"
                f"It may contain edits made on the Mac. It will be moved to\n{ssd.backup_dir}\n"
                f"rather than deleted.")
        if not dry_run and not ui.confirm("Continue?"):
            raise Abort("Cancelled. Nothing was changed.")

    # 3. Archive folders to take along
    if folders is None:
        suggested = folders_from_collection(cfg, cfg.desktop_lrcat, cfg.desktop_root_lr)
        folders = suggested if dry_run else ask_folders(cfg, suggested)
    bring = _plan_bring(cfg, ssd, folders)

    # 4. Folders on the SSD that are not part of this trip
    cleanup = fsops.plan_cleanup(ssd.photos, cfg.desktop_photos, folders, cfg.extra_skip_names)
    remove_old = _ask_cleanup(cfg, cleanup, dry_run)
    freed = sum(g.safe_bytes for g in cleanup) if remove_old else 0
    copies = list(bring)
    refreshed = None
    if not remove_old:  # what stays should match the archive
        refresh, absent = fsops.plan_refresh(ssd.photos, cfg.desktop_photos, folders, cfg.extra_skip_names)
        if _ask_refresh(refresh, absent, dry_run):
            refreshed = refresh
            copies.append(("", refresh))
    replaced = sum(len(c.updated) for _, c in copies)

    # 5. Plan the catalog copy
    plan = _plan_readonly(cfg.desktop_lrcat, cfg.desktop_root_lr, cfg.mac_root_lr)
    data_dir = cfg.desktop_lrcat + "-data"
    catalog_bytes = os.path.getsize(cfg.desktop_lrcat) + (fsops.tree_size(data_dir) if os.path.isdir(data_dir) else 0)
    bring_bytes = sum(c.bytes_to_copy for _, c in copies)
    preset_plan = _plan_presets(cfg, ssd, arriving=False)
    preset_bytes = preset_plan.bytes_to_copy if preset_plan else 0
    _check_space(ssd.root, max(catalog_bytes + bring_bytes + preset_bytes - freed, 0), "SSD")
    _check_space(_backup_drive(cfg), catalog_bytes + sum(c.bytes_replaced for _, c in copies),
                 "backup drive")

    ui.header("Plan", "-")
    steps = []
    if remove_old:
        n = sum(len(g.safe) for g in cleanup)
        steps.append([f"Remove from the SSD: {n} file(s) ({ui.format_size(freed)}) "
                      f"already safe in the archive"])
    steps.append([f"Back up the PC catalog to {cfg.backup_dir} (keeping the last {cfg.keep_backups})"])
    steps.append([f"Copy the catalog to {ssd.lrcat}  ({ui.format_size(catalog_bytes)})"] +
                 [f"   {line}" for line in lr.describe_plan(plan) or ["(no folder paths to change)"]])
    if bring:
        steps.append(["Take along archive folders (whole folders, all files):"] +
                     [f"   {rel}: {len(cmp.to_copy)} file(s) to copy "
                      f"({ui.format_size(cmp.bytes_to_copy)}), {cmp.same} already on SSD"
                      for rel, cmp in bring])
    if refreshed:
        steps.append([f"Refresh the folders staying on the SSD: {len(refreshed.to_copy)} file(s) "
                      f"({ui.format_size(refreshed.bytes_to_copy)})"])
    if replaced:
        steps.append([f"Save the {replaced} older SSD file(s) being replaced to "
                      f"{os.path.join(cfg.backup_dir, REPLACED_DIR)}"])
    if preset_plan:
        steps.append([f"Copy Lightroom presets to {ssd.presets}", f"   {preset_plan.describe()}"])
    steps.append([f"Lock the PC catalog until it comes back "
                  f"(renamed to {cfg.catalog_name}{state.CHECKED_OUT_SUFFIX})"])
    for i, lines in enumerate(steps, 1):
        ui.info(f"{i}. {lines[0]}")
        for line in lines[1:]:
            ui.info(f"   {line}")
    for rel, cmp in bring:
        _show_conflicts(cmp.conflicts, "Archive", "SSD")

    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("\n  Check the catalog out to the SSD now?", default=False):
        raise Abort("Cancelled. Nothing was changed.")

    # ---- do it (state changes only at the very end)
    if remove_old:
        errors = fsops.remove_cleanup(ssd.photos, cleanup)
        for rel, err in errors[:10]:
            ui.info(f"    Could not remove {rel}: {err}")
        log.info("Removed %d file(s) not part of this trip from the SSD (%d errors)",
                 sum(len(g.safe) for g in cleanup) - len(errors), len(errors))
    _copy_bring(cfg, ssd, copies)
    presets_ok = _copy_presets(cfg, ssd, preset_plan, arriving=False)
    backup_pc_catalog(cfg, cfg.desktop_lrcat)
    stashed = stash_ssd_catalog(cfg, ssd)
    if stashed:
        ui.info(f"Previous SSD catalog moved to {stashed}")

    progress = ui.Progress("Catalog ", os.path.getsize(cfg.desktop_lrcat))
    fsops.copy_file_atomic(
        cfg.desktop_lrcat, ssd.lrcat, verify=True, progress=progress,
        before_commit=lambda tmp: lr.retarget_catalog(tmp, cfg.desktop_root_lr, cfg.mac_root_lr))
    progress.finish()
    if os.path.isdir(data_dir):
        fsops.copy_dir_atomic(data_dir, ssd.lrcat + "-data")

    record = {
        "status": "out",
        "checkout_id": uuid.uuid4().hex,
        "since": state.now_iso(),
        "ssd_fingerprint": lr.fingerprint(ssd.lrcat),
        "ssd_data_fingerprint": fsops.tree_fingerprint(ssd.lrcat + "-data"),
        "db_version": lr.db_version(ssd.lrcat),
        "pending_at_leave": _pending_baseline(cfg, ssd),
        "brought": sorted({f for f in ssd_state.get("brought", [])
                           if os.path.isdir(os.path.join(ssd.photos, f))} | set(folders)),
    }
    state.write(ssd.catalog_dir, record)
    os.rename(cfg.desktop_lrcat, state.checked_out_path(cfg.desktop_catalog_dir, cfg.catalog_name))
    state.write(cfg.desktop_catalog_dir, record)
    log.info("Checked out catalog %s to %s (id %s)", cfg.catalog_name, ssd.root, record["checkout_id"])

    ui.header("Checked out. The catalog now lives on the SSD.")
    ui.info("On the Mac:")
    ui.info("  1. Eject the SSD safely, connect it to the Mac")
    ui.info(f"  2. Lightroom Classic > File > Open Catalog > "
            f"{cfg.mac_root_lr.rsplit('/', 2)[0]}/{cfg.ssd_catalog_subdir}/{cfg.catalog_name}.lrcat")
    ui.info(f"  3. Import new photos into {cfg.mac_root_lr}<trip folder>")
    ui.info("  4. Quit Lightroom before ejecting the SSD")
    if presets_ok:
        script = f"{cfg.mac_root_lr.rsplit('/', 2)[0]}/{presets.SSD_DIR}/{presets.MAC_SCRIPT}"
        ui.info("Lightroom presets (Nik presets are not included):")
        ui.info("  With Lightroom closed, run this in Terminal on the Mac, before step 2,")
        ui.info("  and again before ejecting if you made presets there:")
        ui.info(f'  bash "{script}"')
    ui.info("The PC catalog is locked until you run Arrive.")


# ================================================================ ARRIVE

def arrive(cfg: Config, dry_run: bool = False) -> None:
    ui.header("ARRIVING: bring the catalog and new photos back" + ("  [DRY RUN]" if dry_run else ""))
    pc_state = state.read(cfg.desktop_catalog_dir)
    if not state.is_out(pc_state):
        raise Abort("The catalog is not checked out: the PC copy is the current one.\n"
                    "To copy photos from the SSD to the archive, use 'Copy photos to archive'.")
    locked = state.checked_out_path(cfg.desktop_catalog_dir, cfg.catalog_name)
    if not os.path.exists(locked):
        raise Abort(f"Expected the locked PC catalog at {locked} but it is missing.")
    ssd = require_ssd(cfg)
    ssd_state = state.read(ssd.catalog_dir)
    if ssd_state.get("checkout_id") != pc_state.get("checkout_id"):
        raise Abort("This SSD does not hold the checked-out catalog (checkout records differ).\n"
                    "Is it the right SSD? If the SSD is lost, use Emergency release.")
    if not os.path.exists(ssd.lrcat):
        raise Abort(f"The catalog is missing on the SSD: {ssd.lrcat}")
    ensure_lightroom_closed(cfg, ssd, dry_run)

    # Lightroom on the Mac upgraded the catalog into a new file?
    since = _since_ts(pc_state)
    newer = [f for f in os.listdir(ssd.catalog_dir)
             if f.lower().endswith(".lrcat") and not f.startswith("._")  # macOS metadata, not a catalog
             and f != cfg.catalog_name + ".lrcat"
             and os.path.getmtime(os.path.join(ssd.catalog_dir, f)) > since]
    if newer:
        raise Abort("Lightroom on the Mac created a new catalog file: " + ", ".join(newer) + "\n"
                    "This usually means the Mac's Lightroom is newer and upgraded the catalog.\n"
                    "Update Lightroom on the PC, then handle the upgraded catalog manually.")
    ver = lr.db_version(ssd.lrcat)
    if pc_state.get("db_version") and ver != pc_state["db_version"]:
        raise Abort(f"The SSD catalog's database version changed ({pc_state['db_version']} -> {ver}).\n"
                    f"Make sure the PC runs the same Lightroom version as the Mac.")

    changed = (not lr.same_fingerprint(lr.fingerprint(ssd.lrcat), pc_state.get("ssd_fingerprint"))
               or fsops.tree_fingerprint(ssd.lrcat + "-data") != pc_state.get("ssd_data_fingerprint"))
    photos = fsops.compare_trees(ssd.photos, cfg.desktop_photos, cfg.extra_skip_names)
    plan = _plan_readonly(ssd.lrcat, cfg.mac_root_lr, cfg.desktop_root_lr) if changed else None
    smart_previews = os.path.join(cfg.desktop_catalog_dir, cfg.catalog_name + " Smart Previews.lrdata")
    pc_only = lr.preview_pc_only(ssd.lrcat, locked, smart_previews) if changed else None
    preset_plan = _plan_presets(cfg, ssd, arriving=True)

    ui.info(f"Checked out:  {state.describe_since(pc_state)}")
    ui.info(f"SSD catalog:  saved {ui.format_time(os.path.getmtime(ssd.lrcat))}  "
            f"({'edited on the Mac' if changed else 'not edited since checkout'})")
    ui.header("Plan", "-")
    if photos.to_copy:
        ui.info("1. Copy photos from the SSD to the archive:")
        _show_files("   New", photos.new)
        _show_files("   Updated on the Mac", photos.updated)
        _show_saving(cfg, photos)
    else:
        ui.info("1. Photos: archive already has everything on the SSD")
    _show_conflicts(photos.conflicts, "SSD", "archive")
    if changed:
        ui.info(f"2. Copy the catalog back to {cfg.desktop_lrcat}")
        for line in lr.describe_plan(plan) or ["(no folder paths to change)"]:
            ui.info(f"     {line}")
        for line in pc_only.describe():
            ui.info(f"     {line}")
    else:
        ui.info("2. Unlock the PC catalog as it was (no edits to bring back)")
    ui.info(f"3. Mark the SSD copy as returned ({cfg.catalog_name}{state.RETURNED_SUFFIX})")
    if preset_plan:
        ui.info(f"4. Lightroom presets from the SSD: {preset_plan.describe()}")
        for item in preset_plan.items[:10]:
            ui.info(f"     {item.rel}")
        if len(preset_plan.items) > 10:
            ui.info(f"     ... and {len(preset_plan.items) - 10} more")

    if photos.to_copy:
        _check_archive_space(cfg, photos)
    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("\n  Check the catalog back in now?", default=False):
        raise Abort("Cancelled. Nothing was changed.")

    # ---- photos first: if any fail, the catalog stays checked out
    if photos.to_copy:
        saved = _replaced_dir(cfg)
        errors = fsops.copy_files(photos.to_copy, ssd.photos, cfg.desktop_photos, "Photos  ",
                                  os.path.join(saved, "archive"))
        _note_replaced(saved)
        if errors:
            for rel, err in errors[:10]:
                ui.info(f"    ERROR {rel}: {err}")
            raise Abort(f"{len(errors)} photo(s) could not be copied. The catalog is still checked "
                        f"out; fix the problem and run Arrive again (copied files are kept).")
        log.info("Copied %d photo file(s) to archive", len(photos.to_copy))

    if changed:
        def prepare(tmp: str) -> None:
            lr.retarget_catalog(tmp, cfg.mac_root_lr, cfg.desktop_root_lr)
            restored = lr.restore_pc_only(tmp, locked, smart_previews)
            log.info("Kept from the PC catalog: %d Smart Preview record(s), %d setting(s)",
                     len(restored.previews), len(restored.variables))

        progress = ui.Progress("Catalog ", os.path.getsize(ssd.lrcat))
        fsops.copy_file_atomic(ssd.lrcat, cfg.desktop_lrcat, verify=True, progress=progress,
                               before_commit=prepare)
        progress.finish()
        if os.path.isdir(ssd.lrcat + "-data"):
            fsops.copy_dir_atomic(ssd.lrcat + "-data", cfg.desktop_lrcat + "-data")
        os.remove(locked)  # identical to the backup taken when leaving
    else:
        os.rename(locked, cfg.desktop_lrcat)

    returned = state.returned_path(ssd.catalog_dir, cfg.catalog_name)
    if os.path.exists(returned):
        os.remove(returned)
    os.rename(ssd.lrcat, returned)
    record = {"status": "home", "checkout_id": pc_state["checkout_id"],
              "since": pc_state.get("since"), "returned_at": state.now_iso(),
              "brought": ssd_state.get("brought", [])}
    state.write(ssd.catalog_dir, record)
    state.write(cfg.desktop_catalog_dir, record)
    log.info("Checked in catalog %s (changed=%s)", cfg.catalog_name, changed)

    _copy_presets(cfg, ssd, preset_plan, arriving=True)

    ui.header("Checked in. The PC catalog is current again.")
    ui.info(f"Open Lightroom Classic on the PC; the catalog is at {cfg.desktop_lrcat}")
    ui.info("The SSD copy is kept (as .returned) until the next checkout.")


# ================================================================ other procedures

def bring_folders(cfg: Config, folders: Optional[List[str]] = None, dry_run: bool = False) -> None:
    ui.header("TAKE ALONG: copy archive folders to the SSD" + ("  [DRY RUN]" if dry_run else ""))
    ssd = require_ssd(cfg)
    if folders is None:
        # Read the collection from wherever the catalog currently lives.
        if state.is_out(state.read(cfg.desktop_catalog_dir)):
            lrcat, root, cat_dir = ssd.lrcat, cfg.mac_root_lr, ssd.catalog_dir
        else:
            lrcat, root, cat_dir = cfg.desktop_lrcat, cfg.desktop_root_lr, cfg.desktop_catalog_dir
        if lr.lock_files(cat_dir, cfg.catalog_name):
            ui.warn("Lightroom has the catalog open, so its latest collection changes may not be "
                    "visible yet.\nQuit Lightroom for an up-to-date list.")
        suggested = folders_from_collection(cfg, lrcat, root)
        folders = suggested if dry_run else ask_folders(cfg, suggested)
    if not folders:
        raise Abort("No folders chosen.")
    plans = _plan_bring(cfg, ssd, folders)
    for rel, cmp in plans:
        ui.info(f"{rel}: {len(cmp.to_copy)} file(s) to copy ({ui.format_size(cmp.bytes_to_copy)}), "
                f"{cmp.same} already on SSD")
        _show_conflicts(cmp.conflicts, "Archive", "SSD")
    _check_space(ssd.root, sum(c.bytes_to_copy for _, c in plans), "SSD")
    replaced = sum(len(c.updated) for _, c in plans)
    if replaced:
        _check_space(_backup_drive(cfg), sum(c.bytes_replaced for _, c in plans), "backup drive")
        ui.info(f"{replaced} older SSD file(s) will be replaced, and saved first to "
                f"{os.path.join(cfg.backup_dir, REPLACED_DIR)}")
    pc_state = state.read(cfg.desktop_catalog_dir)
    if not state.is_out(pc_state):
        ui.info("Note: the catalog is at home. These photos will be online on the Mac after you Leave.")
    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("Copy now?", default=True):
        raise Abort("Cancelled.")
    _copy_bring(cfg, ssd, plans)
    ssd_state = state.read(ssd.catalog_dir)
    if ssd_state:
        ssd_state["brought"] = sorted(set(ssd_state.get("brought", [])) | set(folders))
        state.write(ssd.catalog_dir, ssd_state)
    ui.info("Done.")


def copy_photos(cfg: Config, dry_run: bool = False) -> None:
    ui.header("COPY PHOTOS: SSD to archive (catalog untouched)" + ("  [DRY RUN]" if dry_run else ""))
    ssd = require_ssd(cfg)
    ui.info(f"From {ssd.photos}  to  {cfg.desktop_photos}")
    cmp = fsops.compare_trees(ssd.photos, cfg.desktop_photos, cfg.extra_skip_names)
    _show_files("New", cmp.new)
    _show_files("Updated", cmp.updated)
    _show_saving(cfg, cmp)
    ui.info(f"Already in archive: {cmp.same}")
    _show_conflicts(cmp.conflicts, "SSD", "archive")
    if not cmp.to_copy:
        ui.info("Nothing to copy.")
        return
    _check_archive_space(cfg, cmp)
    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("Copy now?", default=True):
        raise Abort("Cancelled.")
    saved = _replaced_dir(cfg)
    errors = fsops.copy_files(cmp.to_copy, ssd.photos, cfg.desktop_photos, "Photos  ",
                              os.path.join(saved, "archive"))
    _note_replaced(saved)
    for rel, err in errors[:10]:
        ui.info(f"    ERROR {rel}: {err}")
    ui.info(f"Copied {len(cmp.to_copy) - len(errors)} file(s), {len(errors)} error(s).")
    log.info("Copy photos: %d copied, %d errors", len(cmp.to_copy) - len(errors), len(errors))


def eject_ssd(cfg: Config) -> None:
    ssd = require_ssd(cfg)
    ui.info(f"Ejecting the SSD ({ssd.root})...")
    try:
        eject.eject(ssd.root, [cfg.desktop_catalog_dir, cfg.desktop_photos, cfg.backup_dir])
    except eject.EjectError as e:
        raise Abort(str(e))
    ui.info("Ejected. You can unplug the SSD now.")


def release(cfg: Config) -> None:
    """Emergency: unlock the PC catalog without the SSD (e.g. SSD lost)."""
    ui.header("EMERGENCY RELEASE")
    pc_state = state.read(cfg.desktop_catalog_dir)
    if not state.is_out(pc_state):
        raise Abort("The catalog is not checked out.")
    locked = state.checked_out_path(cfg.desktop_catalog_dir, cfg.catalog_name)
    if not os.path.exists(locked):
        raise Abort(f"Locked catalog not found: {locked}")
    ensure_lightroom_closed(cfg)
    ui.warn(f"This unlocks the PC catalog as it was at checkout ({state.describe_since(pc_state)}).\n"
            f"Edits made on the Mac since then will NOT be brought back.\n"
            f"Use this only if the SSD is lost or its catalog is unusable.")
    if ui.ask("Type RELEASE to confirm: ") != "RELEASE":
        raise Abort("Cancelled.")
    os.rename(locked, cfg.desktop_lrcat)
    record = dict(pc_state, status="home", released_at=state.now_iso())
    state.write(cfg.desktop_catalog_dir, record)
    ssd = drives.connect(cfg)
    if ssd and state.read(ssd.catalog_dir).get("checkout_id") == pc_state.get("checkout_id") \
            and os.path.exists(ssd.lrcat) and not lr.lock_files(ssd.catalog_dir, cfg.catalog_name):
        os.rename(ssd.lrcat, os.path.join(ssd.catalog_dir, cfg.catalog_name + ".lrcat.released"))
        state.write(ssd.catalog_dir, dict(record, status="released"))
        ui.info("The SSD copy was renamed to .lrcat.released so the Mac can't keep editing it.")
    log.warning("Emergency release of checkout %s", pc_state.get("checkout_id"))
    ui.info("PC catalog unlocked.")


def status(cfg: Config) -> None:
    ui.header("STATUS")
    pc_state = state.read(cfg.desktop_catalog_dir)
    out = state.is_out(pc_state)
    problem = situation.record_problem(cfg)
    if problem:
        ui.warn(problem)
        ui.info("Catalog:      NEEDS ATTENTION (see above)")
        lrcat = cfg.desktop_lrcat
    elif out:
        ui.info(f"Catalog:      CHECKED OUT to the laptop since {state.describe_since(pc_state)}")
        lrcat = state.checked_out_path(cfg.desktop_catalog_dir, cfg.catalog_name)
    else:
        ui.info("Catalog:      AT HOME (PC is current)")
        lrcat = cfg.desktop_lrcat
    if os.path.exists(lrcat):
        ui.info(f"PC catalog:   saved {ui.format_time(os.path.getmtime(lrcat))}, "
                f"{ui.format_size(os.path.getsize(lrcat))}{'  (locked)' if out else ''}")
    else:
        ui.info(f"PC catalog:   NOT FOUND ({lrcat})")
    running = lr.lightroom_running()
    if running:
        ui.info("Lightroom:    running on this PC")
    if lr.lock_files(cfg.desktop_catalog_dir, cfg.catalog_name):
        ui.info("              lock files present in the PC catalog folder")

    ssd = drives.connect(cfg)
    if not ssd:
        ui.info("SSD:          not connected")
    else:
        ui.info(f"SSD:          connected at {ssd.root}")
        ssd_state = state.read(ssd.catalog_dir)
        if os.path.exists(ssd.lrcat):
            note = ""
            if out and ssd_state.get("checkout_id") == pc_state.get("checkout_id"):
                edited = not lr.same_fingerprint(lr.fingerprint(ssd.lrcat), pc_state.get("ssd_fingerprint"))
                note = "  (edited on the Mac)" if edited else "  (not edited yet)"
            elif not out:
                note = "  (stale copy from before; will be moved aside on next Leave)"
            ui.info(f"SSD catalog:  saved {ui.format_time(os.path.getmtime(ssd.lrcat))}{note}")
        elif os.path.exists(state.returned_path(ssd.catalog_dir, cfg.catalog_name)):
            ui.info("SSD catalog:  returned (not usable on the Mac until next Leave)")
        else:
            ui.info("SSD catalog:  none")
        if lr.lock_files(ssd.catalog_dir, cfg.catalog_name):
            ui.info("              lock files present: Lightroom wasn't closed properly on the Mac?")
        if os.path.isdir(ssd.photos):
            tops = sorted(e.name for e in os.scandir(ssd.photos) if e.is_dir() and not fsops.skip_dir(e.name))
            ui.info(f"SSD photos:   {', '.join(tops) if tops else '(empty)'}")
        if ssd_state.get("brought"):
            ui.info(f"Taken along:  {'; '.join(ssd_state['brought'])}")

    if os.path.isdir(cfg.backup_dir):
        sets = sorted(d for d in os.listdir(cfg.backup_dir) if d.startswith(cfg.catalog_name + "_"))
        if sets:
            size = sum(fsops.tree_size(os.path.join(cfg.backup_dir, d)) for d in sets)
            ui.info(f"Backups:      {len(sets)} in {cfg.backup_dir} ({ui.format_size(size)}), "
                    f"latest {sets[-1][len(cfg.catalog_name) + 1:]}")
