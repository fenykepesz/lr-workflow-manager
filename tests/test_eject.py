"""Tests for safe ejection. Nothing here can eject a real drive."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lrwm import eject  # noqa: E402

KEEP = ["C:\\Windows", "E:\\Photography"]


class FakeWindows:
    def __init__(self, removable=(21,), letters=None, disks=None, results=(0,)):
        self.removable = removable
        self.letters = letters or {"C:": 0, "E:": 1, "G:": 2}
        self.chains = disks or {0: [10, 11, 1], 1: [12, 11, 1], 2: [20, 21, 22, 23, 1]}
        self.results, self.requested = list(results), []

    def disk_number(self, drive):
        return self.letters.get(drive)

    def disks(self):
        return self.chains

    def capabilities(self, node):
        return eject.REMOVABLE if node in self.removable else 0

    def request_eject(self, node):
        self.requested.append(node)
        return self.results.pop(0), "veto"


class TargetTests(unittest.TestCase):
    def test_picks_the_nearest_removable_device_not_the_hub(self):
        self.assertEqual(eject.choose_target(FakeWindows(removable=(21, 22)), "G:", KEEP), 21)

    def test_refuses_fixed_disk(self):
        with self.assertRaises(eject.EjectError):
            eject.choose_target(FakeWindows(removable=()), "G:", KEEP)

    def test_refuses_device_shared_with_another_disk(self):
        win = FakeWindows(disks={1: [12, 21, 1], 2: [20, 21, 1]})
        with self.assertRaises(eject.EjectError):
            eject.choose_target(win, "G:", KEEP)

    def test_refuses_disk_holding_the_archive(self):
        win = FakeWindows(letters={"C:": 0, "E:": 2, "G:": 2})
        with self.assertRaises(eject.EjectError):
            eject.choose_target(win, "G:", KEEP)

    def test_refuses_unknown_drive(self):
        with self.assertRaises(eject.EjectError):
            eject.choose_target(FakeWindows(), "Q:", KEEP)


@unittest.skipUnless(os.name == "nt", "Windows only")
class EjectTests(unittest.TestCase):
    def run_eject(self, win, root="G:\\"):
        with mock.patch.object(eject, "_Windows", return_value=win), mock.patch.object(eject.time, "sleep"):
            eject.eject(root, KEEP)

    def test_retries_while_windows_lets_go(self):
        win = FakeWindows(results=[23, 23, 0])
        self.run_eject(win)
        self.assertEqual(win.requested, [21, 21, 21])

    def test_gives_up_when_still_in_use(self):
        win = FakeWindows(results=[23] * eject.ATTEMPTS)
        with self.assertRaises(eject.EjectError) as caught:
            self.run_eject(win)
        self.assertIn("Don't unplug", str(caught.exception))

    def test_a_folder_is_never_treated_as_its_drive(self):
        for root in ("C:\\Temp\\ssd", "G:\\Photography", "\\\\server\\share", ""):
            win = FakeWindows()
            with self.assertRaises(eject.EjectError):
                self.run_eject(win, root)
            self.assertEqual(win.requested, [])
