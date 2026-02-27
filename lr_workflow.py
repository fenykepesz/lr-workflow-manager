"""
Lightroom Workflow Manager
==========================
Manages the Lightroom catalog and photo files between an external SSD
and the desktop PC for a cross-platform (Windows/macOS) workflow.

Features:
  [1] ARRIVING HOME - SSD → Desktop
      - Syncs new/modified RAW files from SSD to desktop archive
      - Copies catalog (.lrcat + .lrcat-data) from SSD to local desktop folder
      - Failsafe checks before overwriting local catalog

  [2] LEAVING HOME - Desktop → SSD
      - Copies catalog (.lrcat + .lrcat-data) from desktop back to SSD
      - Failsafe checks before overwriting SSD catalog
      - Warns if file sync hasn't been run

  [3] SYNC FILES ONLY - SSD → Desktop (no catalog copy)
      - Same as the original sync_to_desktop.py

Usage:
  python lr_workflow.py              Interactive menu
  python lr_workflow.py --arrive     Arriving home (SSD → Desktop)
  python lr_workflow.py --leave      Leaving home (Desktop → SSD)
  python lr_workflow.py --sync       Sync files only

DISCLAIMER: This script is provided "as-is" without warranty. 
The author takes no responsibility for any damage caused by its usage.
"""

import os
import sys
import shutil
import glob
import json
import logging
from datetime import datetime
from pathlib import Path

# ============================================================
# LOGGING SETUP
# ============================================================
LOG_FILE = "workflow_log.txt"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION LOADER
# ============================================================
CONFIG_FILE = "config.json"

