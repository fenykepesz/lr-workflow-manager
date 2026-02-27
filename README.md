# Lightroom Workflow Manager

> [!WARNING]
> **DISCLAIMER**: This script is provided **"as-is"** without any warranty of any kind, either express or implied. The author takes **no responsibility** for any damage, data loss, or issues caused by the usage of this script. Use it at your own risk. Always ensure you have backups of your Lightroom catalogs and photos before running any automation.

A Python script for photographers who edit in **Adobe Lightroom Classic** across multiple machines — typically a desktop workstation at home and a laptop on the road — using a portable SSD as the bridge between them.

## The Problem

Lightroom Classic stores everything in a single catalog file (`.lrcat`), and Adobe offers no built-in way to sync that catalog across machines. Photographers who work on both a desktop and a laptop face a painful workflow gap:

- **Cloud sync doesn't work.** Dropbox, OneDrive, and similar services can corrupt Lightroom's SQLite-based catalog through simultaneous access and sync conflicts. Adobe explicitly warns against this.
- **Lightroom's built-in "Export as Catalog" is tedious.** It's designed for handing off subsets of a catalog, not for round-tripping an entire working library between machines.
- **Manual copying is error-prone.** Forgetting which machine has the latest edits, overwriting newer work with an older catalog, or leaving new photos unsynced from a trip can all lead to data loss.

The typical real-world scenario: you shoot on location with your laptop, import and edit on the road, come home, and want to continue editing on your desktop with its larger screen and faster storage — without losing any work from either machine.

## The Solution

This script automates the two critical transitions in a portable-SSD workflow:

### Arriving Home (SSD → Desktop)
1. **Syncs photo files** from the SSD to the desktop archive (new and modified files only, never deletes)
2. **Copies the catalog** (`.lrcat` + `.lrcat-data`) from the SSD to a local working folder on the desktop

### Leaving Home (Desktop → SSD)
1. **Checks for unsynced files** to catch forgotten imports
2. **Copies the catalog** from the desktop back to the SSD, ready for the laptop

### Safety Features

The script includes multiple failsafes to prevent the most common data-loss scenarios:

