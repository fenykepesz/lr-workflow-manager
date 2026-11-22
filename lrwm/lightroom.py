"""Everything that touches Lightroom: open/lock detection, catalog metadata,
and re-pointing a catalog's top-level folders between the PC and the Mac.

Background on folder paths: a catalog stores only its *top-level* folders
with absolute paths (table AgLibraryRootFolder, e.g. 'E:/Photography/').
Every subfolder (AgLibraryFolder) is stored relative to its top-level
folder, and every photo file relative to its folder. So moving a catalog
between machines only needs a few rows in AgLibraryRootFolder changed.

Writes happen only on a temporary *copy* of a catalog, inside one
transaction, followed by an SQLite integrity check. The live catalog is
never opened for writing.
"""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

LOCK_SUFFIXES = (".lrcat.lock", ".lrcat-journal", ".lrcat-wal", ".lrcat-shm", ".lrcat-lock")


class CatalogError(Exception):
    pass


# ---------------------------------------------------------------- open checks

def lock_files(catalog_dir: str, name: str) -> List[str]:
    """Lock/journal files Lightroom keeps next to an open catalog."""
    return [p for p in (os.path.join(catalog_dir, name + s) for s in LOCK_SUFFIXES)
            if os.path.exists(p)]


def lightroom_running() -> Optional[bool]:
    """True/False if Lightroom Classic is running on this computer; None if unknown."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Lightroom.exe", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=20).stdout
            return "lightroom.exe" in out.lower()
        if sys.platform == "darwin":
            r = subprocess.run(["pgrep", "-f", "Adobe Lightroom Classic"],
                               capture_output=True, timeout=20)
            return r.returncode == 0
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- metadata

def _ro(path: str) -> sqlite3.Connection:
    """Strictly read-only connection that creates no side files.

    Lightroom catalogs use SQLite WAL mode, where even a plain read-only open
    leaves '-wal'/'-shm' files behind, which look exactly like an open catalog.
    'immutable=1' avoids that. It is only used after checking that Lightroom
    is closed (no -wal file), so there is no pending data it could miss.
    """
    uri = "file:" + path.replace("\\", "/").replace("?", "%3f").replace("#", "%23")
    return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)


def fingerprint(path: str) -> dict:
    st = os.stat(path)
    return {"size": st.st_size, "mtime": round(st.st_mtime, 3)}


def same_fingerprint(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a and b and a["size"] == b["size"] and abs(a["mtime"] - b["mtime"]) <= 2)


def db_version(path: str) -> Optional[str]:
    try:
        with closing(_ro(path)) as c:
            row = c.execute(
                "select value from Adobe_variablesTable where name='Adobe_DBVersion'").fetchone()
            return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def root_folders(path: str) -> List[Tuple[int, str]]:
    with closing(_ro(path)) as c:
        return c.execute("select id_local, absolutePath from AgLibraryRootFolder "
                         "order by absolutePath").fetchall()


@dataclass
class CollectionFolders:
    found: bool = False                  # a regular collection with that name exists
    smart_only: bool = False             # only a smart collection has that name
    folders: List[Tuple[str, int]] = field(default_factory=list)  # (folder path under root, photo count)
    outside: List[Tuple[str, int]] = field(default_factory=list)  # (folder path, count) not under the archive


def collection_folders(lrcat: str, collection: str, archive_root: str) -> CollectionFolders:
    """Folders (relative to archive_root, e.g. 'Models/Ira') of the photos in a
    regular collection. Folders inside another listed folder are dropped,
    since copying a folder includes its subfolders."""
    result = CollectionFolders()
    with closing(_ro(lrcat)) as c:
        kinds = [k for (k,) in c.execute(
            "select creationId from AgLibraryCollection where lower(name)=lower(?)", (collection,))]
        result.found = "com.adobe.ag.library.collection" in kinds
        result.smart_only = bool(kinds) and not result.found
        if not result.found:
            return result
        rows = c.execute("""
            select r.absolutePath, fo.pathFromRoot, count(distinct i.id_local)
            from AgLibraryCollection col
            join AgLibraryCollectionImage ci on ci.collection = col.id_local
            join Adobe_images i on i.id_local = ci.image
            join AgLibraryFile f on f.id_local = i.rootFile
            join AgLibraryFolder fo on fo.id_local = f.folder
            join AgLibraryRootFolder r on r.id_local = fo.rootFolder
            where lower(col.name) = lower(?) and col.creationId = 'com.adobe.ag.library.collection'
            group by r.absolutePath, fo.pathFromRoot""", (collection,)).fetchall()
    inside = {}
    for root, sub, n in rows:
        full = root + sub
        if _starts(full, archive_root):
            rel = full[len(archive_root):].strip("/")
            inside[rel] = inside.get(rel, 0) + n
        else:
            result.outside.append((full, n))
    for rel in sorted(inside, key=str.casefold):
        parents = [p for p in inside if p and p != rel and
                   (rel.casefold() + "/").startswith(p.casefold() + "/")]
        if rel and not parents:
            total = inside[rel] + sum(n for q, n in inside.items()
                                      if q != rel and q.casefold().startswith(rel.casefold() + "/"))
            result.folders.append((rel, total))
        elif not rel:
            result.outside.append((archive_root + " (photos directly in the archive's top folder)", inside[rel]))
    return result


# ---------------------------------------------------------------- re-pointing

def _starts(path: str, prefix: str) -> bool:
    return path.casefold().startswith(prefix.casefold())


@dataclass
class RetargetPlan:
    renames: List[Tuple[int, str, str]] = field(default_factory=list)  # (root id, old path, new path)
    merges: List[Tuple[int, str, int, str, int]] = field(default_factory=list)
    # (nested root id, its new path, main root id, sub path under main, folder count)
    skipped: List[Tuple[str, List[str]]] = field(default_factory=list)  # (root path, clashing folders)

    @property
    def empty(self) -> bool:
        return not (self.renames or self.merges)


def plan_retarget(conn: sqlite3.Connection, old_prefix: str, new_prefix: str) -> RetargetPlan:
    """Work out how to move top-level folders from old_prefix to new_prefix.

    1. Every top-level folder under old_prefix is renamed to new_prefix.
    2. Top-level folders that end up *inside* the main folder (new_prefix
       itself), e.g. a trip folder imported separately on the Mac, are folded
       into the main folder as ordinary subfolders, unless a subfolder with
       the same path already exists (then it is left alone and reported).
    """
    plan = RetargetPlan()
    roots = conn.execute("select id_local, absolutePath from AgLibraryRootFolder").fetchall()
    final = {}
    for rid, path in roots:
        if _starts(path, old_prefix):
            new = new_prefix + path[len(old_prefix):]
            plan.renames.append((rid, path, new))
            final[rid] = new
        else:
            final[rid] = path

    seen = {}
    for rid, path in final.items():
        key = path.casefold()
        if key in seen:
            raise CatalogError(
                f"Two top-level folders would both point at '{path}'. "
                f"Fix this in Lightroom (Folders panel) before continuing.")
        seen[key] = rid

    main = seen.get(new_prefix.casefold())
    if main is None:
        return plan
    existing = {p.casefold() for (p,) in conn.execute(
        "select pathFromRoot from AgLibraryFolder where rootFolder=?", (main,))}
    for rid, path in final.items():
        if rid == main or not _starts(path, new_prefix):
            continue
        sub = path[len(new_prefix):]
        folders = [p for (p,) in conn.execute(
            "select pathFromRoot from AgLibraryFolder where rootFolder=?", (rid,))]
        clashes = [sub + p for p in folders if (sub + p).casefold() in existing]
        if clashes:
            plan.skipped.append((path, clashes))
        else:
            plan.merges.append((rid, path, main, sub, len(folders)))
    return plan


def apply_retarget(conn: sqlite3.Connection, plan: RetargetPlan) -> None:
    merged = {m[0] for m in plan.merges}
    conn.execute("begin immediate")
    try:
        for rid, _, new in plan.renames:
            if rid not in merged:
                conn.execute("update AgLibraryRootFolder set absolutePath=?, "
                             "relativePathFromCatalog=NULL where id_local=?", (new, rid))
        for rid, _, main, sub, _ in plan.merges:
            conn.execute("update AgLibraryFolder set rootFolder=?, pathFromRoot=? || pathFromRoot "
                         "where rootFolder=?", (main, sub, rid))
            conn.execute("delete from AgLibraryRootFolder where id_local=?", (rid,))
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        raise


def integrity_ok(conn: sqlite3.Connection) -> bool:
    return conn.execute("pragma quick_check").fetchone()[0] == "ok"


def retarget_catalog(path: str, old_prefix: str, new_prefix: str) -> RetargetPlan:
    """Re-point the catalog file at `path` (must be a copy, Lightroom closed)."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        if not integrity_ok(conn):
            raise CatalogError(f"Catalog copy failed the integrity check before changes: {path}")
        plan = plan_retarget(conn, old_prefix, new_prefix)
        if not plan.empty:
            apply_retarget(conn, plan)
            if not integrity_ok(conn):
                raise CatalogError("Catalog failed the integrity check after re-pointing folders.")
        return plan
    finally:
        conn.close()


