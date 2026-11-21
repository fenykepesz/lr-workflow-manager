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