def load_config():
    """Load configuration from config.json or fall back to defaults."""
    # Default paths for developer reference
    defaults = {
        "ssd_photos": r"H:\Photography",
        "ssd_catalog_dir": r"H:\Catalog",
        "desktop_photos": r"E:\Photography",
        "desktop_catalog_dir": r"C:\Lightroom\Catalog",
        "catalog_name": "MyCatalog"
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                return {**defaults, **config}
        except Exception as e:
            print(f"  WARNING: Error loading {CONFIG_FILE}: {e}")
            print("  Using default/previous configuration.")
    
    return defaults

# Load the config
config = load_config()

# SSD paths
SSD_PHOTOS = config["ssd_photos"]
SSD_CATALOG_DIR = config["ssd_catalog_dir"]

# Desktop paths
DESKTOP_PHOTOS = config["desktop_photos"]
DESKTOP_CATALOG_DIR = config["desktop_catalog_dir"]

# Catalog filename
CATALOG_NAME = config["catalog_name"]

# ============================================================
# END OF CONFIGURATION
# ============================================================

# Derived catalog file paths
CATALOG_FILES_EXT = [".lrcat"]           # main catalog
CATALOG_DIRS_EXT = [".lrcat-data"]       # catalog data folder
LOCKFILE_EXT = [".lrcat-wal", ".lrcat-shm", ".lrcat-lock"]  # SQLite temp files = LR is open

# Files/folders to skip during photo sync
SKIP_PATTERNS = [
    "Previews.lrdata",
    "Smart Previews.lrdata",
    "Helper.lrdata",
    ".lrcat-data",
    ".lrcat-wal",
    ".lrcat-shm",
    ".lrcat-lock",
    ".lrcat",
    "Thumbs.db",
    ".DS_Store",
    "desktop.ini",
    ".dropbox",
    ".dropbox.attr",
]

# Timestamp tolerance in seconds (exFAT rounds to 2-second intervals)
TIMESTAMP_TOLERANCE = 2


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def should_skip(path_str):
    """Check if a file/folder should be skipped during photo sync."""
    for pattern in SKIP_PATTERNS:
        if pattern.lower() in path_str.lower():
            return True
    return False


def format_size(size_bytes):
    """Format bytes into human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_time(timestamp):
    """Format a timestamp into readable date/time."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def progress_bar(current, total, total_bytes=0, copied_bytes=0, width=40, prefix=""):
    """Print a text progress bar. Call with end values to finalize."""
    if total == 0:
        return
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    pct_str = f"{pct * 100:5.1f}%"
    count_str = f"{current}/{total}"
    if total_bytes > 0 and copied_bytes > 0:
        size_str = f" | {format_size(copied_bytes)}/{format_size(total_bytes)}"
    else:
        size_str = ""
    line = f"\r  {prefix}[{bar}] {pct_str}  {count_str}{size_str}"
    print(line, end="", flush=True)
    if current >= total:
        print()  # newline when done


def copy_file_with_progress(src, dst, label="", chunk_size=4 * 1024 * 1024):
    """Copy a single large file with a progress bar. Preserves timestamps.
    Uses 4MB chunks for smooth progress on ~1.5GB catalog files."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    total = os.path.getsize(src)
    copied = 0
    width = 40
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(chunk_size)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            pct = copied / total if total > 0 else 1
            filled = int(width * pct)
            bar = "█" * filled + "░" * (width - filled)
            line = f"\r  {label}[{bar}] {pct * 100:5.1f}%  {format_size(copied)}/{format_size(total)}"
            print(line, end="", flush=True)
    print()  # newline when done
    # Preserve timestamps
    shutil.copystat(src, dst)


def copy_file(src, dst):
    """Copy a file, creating directories as needed. Preserves timestamps."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def copy_directory(src, dst, label=""):
    """Copy an entire directory tree, preserving timestamps. Shows progress."""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    # Count files first for progress
    all_files = []
    for root, dirs, files in os.walk(src):
        for f in files:
            all_files.append(os.path.join(root, f))
    total = len(all_files)
    if total == 0:
        shutil.copytree(src, dst, copy_function=shutil.copy2)
        return
    # Copy with progress
    shutil.copytree(src, dst, copy_function=shutil.copy2)
    # Since copytree doesn't have per-file callbacks in older Python,
    # we just show completion
    if label:
        progress_bar(total, total, prefix=label)
    else:
        print(f"  Copied {total} files.")


def check_lightroom_running(catalog_dir, catalog_name):
    """Check if Lightroom has the catalog open (lock files present)."""
    for ext in LOCKFILE_EXT:
        lock_path = os.path.join(catalog_dir, catalog_name + ext)
        if os.path.exists(lock_path):
            return True
    return False


def get_catalog_info(catalog_dir, catalog_name):
    """Get modification time and size of catalog files. Returns dict or None."""
    lrcat_path = os.path.join(catalog_dir, catalog_name + ".lrcat")
    if not os.path.exists(lrcat_path):
        return None

    stat = os.stat(lrcat_path)
    info = {
        "lrcat_path": lrcat_path,
        "lrcat_size": stat.st_size,
        "lrcat_mtime": stat.st_mtime,
    }

    # Check for .lrcat-data folder
    data_path = os.path.join(catalog_dir, catalog_name + ".lrcat-data")
    if os.path.exists(data_path):
        # Sum up all files in the folder
        total_size = 0
        latest_mtime = 0
        for root, dirs, files in os.walk(data_path):
            for f in files:
                fp = os.path.join(root, f)
                s = os.stat(fp)
                total_size += s.st_size
                latest_mtime = max(latest_mtime, s.st_mtime)
        info["data_path"] = data_path
        info["data_size"] = total_size
        info["data_mtime"] = latest_mtime
    else:
        info["data_path"] = None
        info["data_size"] = 0
        info["data_mtime"] = 0

    return info


def print_catalog_comparison(src_info, dst_info, src_label, dst_label):
    """Print a side-by-side comparison of two catalog versions."""
    print(f"\n  {'':30s} {'Size':>12s}   {'Last Modified':>20s}")
    print(f"  {'-'*30} {'-'*12}   {'-'*20}")

    # .lrcat file
    if src_info:
        print(f"  {src_label + ' .lrcat':<30s} {format_size(src_info['lrcat_size']):>12s}   "
              f"{format_time(src_info['lrcat_mtime']):>20s}")
    else:
        print(f"  {src_label + ' .lrcat':<30s} {'NOT FOUND':>12s}")

    if dst_info:
        print(f"  {dst_label + ' .lrcat':<30s} {format_size(dst_info['lrcat_size']):>12s}   "
              f"{format_time(dst_info['lrcat_mtime']):>20s}")
    else:
        print(f"  {dst_label + ' .lrcat':<30s} {'NOT FOUND':>12s}")

    # .lrcat-data
    if src_info and src_info["data_path"]:
        print(f"  {src_label + ' .lrcat-data':<30s} {format_size(src_info['data_size']):>12s}   "
              f"{format_time(src_info['data_mtime']):>20s}")
    if dst_info and dst_info["data_path"]:
        print(f"  {dst_label + ' .lrcat-data':<30s} {format_size(dst_info['data_size']):>12s}   "
              f"{format_time(dst_info['data_mtime']):>20s}")

    # Determine which is newer
    if src_info and dst_info:
        diff = src_info["lrcat_mtime"] - dst_info["lrcat_mtime"]
        if abs(diff) <= TIMESTAMP_TOLERANCE:
            print(f"\n  >> Both catalogs have the same modification time")
        elif diff > 0:
            hours = diff / 3600
            if hours >= 1:
                print(f"\n  >> {src_label} is NEWER by {hours:.1f} hours")
            else:
                print(f"\n  >> {src_label} is NEWER by {diff:.0f} seconds")
        else:
            hours = abs(diff) / 3600
            if hours >= 1:
                print(f"\n  >> {dst_label} is NEWER by {hours:.1f} hours")
            else:
                print(f"\n  >> {dst_label} is NEWER by {abs(diff):.0f} seconds")


def check_unsynced_files(ssd_path, desktop_path):
    """Quick check if there are new files on SSD not yet on desktop.
    Returns count of new files found (checks first 1000 files max for speed)."""
    new_count = 0
    checked = 0
    for root, dirs, files in os.walk(ssd_path):
        dirs[:] = [d for d in dirs if not should_skip(os.path.join(root, d))]
        for filename in files:
            src_path = os.path.join(root, filename)
            if should_skip(src_path):
                continue
            rel_path = os.path.relpath(src_path, ssd_path)
            dst_path = os.path.join(desktop_path, rel_path)
            if not os.path.exists(dst_path):
                new_count += 1
            checked += 1
            if checked >= 1000:
                return new_count, False  # incomplete scan
    return new_count, True  # complete scan


# ============================================================
# PHOTO FILE SYNC (SSD → Desktop)
# ============================================================

def scan_files(source, destination):
    """Scan and categorize files into new, modified, and unchanged."""
    new_files = []
    modified_files = []
    unchanged_files = []
    skipped_files = []

    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if not should_skip(os.path.join(root, d))]
        for filename in files:
            src_path = os.path.join(root, filename)
            if should_skip(src_path):
                skipped_files.append(src_path)
                continue

            rel_path = os.path.relpath(src_path, source)
            dst_path = os.path.join(destination, rel_path)

            src_stat = os.stat(src_path)
            src_size = src_stat.st_size
            src_mtime = src_stat.st_mtime

            if not os.path.exists(dst_path):
                new_files.append({
                    "src": src_path,
                    "dst": dst_path,
                    "rel": rel_path,
                    "size": src_size,
                    "src_mtime": src_mtime,
                })
            else:
                dst_stat = os.stat(dst_path)
                dst_size = dst_stat.st_size
                dst_mtime = dst_stat.st_mtime

                # Check if source is genuinely newer (with exFAT tolerance)
                # or if file size differs
                if src_size != dst_size or src_mtime > dst_mtime + TIMESTAMP_TOLERANCE:
                    modified_files.append({
                        "src": src_path,
                        "dst": dst_path,
                        "rel": rel_path,
                        "src_size": src_size,
                        "dst_size": dst_size,
                        "src_mtime": src_mtime,
                        "dst_mtime": dst_mtime,
                    })
                else:
                    unchanged_files.append(rel_path)

    return new_files, modified_files, unchanged_files, skipped_files


