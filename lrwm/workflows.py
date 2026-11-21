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
from typing import List, Optional

from . import drives, fsops, state, ui
from . import lightroom as lr
from .config import Config

log = logging.getLogger("lrwm")
log.addHandler(logging.NullHandler())


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


def tree_fingerprint(path: str) -> Optional[dict]:
    if not os.path.isdir(path):
        return None
    n = size = 0
    latest = 0.0
    for _, s, m in fsops._walk_all(path):
        n, size, latest = n + 1, size + s, max(latest, m)
    return {"files": n, "size": size, "mtime": round(latest, 3)}


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
        os.rename(p, os.path.join(dest, os.path.basename(p)))
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


def _normalize_rel(rel: str) -> str:
    return os.path.normpath(rel.strip().strip("\"'").strip("\\/")) if rel.strip() else ""


def ask_folders(cfg: Config) -> List[str]:
    """Interactive picker for archive folders to take along."""
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
        folders = [_normalize_rel(p) for p in answer.split(";") if p.strip()]
        missing = [f for f in folders if not os.path.isdir(os.path.join(cfg.desktop_photos, f))]
        if missing:
            ui.info("Not found in the archive: " + "; ".join(missing))
            continue
        return folders


def _plan_bring(cfg: Config, ssd: drives.SSD, folders: List[str]):
    plans = []
    for rel in folders:
        if rel in ("", "."):
            raise Abort("Taking the whole archive along is not supported; pick subfolders.")
        cmp = fsops.compare_trees(os.path.join(cfg.desktop_photos, rel),
                                  os.path.join(ssd.photos, rel), cfg.extra_skip_names)
        plans.append((rel, cmp))
    return plans


def _copy_bring(cfg: Config, ssd: drives.SSD, plans) -> None:
    errors = []
    for rel, cmp in plans:
        if cmp.to_copy:
            errors += fsops.copy_files(cmp.to_copy, os.path.join(cfg.desktop_photos, rel),
                                       os.path.join(ssd.photos, rel), f"{rel[:24]:<24}")
    if errors:
        for rel, err in errors[:10]:
            ui.info(f"    ERROR {rel}: {err}")
        raise Abort(f"{len(errors)} file(s) could not be copied to the SSD. Nothing was checked out.")


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
        folders = [] if dry_run else ask_folders(cfg)
    bring = _plan_bring(cfg, ssd, folders)

    # 4. Plan the catalog copy
    plan = _plan_readonly(cfg.desktop_lrcat, cfg.desktop_root_lr, cfg.mac_root_lr)
    data_dir = cfg.desktop_lrcat + "-data"
    catalog_bytes = os.path.getsize(cfg.desktop_lrcat) + (fsops.tree_size(data_dir) if os.path.isdir(data_dir) else 0)
    bring_bytes = sum(c.bytes_to_copy for _, c in bring)
    _check_space(ssd.root, catalog_bytes + bring_bytes, "SSD")
    _check_space(cfg.backup_dir if os.path.isdir(cfg.backup_dir) else cfg.desktop_catalog_dir,
                 catalog_bytes, "backup drive")

    ui.header("Plan", "-")
    ui.info(f"1. Back up the PC catalog to {cfg.backup_dir} (keeping the last {cfg.keep_backups})")
    ui.info(f"2. Copy the catalog to {ssd.lrcat}  ({ui.format_size(catalog_bytes)})")
    for line in lr.describe_plan(plan) or ["(no folder paths to change)"]:
        ui.info(f"     {line}")
    if bring:
        ui.info("3. Take along archive folders:")
        for rel, cmp in bring:
            ui.info(f"     {rel}: {len(cmp.to_copy)} file(s) to copy "
                    f"({ui.format_size(cmp.bytes_to_copy)}), {cmp.same} already on SSD")
            _show_conflicts(cmp.conflicts, "Archive", "SSD")
    ui.info(f"{'4' if bring else '3'}. Lock the PC catalog until it comes back "
            f"(renamed to {cfg.catalog_name}{state.CHECKED_OUT_SUFFIX})")

    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("\n  Check the catalog out to the SSD now?", default=True):
        raise Abort("Cancelled. Nothing was changed.")

    # ---- do it (state changes only at the very end)
    _copy_bring(cfg, ssd, bring)
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
        "ssd_data_fingerprint": tree_fingerprint(ssd.lrcat + "-data"),
        "db_version": lr.db_version(ssd.lrcat),
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
             if f.lower().endswith(".lrcat") and f != cfg.catalog_name + ".lrcat"
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
               or tree_fingerprint(ssd.lrcat + "-data") != pc_state.get("ssd_data_fingerprint"))
    photos = fsops.compare_trees(ssd.photos, cfg.desktop_photos, cfg.extra_skip_names)
    plan = _plan_readonly(ssd.lrcat, cfg.mac_root_lr, cfg.desktop_root_lr) if changed else None

    ui.info(f"Checked out:  {state.describe_since(pc_state)}")
    ui.info(f"SSD catalog:  saved {ui.format_time(os.path.getmtime(ssd.lrcat))}  "
            f"({'edited on the Mac' if changed else 'not edited since checkout'})")
    ui.header("Plan", "-")
    if photos.to_copy:
        ui.info("1. Copy photos from the SSD to the archive:")
        _show_files("   New", photos.new)
        _show_files("   Updated on the Mac", photos.updated)
    else:
        ui.info("1. Photos: archive already has everything on the SSD")
    _show_conflicts(photos.conflicts, "SSD", "archive")
    if changed:
        ui.info(f"2. Copy the catalog back to {cfg.desktop_lrcat}")
        for line in lr.describe_plan(plan) or ["(no folder paths to change)"]:
            ui.info(f"     {line}")
    else:
        ui.info("2. Unlock the PC catalog as it was (no edits to bring back)")
    ui.info(f"3. Mark the SSD copy as returned ({cfg.catalog_name}{state.RETURNED_SUFFIX})")

    if photos.to_copy:
        _check_space(cfg.desktop_photos, photos.bytes_to_copy, "archive drive")
    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("\n  Check the catalog back in now?", default=True):
        raise Abort("Cancelled. Nothing was changed.")

    # ---- photos first: if any fail, the catalog stays checked out
    if photos.to_copy:
        errors = fsops.copy_files(photos.to_copy, ssd.photos, cfg.desktop_photos, "Photos  ")
        if errors:
            for rel, err in errors[:10]:
                ui.info(f"    ERROR {rel}: {err}")
            raise Abort(f"{len(errors)} photo(s) could not be copied. The catalog is still checked "
                        f"out; fix the problem and run Arrive again (copied files are kept).")
        log.info("Copied %d photo file(s) to archive", len(photos.to_copy))

    if changed:
        progress = ui.Progress("Catalog ", os.path.getsize(ssd.lrcat))
        fsops.copy_file_atomic(
            ssd.lrcat, cfg.desktop_lrcat, verify=True, progress=progress,
            before_commit=lambda tmp: lr.retarget_catalog(tmp, cfg.mac_root_lr, cfg.desktop_root_lr))
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

    ui.header("Checked in. The PC catalog is current again.")
    ui.info(f"Open Lightroom Classic on the PC; the catalog is at {cfg.desktop_lrcat}")
    ui.info("The SSD copy is kept (as .returned) until the next checkout.")


