r"""Every ctypes call and every Windows path quirk lives here.

Nothing else in the package touches ``ctypes``. Keeping the Win32 surface in one
module is what lets the rest of the code be faked in tests: calling
``ctypes.windll`` from inside the media-detection function would leave no seam,
and none of the media logic could then be tested without a physical disc.

Two details that are easy to get wrong and expensive to debug:

* ``restype``/``argtypes`` are declared for every function. Without an explicit
  ``restype = HANDLE``, ctypes truncates a 64-bit handle to a signed 32-bit int
  and you get either a bogus handle or a spurious INVALID_HANDLE_VALUE.
* The device is opened as ``\\.\D:`` with **no** trailing backslash. With one,
  you open the filesystem root instead of the device and every IOCTL fails.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import PurePath, PureWindowsPath

# --------------------------------------------------------------------------
# Paths (pure, testable anywhere)
# --------------------------------------------------------------------------

MAX_PATH = 260
LONG_PATH_PREFIX = "\\\\?\\"
LONG_PATH_UNC_PREFIX = "\\\\?\\UNC\\"


def long_path(path: str | os.PathLike[str]) -> str:
    r"""Return *path* in the extended-length form Windows needs past MAX_PATH.

        Z:\a\b            ->  \\?\Z:\a\b
        \\server\share\a  ->  \\?\UNC\server\share\a

    The prefix disables all path normalisation, so the input must already be
    absolute: a relative path would silently resolve against the wrong root, so
    we raise instead. ``..`` is rejected for the same reason - Windows would
    take it literally. A lone ``.`` needs no special handling because pathlib
    drops it while parsing, before it can reach the prefix.
    """
    text = os.fspath(path)
    if text.startswith(LONG_PATH_PREFIX):
        return text

    pure = PureWindowsPath(text)
    if not pure.is_absolute():
        raise ValueError(f"long_path() requires an absolute path: {text!r}")
    if any(part == ".." for part in pure.parts):
        raise ValueError(f"long_path() cannot take a path containing '..': {text!r}")

    normalised = str(pure)
    if normalised.startswith("\\\\"):
        return LONG_PATH_UNC_PREFIX + normalised[2:]
    return LONG_PATH_PREFIX + normalised


def needs_long_path(path: str | os.PathLike[str]) -> bool:
    """True when *path* is long enough that Windows will reject the plain form."""
    text = os.fspath(path)
    return not text.startswith(LONG_PATH_PREFIX) and len(text) >= MAX_PATH


def safe_path(path: str | os.PathLike[str]) -> str:
    """The plain path, upgraded to extended-length form only when it has to be.

    Used for every filesystem call in the copy loop: short paths stay readable
    in tracebacks, long ones still work.
    """
    text = os.fspath(path)
    if not needs_long_path(text):
        return text
    try:
        return long_path(text)
    except ValueError:
        # Relative or dot-containing: nothing safe to do, let the OS complain.
        return text


def longest_destination_path(destination_root: PurePath, relative_paths) -> int:
    """Length of the longest full path a copy would create under *destination_root*.

    Used for the pre-flight warning: a deep disc tree under a nested network
    destination clears 260 characters easily, and finding that out 40 minutes
    into a copy is expensive.
    """
    root_text = str(destination_root)
    longest = len(root_text)
    for relative in relative_paths:
        candidate = len(root_text) + 1 + len(str(relative))
        if candidate > longest:
            longest = candidate
    return longest


# --------------------------------------------------------------------------
# Win32 constants
# --------------------------------------------------------------------------

DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

SEM_FAILCRITICALERRORS = 0x0001

# CTL_CODE(DeviceType, Function, Method, Access)
#   = (DeviceType << 16) | (Access << 14) | (Function << 2) | Method
# FILE_DEVICE_MASS_STORAGE = 0x2D, METHOD_BUFFERED = 0, FILE_READ_ACCESS = 1
IOCTL_STORAGE_CHECK_VERIFY = 0x002D4800
IOCTL_STORAGE_CHECK_VERIFY2 = 0x002D0800  # same, but needs no access rights
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
IOCTL_STORAGE_LOAD_MEDIA = 0x002D480C  # close the tray

# FILE_DEVICE_FILE_SYSTEM = 0x09
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020

ERROR_ACCESS_DENIED = 5
ERROR_NOT_READY = 21

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def is_windows() -> bool:
    return os.name == "nt"


class _PreventMediaRemoval(ctypes.Structure):
    _fields_ = [("PreventMediaRemoval", ctypes.c_byte)]


_kernel32 = None


def _k32():
    """Lazily bind kernel32 with explicit signatures (see the module docstring)."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    if not is_windows():
        raise OSError("Win32 calls are only available on Windows")

    lib = ctypes.WinDLL("kernel32", use_last_error=True)

    lib.GetLogicalDrives.restype = wintypes.DWORD
    lib.GetLogicalDrives.argtypes = []

    lib.GetDriveTypeW.restype = wintypes.UINT
    lib.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]

    lib.GetVolumeInformationW.restype = wintypes.BOOL
    lib.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,  # lpRootPathName
        wintypes.LPWSTR,   # lpVolumeNameBuffer
        wintypes.DWORD,    # nVolumeNameSize
        ctypes.POINTER(wintypes.DWORD),  # lpVolumeSerialNumber
        ctypes.POINTER(wintypes.DWORD),  # lpMaximumComponentLength
        ctypes.POINTER(wintypes.DWORD),  # lpFileSystemFlags
        wintypes.LPWSTR,   # lpFileSystemNameBuffer
        wintypes.DWORD,    # nFileSystemNameSize
    ]

    # HANDLE, not int - the single most common ctypes bug on 64-bit Windows.
    lib.CreateFileW.restype = wintypes.HANDLE
    lib.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]

    lib.DeviceIoControl.restype = wintypes.BOOL
    lib.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]

    lib.CloseHandle.restype = wintypes.BOOL
    lib.CloseHandle.argtypes = [wintypes.HANDLE]

    lib.SetErrorMode.restype = wintypes.UINT
    lib.SetErrorMode.argtypes = [wintypes.UINT]

    _kernel32 = lib
    return lib


