# Lightroom Workflow Manager

> [!WARNING]
> **DISCLAIMER**: This script is provided **"as-is"** without any warranty of any kind, either express or implied. The author takes **no responsibility** for any damage, data loss, or issues caused by its use. Use it at your own risk and keep independent backups of your catalogs and photos.

Edit with **Adobe Lightroom Classic** on a travel laptop while your desktop PC stays the main workstation. The tool runs on the Windows PC. It needs only Python, with nothing extra to install.

## The idea: check the catalog out, check it back in

A Lightroom Classic catalog can't be shared between computers or synced through cloud folders. This tool treats your catalog like a library book:

- **Leave**: the catalog is copied to a portable SSD and the PC copy is **locked**, so you can't edit it by accident while you're away.
- **On the road**: open the catalog from the SSD on the laptop and work as usual, with Photoshop, plugins and everything else.
- **Arrive**: new and edited photos are copied into your archive, the catalog comes back to the PC, and the SSD copy is retired.

The catalog is only ever editable in one place, so there's nothing to merge and nothing can conflict. Inside Lightroom you don't use any export, import or sync feature. You just open the catalog.

### No manual "Update Folder Location"

Lightroom stores the absolute path of each **top-level** folder only, such as `E:/Photography/`. Everything below it is stored relative to that folder. When copying the catalog, the tool changes that path:

| Direction | Top-level folder becomes |
|---|---|
| Leave (PC to SSD) | `E:/Photography/` becomes `/Volumes/<SSD name>/Photography/` |
| Arrive (SSD to PC) | `/Volumes/<SSD name>/Photography/` becomes `E:/Photography/` |

On the laptop, photos that are on the SSD show as online and the rest of the archive shows as offline, but you can still search and browse it. New imports go under the same folder structure as the archive.

If a trip was imported on the laptop as a separate top-level folder, the tool folds it into the main folder as an ordinary subfolder. It skips this if a folder with the same path already exists there, and reports it.

> [!NOTE]
> This edits Lightroom's catalog database, which Adobe doesn't document. The tool only changes the copy, never your live catalog. It changes only the top-level folder table, inside a single transaction, after an integrity check, and checks integrity again afterwards. A full backup is always taken first.

### Taking archive photos along

When leaving, you can choose archive folders to take along, for example `Models\Ira; Abroad\25-10 Budapest`. They're copied to the same relative place on the SSD, so their originals are online on the laptop: full-size editing, Photoshop, exports. When you're back, anything edited or added in them is copied back to the archive.

## Safety features

- **Lightroom must be closed**: the tool checks both the running program and the catalog's lock and journal files (`.lrcat.lock`, `-wal`, `-shm`, `-journal`) on the PC and on the SSD.
- **One editable copy**: while the catalog is out, the PC copy is renamed to `.lrcat.checked-out`. Once it's back, the SSD copy is renamed to `.lrcat.returned`.
- **Copies are all-or-nothing**: every copy is written to a temporary file and only replaces the original once complete. Catalogs are also checked against a checksum. An interrupted copy never leaves a half-written catalog.
- **State changes last**: photos and catalog are copied before anything is locked or unlocked. If something fails, the catalog stays usable where it was, and you can re-run.
- **Backups, cleaned up automatically**: a full backup of the PC catalog is made before every checkout (keeping the last 5 by default). A catalog already on the SSD is moved to `LRWM-Backups` on the SSD, never deleted.
- **Never overwrites newer files**: if a file differs and the destination copy is newer or the same age, it's left alone and reported.
- **Catches forgotten photos**: Leave warns if the SSD has photos that never made it into the archive.
- **Wrong SSD, or a catalog upgraded on the laptop**: Arrive refuses if the SSD doesn't hold this checkout, or if Lightroom on the laptop upgraded the catalog to a newer version.
- **Dry run**: `--dry-run`, or `D` in the menu, shows the full plan without changing anything.
- **Emergency release**: if the SSD is lost, unlock the PC catalog as it was at checkout.
- **Skips system junk**: macOS `._*` and `.DS_Store` files, Windows `Thumbs.db`, and Lightroom preview caches.

## Requirements

- Python 3.8+ on the Windows PC, with nothing else to install.
- An SSD formatted as **exFAT**, so both Windows and macOS can read and write it.
- **The same Lightroom Classic version on both computers.** A newer version upgrades the catalog, and then the older one can't open it.
- Install your plugins and presets on both machines. They live in each machine's Lightroom settings, not in the catalog.

## Setup

1. Lay out the SSD like this:
   ```
   SSD (named e.g. "TravelSSD")
   ├── Catalog\        ← the tool puts the catalog here
   └── Photography\    ← photos; mirrors the structure of your archive
   ```
2. Copy `config.example.json` to `config.json` and fill it in:

   | Setting | Meaning |
   |---|---|
   | `catalog_name` | Catalog file name without `.lrcat` |
   | `desktop_catalog_dir` | Folder holding the catalog on the PC |
   | `desktop_photos` | The archive's top-level folder, as it appears in Lightroom's Folders panel |
   | `backup_dir` | Where catalog backups and the log go |
   | `ssd_label` | The SSD's volume name. It's found by name whatever drive letter Windows assigns. |
   | `ssd_fallback_drive` | Drive letter to try if no drive has that name |
   | `mac_volume_name` | The SSD's name on the Mac, if different from `ssd_label` |
   | `keep_backups` / `keep_ssd_backups` | How many backups to keep |

   `config.json` is listed in `.gitignore`, so your paths stay private.
3. Run `lr_workflow.bat` (double-click), or `python lr_workflow.py`.

## Usage

```
  [1] Leave        check the catalog out to the SSD (+ take archive folders along)
  [2] Arrive       bring the catalog and new photos back to the PC
  [3] Take along   copy more archive folders to the SSD
  [4] Copy photos  copy new photos from the SSD to the archive only
  [5] Status       details, backups, what's on the SSD
  [D] Dry run      show plans without changing anything
  [R] Emergency release   (only while checked out)
```

Command line:

```bash
python lr_workflow.py --status
python lr_workflow.py --leave --folders "Models\Ira;Abroad\25-10 Budapest"
python lr_workflow.py --arrive
python lr_workflow.py --leave --dry-run
python lr_workflow.py --take "Models\Ivory Flame"     # while the catalog is out, too
python lr_workflow.py --copy-photos                    # (--sync still works)
```

### On the laptop

1. Connect the SSD. In Lightroom Classic, choose **File > Open Catalog** and pick `/Volumes/<SSD>/Catalog/<name>.lrcat`.
2. Import new photos into `/Volumes/<SSD>/Photography/<trip folder>`.
3. Edit, including Photoshop round-trips. The PSDs and TIFFs land next to the originals on the SSD.
4. Quit Lightroom before ejecting the SSD.

## What is copied

| Item | Copied? |
|---|---|
| `.lrcat` (the catalog) | Yes, checked against a checksum |
| `.lrcat-data` (masks and AI edit data) | Yes |
| `Previews.lrdata`, `Helper.lrdata`, `Smart Previews.lrdata` | No. These belong to each machine and Lightroom rebuilds them. |
| Photos | Leave: only the folders you choose. Arrive: everything on the SSD that's new or newer than the archive. |

Nothing is ever deleted from the archive.

## Tests

```bash
python -m unittest discover -s tests
```

## License

MIT License. See [LICENSE](LICENSE).
