"""File operations: scanning, comparing and copying with all-or-nothing replacement.

Every copy goes to '<dst>.lrwm-tmp' first and only replaces the destination
once the copy is complete (and, for catalogs, verified by checksum). An
interrupted copy therefore never leaves a half-written file behind.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Tuple

from .ui import Progress

TMP_SUFFIX = ".lrwm-tmp"
OLD_SUFFIX = ".lrwm-old"
TIMESTAMP_TOLERANCE = 2.0  # exFAT stores modification times in 2-second steps
CHUNK = 4 * 1024 * 1024

SKIP_FILE_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", ".dropbox", ".dropbox.attr"}
SKIP_FILE_SUFFIXES = (".lrcat", ".lrcat-journal", ".lrcat-wal", ".lrcat-shm",
                      ".lrcat.lock", ".lrcat-lock", TMP_SUFFIX, OLD_SUFFIX)
SKIP_DIR_NAMES = {"$recycle.bin", "system volume information", ".spotlight-v100",
                  ".fseventsd", ".trashes", ".temporaryitems"}
SKIP_DIR_SUFFIXES = (".lrdata", ".lrcat-data", TMP_SUFFIX, OLD_SUFFIX)


def skip_file(name: str, extra: Iterable[str] = ()) -> bool:
    low = name.lower()
    return (low in SKIP_FILE_NAMES or low in extra or name.startswith("._")
            or low.endswith(SKIP_FILE_SUFFIXES))


def skip_dir(name: str) -> bool:
    low = name.lower()
    return low in SKIP_DIR_NAMES or low.endswith(SKIP_DIR_SUFFIXES)


def walk_files(root: str, extra_skip: Iterable[str] = ()) -> Iterable[Tuple[str, int, float]]:
    """Yield (relative_path, size, mtime) for every photo-relevant file under root."""
    extra = {e.lower() for e in extra_skip}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if not skip_dir(e.name):
                    stack.append(e.path)
            elif e.is_file(follow_symlinks=False) and not skip_file(e.name, extra):
                st = e.stat()
                yield os.path.relpath(e.path, root), st.st_size, st.st_mtime


@dataclass
class FileItem:
    rel: str
    size: int
    src_mtime: float
    dst_size: Optional[int] = None
    dst_mtime: Optional[float] = None


@dataclass
class Comparison:
    new: List[FileItem] = field(default_factory=list)        # missing at destination
    updated: List[FileItem] = field(default_factory=list)    # source is newer
    conflicts: List[FileItem] = field(default_factory=list)  # destination newer, or same time but different size
    same: int = 0

    @property
    def to_copy(self) -> List[FileItem]:
        return self.new + self.updated

    @property
    def bytes_to_copy(self) -> int:
        return sum(f.size for f in self.to_copy)

    @property
    def bytes_replaced(self) -> int:
        return sum(f.dst_size or 0 for f in self.updated)


def compare_trees(src: str, dst: str, extra_skip: Iterable[str] = ()) -> Comparison:
    """Classify files under src against dst. Never looks at files only in dst."""
    result = Comparison()
    if not os.path.isdir(src):
        return result
    for rel, size, mtime in walk_files(src, extra_skip):
        _classify(result, rel, size, mtime, os.path.join(dst, rel))
    return result


def _classify(result: Comparison, rel: str, size: int, mtime: float, target: str) -> None:
    try:
        st = os.stat(target)
    except FileNotFoundError:
        result.new.append(FileItem(rel, size, mtime))
        return
    item = FileItem(rel, size, mtime, st.st_size, st.st_mtime)
    if mtime > st.st_mtime + TIMESTAMP_TOLERANCE:
        result.updated.append(item)
    elif size == st.st_size:
        result.same += 1
    else:
        result.conflicts.append(item)


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def sha256_file(path: str, progress: Optional[Progress] = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            if progress:
                progress.add(len(buf))
    return h.hexdigest()


def copy_file_atomic(src: str, dst: str, *, verify: bool = False,
                     progress: Optional[Progress] = None,
                     before_commit: Optional[Callable[[str], None]] = None) -> None:
    """Copy src to dst via a temporary file, preserving timestamps.

    verify: re-read the copy and compare SHA-256 with the source.
    before_commit: called with the temp path after copying/verifying and
        before it replaces dst (used to adjust catalog paths). If it raises,
        dst is left untouched.
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst + TMP_SUFFIX
    _remove(tmp)
    try:
        h = hashlib.sha256()
        with open(src, "rb") as fs, open(tmp, "wb") as fd:
            while True:
                buf = fs.read(CHUNK)
                if not buf:
                    break
                fd.write(buf)
                if verify:
                    h.update(buf)
                if progress:
                    progress.add(len(buf))
            fd.flush()
            os.fsync(fd.fileno())
        shutil.copystat(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise IOError(f"Size mismatch after copying {src}")
        if verify and sha256_file(tmp) != h.hexdigest():
            raise IOError(f"Checksum mismatch after copying {src}")
        if before_commit:
            before_commit(tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            _remove(tmp)
        except OSError:
            pass
        raise


def tree_size(path: str) -> int:
    return sum(size for _, size, _ in _walk_all(path))


def _walk_all(root: str) -> Iterable[Tuple[str, int, float]]:
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            st = os.stat(p)
            yield os.path.relpath(p, root), st.st_size, st.st_mtime


def tree_fingerprint(path: str) -> Optional[dict]:
    if not os.path.isdir(path):
        return None
    n = size = 0
    latest = 0.0
    for _, s, m in _walk_all(path):
        n, size, latest = n + 1, size + s, max(latest, m)
    return {"files": n, "size": size, "mtime": round(latest, 3)}


def copy_dir_atomic(src: str, dst: str) -> None:
    """Replace directory dst with a full copy of src, all-or-nothing."""
    tmp, old = dst + TMP_SUFFIX, dst + OLD_SUFFIX
    _remove(tmp)
    try:
        shutil.copytree(src, tmp, copy_function=shutil.copy2)
        expected = {rel: size for rel, size, _ in _walk_all(src)}
        actual = {rel: size for rel, size, _ in _walk_all(tmp)}
        if expected != actual:
            raise IOError(f"Copy of {src} is incomplete")
    except BaseException:
        try:
            _remove(tmp)
        except OSError:
            pass
        raise
    _remove(old)
    if os.path.exists(dst):
        os.rename(dst, old)
    os.rename(tmp, dst)
    _remove(old)


def same_content(a: str, b: str) -> bool:
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            buf = fa.read(CHUNK)
            if buf != fb.read(CHUNK):
                return False
            if not buf:
                return True


def copy_files(items: List[FileItem], src_root: str, dst_root: str, label: str,
               backup_root: str) -> List[Tuple[str, str]]:
    """Copy a list of compared files. Returns [(rel, error)] for failures.

    A destination file with other content is first copied to backup_root. If
    that fails, the file isn't replaced.
    """
    errors = []
    progress = Progress(label, sum(f.size for f in items))
    for f in items:
        try:
            src, dst = os.path.join(src_root, f.rel), os.path.join(dst_root, f.rel)
            # Looked at now, not when planning: the file may have appeared since.
            if os.path.exists(dst) and not same_content(src, dst):
                copy_file_atomic(dst, os.path.join(backup_root, f.rel), verify=True)
            copy_file_atomic(src, dst, progress=progress)
        except Exception as e:  # keep going; report at the end
            errors.append((f.rel, str(e)))
    progress.finish()
    return errors


def plan_refresh(ssd_photos: str, archive: str, taken: List[str],
                 extra_skip: Iterable[str] = ()) -> Tuple[Comparison, List[str]]:
    """What folders already on the SSD, other than the `taken` ones, lack from the archive.

    Only folders that hold files on the SSD, and only the files directly in
    them, so a loose file in 'Models' doesn't pull in every folder under it.
    Returns (archive compared to SSD, their subfolders that aren't on the SSD).
    """
    extra = {e.lower() for e in extra_skip}
    taken = [os.path.normpath(t) for t in taken]
    result, absent = Comparison(), []
    here = {os.path.dirname(rel) for rel, _, _ in walk_files(ssd_photos, extra)}
    for folder in sorted(here, key=str.casefold):
        source = os.path.join(archive, folder)
        if any(_is_under(folder, t) for t in taken) or not os.path.isdir(source):
            continue
        with os.scandir(source) as entries:
            for e in entries:
                rel = os.path.join(folder, e.name)
                target = os.path.join(ssd_photos, rel)
                if e.is_dir(follow_symlinks=False):
                    if (not skip_dir(e.name) and not os.path.isdir(target)
                            and not any(_is_under(rel, t) or _is_under(t, rel) for t in taken)
                            and next(iter(walk_files(e.path, extra)), None)):
                        absent.append(rel)
                elif e.is_file(follow_symlinks=False) and not skip_file(e.name, extra):
                    st = e.stat()
                    _classify(result, rel, st.st_size, st.st_mtime, target)
    return result, sorted(absent, key=str.casefold)


def free_space(path: str) -> int:
    return shutil.disk_usage(path).free


# ---------------------------------------------------------------- SSD cleanup

@dataclass
class CleanupGroup:
    """Files on the SSD under one folder that is not part of the current trip."""
    folder: str                       # archive-relative folder shown to the user
    files_only: bool = False          # True: loose files in a parent of a kept folder; keep the folder
    safe: List[str] = field(default_factory=list)          # rel paths safely in the archive
    safe_bytes: int = 0
    unsafe: List[FileItem] = field(default_factory=list)   # kept: not (or not identically) in the archive


def _is_under(rel: str, folder: str) -> bool:
    r, f = rel.casefold(), folder.casefold()
    return r == f or r.startswith(f + os.sep)


def safe_in_archive(archive: str, rel: str, size: int, mtime: float) -> bool:
    """True if the archive holds this file identically, or a newer version of it."""
    try:
        st = os.stat(os.path.join(archive, rel))
    except FileNotFoundError:
        return False
    if st.st_size == size and abs(st.st_mtime - mtime) <= TIMESTAMP_TOLERANCE:
        return True
    return st.st_mtime > mtime + TIMESTAMP_TOLERANCE


def plan_cleanup(ssd_photos: str, archive: str, keep: List[str],
                 extra_skip: Iterable[str] = ()) -> List[CleanupGroup]:
    """Group SSD files that are outside the `keep` folders, and mark which are safe to remove.

    Groups are the highest folders that contain no kept folder, e.g. keeping
    'Models\\Ira' groups 'Abroad' and 'Models\\Ivory Flame' separately.
    """
    keep = [os.path.normpath(k) for k in keep]
    groups = {}
    for rel, size, mtime in walk_files(ssd_photos, extra_skip):
        if any(_is_under(rel, k) for k in keep):
            continue
        parts = rel.split(os.sep)[:-1]
        group = None
        for i in range(1, len(parts) + 1):
            prefix = os.sep.join(parts[:i])
            if not any(_is_under(k, prefix) for k in keep):
                group = prefix
                break
        key = group or (os.path.dirname(rel) or ".")
        g = groups.setdefault(key, CleanupGroup(key, files_only=group is None))
        if safe_in_archive(archive, rel, size, mtime):
            g.safe.append(rel)
            g.safe_bytes += size
        else:
            try:
                st = os.stat(os.path.join(archive, rel))
                g.unsafe.append(FileItem(rel, size, mtime, st.st_size, st.st_mtime))
            except FileNotFoundError:
                g.unsafe.append(FileItem(rel, size, mtime))
    return [groups[k] for k in sorted(groups, key=str.casefold)]


def remove_cleanup(ssd_photos: str, groups: List[CleanupGroup]) -> List[Tuple[str, str]]:
    """Delete the safe files, then leftover system junk and empty folders. Returns errors."""
    errors = []
    for g in groups:
        for rel in g.safe:
            try:
                os.remove(os.path.join(ssd_photos, rel))
            except OSError as e:
                errors.append((rel, str(e)))
        if g.files_only:
            continue
        base = os.path.join(ssd_photos, g.folder)
        for dirpath, _, files in os.walk(base, topdown=False):
            for f in files:
                p = os.path.join(dirpath, f)
                orphan_appledouble = f.startswith("._") and not os.path.exists(os.path.join(dirpath, f[2:]))
                if orphan_appledouble:
                    _try_remove(p)
            remaining = os.listdir(dirpath)
            if all(n.lower() in SKIP_FILE_NAMES for n in remaining):
                for n in remaining:
                    _try_remove(os.path.join(dirpath, n))
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    return errors


def _try_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