- **Lightroom lock detection** — blocks all operations if Lightroom is still running (checks for `.lrcat-wal`/`.shm`/`.lock` files)
- **Timestamp conflict detection** — warns if the destination catalog is *newer* than the source (meaning you'd overwrite recent edits), requiring you to type `OVERWRITE` to confirm
- **Redundant copy detection** — flags when both catalogs have identical timestamps, avoiding unnecessary overwrites
- **Forgotten sync warning** — when leaving home, scans the SSD for files not yet backed up to the desktop
- **Timestamped backups** — optionally backs up the destination catalog before every overwrite, with filenames like `MyCatalog_20260227_143022.lrcat`
- **exFAT timestamp tolerance** — handles the 2-second rounding inherent to exFAT-formatted drives, eliminating false "modified" detections during file comparison
- **Progress bars** — visual feedback for both multi-file syncs and large single-file catalog copies

## Requirements

- **Python 3.6+** (uses f-strings and `pathlib`)
- **Windows** (paths use drive letters; macOS adaptation possible — see notes below)
- No external dependencies — standard library only

## Setup

### 1. Prepare Your SSD

Format your portable SSD as **exFAT** for cross-platform compatibility (readable by both Windows and macOS). Organize it with separate folders for photos and the catalog:

```
SSD (H:\)
├── Catalog/
│   ├── MyCatalog.lrcat
│   └── MyCatalog.lrcat-data/
└── Photography/
    ├── 2024/
    ├── 2025/
    └── ...
```

Keeping the catalog in its own folder (separate from photos) avoids clutter from Lightroom's auto-generated preview and helper files.

### 2. Configure the Script

Edit the configuration section at the top of `lr_workflow.py`:

```python
# SSD paths (drive letter assigned when SSD is connected)
SSD_PHOTOS = r"H:\Photography"
SSD_CATALOG_DIR = r"H:\Catalog"

# Desktop paths
DESKTOP_PHOTOS = r"E:\Photography"
DESKTOP_CATALOG_DIR = r"C:\Lightroom\Catalog"

# Your catalog filename (without extension)
CATALOG_NAME = "MyCatalog"
```

### 3. Place the Files

Put both `lr_workflow.py` and `lr_workflow.bat` in a convenient location (e.g., `E:\Scripts\`). Double-click the `.bat` file to launch, or run from a terminal.

### 4. First Run

1. Close Lightroom on all machines
2. Connect the SSD
3. Run the script → choose **[1] Arriving Home**
4. Open Lightroom → **File > Open Catalog** → navigate to the desktop catalog path
5. In the **Folders panel**, right-click the top-level folder → **Update Folder Location** → point to your desktop photo archive (e.g., `E:\Photography`)

The folder location remap is a one-time step per machine. Lightroom remembers it for subsequent opens.

## Usage

### Interactive Menu

```
  =====================================================================
  LIGHTROOM WORKFLOW MANAGER
  =====================================================================
  SSD Photos:       H:\Photography
  Desktop Photos:   E:\Photography
  SSD Catalog:      H:\Catalog
  Desktop Catalog:  C:\Lightroom\Catalog
  Catalog Name:     MyCatalog

  SSD Status:       CONNECTED
  SSD Catalog:      2026-02-27 09:15:32  (1.54 GB)
  Desktop Catalog:  2026-02-25 18:42:10  (1.54 GB)

  Options:
    [1] Arriving Home  - Sync files + copy catalog SSD → Desktop
    [2] Leaving Home   - Copy catalog Desktop → SSD
    [3] Sync Files     - Copy photos SSD → Desktop only
    [4] Compare        - Show catalog comparison only
    [Q] Quit
```

### Command-Line Shortcuts

```bash
python lr_workflow.py --arrive       # Arriving home workflow
python lr_workflow.py --leave        # Leaving home workflow
python lr_workflow.py --sync         # Sync files only
python lr_workflow.py --sync --auto  # Sync files, auto-confirm all
```

## How It Works

### Photo Sync Logic

The script scans the SSD photo folder and compares each file against the desktop archive:

- **New files** (exist on SSD but not on desktop) → copied to desktop
- **Modified files** (exist on both but differ in size or timestamp) → prompted to replace, with size and date comparison shown
- **Unchanged files** → skipped
- **Desktop-only files** → left untouched (the script never deletes)

A 2-second timestamp tolerance accounts for exFAT's reduced time resolution compared to NTFS.

### Catalog Copy Logic

Only the essential catalog components are copied:

| File | Purpose | Copied? |
|------|---------|---------|
| `.lrcat` | Main catalog database (SQLite) | ✅ Yes |
| `.lrcat-data/` | Mask and AI edit data (LR Classic 11+) | ✅ Yes |
| `Previews.lrdata/` | Rendered previews (can be 50+ GB) | ❌ Machine-specific, auto-regenerated |
| `Helper.lrdata/` | Face detection and search index | ❌ Machine-specific, auto-regenerated |
| `Smart Previews.lrdata/` | Compressed DNGs for offline editing | ❌ Machine-specific, regenerate as needed |

### What About the Mac Side?

This script handles the Windows desktop side of the workflow. On macOS:

1. Connect the SSD
2. Open Lightroom → **File > Open Catalog** → select the `.lrcat` on the SSD
3. **Update Folder Location** to `/Volumes/[SSD_Name]/Photography` (first time only)
4. Edit normally — all changes are saved directly to the SSD catalog
5. Close Lightroom before disconnecting the SSD

When you return home, the script's **Arriving Home** workflow picks up where the Mac left off.

## Backup Management

Backups accumulate in a `Backups/` subfolder at each catalog location. Each backup is timestamped and includes both the `.lrcat` file and `.lrcat-data` folder. At ~1.5 GB per catalog backup, these can grow quickly — periodically delete older backups you no longer need.

```
C:\Lightroom\Catalog\Backups\
├── MyCatalog_20260220_091500.lrcat
├── MyCatalog_20260220_091500.lrcat-data/
├── MyCatalog_20260225_184200.lrcat
├── MyCatalog_20260225_184200.lrcat-data/
└── ...
```

## License

MIT License — see [LICENSE](LICENSE).
