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

When leaving, you can take archive folders along. Each one is copied **whole**, with every file and subfolder (raws, TIFFs, PSDs, exports, sidecars), to the same relative place on the SSD. Their originals are then online on the laptop: full-size editing, Photoshop, exports. When you're back, anything edited or added in them is copied back to the archive.

**Choosing folders from Lightroom:** create a regular collection called **Take along** (the name is configurable). Add any photo from a folder you want, and that whole folder is taken. One photo per folder is enough. Collections only list photos, so adding to one never moves files or changes folders. To be safe, add photos with right-click > *Add to Collection*, or set it as the target collection and press `B`. Don't drag photos onto a folder in the Folders panel, which moves them on disk.

At Leave, the tool lists the folders it found, with their sizes:

```
  Folders of the photos in the Lightroom collection 'Take along':
    Models\Ira                                       12.40 GB   (3 photo(s) in collection)
    Abroad\25-10 Budapest                            48.12 GB   (1 photo(s) in collection)

  [Enter] take these folders   [+] take these and add more   [M] choose manually instead   [N] take nothing
```

You can also type folder paths yourself (`?` lists the folders), or pass them with `--folders "Models\Ira;Abroad\25-10 Budapest"`. To stop taking a folder, remove its photos from the collection. That only takes them off the list; the photos themselves aren't touched.

### Keeping the SSD in step with the trip

Photos stay on the SSD between trips; Arrive never removes anything. At each **Leave**, any folders on the SSD that aren't part of this trip are listed, and you're asked whether to remove them (default **No**):

```
  On the SSD but not part of this trip:
      Abroad\24-11 San Francisco                      312 file(s)   12.10 GB safe in archive
      Models\Ivory Flame                              617 file(s)   15.44 GB safe in archive
  Remove the files that are safe in the archive from the SSD? (y/N)
```

A file is only removed if the archive holds the same file, or a newer version of it. Anything else stays on the SSD and is listed. This keeps old versions off the laptop, and stops photos you deleted at home from being copied back by the next Arrive.

### Lightroom presets

Leave and Arrive also carry your Lightroom presets, through a `Presets` folder on the SSD:

| Preset folder (under the Adobe settings folder) | Holds |
|---|---|
| `CameraRaw/Settings` | Develop presets and looks |
| `CameraRaw/ImportedSettings` | Imported presets and looks |
| `Lightroom/Local Adjustment Presets` | Brush and mask presets |