def sync_photos(auto_mode=False):
    """Sync photo files from SSD to desktop. Returns True if sync was run."""
    logger.info(f"Starting SYNC: {SSD_PHOTOS} -> {DESKTOP_PHOTOS}")

    if not os.path.exists(SSD_PHOTOS):
        logger.error(f"SSD not found at {SSD_PHOTOS}")
        return False

    if not os.path.exists(DESKTOP_PHOTOS):
        logger.info(f"Creating destination: {DESKTOP_PHOTOS}")
        os.makedirs(DESKTOP_PHOTOS, exist_ok=True)

    print("\n  Scanning files...")
    new_files, modified_files, unchanged_files, skipped_files = scan_files(
        SSD_PHOTOS, DESKTOP_PHOTOS
    )

    total_new_size = sum(f["size"] for f in new_files)
    total_mod_size = sum(f["src_size"] for f in modified_files)

    logger.info(f"Scan complete: {len(new_files)} new, {len(modified_files)} modified, {len(unchanged_files)} unchanged")
    print(f"    New files:       {len(new_files):>6}  ({format_size(total_new_size)})")
    print(f"    Modified files:  {len(modified_files):>6}  ({format_size(total_mod_size)})")
    print(f"    Unchanged:       {len(unchanged_files):>6}")
    print(f"    Skipped:         {len(skipped_files):>6}")

    if not new_files and not modified_files:
        print("\n  Everything is up to date. Nothing to copy.")
        return True

    # ---- Copy new files ----
    if new_files:
        print(f"\n  --- {len(new_files)} new files ({format_size(total_new_size)}) ---")
        if not auto_mode:
            response = input("  Proceed with copying new files? (Y/n/list): ").strip().lower()
            if response == "list":
                for f in new_files[:50]:
                    print(f"    + {f['rel']}  ({format_size(f['size'])})")
                if len(new_files) > 50:
                    print(f"    ... and {len(new_files) - 50} more files")
                response = input("\n  Proceed? (Y/n): ").strip().lower()
            if response == "n":
                print("  Skipped new files.")
                new_files = []

        copied = 0
        errors = 0
        copied_bytes = 0
        for f in new_files:
            try:
                copy_file(f["src"], f["dst"])
                copied += 1
                copied_bytes += f["size"]
                progress_bar(copied, len(new_files), total_new_size, copied_bytes, prefix="New: ")
            except Exception as e:
                errors += 1
                print(f"\n    ERROR copying {f['rel']}: {e}")

        if new_files:
            logger.info(f"New files: {copied} copied, {errors} errors")

    # ---- Handle modified files ----
    if modified_files:
        print(f"\n  --- {len(modified_files)} modified files ---")

        if auto_mode:
            copied = 0
            errors = 0
            total_mod = len(modified_files)
            for f in modified_files:
                try:
                    copy_file(f["src"], f["dst"])
                    copied += 1
                    progress_bar(copied, total_mod, prefix="Modified: ")
                except Exception as e:
                    errors += 1
                    print(f"    ERROR: {f['rel']}: {e}")
            print(f"  Modified files: {copied} replaced, {errors} errors")
        else:
            print("  Options:")
            print("    [A] Replace ALL modified files")
            print("    [O] Review ONE by ONE")
            print("    [S] Skip all modified files")
            choice = input("  Choose (A/O/S): ").strip().upper()

            if choice == "A":
                copied = 0
                errors = 0
                total_mod = len(modified_files)
                for f in modified_files:
                    try:
                        copy_file(f["src"], f["dst"])
                        copied += 1
                        progress_bar(copied, total_mod, prefix="Modified: ")
                    except Exception as e:
                        errors += 1
                        print(f"\n    ERROR: {f['rel']}: {e}")
                print(f"  Modified files: {copied} replaced, {errors} errors")

            elif choice == "O":
                copied = 0
                skipped_count = 0
                errors = 0
                for f in modified_files:
                    print(f"\n    File: {f['rel']}")
                    print(f"      SSD:     {format_size(f['src_size'])}  modified {format_time(f['src_mtime'])}")
                    print(f"      Desktop: {format_size(f['dst_size'])}  modified {format_time(f['dst_mtime'])}")

                    if f["src_mtime"] > f["dst_mtime"]:
                        print("      >> SSD version is NEWER")
                    else:
                        print("      >> Desktop version is NEWER (size differs)")

                    resp = input("    Replace desktop file? (y/N/all): ").strip().lower()
                    if resp == "all":
                        try:
                            copy_file(f["src"], f["dst"])
                            copied += 1
                        except Exception as e:
                            errors += 1
                            print(f"    ERROR: {e}")
                        remaining = modified_files[modified_files.index(f) + 1:]
                        for rf in remaining:
                            try:
                                copy_file(rf["src"], rf["dst"])
                                copied += 1
                            except Exception as e:
                                errors += 1
                        break
                    elif resp == "y":
                        try:
                            copy_file(f["src"], f["dst"])
                            copied += 1
                        except Exception as e:
                            errors += 1
                            print(f"    ERROR: {e}")
                    else:
                        skipped_count += 1

                print(f"\n  Modified files: {copied} replaced, {skipped_count} skipped, {errors} errors")
            else:
                print("  Skipped all modified files.")

    print("\n  File sync complete.")
    return True


