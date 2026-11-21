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


def compare_trees(src: str, dst: str, extra_skip: Iterable[str] = ()) -> Comparison:
    """Classify files under src against dst. Never looks at files only in dst."""
    result = Comparison()
    if not os.path.isdir(src):
        return result
    for rel, size, mtime in walk_files(src, extra_skip):
        try:
            st = os.stat(os.path.join(dst, rel))
        except FileNotFoundError:
            result.new.append(FileItem(rel, size, mtime))
            continue
        item = FileItem(rel, size, mtime, st.st_size, st.st_mtime)
        if mtime > st.st_mtime + TIMESTAMP_TOLERANCE:
            result.updated.append(item)
        elif size == st.st_size:
            result.same += 1
        else:
            result.conflicts.append(item)
    return result


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


def copy_files(items: List[FileItem], src_root: str, dst_root: str, label: str) -> List[Tuple[str, str]]:
    """Copy a list of compared files. Returns [(rel, error)] for failures."""
    errors = []
    progress = Progress(label, sum(f.size for f in items))
    for f in items:
        try:
            copy_file_atomic(os.path.join(src_root, f.rel), os.path.join(dst_root, f.rel),
                             progress=progress)
        except Exception as e:  # keep going; report at the end
            errors.append((f.rel, str(e)))
    progress.finish()
    return errors


def free_space(path: str) -> int:
    return shutil.disk_usage(path).free