1. **Leave** copies new and changed presets to the SSD and puts `Sync Presets.command` next to them.
2. **On the laptop**, quit Lightroom and run the script (change `TravelSSD` to your SSD's name):
   ```bash
   bash "/Volumes/TravelSSD/Presets/Sync Presets.command"
   ```
   It syncs both ways. Run it again before ejecting if you made presets on the laptop, otherwise they stay there.
3. **Arrive** copies new and changed presets to the PC.

- Newer wins, and identical files are skipped whatever their dates.
- Replaced presets are backed up: on the PC in `PresetBackups` inside `backup_dir`, on the Mac in `~/Library/Application Support/LRWM/PresetBackups`. These backups are not rotated.
- Deletions aren't passed on. Delete a preset on each computer where you don't want it. A preset you deleted isn't restored by the copy on the SSD unless that copy was edited later.
- A renamed preset counts as a new one, so the old name stays on the other computer.
- A problem with presets is reported and never blocks the catalog.
- **Not included:** Nik and other plug-in presets (use export and import inside the plug-in; Nik 7 keeps them in a database), camera and lens profiles, export presets, and preset favorites.
- Keep Lightroom's *Store presets with this catalog* **off**. Presets stored that way are in a folder the tool doesn't copy.

To turn this off, set `"sync_presets": false`.

## Safety features

- **Lightroom must be closed**: the tool checks both the running program and the catalog's lock and journal files (`.lrcat.lock`, `-wal`, `-shm`, `-journal`) on the PC and on the SSD.
- **One editable copy**: while the catalog is out, the PC copy is renamed to `.lrcat.checked-out`. Once it's back, the SSD copy is renamed to `.lrcat.returned`.
- **Copies are all-or-nothing**: every copy is written to a temporary file and only replaces the original once complete. Catalogs are also checked against a checksum. An interrupted copy never leaves a half-written catalog.
- **State changes last**: photos and catalog are copied before anything is locked or unlocked. If something fails, the catalog stays usable where it was, and you can re-run.
- **Backups, cleaned up automatically**: a full backup of the PC catalog is made before every checkout (keeping the last 5 by default). A catalog already on the SSD is moved to `LRWM-Backups` on the SSD, never deleted, and renamed `.lrcat.backup` so the laptop can't open it by mistake.
- **Never overwrites newer files**: if a file differs and the destination copy is newer or the same age, it's left alone and reported.
- **Catches forgotten photos**: Leave warns if the SSD has photos that never made it into the archive.
- **Wrong SSD, or a catalog upgraded on the laptop**: Arrive refuses if the SSD doesn't hold this checkout, or if Lightroom on the laptop upgraded the catalog to a newer version.
- **Deliberate checkouts**: checking out or in needs a typed `y`, so repeated Enter presses can't lock the catalog.
- **Dry run**: `--dry-run`, or `M` then `D` from the start screen, shows the full plan without changing anything.
- **Emergency release**: if the SSD is lost, unlock the PC catalog as it was at checkout.
- **Skips system junk**: macOS `._*` and `.DS_Store` files, Windows `Thumbs.db`, and Lightroom preview caches.

## Requirements

- Python 3.8+ on the Windows PC, with nothing else to install.
- An SSD formatted as **exFAT**, so both Windows and macOS can read and write it.
- **The same Lightroom Classic version on both computers.** A newer version upgrades the catalog, and then the older one can't open it.
- Install your plugins and camera profiles on both machines. The tool carries [Lightroom presets](#lightroom-presets), but not plugins, their presets, or profiles.

## Setup

1. Lay out the SSD like this:
   ```
   SSD (named e.g. "TravelSSD")
   ├── Catalog\        ← the tool puts the catalog here
   ├── Presets\        ← created by the tool at Leave
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
   | `take_along_collection` | Lightroom collection marking folders to take along (default `Take along`) |
   | `sync_presets` | Carry Lightroom presets at Leave and Arrive (default `true`) |
   | `preset_adobe_dir` | Adobe settings folder, if not `%APPDATA%\Adobe` |
   | `keep_backups` / `keep_ssd_backups` | How many backups to keep |

   `config.json` is listed in `.gitignore`, so your paths stay private.
3. Run `lr_workflow.bat` (double-click), or `python lr_workflow.py`.

## Usage

Run the tool. It works out where you are in the trip and suggests one step:

```
  Catalog is AT HOME. SSD connected.

  [Enter] Leave: prepare the SSD for a trip
  [M] More options     [Q] Quit
```

| What it finds | What Enter does |
|---|---|
| Catalog at home | Starts Leave |
| Catalog at home, but the SSD has files the archive doesn't | Starts Copy photos |
| Catalog out, nothing changed since Leave | Quits: you're ready to travel (`A` cancels the trip with Arrive) |
| Catalog out, and the catalog, photos or presets on the SSD changed | Starts Arrive |
| SSD missing, or not the one holding the checkout | Checks again, after saying what to connect |
| Damaged or contradictory checkout records | Nothing, and explains the problem |
| A Leave or Arrive has just finished | Quits |

It's only a suggestion: nothing starts until you press Enter, and checking the catalog out or in needs a typed `y`. Plain Enter at that question cancels.

`M` shows every option:

```
  [1] Leave        check the catalog out to the SSD (+ take archive folders along)
  [2] Arrive       bring the catalog and new photos back to the PC
  [3] Take along   copy more archive folders to the SSD
  [4] Copy photos  copy new photos from the SSD to the archive only
  [5] Status       details, backups, what's on the SSD
  [D] Dry run      show plans without changing anything
  [R] Emergency release   (only while checked out)
  [B] Back
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

1. Connect the SSD and [sync the presets](#lightroom-presets). In Lightroom Classic, choose **File > Open Catalog** and pick `/Volumes/<SSD>/Catalog/<name>.lrcat`.
2. Import new photos into `/Volumes/<SSD>/Photography/<trip folder>`.
3. Edit, including Photoshop round-trips. The PSDs and TIFFs land next to the originals on the SSD.
4. Quit Lightroom before ejecting the SSD. If you made presets, run the preset sync again first.

## What is copied

| Item | Copied? |
|---|---|
| `.lrcat` (the catalog) | Yes, checked against a checksum |
| `.lrcat-data` (masks and AI edit data) | Yes |
| `Previews.lrdata`, `Helper.lrdata`, `Smart Previews.lrdata` | No. These belong to each machine and Lightroom rebuilds them. |
| Lightroom presets | Yes, new and changed ones. See [Lightroom presets](#lightroom-presets). |
| Photos | Leave: only the folders you choose. Arrive: everything on the SSD that's new or newer than the archive. |

Nothing is ever deleted from the archive.

## Tests

```bash
python -m unittest discover -s tests
```

## License

MIT License. See [LICENSE](LICENSE).