# ============================================================
# CATALOG MANAGEMENT
# ============================================================

def copy_catalog(src_dir, dst_dir, src_label, dst_label):
    """Copy catalog files from source to destination with failsafe checks.
    Returns True if copy was performed."""

    print(f"\n  Checking catalogs...")

    # Check for Lightroom lock files at BOTH locations
    for check_dir, check_label in [(src_dir, src_label), (dst_dir, dst_label)]:
        if check_lightroom_running(check_dir, CATALOG_NAME):
            print(f"\n  !! STOP: Lightroom appears to be OPEN at {check_label} location!")
            print(f"     Lock files found in: {check_dir}")
            print(f"     Close Lightroom completely before proceeding.")
            return False

    # Get catalog info from both locations
    src_info = get_catalog_info(src_dir, CATALOG_NAME)
    dst_info = get_catalog_info(dst_dir, CATALOG_NAME)

    if not src_info:
        print(f"\n  ERROR: Catalog not found at {src_label} location:")
        print(f"    {os.path.join(src_dir, CATALOG_NAME + '.lrcat')}")
        return False

    # Show comparison
    print_catalog_comparison(src_info, dst_info, src_label, dst_label)

    # ---- FAILSAFE CHECKS ----
    warnings = []

    if dst_info:
        src_time = src_info["lrcat_mtime"]
        dst_time = dst_info["lrcat_mtime"]

        # Check 1: Destination is NEWER than source
        if dst_time > src_time + TIMESTAMP_TOLERANCE:
            diff_hours = (dst_time - src_time) / 3600
            warnings.append(
                f"CONFLICT: {dst_label} catalog is NEWER than {src_label} "
                f"by {diff_hours:.1f} hours!\n"
                f"     This means edits were made at the {dst_label} location\n"
                f"     AFTER the last catalog copy. Overwriting will LOSE those edits."
            )

        # Check 2: Same modification time (catalog wasn't edited)
        if abs(src_time - dst_time) <= TIMESTAMP_TOLERANCE:
            warnings.append(
                f"NOTE: Both catalogs have the same modification time.\n"
                f"     No edits appear to have been made since the last copy.\n"
                f"     Copying would be redundant."
            )

    if warnings:
        print(f"\n  {'!'*60}")
        for i, w in enumerate(warnings):
            print(f"  !! WARNING {i+1}:")
            for line in w.split("\n"):
                print(f"     {line}")
        print(f"  {'!'*60}")

        # If destination is newer, require explicit confirmation
        if dst_info and dst_info["lrcat_mtime"] > src_info["lrcat_mtime"] + TIMESTAMP_TOLERANCE:
            print(f"\n  This will OVERWRITE newer edits at {dst_label}.")
            confirm = input(f"  Type 'OVERWRITE' to confirm, or anything else to cancel: ").strip()
            if confirm != "OVERWRITE":
                print("  Cancelled. No files were changed.")
                return False
        else:
            resp = input(f"\n  Proceed with copy? (y/N): ").strip().lower()
            if resp != "y":
                print("  Cancelled.")
                return False
    else:
        # Normal case: source is newer, safe to copy
        if dst_info:
            resp = input(f"\n  Copy {src_label} catalog to {dst_label}? (Y/n): ").strip().lower()
            if resp == "n":
                print("  Cancelled.")
                return False
        else:
            print(f"\n  No existing catalog at {dst_label}. This will be a fresh copy.")
            resp = input(f"  Proceed? (Y/n): ").strip().lower()
            if resp == "n":
                print("  Cancelled.")
                return False

    # ---- PERFORM THE COPY ----
    os.makedirs(dst_dir, exist_ok=True)

    # Backup existing destination catalog before overwriting
    if dst_info:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(dst_dir, "Backups")

        print(f"\n  A backup of the {dst_label} catalog can be saved before overwriting.")
        print(f"    Location: {backup_dir}")
        print(f"    Files:    {CATALOG_NAME}_{timestamp}.lrcat")
        if dst_info["data_path"]:
            print(f"              {CATALOG_NAME}_{timestamp}.lrcat-data")

        backup_choice = input("  Create backup? (Y/n): ").strip().lower()

        if backup_choice != "n":
            os.makedirs(backup_dir, exist_ok=True)

            # Backup .lrcat
            backup_lrcat = os.path.join(backup_dir, f"{CATALOG_NAME}_{timestamp}.lrcat")
            print(f"  Backing up .lrcat ({format_size(dst_info['lrcat_size'])})...")
            try:
                copy_file_with_progress(dst_info["lrcat_path"], backup_lrcat, label="Backup .lrcat: ")
                logger.info(f"Backup .lrcat done: {backup_lrcat}")
            except Exception as e:
                print(f"\n  WARNING: Could not backup .lrcat: {e}")
                resp = input("  Continue without backup? (y/N): ").strip().lower()
                if resp != "y":
                    return False

            # Backup .lrcat-data
            if dst_info["data_path"] and os.path.exists(dst_info["data_path"]):
                backup_data = os.path.join(backup_dir, f"{CATALOG_NAME}_{timestamp}.lrcat-data")
                print(f"  Backing up .lrcat-data ({format_size(dst_info['data_size'])})...")
                try:
                    copy_directory(dst_info["data_path"], backup_data, label="Backup .lrcat-data: ")
                    logger.info(f"Backup .lrcat-data done: {backup_data}")
                except Exception as e:
                    print(f"\n  WARNING: Could not backup .lrcat-data: {e}")

            # Show backup folder size and count
            backup_count = len([f for f in os.listdir(backup_dir) if f.endswith(".lrcat")])
            backup_total = sum(
                os.path.getsize(os.path.join(backup_dir, f))
                for f in os.listdir(backup_dir)
                if os.path.isfile(os.path.join(backup_dir, f))
            )
            print(f"  Backup saved. ({backup_count} backup(s) in folder, {format_size(backup_total)} total)")
        else:
            print("  Skipping backup.")

    # Copy .lrcat file
    src_lrcat = os.path.join(src_dir, CATALOG_NAME + ".lrcat")
    dst_lrcat = os.path.join(dst_dir, CATALOG_NAME + ".lrcat")
    print(f"  Copying .lrcat ({format_size(src_info['lrcat_size'])})...")
    try:
        copy_file_with_progress(src_lrcat, dst_lrcat, label="Copy .lrcat: ")
        print("  Copy .lrcat done.")
    except Exception as e:
        print(f"\n  ERROR copying .lrcat: {e}")
        return False

    # Copy .lrcat-data folder
    src_data = os.path.join(src_dir, CATALOG_NAME + ".lrcat-data")
    dst_data = os.path.join(dst_dir, CATALOG_NAME + ".lrcat-data")
    if os.path.exists(src_data):
        print(f"  Copying .lrcat-data ({format_size(src_info['data_size'])})...")
        try:
            copy_directory(src_data, dst_data, label="Copy .lrcat-data: ")
            print("  Copy .lrcat-data done.")
        except Exception as e:
            print(f"\n  ERROR copying .lrcat-data: {e}")
            return False
    elif os.path.exists(dst_data):
        print(f"  Note: No .lrcat-data in {src_label}; leaving {dst_label} copy as-is.")

    logger.info(f"Catalog successfully copied: {src_label} → {dst_label}")
    return True


