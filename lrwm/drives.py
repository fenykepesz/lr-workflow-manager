"""Locating the portable SSD."""

import os
from typing import Optional

from .config import Config


def find_drive_by_label(label: str) -> Optional[str]:
    """Return the root (e.g. 'G:\\') of the volume with this label, or None. Windows only."""
    if not label or os.name != "nt":
        return None
    try:
        import ctypes
        import string
        kernel32 = ctypes.windll.kernel32
        bitmask = kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if not bitmask & (1 << i):
                continue
            root = f"{letter}:\\"
            buf = ctypes.create_unicode_buffer(261)
            if kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), buf, 261,
                                              None, None, None, None, 0):
                if buf.value.lower() == label.lower():
                    return root
    except Exception:
        pass
    return None


def ssd_root(cfg: Config) -> Optional[str]:
    """Root folder of the connected SSD, or None if it isn't connected."""
    if cfg.ssd_root:
        return cfg.ssd_root if os.path.isdir(cfg.ssd_root) else None
    root = find_drive_by_label(cfg.ssd_label)
    if root:
        return root
    if cfg.ssd_fallback_drive:
        fb = cfg.ssd_fallback_drive.rstrip("\\/") + "\\"
        if os.path.isdir(os.path.join(fb, cfg.ssd_photos_subdir)):
            return fb
    return None


class SSD:
    """Paths on a connected SSD."""

    def __init__(self, cfg: Config, root: str):
        self.root = root
        self.photos = os.path.join(root, cfg.ssd_photos_subdir)
        self.catalog_dir = os.path.join(root, cfg.ssd_catalog_subdir)
        self.lrcat = os.path.join(self.catalog_dir, cfg.catalog_name + ".lrcat")
        self.backup_dir = os.path.join(self.catalog_dir, "LRWM-Backups")


def connect(cfg: Config) -> Optional[SSD]:
    root = ssd_root(cfg)
    return SSD(cfg, root) if root else None
