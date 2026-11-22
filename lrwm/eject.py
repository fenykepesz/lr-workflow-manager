"""Safely eject the SSD on Windows (the same request as 'Safely Remove Hardware').

Ejects the nearest removable device above the SSD's disk, and refuses if that
would take another disk with it, or if the disk holds Windows, the catalog,
the archive or the backups. Uses only Windows' own APIs through ctypes.
"""

import logging
import os
import re
import time
import uuid
from typing import Dict, Iterable, List, Optional

log = logging.getLogger("lrwm")

REMOVABLE = 0x4          # CM_DEVCAP_REMOVABLE
MAX_LEVELS = 4           # how far above the disk to look for the removable device
ATTEMPTS = 4             # Windows often needs a moment to let go after a big copy
RETRY_DELAY = 1.5        # seconds


class EjectError(Exception):
    pass


def drive_of(root: str) -> Optional[str]:
    """'G:' for a drive root such as 'G:\\'; None for anything else, including folders."""
    match = re.fullmatch(r"([A-Za-z]):[\\/]?", root)
    return match.group(1).upper() + ":" if match else None


def choose_target(win, drive: str, keep: Iterable[str]) -> int:
    """The device to eject for this drive. Only reads; raises EjectError when it isn't safe."""
    number = win.disk_number(drive)
    if number is None:
        raise EjectError(f"Could not tell which disk {drive} is on.")
    for path in keep:
        other = os.path.splitdrive(path)[0].upper()
        if re.fullmatch(r"[A-Z]:", other) and (other == drive or win.disk_number(other) == number):
            raise EjectError(f"{drive} is on the same disk as {path}, so it won't be ejected.")

    disks: Dict[int, List[int]] = win.disks()  # {disk number: [disk node, parent, ...]}
    if number not in disks:
        raise EjectError(f"Could not find the device behind {drive}.")
    for node in disks[number][:MAX_LEVELS]:
        if win.capabilities(node) & REMOVABLE:
            if any(node in chain for n, chain in disks.items() if n != number):
                raise EjectError("Ejecting it would remove another disk too. Use the tray icon instead.")
            return node
    raise EjectError(f"Windows doesn't list {drive} as a removable device.")


def eject(root: str, keep: Iterable[str] = ()) -> None:
    """Eject the drive at root, or raise EjectError saying why not."""
    if os.name != "nt":
        raise EjectError("Ejecting is only supported on Windows.")
    drive = drive_of(root)
    if not drive:
        raise EjectError(f"{root} is not a drive, so it can't be ejected.")
    win = _Windows()
    system = os.environ.get("SystemRoot", "C:\\Windows")
    node = choose_target(win, drive, [system, *keep])

    if os.path.splitdrive(os.getcwd())[0].upper() == drive:
        os.chdir(system)  # a program can't eject the drive it is standing on
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAY)
        result, reason = win.request_eject(node)
        if result == 0:
            log.info("Ejected %s", drive)
            return
    log.warning("Eject of %s refused: code %s, %s", drive, result, reason)
    raise EjectError(
        "Windows says the SSD is still in use. Close anything using it (Photoshop, a photo viewer,\n"
        f"a Command Prompt showing {drive}\\>), then try again. Don't unplug it yet.")


class _Windows:
    """The few Win32 calls needed."""

    DISK_INTERFACE = uuid.UUID("53f56307-b6bf-11d0-94f2-00a0c91efb8b")
    INSTANCE_ID_KEY = (uuid.UUID("78c34fc8-104a-4aca-9ea4-524d52996e57"), 256)
    IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
    CM_DRP_CAPABILITIES = 0x10
    DISK_DEVICE = 7

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        class Guid(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ubyte * 16)]

        class PropertyKey(ctypes.Structure):
            _fields_ = [("fmtid", Guid), ("pid", w.ULONG)]

        self.ct, self.w = ctypes, w
        self.guid = Guid.from_buffer_copy(self.DISK_INTERFACE.bytes_le)
        fmtid, pid = self.INSTANCE_ID_KEY
        self.key = PropertyKey(Guid.from_buffer_copy(fmtid.bytes_le), pid)

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.cm = ctypes.WinDLL("cfgmgr32")
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                            w.DWORD, w.DWORD, w.HANDLE]
        self.kernel.DeviceIoControl.restype = w.BOOL
        self.kernel.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD,
                                                ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
                                                ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]

    def disk_number(self, drive: str) -> Optional[int]:
        return self._number("\\\\.\\" + drive)

    def _number(self, path: str) -> Optional[int]:
        ct, w = self.ct, self.w
        # Access 0 only queries the device, so it needs no admin rights.
        handle = self.kernel.CreateFileW(path, 0, 3, None, 3, 0, None)
        if handle == ct.c_void_p(-1).value:
            return None
        try:
            out, got = (w.DWORD * 3)(), w.DWORD()
            ok = self.kernel.DeviceIoControl(handle, self.IOCTL_STORAGE_GET_DEVICE_NUMBER,
                                             None, 0, out, ct.sizeof(out), ct.byref(got), None)
            return out[1] if ok and out[0] == self.DISK_DEVICE else None
        finally:
            self.kernel.CloseHandle(handle)

    def disks(self) -> Dict[int, List[int]]:
        ct, w = self.ct, self.w
        size = w.ULONG()
        if self.cm.CM_Get_Device_Interface_List_SizeW(ct.byref(size), ct.byref(self.guid), None, 0):
            raise EjectError("Windows didn't list the disks.")
        buf = ct.create_unicode_buffer(size.value)
        if self.cm.CM_Get_Device_Interface_ListW(ct.byref(self.guid), None, buf, size, 0):
            raise EjectError("Windows didn't list the disks.")
        found = {}
        for path in filter(None, buf[:].split("\0")):
            number = self._number(path)
            if number is None:
                continue
            kind, name = w.ULONG(), ct.create_unicode_buffer(512)
            length, node = w.ULONG(ct.sizeof(name)), w.DWORD()
            if (self.cm.CM_Get_Device_Interface_PropertyW(
                    ct.c_wchar_p(path), ct.byref(self.key), ct.byref(kind), name, ct.byref(length), 0)
                    or self.cm.CM_Locate_DevNodeW(ct.byref(node), name, 0)):
                # An unknown disk could share the device being ejected, so don't guess.
                raise EjectError("Could not identify every disk, so nothing was ejected.")
            chain, parent = [node.value], w.DWORD()
            while len(chain) < 32 and not self.cm.CM_Get_Parent(ct.byref(parent), w.DWORD(chain[-1]), 0):
                chain.append(parent.value)
            found[number] = chain
        return found

    def capabilities(self, node: int) -> int:
        ct, w = self.ct, self.w
        value, size = w.DWORD(), w.ULONG(4)
        failed = self.cm.CM_Get_DevNode_Registry_PropertyW(
            w.DWORD(node), self.CM_DRP_CAPABILITIES, None, ct.byref(value), ct.byref(size), 0)
        return 0 if failed else value.value

    def request_eject(self, node: int):
        ct, w = self.ct, self.w
        veto, name = ct.c_int(0), ct.create_unicode_buffer(260)
        # Passing the buffers stops Windows showing its own pop-up.
        result = self.cm.CM_Request_Device_EjectW(w.DWORD(node), ct.byref(veto), name, 260, 0)
        if result == 0 and veto.value:
            result = -1
        return result, f"veto {veto.value} {name.value}"