# ============================================================
# WORKFLOW: ARRIVING HOME
# ============================================================

def arriving_home():
    """Full workflow for returning home with SSD."""
    logger.info("WORKFLOW: ARRIVING HOME started.")

    # Step 0: Verify SSD is connected
    if not os.path.exists(SSD_PHOTOS):
        print(f"\n  ERROR: SSD not found at {SSD_PHOTOS}")
        print("  Connect the SSD and try again.")
        input("\n  Press Enter to exit...")
        return

    # Check Lightroom isn't running on desktop
    if check_lightroom_running(DESKTOP_CATALOG_DIR, CATALOG_NAME):
        print(f"\n  !! STOP: Lightroom appears to be OPEN on the desktop!")
        print(f"     Close Lightroom before proceeding.")
        input("\n  Press Enter to exit...")
        return

    if check_lightroom_running(SSD_CATALOG_DIR, CATALOG_NAME):
        print(f"\n  !! STOP: Lightroom lock files found on SSD!")
        print(f"     Was Lightroom closed properly on the Mac?")
        input("\n  Press Enter to exit...")
        return

    # Step 1: Sync photo files
    print(f"\n  STEP 1: Sync photo files from SSD to desktop")
    print(f"  " + "-" * 50)
    sync_result = sync_photos()

    # Step 2: Copy catalog from SSD to desktop
    print(f"\n\n  STEP 2: Copy catalog from SSD to desktop")
    print(f"  " + "-" * 50)
    copy_catalog(SSD_CATALOG_DIR, DESKTOP_CATALOG_DIR, "SSD", "Desktop")

    # Final reminders
    print("\n" + "=" * 65)
    print("  ARRIVING HOME - Complete!")
    print("=" * 65)
    print("\n  Next steps:")
    print("    1. Disconnect the SSD")
    print("    2. Open Lightroom Classic")
    print("    3. File > Open Catalog > navigate to:")
    print(f"       {os.path.join(DESKTOP_CATALOG_DIR, CATALOG_NAME + '.lrcat')}")
    print("    4. In the Folders panel, right-click the top folder")
    print("       > Update Folder Location > point to E:\\Photography")
    print("       (only needed the first time or if folder structure changed)")

    input("\n  Press Enter to exit...")


