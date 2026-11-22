"""Loading and validating the user's settings (config.json)."""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional


class ConfigError(Exception):
    pass


REQUIRED = ["catalog_name", "desktop_catalog_dir", "desktop_photos", "backup_dir"]


@dataclass
class Config:
    catalog_name: str
    desktop_catalog_dir: str
    desktop_photos: str
    backup_dir: str
    ssd_label: str = ""
    ssd_fallback_drive: str = ""
    ssd_root: str = ""                  # explicit SSD root path; overrides label detection
    ssd_photos_subdir: str = "Photography"
    ssd_catalog_subdir: str = "Catalog"
    mac_volume_name: str = ""           # defaults to ssd_label
    take_along_collection: str = "Take along"  # Lightroom collection marking folders to take
    sync_presets: bool = True           # carry Lightroom presets at Leave and Arrive
    preset_adobe_dir: str = ""          # defaults to %APPDATA%\Adobe
    keep_backups: int = 5
    keep_ssd_backups: int = 2
    extra_skip_names: List[str] = field(default_factory=list)

    # ---- derived values ----
    @property
    def desktop_lrcat(self) -> str:
        return os.path.join(self.desktop_catalog_dir, self.catalog_name + ".lrcat")

    @property
    def desktop_root_lr(self) -> str:
        """The photo archive path the way Lightroom stores it on Windows: 'E:/Photography/'."""
        return to_lr_path(self.desktop_photos)

    @property
    def adobe_dir(self) -> str:
        """The folder holding Lightroom's and Camera Raw's preset folders, or '' if unknown."""
        if self.preset_adobe_dir:
            return self.preset_adobe_dir
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "Adobe") if appdata else ""

    @property
    def mac_root_lr(self) -> str:
        """Where the SSD photo folder appears on the Mac: '/Volumes/TravelSSD/Photography/'."""
        volume = self.mac_volume_name or self.ssd_label
        return f"/Volumes/{volume}/{self.ssd_photos_subdir.strip('/')}/"


def to_lr_path(path: str) -> str:
    p = path.replace("\\", "/")
    return p if p.endswith("/") else p + "/"


def default_config_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "config.json")


def load_config(path: Optional[str] = None) -> Config:
    path = path or default_config_path()
    if not os.path.exists(path):
        raise ConfigError(
            f"Settings file not found: {path}\n"
            f"Copy config.example.json to config.json and fill in your paths."
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")

    data = {k: v for k, v in data.items() if not k.startswith("_")}  # allow "_comment" keys
    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise ConfigError(f"{path} is missing: {', '.join(missing)}")
    known = set(Config.__dataclass_fields__)
    unknown = [k for k in data if k not in known]
    if unknown:
        raise ConfigError(f"{path} has unknown settings: {', '.join(unknown)}")
    cfg = Config(**data)
    if not (cfg.ssd_label or cfg.ssd_fallback_drive or cfg.ssd_root):
        raise ConfigError(f"{path}: set ssd_label, ssd_fallback_drive or ssd_root.")
    if not (cfg.mac_volume_name or cfg.ssd_label):
        raise ConfigError(f"{path}: set mac_volume_name (the SSD's name as the Mac shows it).")
    return cfg
