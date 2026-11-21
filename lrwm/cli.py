"""Command line and interactive menu."""

import argparse
import sys

from . import __version__, drives, state, ui, workflows
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


def menu(cfg, dry_run: bool) -> None:
    while True:
        pc_state = state.read(cfg.desktop_catalog_dir)
        out = state.is_out(pc_state)
        ssd = drives.connect(cfg)
        ui.header(f"LIGHTROOM WORKFLOW MANAGER {__version__}" + ("   [DRY RUN]" if dry_run else ""))
        where = (f"CHECKED OUT to the laptop since {state.describe_since(pc_state)}"
                 if out else "AT HOME (PC)")
        ui.info(f"Catalog: {where}")
        ui.info(f"SSD:     {'connected at ' + ssd.root if ssd else 'not connected'}")
        rec = "2" if out else "1"
        print()
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
        options.append(("Q", "Quit", ""))
        for key, name, desc in options:
            mark = "  <- suggested" if key == rec and ssd else ""
            ui.info(f"[{key}] {name:<18} {desc}{mark}")
        try:
            choice = ui.ask("\n  Choose: ").upper()
        except EOFError:
            return
        if choice == "1":
            _run(workflows.leave, cfg, dry_run=dry_run)
        elif choice == "2":
            _run(workflows.arrive, cfg, dry_run=dry_run)
        elif choice == "3":
            _run(workflows.bring_folders, cfg, dry_run=dry_run)
        elif choice == "4":
            _run(workflows.copy_photos, cfg, dry_run=dry_run)
        elif choice == "5":
            _run(workflows.status, cfg)
        elif choice == "D":
            dry_run = not dry_run
            continue
        elif choice == "R" and out:
            _run(workflows.release, cfg)
        elif choice == "Q":
            return
        else:
            continue
        ui.pause()


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
    menu(cfg, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