# ============================================================
# WORKFLOW: LEAVING HOME
# ============================================================

def leaving_home():
    """Full workflow for preparing SSD to leave with."""
    logger.info("WORKFLOW: LEAVING HOME started.")

    # Step 0: Verify SSD is connected
    if not os.path.exists(SSD_PHOTOS):
        print(f"\n  ERROR: SSD not found at {SSD_PHOTOS}")
        print("  Connect the SSD and try again.")
        input("\n  Press Enter to exit...")
        return

    # Check Lightroom isn't running
    if check_lightroom_running(DESKTOP_CATALOG_DIR, CATALOG_NAME):
        print(f"\n  !! STOP: Lightroom appears to be OPEN on the desktop!")
        print(f"     Close Lightroom before proceeding.")
        input("\n  Press Enter to exit...")
        return

    # Step 1: Check for unsynced files (SSD → Desktop)
    print(f"\n  PRE-CHECK: Looking for unsynced files on SSD...")
    new_count, complete = check_unsynced_files(SSD_PHOTOS, DESKTOP_PHOTOS)
    if new_count > 0:
        scan_note = "" if complete else " (quick scan, may be more)"
        print(f"\n  !! WARNING: {new_count} files on SSD are NOT on the desktop{scan_note}")
        print(f"     You may have forgotten to sync after your last return.")
        print(f"     Running 'Arriving Home' first is recommended to avoid data loss.")
        resp = input(f"\n  Continue anyway? (y/N): ").strip().lower()
        if resp != "y":
            print("  Cancelled. Run 'Arriving Home' first.")
            input("\n  Press Enter to exit...")
            return
    else:
        print(f"  All SSD files are backed up on desktop. Good to go.")

    # Step 2: Copy catalog from desktop to SSD
    print(f"\n  STEP 1: Copy catalog from desktop to SSD")
    print(f"  " + "-" * 50)
    copy_catalog(DESKTOP_CATALOG_DIR, SSD_CATALOG_DIR, "Desktop", "SSD")

    # Final reminders
    print("\n" + "=" * 65)
    print("  LEAVING HOME - Complete!")
    print("=" * 65)
    print("\n  Next steps:")
    print("    1. Safely eject the SSD")
    print("    2. Connect SSD to MacBook")
    print("    3. Open Lightroom Classic")
    print("    4. File > Open Catalog > navigate to catalog on SSD")
    print("    5. In the Folders panel, right-click the top folder")
    print("       > Update Folder Location > point to /Volumes/[SSD_name]/Photography")
    print("       (only needed the first time or if folder structure changed)")

    input("\n  Press Enter to exit...")