def silence_missing_media_dialogs() -> None:
    """Stop Windows popping "There is no disk in drive D:" while we poll.

    With a once-per-second poll against an empty drive that dialog will
    eventually appear. Called once at startup.
    """
    if not is_windows():
        return
    lib = _k32()
    previous = lib.SetErrorMode(SEM_FAILCRITICALERRORS)
    lib.SetErrorMode(previous | SEM_FAILCRITICALERRORS)


# --------------------------------------------------------------------------
# Drive enumeration
# --------------------------------------------------------------------------


def logical_drive_roots() -> list[str]:
    r"""Every mounted drive root, e.g. ["C:\\", "D:\\", "Z:\\"]."""
    if not is_windows():
        return []
    mask = _k32().GetLogicalDrives()
    return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]


def drive_type(root: str) -> int:
    if not is_windows():
        return DRIVE_UNKNOWN
    return _k32().GetDriveTypeW(root)


def optical_drive_roots() -> list[str]:
    r"""Roots of every CD/DVD/BD drive, whether or not a disc is loaded."""
    return [root for root in logical_drive_roots() if drive_type(root) == DRIVE_CDROM]


@dataclass(frozen=True)
class VolumeInfo:
    label: str
    serial: int | None
    fs_name: str


def volume_information(root: str) -> VolumeInfo | None:
    """Label, serial and filesystem name, or None when no disc is readable.

    The filesystem name is worth asking for: CDFS vs UDF is a useful signal
    when deciding whether a disc is audio.
    """
    if not is_windows():
        return None
    label_buffer = ctypes.create_unicode_buffer(261)
    fs_buffer = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()

    ok = _k32().GetVolumeInformationW(
        root,
        label_buffer,
        len(label_buffer),
        ctypes.byref(serial),
        None,
        None,
        fs_buffer,
        len(fs_buffer),
    )
    if not ok:
        return None
    return VolumeInfo(
        label=label_buffer.value,
        serial=serial.value or None,
        fs_name=fs_buffer.value,
    )


# --------------------------------------------------------------------------
# Device handles, media presence, eject
# --------------------------------------------------------------------------


def _device_path(drive: str) -> str:
    r"""'D:' or 'D:\' or 'D' -> '\\.\D:' (no trailing backslash)."""
    letter = drive.strip().rstrip("\\/").rstrip(":")[:1].upper()
    if not letter.isalpha():
        raise ValueError(f"Not a drive letter: {drive!r}")
    return f"\\\\.\\{letter}:"


