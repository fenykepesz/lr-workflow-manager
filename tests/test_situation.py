"""Tests for the start screen and the step it suggests."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lrwm import cli, drives, situation, state, ui, workflows  # noqa: E402
from lrwm import lightroom as lr  # noqa: E402
from lrwm.config import Config  # noqa: E402
from test_lrwm import make_catalog, write  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        j = os.path.join
        base = tmp.name
        self.cfg = Config(catalog_name="Cat", desktop_catalog_dir=j(base, "cat"),
                          desktop_photos=j(base, "archive"), backup_dir=j(base, "backups"),
                          ssd_root=j(base, "ssd"), ssd_label="TravelSSD",
                          preset_adobe_dir=j(base, "adobe"))
        for d in (self.cfg.desktop_catalog_dir, self.cfg.desktop_photos,
                  j(self.cfg.ssd_root, "Photography")):
            os.makedirs(d)
        make_catalog(self.cfg.desktop_lrcat, {self.cfg.desktop_root_lr: [""]})
        write(j(self.cfg.desktop_lrcat + "-data", "mask.bin"), b"mask")
        write(j(self.cfg.preset_adobe_dir, "CameraRaw", "Settings", "a.xmp"), b"preset")
        self.ssd = drives.SSD(self.cfg, self.cfg.ssd_root)
        self.answers, self.shown = [], []
        for patch in (mock.patch.object(lr, "lightroom_running", return_value=False),
                      mock.patch.object(ui, "ask", side_effect=self.answer),
                      mock.patch("builtins.print",
                                 side_effect=lambda *a, **k: self.shown.append(" ".join(map(str, a))))):
            patch.start()
            self.addCleanup(patch.stop)

    def answer(self, prompt):
        if not self.answers:
            raise AssertionError(f"Unexpected question: {prompt}")
        return self.answers.pop(0)

    def leave(self):
        self.answers = ["y"]
        workflows.leave(self.cfg, folders=[])

    def photo(self, name="trip.raf"):
        write(os.path.join(self.ssd.photos, "Trip", name), b"photo")

    def is_out(self):
        return state.is_out(state.read(self.cfg.desktop_catalog_dir))

    def expect(self, kind):
        self.assertEqual(situation.detect(self.cfg).kind, kind)


class DetectTests(Base):
    def test_home(self):
        self.expect(situation.LEAVE)

    def test_home_with_photos_not_in_archive(self):
        self.photo()
        self.expect(situation.COPY_PHOTOS)

    def test_home_after_a_trip(self):
        self.leave()
        self.answers = ["y"]
        workflows.arrive(self.cfg)
        self.expect(situation.LEAVE)

    def test_ready_right_after_leave(self):
        self.leave()
        self.expect(situation.READY)

    def test_ready_despite_exfat_rounding(self):
        self.leave()
        for path in (self.ssd.lrcat, os.path.join(self.ssd.lrcat + "-data", "mask.bin")):
            t = os.path.getmtime(path) + 1.5
            os.utime(path, (t, t))
        self.expect(situation.READY)

    def test_ready_when_photos_were_already_pending_at_leave(self):
        self.photo()
        self.answers = ["y", "y"]  # continue anyway, check out
        workflows.leave(self.cfg, folders=[])
        self.expect(situation.READY)
        self.photo("second.raf")
        self.expect(situation.ARRIVE)

    def test_ready_when_presets_were_already_pending_at_leave(self):
        write(os.path.join(self.ssd.presets, "CameraRaw", "Settings", "mac.xmp"))
        self.leave()
        self.expect(situation.READY)

    def test_ready_after_taking_more_along(self):
        write(os.path.join(self.cfg.desktop_photos, "More", "m.raf"))
        self.leave()
        self.answers = ["y"]
        workflows.bring_folders(self.cfg, folders=["More"])
        self.expect(situation.READY)

    def test_arrive_after_catalog_edit(self):
        self.leave()
        t = time.time() + 60
        os.utime(self.ssd.lrcat, (t, t))
        self.expect(situation.ARRIVE)

    def test_arrive_after_mask_edit(self):
        self.leave()
        write(os.path.join(self.ssd.lrcat + "-data", "mask2.bin"))
        self.expect(situation.ARRIVE)

    def test_arrive_after_new_photo(self):
        self.leave()
        self.photo()
        self.expect(situation.ARRIVE)

    def test_arrive_after_new_preset(self):
        self.leave()
        write(os.path.join(self.ssd.presets, "CameraRaw", "Settings", "mac.xmp"))
        self.expect(situation.ARRIVE)

    def test_unreadable_presets_do_not_block_detection(self):
        self.leave()
        with mock.patch("lrwm.presets.plan_in", side_effect=OSError("denied")):
            self.expect(situation.READY)

    def test_no_ssd_at_home_and_out(self):
        with mock.patch.object(drives, "connect", return_value=None):
            self.expect(situation.NO_SSD)
        self.leave()
        with mock.patch.object(drives, "connect", return_value=None):
            self.expect(situation.NO_SSD)

    def test_wrong_ssd(self):
        self.leave()
        record = state.read(self.ssd.catalog_dir)
        state.write(self.ssd.catalog_dir, dict(record, checkout_id="other"))
        self.expect(situation.WRONG_SSD)

    def test_blank_ssd_while_out(self):
        self.leave()
        shutil.rmtree(self.ssd.catalog_dir)
        self.expect(situation.WRONG_SSD)

    def test_damaged_records(self):
        path = os.path.join(self.cfg.desktop_catalog_dir, state.STATE_FILE)
        for bad in (b"{broken", b"[]", b"\xff\xfe\x00"):
            write(path, bad)
            self.expect(situation.PROBLEM)

    def test_damaged_ssd_record_while_out(self):
        self.leave()
        write(os.path.join(self.ssd.catalog_dir, state.STATE_FILE), b"{broken")
        self.expect(situation.PROBLEM)

    def test_home_record_with_locked_catalog(self):
        self.leave()
        os.remove(os.path.join(self.cfg.desktop_catalog_dir, state.STATE_FILE))
        self.expect(situation.PROBLEM)

    def test_out_record_with_unlocked_catalog(self):
        self.leave()
        write(self.cfg.desktop_lrcat, b"new catalog")
        self.expect(situation.PROBLEM)

    def test_out_record_without_locked_catalog(self):
        self.leave()
        os.remove(state.checked_out_path(self.cfg.desktop_catalog_dir, "Cat"))
        self.expect(situation.PROBLEM)

    def test_catalog_missing_on_pc(self):
        os.remove(self.cfg.desktop_lrcat)
        self.expect(situation.PROBLEM)

    def test_catalog_missing_on_ssd(self):
        self.leave()
        os.remove(self.ssd.lrcat)
        self.expect(situation.PROBLEM)

    def test_never_returned_catalog_on_ssd(self):
        write(self.ssd.lrcat, b"mac edits")
        state.write(self.ssd.catalog_dir, {"status": "out", "checkout_id": "old"})
        self.expect(situation.PROBLEM)

    def test_failure_while_looking_is_reported(self):
        with mock.patch.object(drives, "connect", side_effect=OSError("unplugged")):
            self.expect(situation.PROBLEM)

    def test_detection_changes_nothing(self):
        def snapshot():
            base = os.path.dirname(self.cfg.ssd_root)
            return sorted((os.path.join(d, f), os.stat(os.path.join(d, f)).st_mtime_ns)
                          for d, _, files in os.walk(base) for f in files)
        self.leave()
        before = snapshot()
        situation.detect(self.cfg)
        self.assertEqual(snapshot(), before)


class ScreenTests(Base):
    def run_screen(self, *answers):
        self.answers = list(answers)
        cli.start(self.cfg)
        self.assertEqual(self.answers, [])

    def test_enter_leaves_then_enter_quits(self):
        # start Leave, no folders, confirm, pause, quit
        self.run_screen("", "", "y", "", "")
        self.assertTrue(self.is_out())

    def test_only_pressing_enter_never_checks_out(self):
        self.answers = [""] * 20
        with self.assertRaises(AssertionError):  # still asking when the presses run out
            cli.start(self.cfg)
        self.assertFalse(self.is_out())

    def test_ready_enter_quits(self):
        self.leave()
        self.run_screen("")
        self.assertTrue(self.is_out())

    def test_ready_a_cancels_the_trip_then_enter_quits(self):
        self.leave()
        self.run_screen("a", "y", "", "")
        self.assertFalse(self.is_out())

    def test_enter_arrives_then_enter_quits(self):
        self.leave()
        self.photo()
        self.run_screen("", "y", "", "")
        self.assertFalse(self.is_out())
        self.assertTrue(os.path.exists(os.path.join(self.cfg.desktop_photos, "Trip", "trip.raf")))

    def test_enter_at_arrive_confirmation_cancels(self):
        self.leave()
        self.photo()
        self.run_screen("", "", "", "q")
        self.assertTrue(self.is_out())

    def test_copy_photos_then_offers_leave(self):
        self.photo()
        self.run_screen("", "y", "", "q")
        self.assertIn("[Enter] Leave", "\n".join(self.shown))

    def test_problem_enter_does_nothing(self):
        write(os.path.join(self.cfg.desktop_catalog_dir, state.STATE_FILE), b"{broken")
        self.run_screen("", "q")
        screen = "\n".join(self.shown)
        self.assertNotIn("AT HOME", screen)
        self.assertNotIn("[Enter]", screen)

    def test_status_does_not_say_home_when_damaged(self):
        write(os.path.join(self.cfg.desktop_catalog_dir, state.STATE_FILE), b"{broken")
        workflows.status(self.cfg)
        self.assertNotIn("AT HOME", "\n".join(self.shown))

    def test_no_ssd_enter_checks_again(self):
        with mock.patch.object(drives, "connect", return_value=None):
            self.run_screen("", "q")
        self.assertFalse(self.is_out())

    def test_more_options_enter_goes_back(self):
        self.run_screen("m", "", "q")
        self.assertFalse(self.is_out())

    def test_dry_run_from_more_options(self):
        self.run_screen("m", "d", "", "", "q")  # toggle, start Leave, pause, quit
        self.assertFalse(self.is_out())

    def test_end_of_input_exits(self):
        with mock.patch.object(ui, "ask", side_effect=EOFError):
            cli.start(self.cfg)