# ------------------------------------------------- what belongs to the PC

# Catalog variables that only make sense on the PC. The laptop's values must not come home.
PC_ONLY_VARIABLES = ("AgLibrary_BackupPathToFolder", "AgMetadata_autoWriteXmpBlocked")


@dataclass
class PCOnlyPlan:
    previews: List[tuple] = field(default_factory=list)   # (id_local, fileUUID, status, statusDateTime)
    variables: List[tuple] = field(default_factory=list)  # (name, PC value, PC has it)

    @property
    def empty(self) -> bool:
        return not (self.previews or self.variables)

    def describe(self) -> List[str]:
        lines = []
        if self.previews:
            lines.append(f"Keep the PC's {len(self.previews)} Smart Preview record(s)")
        if self.variables:
            lines.append(f"Keep {len(self.variables)} PC-only catalog setting(s), e.g. the backup folder")
        return lines


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone())


def plan_pc_only(conn: sqlite3.Connection, pc: sqlite3.Connection, smart_previews: str) -> PCOnlyPlan:
    """What to carry over from the PC's catalog into the one coming home.

    Lightroom on the laptop deletes the records of Smart Previews it can't find,
    and those files stay on the PC. A record is put back if its file is still
    there and its photo is still in the catalog.
    """
    plan = PCOnlyPlan()
    table = "AgDNGProxyInfo"
    if os.path.isdir(smart_previews) and _has_table(conn, table) and _has_table(pc, table):
        on_disk = {os.path.splitext(f)[0].upper()
                   for _, _, files in os.walk(smart_previews) for f in files}
        sql = f"select id_local, fileUUID, status, statusDateTime from {table}"
        now = {(row[1], row[2]) for row in conn.execute(sql)}
        photos = {u for (u,) in conn.execute("select id_global from AgLibraryFile")}
        plan.previews = [row for row in pc.execute(sql)
                         if row[1].upper() in on_disk and row[1] in photos and (row[1], row[2]) not in now]
    for name in PC_ONLY_VARIABLES:
        sql = "select value from Adobe_variablesTable where name=?"
        ours, theirs = pc.execute(sql, (name,)).fetchone(), conn.execute(sql, (name,)).fetchone()
        if theirs is not None and ours != theirs:
            plan.variables.append((name, ours[0] if ours else None, ours is not None))
    return plan