def _open_device(drive: str, write: bool = True):
    """Open the raw device. Falls back to read-only on access-denied.

    Read access alone is enough for IOCTL_STORAGE_EJECT_MEDIA on a CD-ROM; only
    lock/dismount really want write, and those are best-effort anyway. Without
    this fallback, eject would need elevation.
    """
    lib = _k32()
    access = (GENERIC_READ | GENERIC_WRITE) if write else GENERIC_READ
    handle = lib.CreateFileW(
        _device_path(drive),
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        if write and code == ERROR_ACCESS_DENIED:
            return _open_device(drive, write=False)
        raise ctypes.WinError(code)
    return handle


def _ioctl(handle, code: int, in_buffer=None, in_size: int = 0) -> tuple[bool, int]:
    """Returns (succeeded, last_error)."""
    returned = wintypes.DWORD()
    ok = _k32().DeviceIoControl(
        handle,
        code,
        in_buffer,
        in_size,
        None,
        0,
        ctypes.byref(returned),
        None,
    )
    return bool(ok), (0 if ok else ctypes.get_last_error())


def media_present(drive: str) -> bool:
    """True when a disc is loaded and readable.

    Uses CHECK_VERIFY2 on the device rather than touching the filesystem root,
    so an empty drive costs one failed IOCTL instead of a slow, noisy path probe.
    """
    if not is_windows():
        return False
    try:
        handle = _open_device(drive, write=False)
    except OSError:
        return False
    try:
        ok, _ = _ioctl(handle, IOCTL_STORAGE_CHECK_VERIFY2)
        return ok
    finally:
        _k32().CloseHandle(handle)


@dataclass
class EjectResult:
    ok: bool
    drive: str
    steps: list[tuple[str, bool, str]] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        done = ", ".join(name for name, ok, _ in self.steps if ok) or "nenhuma"
        return f"{self.drive}: etapas concluidas: {done}"


def eject(drive: str, lock_retries: int = 10, lock_delay: float = 0.25) -> EjectResult:
    """Open the tray of *drive*.

    Lock and dismount are best-effort: if another process holds the volume we
    still attempt the eject, because it usually works anyway.

    On this machine the drive is an external USB unit, and those are
    inconsistent - the IOCTL can report success while the tray never moves. So
    eject is treated as advisory everywhere it is called: the runner always
    falls through to "wait for the disc to be removed" and never blocks on it.
    """
    result = EjectResult(ok=False, drive=drive)
    if not is_windows():
        result.error = "Eject is only available on Windows"
        return result

    lib = _k32()
    try:
        handle = _open_device(drive)
    except OSError as exc:
        result.error = str(exc)
        return result

    try:
        locked = False
        for attempt in range(lock_retries):
            locked, code = _ioctl(handle, FSCTL_LOCK_VOLUME)
            if locked:
                break
            if attempt + 1 < lock_retries:
                time.sleep(lock_delay)
        result.steps.append(("lock", locked, "" if locked else "volume em uso"))

        dismounted, code = _ioctl(handle, FSCTL_DISMOUNT_VOLUME)
        result.steps.append(("dismount", dismounted, "" if dismounted else str(code)))

        allow = _PreventMediaRemoval(PreventMediaRemoval=0)
        allowed, code = _ioctl(
            handle,
            IOCTL_STORAGE_MEDIA_REMOVAL,
            ctypes.byref(allow),
            ctypes.sizeof(allow),
        )
        result.steps.append(("allow_removal", allowed, "" if allowed else str(code)))

        ejected, code = _ioctl(handle, IOCTL_STORAGE_EJECT_MEDIA)
        result.steps.append(("eject", ejected, "" if ejected else str(code)))
        result.ok = ejected
        if not ejected:
            result.error = str(ctypes.WinError(code)) if code else "eject falhou"
        return result
    finally:
        lib.CloseHandle(handle)


def close_tray(drive: str) -> bool:
    """Pull the tray back in. Off by default - it surprises people."""
    if not is_windows():
        return False
    try:
        handle = _open_device(drive)
    except OSError:
        return False
    try:
        ok, _ = _ioctl(handle, IOCTL_STORAGE_LOAD_MEDIA)
        return ok
    finally:
        _k32().CloseHandle(handle)
