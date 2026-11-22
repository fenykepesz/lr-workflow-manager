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

from lrwm import fsops, presets, state, ui, workflows  # noqa: E402
from lrwm import lightroom as lr  # noqa: E402
from lrwm.config import Config, load_config, ConfigError  # noqa: E402


def setUpModule():
    """Never let a test reach the real Lightroom presets."""
    fake = tempfile.TemporaryDirectory()
    env = mock.patch.dict(os.environ, {"APPDATA": fake.name})
    env.start()
    unittest.addModuleCleanup(fake.cleanup)
    unittest.addModuleCleanup(env.stop)


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
            CREATE TABLE AgLibraryFile (id_local INTEGER PRIMARY KEY, id_global UNIQUE NOT NULL,
                baseName NOT NULL DEFAULT '', extension NOT NULL DEFAULT '', folder INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE Adobe_images (id_local INTEGER PRIMARY KEY, id_global UNIQUE NOT NULL, rootFile INTEGER);
            CREATE TABLE AgLibraryCollection (id_local INTEGER PRIMARY KEY, creationId NOT NULL DEFAULT '',
                genealogy NOT NULL DEFAULT '', imageCount, name NOT NULL DEFAULT '', parent INTEGER,
                systemOnly NOT NULL DEFAULT '');
            CREATE TABLE AgLibraryCollectionImage (id_local INTEGER PRIMARY KEY, collection INTEGER NOT NULL DEFAULT 0,
                image INTEGER NOT NULL DEFAULT 0, pick NOT NULL DEFAULT 0, positionInCollection);
        """)
        for absolute, folders in roots.items():
            rid = c.execute("insert into AgLibraryRootFolder (id_global, absolutePath, name) values (?,?,?)",
                            (uuid.uuid4().hex, absolute, absolute.rstrip("/").split("/")[-1])).lastrowid
            for f in folders:
                c.execute("insert into AgLibraryFolder (id_global, pathFromRoot, rootFolder) values (?,?,?)",
                          (uuid.uuid4().hex, f, rid))
        c.commit()


def add_photo(path, root, folder, name, collection=None, smart=False):
    """Add a photo at root+folder to the catalog, optionally into a collection."""
    with closing(sqlite3.connect(path)) as c:
        rid = c.execute("select id_local from AgLibraryRootFolder where absolutePath=?", (root,)).fetchone()[0]
        row = c.execute("select id_local from AgLibraryFolder where rootFolder=? and pathFromRoot=?",
                        (rid, folder)).fetchone()
        fid = row[0] if row else c.execute(
            "insert into AgLibraryFolder (id_global, pathFromRoot, rootFolder) values (?,?,?)",
            (uuid.uuid4().hex, folder, rid)).lastrowid
        base, ext = name.rsplit(".", 1)
        file_id = c.execute("insert into AgLibraryFile (id_global, baseName, extension, folder) values (?,?,?,?)",
                            (uuid.uuid4().hex, base, ext, fid)).lastrowid
        img = c.execute("insert into Adobe_images (id_global, rootFile) values (?,?)",
                        (uuid.uuid4().hex, file_id)).lastrowid
        if collection:
            kind = "com.adobe.ag.library.smart_collection" if smart else "com.adobe.ag.library.collection"
            row = c.execute("select id_local from AgLibraryCollection where name=? and creationId=?",
                            (collection, kind)).fetchone()
            cid = row[0] if row else c.execute(
                "insert into AgLibraryCollection (creationId, name) values (?,?)", (kind, collection)).lastrowid
            if not smart:
                c.execute("insert into AgLibraryCollectionImage (collection, image) values (?,?)", (cid, img))
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

    def test_collection_folders(self):
        make_catalog(self.cat, {WIN_ROOT: [""], "C:/Studio/": [""]})
        add_photo(self.cat, WIN_ROOT, "Models/Ira/", "a.cr2", "Take along")
        add_photo(self.cat, WIN_ROOT, "Models/Ira/", "b.cr2", "Take along")
        add_photo(self.cat, WIN_ROOT, "Models/Ira/Selects/", "c.tif", "Take along")  # inside Ira
        add_photo(self.cat, WIN_ROOT, "Abroad/25-10 Budapest/", "d.raf", "take along")  # name case
        add_photo(self.cat, WIN_ROOT, "Abroad/Other/", "e.raf", "Something else")
        add_photo(self.cat, "C:/Studio/", "", "f.cr2", "Take along")
        res = lr.collection_folders(self.cat, "Take along", WIN_ROOT)
        self.assertTrue(res.found)
        self.assertEqual(res.folders, [("Abroad/25-10 Budapest", 1), ("Models/Ira", 3)])
        self.assertEqual(res.outside, [("C:/Studio/", 1)])

    def test_collection_missing_or_smart(self):
        make_catalog(self.cat, {WIN_ROOT: [""]})
        self.assertFalse(lr.collection_folders(self.cat, "Take along", WIN_ROOT).found)
        add_photo(self.cat, WIN_ROOT, "X/", "a.cr2", "Take along", smart=True)
        res = lr.collection_folders(self.cat, "Take along", WIN_ROOT)
        self.assertTrue(res.smart_only)
        self.assertEqual(res.folders, [])

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

    def test_cleanup_groups_and_safety(self):
        t = time.time() - 1000
        ssd, arc = self.src, self.dst
        j = os.path.join
        for rel, data, in_archive in [
            (j("Models", "Ira", "a.raf"), b"a", True),             # part of the trip: untouched
            (j("Models", "Ivory", "b.raf"), b"b", True),           # safe
            (j("Models", "loose.txt"), b"l", True),                # loose file next to a kept folder
            (j("Abroad", "X", "c.raf"), b"c", True),               # safe
            (j("Abroad", "Y", "d.raf"), b"d", False),              # only on SSD: must be kept
        ]:
            write(j(ssd, rel), data, t)
            if in_archive:
                write(j(arc, rel), data, t)
        write(j(ssd, "Abroad", "X", "old.tif"), b"old", t)          # archive has a newer, bigger one
        write(j(arc, "Abroad", "X", "old.tif"), b"newer!", t + 500)
        write(j(ssd, "Abroad", "X", "._c.raf"), b"apple", t)
        write(j(ssd, "Abroad", "X", ".DS_Store"), b"apple", t)

        groups = fsops.plan_cleanup(ssd, arc, [j("Models", "Ira")])
        by = {g.folder: g for g in groups}
        self.assertEqual(sorted(by), ["Abroad", "Models", j("Models", "Ivory")])
        self.assertTrue(by["Models"].files_only)
        self.assertEqual([f.rel for f in by["Abroad"].unsafe], [j("Abroad", "Y", "d.raf")])
        self.assertEqual(len(by["Abroad"].safe), 2)

        self.assertEqual(fsops.remove_cleanup(ssd, groups), [])
        self.assertFalse(os.path.exists(j(ssd, "Abroad", "X")))       # emptied, junk removed
        self.assertTrue(os.path.exists(j(ssd, "Abroad", "Y", "d.raf")))
        self.assertTrue(os.path.exists(j(ssd, "Models", "Ira", "a.raf")))
        self.assertFalse(os.path.exists(j(ssd, "Models", "Ivory")))
        self.assertFalse(os.path.exists(j(ssd, "Models", "loose.txt")))
        self.assertTrue(os.path.exists(j(arc, "Abroad", "X", "c.raf")))  # archive untouched

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
                          ssd_root=self.ssd_root, ssd_label="TravelSSD",
                          preset_adobe_dir=os.path.join(base, "pc", "Adobe"))
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

        # Second trip, taking nothing: remove last trip's folders (all safe in archive),
        # and the returned copy is moved aside, not deleted
        self.answers = ["y", "y"]  # remove old folders, confirm plan
        workflows.leave(self.cfg, folders=[])
        self.assertTrue(os.path.isdir(os.path.join(self.ssd_root, "Catalog", "LRWM-Backups")))
        self.assertEqual(os.listdir(os.path.join(self.ssd_root, "Photography")), [])
        self.assertTrue(os.path.exists(os.path.join(self.archive, "Abroad", "26-12 Lisbon", "a.raf")))

    def test_leave_takes_whole_folders_from_collection(self):
        write(os.path.join(self.archive, "Models", "Ira", "Developed", "1.jpg"), b"jpg")
        write(os.path.join(self.archive, "Models", "Ira", "1-Edit.tif"), b"tif")
        write(os.path.join(self.archive, "Models", "Other", "2.raf"), b"other")
        add_photo(self.cfg.desktop_lrcat, self.win_root, "Models/Ira/", "1.raf", "Take along")
        self.answers = ["", "y"]  # Enter = take the collection's folders, then confirm
        workflows.leave(self.cfg)
        ssd_ira = os.path.join(self.ssd_root, "Photography", "Models", "Ira")
        self.assertEqual(sorted(os.listdir(ssd_ira)), ["1-Edit.tif", "1.raf", "Developed"])
        self.assertTrue(os.path.exists(os.path.join(ssd_ira, "Developed", "1.jpg")))
        self.assertFalse(os.path.exists(os.path.join(self.ssd_root, "Photography", "Models", "Other")))
        self.assertEqual(state.read(self.pc_cat_dir)["brought"], [os.path.join("Models", "Ira")])

    def test_collection_choice_none_takes_nothing(self):
        add_photo(self.cfg.desktop_lrcat, self.win_root, "Models/Ira/", "1.raf", "Take along")
        self.answers = ["n", "y"]
        workflows.leave(self.cfg)
        self.assertFalse(os.path.exists(os.path.join(self.ssd_root, "Photography", "Models")))

    def test_leave_declining_cleanup_keeps_ssd_folders(self):
        old = os.path.join(self.ssd_root, "Photography", "Models", "Ira", "1.raf")
        write(old, b"raw1", os.path.getmtime(os.path.join(self.archive, "Models", "Ira", "1.raf")))
        self.answers = ["n", "y"]  # keep old folders, confirm plan
        workflows.leave(self.cfg, folders=[])
        self.assertTrue(os.path.exists(old))

    def test_stale_ssd_catalog_is_set_aside_unopenable(self):
        write(self.ssd_lrcat, b"stale catalog")
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        backups = os.path.join(self.ssd_root, "Catalog", "LRWM-Backups")
        (only,) = os.listdir(backups)
        self.assertEqual(os.listdir(os.path.join(backups, only)), ["Cat.lrcat.backup"])

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

    def test_presets_round_trip(self):
        folder = os.path.join("CameraRaw", "Settings")
        pc = os.path.join(self.cfg.preset_adobe_dir, folder)
        ssd = os.path.join(self.ssd_root, "Presets", folder)
        t = time.time() - 1000
        for name in ("edit.xmp", "gone.xmp", "Index.dat"):
            write(os.path.join(pc, name), b"pc", t)
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        self.assertEqual(sorted(os.listdir(ssd)), ["edit.xmp", "gone.xmp"])
        self.assertTrue(os.path.exists(os.path.join(self.ssd_root, "Presets", presets.MAC_SCRIPT)))

        write(os.path.join(ssd, "edit.xmp"), b"mac edit", t + 100)
        write(os.path.join(ssd, "new.xmp"), b"mac new")
        os.remove(os.path.join(pc, "gone.xmp"))
        self.answers = ["y"]
        workflows.arrive(self.cfg)
        with open(os.path.join(pc, "edit.xmp"), "rb") as f:
            self.assertEqual(f.read(), b"mac edit")
        self.assertTrue(os.path.exists(os.path.join(pc, "new.xmp")))
        self.assertFalse(os.path.exists(os.path.join(pc, "gone.xmp")))
        saved = os.path.join(self.cfg.backup_dir, presets.BACKUP_DIR)
        (only,) = os.listdir(saved)
        with open(os.path.join(saved, only, folder, "edit.xmp"), "rb") as f:
            self.assertEqual(f.read(), b"pc")

    def test_dry_runs_leave_presets_untouched(self):
        pc = os.path.join(self.cfg.preset_adobe_dir, "CameraRaw", "Settings")
        write(os.path.join(pc, "a.xmp"))
        workflows.leave(self.cfg, folders=[], dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(self.ssd_root, "Presets")))
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        write(os.path.join(self.ssd_root, "Presets", "CameraRaw", "Settings", "b.xmp"))
        workflows.arrive(self.cfg, dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(pc, "b.xmp")))

    def test_sync_presets_off(self):
        write(os.path.join(self.cfg.preset_adobe_dir, "CameraRaw", "Settings", "a.xmp"))
        self.cfg.sync_presets = False
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])
        self.assertFalse(os.path.exists(os.path.join(self.ssd_root, "Presets")))

    def test_preset_failures_never_block_the_catalog(self):
        write(os.path.join(self.cfg.preset_adobe_dir, "CameraRaw", "Settings", "a.xmp"))
        with mock.patch.object(presets, "copy_out", side_effect=OSError("disk")):
            self.answers = ["y"]
            workflows.leave(self.cfg, folders=[])
        self.assertTrue(os.path.exists(self.ssd_lrcat))
        os.makedirs(os.path.join(self.ssd_root, "Presets"))
        with mock.patch.object(presets, "plan_in", side_effect=ValueError("bad record")):
            self.answers = ["y"]
            workflows.arrive(self.cfg)
        self.assertTrue(os.path.exists(self.cfg.desktop_lrcat))

    def test_cleanup_never_touches_presets(self):
        kept = os.path.join(self.ssd_root, "Presets", "CameraRaw", "Settings", "mac.xmp")
        write(kept)
        t = time.time() - 500
        write(os.path.join(self.archive, "Old", "p.raf"), b"p", t)
        write(os.path.join(self.ssd_root, "Photography", "Old", "p.raf"), b"p", t)
        self.answers = ["y", "y"]
        workflows.leave(self.cfg, folders=[])
        self.assertTrue(os.path.exists(kept))


class PresetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pc, self.ssd, self.backup = (os.path.join(tmp.name, n) for n in ("pc", "ssd", "backup"))
        self.t = time.time() - 1000

    def put(self, root, name, data=b"x", age=0, folder="CameraRaw/Settings"):
        write(os.path.join(root, *folder.split("/"), name), data, self.t + age)

    def send(self):
        presets.copy_out(presets.plan_out(self.pc, self.ssd), self.pc, self.ssd, self.backup)

    def test_same_content_with_other_date_is_skipped(self):
        self.put(self.pc, "a.xmp")
        self.put(self.ssd, "a.xmp", age=500)
        plan = presets.plan_in(self.ssd, self.pc, self.backup)
        self.assertEqual((plan.items, plan.same), ([], 1))

    def test_newer_destination_is_left_alone(self):
        self.put(self.pc, "a.xmp", b"old")
        self.put(self.ssd, "a.xmp", b"newer", 500)
        plan = presets.plan_out(self.pc, self.ssd)
        self.assertEqual((plan.items, len(plan.conflicts)), ([], 1))

    def test_unicode_forms_are_one_preset(self):
        self.put(self.pc, "Caf\u00e9.xmp")       # composed, as Windows writes it
        self.put(self.ssd, "Cafe\u0301.xmp")     # decomposed, as a Mac may write it
        plan = presets.plan_in(self.ssd, self.pc, self.backup)
        self.assertEqual((plan.items, plan.same), ([], 1))

    def test_deleted_preset_returns_only_if_edited_later(self):
        self.put(self.pc, "keep.xmp")
        self.put(self.pc, "gone.xmp")
        self.send()
        os.remove(os.path.join(self.pc, "CameraRaw", "Settings", "gone.xmp"))
        self.send()  # a second trip must not forget the deletion
        self.assertEqual(len(presets.plan_in(self.ssd, self.pc, self.backup).deleted), 1)
        self.put(self.ssd, "gone.xmp", b"edited", 500)
        self.assertEqual(len(presets.plan_in(self.ssd, self.pc, self.backup).items), 1)

    def test_preset_never_on_pc_survives_another_leave(self):
        self.put(self.pc, "a.xmp")
        self.put(self.ssd, "mac.xmp")
        self.send()
        self.assertEqual(len(presets.plan_in(self.ssd, self.pc, self.backup).items), 1)

    def test_empty_pc_gets_everything_back(self):
        self.put(self.pc, "a.xmp")
        self.send()
        os.remove(os.path.join(self.pc, "CameraRaw", "Settings", "a.xmp"))
        self.assertEqual(len(presets.plan_in(self.ssd, self.pc, self.backup).items), 1)

    def test_only_preset_folders_and_types_are_copied(self):
        self.put(self.pc, "b.lrtemplate", folder="Lightroom/Local Adjustment Presets")
        self.put(self.pc, "._a.xmp")
        self.put(self.pc, "notes.pdf")
        self.put(self.pc, "x.xmp", folder="Lightroom/Export Presets")
        self.assertEqual([os.path.basename(i.rel) for i in presets.plan_out(self.pc, self.ssd).items],
                         ["b.lrtemplate"])

    def test_mac_script_has_unix_line_endings(self):
        presets.install_mac_script(self.ssd)
        with open(os.path.join(self.ssd, presets.MAC_SCRIPT), "rb") as f:
            body = f.read()
        self.assertTrue(body.startswith(b"#!/bin/bash\n"))
        self.assertNotIn(b"\r", body)


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