def preview_pc_only(path: str, pc_catalog: str, smart_previews: str) -> PCOnlyPlan:
    with closing(_ro(path)) as conn, closing(_ro(pc_catalog)) as pc:
        return plan_pc_only(conn, pc, smart_previews)


def restore_pc_only(path: str, pc_catalog: str, smart_previews: str) -> PCOnlyPlan:
    """Apply plan_pc_only to the catalog file at `path` (must be a copy, Lightroom closed)."""
    with closing(sqlite3.connect(path, isolation_level=None)) as conn, closing(_ro(pc_catalog)) as pc:
        plan = plan_pc_only(conn, pc, smart_previews)
        if plan.empty:
            return plan
        conn.execute("begin immediate")
        try:
            for row in plan.previews:
                conn.execute("delete from AgDNGProxyInfo where fileUUID=?", (row[1],))
                taken = conn.execute("select 1 from AgDNGProxyInfo where id_local=?", (row[0],)).fetchone()
                if taken:
                    raise CatalogError("A Smart Preview record's ID is used by another photo in the returning catalog.")
                conn.execute("insert into AgDNGProxyInfo (id_local, fileUUID, status, statusDateTime) "
                             "values (?,?,?,?)", row)
            for name, value, present in plan.variables:
                if present:
                    conn.execute("update Adobe_variablesTable set value=? where name=?", (value, name))
                else:
                    conn.execute("delete from Adobe_variablesTable where name=?", (name,))
            conn.execute("commit")
        except BaseException:
            conn.execute("rollback")
            raise
        if not integrity_ok(conn):
            raise CatalogError("Catalog failed the integrity check after restoring the PC's records.")
        return plan


def describe_plan(plan: RetargetPlan) -> List[str]:
    lines = []
    for _, old, new in plan.renames:
        if not any(m[1] == new for m in plan.merges):
            lines.append(f"Top-level folder: {old}  ->  {new}")
    for _, path, _, sub, n in plan.merges:
        lines.append(f"Folded into main folder as '{sub}': {path} ({n} folder(s))")
    for path, clashes in plan.skipped:
        lines.append(f"Left as separate top-level folder (already exists inside main folder): "
                     f"{path} [{', '.join(clashes[:3])}]")
    return lines
