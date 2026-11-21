"""Tests for the Lightroom Workflow Manager. Run:  python -m unittest discover -s tests"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lrwm import fsops, state, ui, workflows  # noqa: E402
from lrwm import lightroom as lr  # noqa: E402
from lrwm.config import Config, load_config, ConfigError  # noqa: E402

WIN_ROOT = "E:/Photography/"
MAC_ROOT = "/Volumes/TravelSSD/Photography/"


def make_catalog(path, roots):
    """roots: {absolutePath: [pathFromRoot, ...]} -> creates a minimal catalog."""
    with closing(sqlite3.connect(path)) as c:
        c.executescript("""
            CREATE TABLE AgLibraryRootFolder (id_local INTEGER PRIMARY KEY, id_global UNIQUE NOT NULL,
                absolutePath UNIQUE NOT NULL DEFAULT '', name NOT NULL DEFAULT '', relativePathFromCatalog);
            CREATE TABLE AgLibraryFolder (id_local INTEGER PRIMARY KEY, id_global UNIQUE NOT NULL,
                parentId INTEGER, pathFromRoot NOT NULL DEFAULT '', rootFolder INTEGER NOT NULL DEFAULT 0,
                visibility INTEGER);
            CREATE UNIQUE INDEX index_AgLibraryFolder_rootFolderAndPath ON AgLibraryFolder(rootFolder, pathFromRoot);
            CREATE TABLE Adobe_variablesTable (id_local INTEGER PRIMARY KEY, id_global UNIQUE NOT NULL,
                name, type, value NOT NULL DEFAULT '');
            INSERT INTO Adobe_variablesTable VALUES (1, 'v', 'Adobe_DBVersion', NULL, '1504001');
        """)
        for absolute, folders in roots.items():
            rid = c.execute("insert into AgLibraryRootFolder (id_global, absolutePath, name) values (?,?,?)",
                            (uuid.uuid4().hex, absolute, absolute.rstrip("/").split("/")[-1])).lastrowid
            for f in folders:
                c.execute("insert into AgLibraryFolder (id_global, pathFromRoot, rootFolder) values (?,?,?)",
                          (uuid.uuid4().hex, f, rid))
        c.commit()


def folders_by_root(path):
    with closing(sqlite3.connect(path)) as c:
        out = {}
        for root, folder in c.execute(
                "select r.absolutePath, f.pathFromRoot from AgLibraryFolder f "
                "join AgLibraryRootFolder r on r.id_local=f.rootFolder"):
            out.setdefault(root, set()).add(folder)
        return out


def write(path, content=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime:
        os.utime(path, (mtime, mtime))


class RetargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cat = os.path.join(self.tmp.name, "c.lrcat")

    def tearDown(self):
        self.tmp.cleanup()

    def test_leave_renames_main_and_folds_nested_mac_root(self):
        make_catalog(self.cat, {
            WIN_ROOT: ["", "Abroad/", "Abroad/24-11 SF/"],
            "C:/Users/me/Pictures/Studio/": [""],
            MAC_ROOT + "Abroad/25-01 Bangkok/": ["", "Fuji/"],
        })
        plan = lr.retarget_catalog(self.cat, WIN_ROOT, MAC_ROOT)
        self.assertEqual(len(plan.renames), 1)
        self.assertEqual(len(plan.merges), 1)
        got = folders_by_root(self.cat)
        self.assertEqual(set(got), {MAC_ROOT, "C:/Users/me/Pictures/Studio/"})
        self.assertEqual(got[MAC_ROOT], {"", "Abroad/", "Abroad/24-11 SF/",
                                         "Abroad/25-01 Bangkok/", "Abroad/25-01 Bangkok/Fuji/"})

    def test_round_trip_restores_windows_paths(self):
        make_catalog(self.cat, {WIN_ROOT: ["", "Models/"], "C:/Studio/": [""]})
        lr.retarget_catalog(self.cat, WIN_ROOT, MAC_ROOT)
        lr.retarget_catalog(self.cat, MAC_ROOT, WIN_ROOT)
        self.assertEqual(folders_by_root(self.cat), {WIN_ROOT: {"", "Models/"}, "C:/Studio/": {""}})

    def test_case_insensitive_prefix(self):
        make_catalog(self.cat, {"e:/photography/": [""]})
        lr.retarget_catalog(self.cat, WIN_ROOT, MAC_ROOT)
        self.assertIn(MAC_ROOT, folders_by_root(self.cat))

    def test_clashing_nested_root_is_left_alone(self):
        make_catalog(self.cat, {
            MAC_ROOT: ["", "Abroad/", "Abroad/Trip/"],
            "E:/Photography/Abroad/Trip/": [""],
        })
        plan = lr.retarget_catalog(self.cat, MAC_ROOT, WIN_ROOT)
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(len(plan.merges), 0)
        self.assertIn("E:/Photography/Abroad/Trip/", folders_by_root(self.cat))

    def test_duplicate_target_aborts_without_change(self):
        make_catalog(self.cat, {WIN_ROOT: [""], MAC_ROOT: [""]})
        with self.assertRaises(lr.CatalogError):
            lr.retarget_catalog(self.cat, WIN_ROOT, MAC_ROOT)
        self.assertEqual(set(folders_by_root(self.cat)), {WIN_ROOT, MAC_ROOT})

    def test_wal_catalog_reads_leave_no_side_files_and_retarget_cleans_up(self):
        make_catalog(self.cat, {WIN_ROOT: [""]})
        with closing(sqlite3.connect(self.cat)) as c:
            self.assertEqual(c.execute("pragma journal_mode=wal").fetchone()[0], "wal")
        self.assertEqual(lr.lock_files(self.tmp.name, "c"), [])
        lr.db_version(self.cat)
        lr.root_folders(self.cat)
        self.assertEqual(lr.lock_files(self.tmp.name, "c"), [])
        lr.retarget_catalog(self.cat, WIN_ROOT, MAC_ROOT)
        self.assertEqual(lr.lock_files(self.tmp.name, "c"), [])
        self.assertIn(MAC_ROOT, dict((p, 1) for _, p in lr.root_folders(self.cat)))

    def test_db_version(self):
        make_catalog(self.cat, {WIN_ROOT: [""]})
        self.assertEqual(lr.db_version(self.cat), "1504001")


class FsopsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        self.dst = os.path.join(self.tmp.name, "dst")

    def tearDown(self):
        self.tmp.cleanup()

    def test_compare_classifies_and_skips_junk(self):
        t = time.time() - 1000
        write(os.path.join(self.src, "a", "new.raf"), b"1", t)
        write(os.path.join(self.src, "a", "same.raf"), b"22", t)
        write(os.path.join(self.dst, "a", "same.raf"), b"22", t + 1)          # within exFAT tolerance
        write(os.path.join(self.src, "a", "edited.psd"), b"333", t + 100)
        write(os.path.join(self.dst, "a", "edited.psd"), b"3", t)
        write(os.path.join(self.src, "a", "pcnewer.xmp"), b"4444", t)
        write(os.path.join(self.dst, "a", "pcnewer.xmp"), b"44", t + 100)
        write(os.path.join(self.src, "a", "._new.raf"), b"apple", t)
        write(os.path.join(self.src, "a", ".DS_Store"), b"apple", t)
        write(os.path.join(self.src, "x Previews.lrdata", "p.db"), b"p", t)
        cmp = fsops.compare_trees(self.src, self.dst)
        self.assertEqual([f.rel for f in cmp.new], [os.path.join("a", "new.raf")])
        self.assertEqual([f.rel for f in cmp.updated], [os.path.join("a", "edited.psd")])
        self.assertEqual([f.rel for f in cmp.conflicts], [os.path.join("a", "pcnewer.xmp")])
        self.assertEqual(cmp.same, 1)

    def test_atomic_copy_failure_leaves_destination(self):
        write(os.path.join(self.src, "f"), b"new content")
        write(os.path.join(self.dst, "f"), b"old")

        def boom(tmp):
            raise RuntimeError("fail")
        with self.assertRaises(RuntimeError):
            fsops.copy_file_atomic(os.path.join(self.src, "f"), os.path.join(self.dst, "f"),
                                   verify=True, before_commit=boom)
        with open(os.path.join(self.dst, "f"), "rb") as f:
            self.assertEqual(f.read(), b"old")
        self.assertFalse(os.path.exists(os.path.join(self.dst, "f") + fsops.TMP_SUFFIX))

    def test_copy_dir_atomic_replaces(self):
        write(os.path.join(self.src, "d", "1"), b"a")
        write(os.path.join(self.dst, "d", "old"), b"b")
        fsops.copy_dir_atomic(os.path.join(self.src, "d"), os.path.join(self.dst, "d"))
        self.assertEqual(os.listdir(os.path.join(self.dst, "d")), ["1"])


class WorkflowTests(unittest.TestCase):
    """Full leave/arrive cycles with fake PC + SSD folders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.pc_cat_dir = os.path.join(base, "pc", "Catalog")
        self.archive = os.path.join(base, "pc", "Photography")
        self.ssd_root = os.path.join(base, "ssd")
        os.makedirs(os.path.join(self.ssd_root, "Photography"))
        self.cfg = Config(catalog_name="Cat", desktop_catalog_dir=self.pc_cat_dir,
                          desktop_photos=self.archive, backup_dir=os.path.join(base, "pc", "Backups"),
                          ssd_root=self.ssd_root, ssd_label="TravelSSD")
        # Archive root as Lightroom would store it for this temp path
        self.win_root = self.cfg.desktop_root_lr
        os.makedirs(self.pc_cat_dir)
        make_catalog(self.cfg.desktop_lrcat, {self.win_root: ["", "Models/", "Models/Ira/"]})
        write(os.path.join(self.cfg.desktop_lrcat + "-data", "m.bin"), b"mask")
        write(os.path.join(self.archive, "Models", "Ira", "1.raf"), b"raw1", time.time() - 5000)
        self.ssd_lrcat = os.path.join(self.ssd_root, "Catalog", "Cat.lrcat")
        self.answers = []
        patches = [
            mock.patch.object(lr, "lightroom_running", return_value=False),
            mock.patch.object(ui, "ask", side_effect=lambda prompt: self.answers.pop(0)),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_cycle_with_mac_edits_and_new_trip(self):
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[os.path.join("Models", "Ira")])
        # PC locked, SSD has catalog pointing at the Mac path, folder taken along
        self.assertFalse(os.path.exists(self.cfg.desktop_lrcat))
        self.assertTrue(os.path.exists(state.checked_out_path(self.pc_cat_dir, "Cat")))
        self.assertIn(MAC_ROOT, folders_by_root(self.ssd_lrcat))
        self.assertTrue(os.path.exists(os.path.join(self.ssd_root, "Photography", "Models", "Ira", "1.raf")))
        backups = [d for d in os.listdir(self.cfg.backup_dir) if d.startswith("Cat_")]
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.exists(os.path.join(self.cfg.backup_dir, backups[0], "Cat.lrcat")))

        # On the Mac: import a trip as a separate top-level folder, edit a PSD
        with closing(sqlite3.connect(self.ssd_lrcat)) as c:
            rid = c.execute("insert into AgLibraryRootFolder (id_global, absolutePath, name) values (?,?,?)",
                            ("g1", MAC_ROOT + "Abroad/26-12 Lisbon/", "26-12 Lisbon")).lastrowid
            c.execute("insert into AgLibraryFolder (id_global, pathFromRoot, rootFolder) values ('g2','',?)", (rid,))
            c.commit()
        later = time.time() + 10
        os.utime(self.ssd_lrcat, (later, later))
        write(os.path.join(self.ssd_root, "Photography", "Abroad", "26-12 Lisbon", "a.raf"), b"trip")
        write(os.path.join(self.ssd_root, "Photography", "Models", "Ira", "1.psd"), b"psd")
        write(os.path.join(self.ssd_root, "Photography", "Abroad", "26-12 Lisbon", "._a.raf"), b"junk")

        self.answers = ["y"]
        workflows.arrive(self.cfg)
        got = folders_by_root(self.cfg.desktop_lrcat)
        self.assertEqual(set(got), {self.win_root})
        self.assertIn("Abroad/26-12 Lisbon/", got[self.win_root])
        self.assertTrue(os.path.exists(os.path.join(self.archive, "Abroad", "26-12 Lisbon", "a.raf")))
        self.assertTrue(os.path.exists(os.path.join(self.archive, "Models", "Ira", "1.psd")))
        self.assertFalse(os.path.exists(os.path.join(self.archive, "Abroad", "26-12 Lisbon", "._a.raf")))
        self.assertFalse(os.path.exists(state.checked_out_path(self.pc_cat_dir, "Cat")))
        self.assertFalse(os.path.exists(self.ssd_lrcat))
        self.assertTrue(os.path.exists(state.returned_path(os.path.dirname(self.ssd_lrcat), "Cat")))
        self.assertEqual(state.read(self.pc_cat_dir)["status"], "home")

        # Second trip: stale returned copy is moved aside, not deleted
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        self.assertTrue(os.path.isdir(os.path.join(self.ssd_root, "Catalog", "LRWM-Backups")))

    def test_arrive_without_edits_just_unlocks(self):
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        self.answers = ["y"]
        workflows.arrive(self.cfg)
        self.assertEqual(folders_by_root(self.cfg.desktop_lrcat), {self.win_root: {"", "Models/", "Models/Ira/"}})

    def test_leave_refused_when_lightroom_lock_present(self):
        write(os.path.join(self.pc_cat_dir, "Cat.lrcat.lock"), b"")
        with self.assertRaises(workflows.Abort):
            workflows.leave(self.cfg, folders=[])
        self.assertTrue(os.path.exists(self.cfg.desktop_lrcat))

    def test_cannot_leave_twice_or_arrive_when_home(self):
        with self.assertRaises(workflows.Abort):
            workflows.arrive(self.cfg)
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        with self.assertRaises(workflows.Abort):
            workflows.leave(self.cfg, folders=[])

    def test_arrive_refuses_wrong_ssd(self):
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        rec = state.read(os.path.join(self.ssd_root, "Catalog"))
        rec["checkout_id"] = "other"
        state.write(os.path.join(self.ssd_root, "Catalog"), rec)
        with self.assertRaises(workflows.Abort):
            workflows.arrive(self.cfg)
        self.assertTrue(os.path.exists(state.checked_out_path(self.pc_cat_dir, "Cat")))

    def test_dry_run_changes_nothing(self):
        before = sorted(os.listdir(self.pc_cat_dir))
        workflows.leave(self.cfg, folders=[os.path.join("Models", "Ira")], dry_run=True)
        self.assertEqual(sorted(os.listdir(self.pc_cat_dir)), before)
        self.assertFalse(os.path.exists(self.ssd_lrcat))

    def test_release_unlocks_and_retires_ssd_copy(self):
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        self.answers = ["RELEASE"]
        workflows.release(self.cfg)
        self.assertTrue(os.path.exists(self.cfg.desktop_lrcat))
        self.assertFalse(os.path.exists(self.ssd_lrcat))


class ConfigTests(unittest.TestCase):
    def test_load_and_derived_paths(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as f:
                json.dump({"_comment": "x", "catalog_name": "C", "desktop_catalog_dir": "C:\\L",
                           "desktop_photos": "E:\\Photography", "backup_dir": "C:\\B",
                           "ssd_label": "TravelSSD"}, f)
            cfg = load_config(p)
            self.assertEqual(cfg.desktop_root_lr, "E:/Photography/")
            self.assertEqual(cfg.mac_root_lr, "/Volumes/TravelSSD/Photography/")

    def test_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w") as f:
                json.dump({"catalog_name": "C", "desktop_catalog_dir": "a", "desktop_photos": "b",
                           "backup_dir": "c", "ssd_label": "x", "typo": 1}, f)
            with self.assertRaises(ConfigError):
                load_config(p)


if __name__ == "__main__":
    unittest.main()