# ================================================================ other procedures

def bring_folders(cfg: Config, folders: Optional[List[str]] = None, dry_run: bool = False) -> None:
    ui.header("TAKE ALONG: copy archive folders to the SSD" + ("  [DRY RUN]" if dry_run else ""))
    ssd = require_ssd(cfg)
    if folders is None:
        folders = ask_folders(cfg)
    if not folders:
        raise Abort("No folders chosen.")
    plans = _plan_bring(cfg, ssd, folders)
    for rel, cmp in plans:
        ui.info(f"{rel}: {len(cmp.to_copy)} file(s) to copy ({ui.format_size(cmp.bytes_to_copy)}), "
                f"{cmp.same} already on SSD")
        _show_conflicts(cmp.conflicts, "Archive", "SSD")
    _check_space(ssd.root, sum(c.bytes_to_copy for _, c in plans), "SSD")
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
    ui.info(f"Already in archive: {cmp.same}")
    _show_conflicts(cmp.conflicts, "SSD", "archive")
    if not cmp.to_copy:
        ui.info("Nothing to copy.")
        return
    _check_space(cfg.desktop_photos, cmp.bytes_to_copy, "archive drive")
    if dry_run:
        ui.info("\n  Dry run: nothing was changed.")
        return
    if not ui.confirm("Copy now?", default=True):
        raise Abort("Cancelled.")
    errors = fsops.copy_files(cmp.to_copy, ssd.photos, cfg.desktop_photos, "Photos  ")
    for rel, err in errors[:10]:
        ui.info(f"    ERROR {rel}: {err}")
    ui.info(f"Copied {len(cmp.to_copy) - len(errors)} file(s), {len(errors)} error(s).")
    log.info("Copy photos: %d copied, %d errors", len(cmp.to_copy) - len(errors), len(errors))


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
    if out:
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
