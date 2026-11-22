"""Command line and interactive menu."""

import argparse
import sys

from . import __version__, situation, state, ui, workflows
from .config import ConfigError, load_config


def _folders_arg(value):
    if value is None:
        return None
    return [workflows._normalize_rel(p) for p in value.split(";") if p.strip()]


def _run(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return True
    except workflows.Abort as e:
        ui.warn(str(e))
    except KeyboardInterrupt:
        ui.warn("Interrupted. Any copy in progress was discarded; nothing was half-written.")
    except Exception as e:
        workflows.log.exception("Unexpected error")
        ui.warn(f"Unexpected error: {e}\nDetails are in lrwm.log in the backup folder.")
    return False


SUGGESTED = {
    # situation: (menu key, what Enter does)
    situation.LEAVE: ("1", "Leave: prepare the SSD for a trip"),
    situation.ARRIVE: ("2", "Arrive: bring the catalog and photos home"),
    situation.COPY_PHOTOS: ("4", "Copy photos to the archive first"),
}
PROCEDURES = {"1": "leave", "2": "arrive", "3": "bring_folders", "4": "copy_photos"}


def start(cfg, dry_run: bool = False) -> None:
    """Interactive start: one suggested step, with everything else behind [M]."""
    finished = None  # set after a Leave or Arrive, so another Enter can't start the opposite one
    try:
        while True:
            print("\n  Checking the catalog and SSD...")
            now = situation.detect(cfg)
            ui.header(f"LIGHTROOM WORKFLOW MANAGER {__version__}" + ("   [DRY RUN]" if dry_run else ""))
            ui.info(now.where)
            if now.detail:
                for line in now.detail.split("\n"):
                    ui.info(line)
            print()

            enter = None  # a menu key, "quit", "again", or None for nothing
            if finished and now.kind != situation.PROBLEM:
                ui.info(finished)
                enter, label = "quit", "Quit"
            elif now.kind in SUGGESTED:
                enter, label = SUGGESTED[now.kind]
            elif now.kind == situation.READY:
                enter, label = "quit", "Quit"
            elif now.kind in (situation.NO_SSD, situation.WRONG_SSD):
                enter, label = "again", "Check again"
            if enter:
                ui.info(f"[Enter] {label}")
            if now.kind == situation.READY:
                ui.info("[A] Trip cancelled: Arrive")
            ui.info("[M] More options     [Q] Quit")

            choice = ui.ask("\n  Choose: ").upper()
            if choice == "Q" or (not choice and enter == "quit"):
                return
            if choice == "M":
                key = menu(cfg, now, dry_run)
                if key == "Q":
                    return
                if key == "D":
                    dry_run, finished = not dry_run, None
                    continue
                if key == "B":
                    continue
            elif choice == "A" and now.kind == situation.READY:
                key = "2"
            elif not choice and enter not in (None, "again"):
                key = enter
            else:
                continue

            finished = None
            if key == "5":
                _run(workflows.status, cfg)
            elif key == "R":
                _run(workflows.release, cfg)
            else:
                ok = _run(getattr(workflows, PROCEDURES[key]), cfg, dry_run=dry_run)
                if ok and not dry_run and key in ("1", "2"):
                    finished = ("Done. Eject the SSD before you unplug it." if key == "1"
                                else "Done. You can open Lightroom on this PC.")
            ui.pause()
    except (EOFError, KeyboardInterrupt):
        return


def menu(cfg, now: situation.Situation, dry_run: bool) -> str:
    """All options. Returns the key chosen; Enter never starts anything here."""
    out = state.is_out(state.read(cfg.desktop_catalog_dir))
    suggested = SUGGESTED.get(now.kind, ("", ""))[0]
    ui.header("MORE OPTIONS", "-")
    options = [
        ("1", "Leave", "check the catalog out to the SSD (+ take archive folders along)"),
        ("2", "Arrive", "bring the catalog and new photos back to the PC"),
        ("3", "Take along", "copy more archive folders to the SSD"),
        ("4", "Copy photos", "copy new photos from the SSD to the archive only"),
        ("5", "Status", "details, backups, what's on the SSD"),
        ("D", "Dry run", f"show plans without changing anything (now {'ON' if dry_run else 'OFF'})"),
    ]
    if out:
        options.append(("R", "Emergency release", "unlock the PC catalog without the SSD"))
    options += [("B", "Back", ""), ("Q", "Quit", "")]
    for key, name, desc in options:
        mark = "  <- suggested" if key == suggested else ""
        ui.info(f"[{key}] {name:<18} {desc}{mark}")
    keys = [key for key, _, _ in options]
    while True:
        choice = ui.ask("\n  Choose: ").upper()
        if not choice:
            return "B"
        if choice in keys:
            return choice


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="lr_workflow",
        description="Check a Lightroom Classic catalog out to a travel SSD and back in.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--leave", action="store_true", help="check the catalog out to the SSD")
    g.add_argument("--arrive", action="store_true", help="bring the catalog back from the SSD")
    g.add_argument("--take", metavar="FOLDERS", help="copy archive folders to the SSD ('a;b')")
    g.add_argument("--copy-photos", "--sync", action="store_true", dest="copy_photos",
                   help="copy new photos from the SSD to the archive")
    g.add_argument("--status", action="store_true", help="show status")
    g.add_argument("--release", action="store_true", help="emergency: unlock the PC catalog")
    p.add_argument("--folders", metavar="FOLDERS",
                   help="with --leave: archive folders to take along, 'Models\\Ira;Abroad\\X'")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--config", help="path to config.json")
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # accepted for old shortcuts
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        ui.warn(str(e))
        return 2
    workflows.setup_logging(cfg)

    if args.leave:
        return 0 if _run(workflows.leave, cfg, folders=_folders_arg(args.folders), dry_run=args.dry_run) else 1
    if args.arrive:
        return 0 if _run(workflows.arrive, cfg, dry_run=args.dry_run) else 1
    if args.take:
        return 0 if _run(workflows.bring_folders, cfg, folders=_folders_arg(args.take), dry_run=args.dry_run) else 1
    if args.copy_photos:
        return 0 if _run(workflows.copy_photos, cfg, dry_run=args.dry_run) else 1
    if args.status:
        return 0 if _run(workflows.status, cfg) else 1
    if args.release:
        return 0 if _run(workflows.release, cfg) else 1
    start(cfg, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