# ============================================================
# MAIN MENU
# ============================================================

def main():
    # Handle command-line arguments
    if "--arrive" in sys.argv:
        arriving_home()
        return
    elif "--leave" in sys.argv:
        leaving_home()
        return
    elif "--sync" in sys.argv:
        sync_photos(auto_mode="--auto" in sys.argv)
        input("\n  Press Enter to exit...")
        return

    # Interactive menu
    while True:
        print("\n" + "=" * 65)
        print("  LIGHTROOM WORKFLOW MANAGER")
        print("=" * 65)
        print(f"  SSD Photos:       {SSD_PHOTOS}")
        print(f"  Desktop Photos:   {DESKTOP_PHOTOS}")
        print(f"  SSD Catalog:      {SSD_CATALOG_DIR}")
        print(f"  Desktop Catalog:  {DESKTOP_CATALOG_DIR}")
        print(f"  Catalog Name:     {CATALOG_NAME}")

        # Show SSD connection status
        ssd_connected = os.path.exists(SSD_PHOTOS)
        print(f"\n  SSD Status:       {'CONNECTED' if ssd_connected else 'NOT CONNECTED'}")

        # Show catalog timestamps if available
        ssd_info = get_catalog_info(SSD_CATALOG_DIR, CATALOG_NAME) if ssd_connected else None
        desk_info = get_catalog_info(DESKTOP_CATALOG_DIR, CATALOG_NAME)

        if ssd_info:
            print(f"  SSD Catalog:      {format_time(ssd_info['lrcat_mtime'])}  ({format_size(ssd_info['lrcat_size'])})")
        if desk_info:
            print(f"  Desktop Catalog:  {format_time(desk_info['lrcat_mtime'])}  ({format_size(desk_info['lrcat_size'])})")

        print(f"\n  Options:")
        print(f"    [1] Arriving Home  - Sync files + copy catalog SSD → Desktop")
        print(f"    [2] Leaving Home   - Copy catalog Desktop → SSD")
        print(f"    [3] Sync Files     - Copy photos SSD → Desktop only")
        print(f"    [4] Compare        - Show catalog comparison only")
        print(f"    [Q] Quit")

        choice = input("\n  Choose (1/2/3/4/Q): ").strip().upper()

        if choice == "1":
            arriving_home()
        elif choice == "2":
            leaving_home()
        elif choice == "3":
            sync_photos()
            input("\n  Press Enter to continue...")
        elif choice == "4":
            if ssd_info or desk_info:
                print_catalog_comparison(ssd_info, desk_info, "SSD", "Desktop")
            else:
                print("\n  No catalogs found to compare.")
            input("\n  Press Enter to continue...")
        elif choice == "Q":
            print("\n  Goodbye!")
            break
        else:
            print("  Invalid choice.")


if __name__ == "__main__":
    main()
