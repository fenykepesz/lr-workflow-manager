"""Work out where the user is in the trip, to suggest the next step.

Read-only, and only ever a suggestion: every procedure still makes its own
checks and asks before changing anything.
"""

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from . import drives, fsops, presets, state
from . import lightroom as lr
from .config import Config

log = logging.getLogger("lrwm")

PROBLEM = "problem"
NO_SSD = "no_ssd"
WRONG_SSD = "wrong_ssd"
READY = "ready"          # checked out and nothing changed since
ARRIVE = "arrive"
COPY_PHOTOS = "copy_photos"
LEAVE = "leave"


@dataclass
class Situation:
    kind: str
    where: str
    detail: str = ""


def record_problem(cfg: Config) -> Optional[str]:
    """Why the PC's checkout record can't be trusted, or None if it matches the files."""
    record = state.read(cfg.desktop_catalog_dir)
    record_path = os.path.join(cfg.desktop_catalog_dir, state.STATE_FILE)
    if record.get("status") == "unreadable":
        return (f"The checkout record is damaged: {record_path}\n"
                "Don't Leave or create a new catalog until it's fixed.")
    has_catalog = os.path.exists(cfg.desktop_lrcat)
    has_locked = os.path.exists(state.checked_out_path(cfg.desktop_catalog_dir, cfg.catalog_name))
    if state.is_out(record):
        if not has_locked:
            return ("The record says the catalog is checked out, but the locked PC catalog is "
                    f"missing.\nCheck {cfg.desktop_catalog_dir}")
        if has_catalog:
            return ("The record says the catalog is checked out, but the PC also has an unlocked one.\n"
                    "An Arrive may have been interrupted, or a new catalog was created here.\n"
                    f"Check {cfg.desktop_catalog_dir} before running Arrive, which replaces that file.")
    elif has_locked:
        return ("The record says the catalog is at home, but a locked (checked-out) catalog is here.\n"
                f"A Leave or Arrive may have been interrupted. Check {cfg.desktop_catalog_dir}")
    elif not has_catalog:
        return f"The catalog is not at {cfg.desktop_lrcat}\nWas it renamed or moved?"
    return None


def pending_on_ssd(cfg: Config, ssd: drives.SSD) -> List[str]:
    """What Arrive would copy to the PC, as comparable strings. Saved at Leave as a baseline."""
    photos = fsops.compare_trees(ssd.photos, cfg.desktop_photos, cfg.extra_skip_names)
    found = [f"photo|{presets.key(f.rel)}|{f.size}|{int(f.src_mtime // 2)}" for f in photos.to_copy]
    if cfg.sync_presets and cfg.adobe_dir and os.path.isdir(ssd.presets):
        try:
            plan = presets.plan_in(ssd.presets, cfg.adobe_dir, cfg.backup_dir)
            found += [f"preset|{presets.key(i.rel)}|{i.size}|{int(i.mtime // 2)}" for i in plan.items]
        except Exception:  # presets never get in the way
            log.exception("Could not check presets on the SSD")
    return found


def _data_changed(now: Optional[dict], then: Optional[dict]) -> bool:
    if not now or not then:
        return bool(now) != bool(then)
    return now["files"] != then["files"] or not lr.same_fingerprint(now, then)


def detect(cfg: Config) -> Situation:
    try:
        return _detect(cfg)
    except Exception as e:  # e.g. the SSD was pulled out while looking
        log.exception("Could not work out the next step")
        return Situation(PROBLEM, "Could not check the catalog.", str(e))


def _detect(cfg: Config) -> Situation:
    problem = record_problem(cfg)
    if problem:
        return Situation(PROBLEM, "The catalog needs attention.", problem)
    record = state.read(cfg.desktop_catalog_dir)
    out = state.is_out(record)
    since = state.describe_since(record)
    where = f"Catalog is CHECKED OUT since {since}." if out else "Catalog is AT HOME."

    ssd = drives.connect(cfg)
    if not ssd:
        name = f"'{cfg.ssd_label}'" if cfg.ssd_label else "travel"
        purpose = "bring the catalog back" if out else "prepare a trip"
        return Situation(NO_SSD, f"{where} SSD not connected.",
                         f"Connect the {name} SSD to {purpose}.")
    where += " SSD connected."
    ssd_record = state.read(ssd.catalog_dir)

    if out:
        if ssd_record.get("status") == "unreadable":
            return Situation(PROBLEM, where, "The checkout record on the SSD is damaged:\n"
                             + os.path.join(ssd.catalog_dir, state.STATE_FILE))
        if ssd_record.get("checkout_id") != record.get("checkout_id"):
            return Situation(WRONG_SSD, where, "This SSD doesn't hold the checked-out catalog.\n"
                             f"Connect the one you left with on {since}.")
        if not os.path.exists(ssd.lrcat):
            return Situation(PROBLEM, where, f"The catalog is missing on the SSD: {ssd.lrcat}")
        reasons = []
        data = ssd.lrcat + "-data"
        data_now = fsops.tree_fingerprint(data)
        if (not lr.same_fingerprint(lr.fingerprint(ssd.lrcat), record.get("ssd_fingerprint"))
                or _data_changed(data_now, record.get("ssd_data_fingerprint"))):
            reasons.append("the catalog was edited")
        new = set(pending_on_ssd(cfg, ssd)) - set(record.get("pending_at_leave", []))
        for kind in ("photo", "preset"):
            count = sum(item.startswith(kind + "|") for item in new)
            if count:
                reasons.append(f"{count} new or changed {kind} file(s)")
        if reasons:
            return Situation(ARRIVE, where, "Since you left: " + ", ".join(reasons) + ".")
        return Situation(READY, where, "Ready to travel: nothing has changed since Leave.")

    if os.path.exists(ssd.lrcat) and ssd_record.get("status") in ("out", "released", "unreadable"):
        return Situation(PROBLEM, where,
                         "The SSD holds a catalog that was never brought back. It may contain Mac edits.\n"
                         "Leave (under More options) would set it aside without bringing those edits in.")
    count = len(fsops.compare_trees(ssd.photos, cfg.desktop_photos, cfg.extra_skip_names).to_copy)
    if count:
        return Situation(COPY_PHOTOS, where,
                         f"{count} file(s) on the SSD aren't in the archive.\n"
                         "They are new, changed, or were deleted from the archive: check the list.")
    return Situation(LEAVE, where)
