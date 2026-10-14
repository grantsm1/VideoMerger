"""Per-user "install": copy the exe to a fixed place and add shortcuts.

The downloaded VideoMerger.exe can live anywhere (often Downloads, which
people clean out). Installing copies it to
    %LOCALAPPDATA%\\Programs\\VideoMerger\\VideoMerger.exe
and puts "VideoMerger" shortcuts on the Desktop and in the Start menu, so the
icons keep working after the download is deleted. No administrator rights
are needed; nothing outside the user's own folders is touched.

Shortcuts are created through Windows' own Shell Link COM interface via
ctypes (no PowerShell, no extra packages).
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import APP_NAME

EXE_NAME = f"{APP_NAME}.exe"
SHORTCUT_NAME = f"{APP_NAME}.lnk"
DESCRIPTION = "Merge video files into one"

# Known folder IDs (KNOWNFOLDERID). Using these instead of guessing paths
# handles Desktops that OneDrive or a company has moved elsewhere.
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"          # Start menu
_FOLDERID_USER_PROGRAM_FILES = "{5CD7AEE2-2219-4A67-B85D-6C9CE15660CB}"  # LocalAppData\Programs


class InstallError(Exception):
    pass


class InstallState(Enum):
    NOT_INSTALLED = "not installed"
    THIS_COPY = "running the installed copy"
    SAME = "installed, same version"
    DIFFERENT = "installed, different version"


@dataclass
class InstallResult:
    exe: Path
    shortcuts: list[Path]
    copied: bool


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        u = uuid.UUID(text)
        self.Data1, self.Data2, self.Data3 = u.fields[0], u.fields[1], u.fields[2]
        self.Data4[:] = u.bytes[8:]


def _known_folder(folder_id: str) -> Path:
    path = ctypes.c_wchar_p()
    ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(_GUID(folder_id)), 0, None, ctypes.byref(path))
    try:
        if not path.value:
            raise InstallError(f"Windows didn't return folder {folder_id}")
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def desktop_dir() -> Path:
    return _known_folder(_FOLDERID_DESKTOP)


def start_menu_dir() -> Path:
    return _known_folder(_FOLDERID_PROGRAMS)


def install_dir() -> Path:
    return _known_folder(_FOLDERID_USER_PROGRAM_FILES) / APP_NAME


def installed_exe() -> Path:
    return install_dir() / EXE_NAME


def shortcut_paths() -> list[Path]:
    return [desktop_dir() / SHORTCUT_NAME, start_menu_dir() / SHORTCUT_NAME]


def running_exe() -> Path | None:
    """The .exe we're running as, or None when running from Python source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def install_state(exe: Path) -> InstallState:
    target = installed_exe()
    if _same_file(exe, target):
        return InstallState.THIS_COPY
    if not target.is_file():
        return InstallState.NOT_INSTALLED
    if target.stat().st_size == exe.stat().st_size and _sha256(target) == _sha256(exe):
        return InstallState.SAME
    return InstallState.DIFFERENT


def shortcuts_exist() -> bool:
    return all(p.is_file() for p in shortcut_paths())


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

def install(exe: Path) -> InstallResult:
    """Copy `exe` into place (if needed) and create both shortcuts."""
    exe = Path(exe).resolve()
    if not exe.is_file():
        raise InstallError(f"{exe} doesn't exist")
    target = installed_exe()
    copied = False

    if not _same_file(exe, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy beside the target first, then swap it in, so a failed copy
        # never leaves a broken VideoMerger.exe behind.
        temp = target.with_name(target.name + ".new")
        try:
            shutil.copy2(exe, temp)
            os.replace(temp, target)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise InstallError(
                "Couldn't copy VideoMerger into place. If the installed VideoMerger "
                f"is open, close it and try again. ({exc})") from exc
        copied = True

    shortcuts = []
    for link in shortcut_paths():
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            create_shortcut(link, target, DESCRIPTION)
        except OSError as exc:
            raise InstallError(f"Couldn't create the shortcut {link} ({exc})") from exc
        shortcuts.append(link)
    return InstallResult(target, shortcuts, copied)


# --------------------------------------------------------------------------- #
# Shortcut (.lnk) creation through IShellLinkW / IPersistFile
# --------------------------------------------------------------------------- #

_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_CLSCTX_INPROC_SERVER = 1

# Method positions in the COM vtables (they follow the interface definitions
# in ShObjIdl_core.h / ObjIdl.h).
_RELEASE = 2
_LINK_SET_DESCRIPTION = 7
_LINK_SET_WORKING_DIRECTORY = 9
_LINK_SET_ICON_LOCATION = 17
_LINK_SET_PATH = 20
_PERSIST_SAVE = 6


def _call(obj: ctypes.c_void_p, index: int, *args, argtypes=()):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])(obj, *args)   # HRESULT failures raise OSError


def _release(obj: ctypes.c_void_p):
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(obj)


def create_shortcut(link: Path, target: Path, description: str = "") -> None:
    """Write a .lnk at `link` that starts `target`, using its own icon."""
    ole32 = ctypes.oledll.ole32
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # apartment threaded
    initialized = hr in (0, 1)                           # S_OK / S_FALSE
    shell_link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(_GUID(_CLSID_SHELL_LINK)), None,
                               _CLSCTX_INPROC_SERVER,
                               ctypes.byref(_GUID(_IID_ISHELL_LINK_W)),
                               ctypes.byref(shell_link))
        wstr = (ctypes.c_wchar_p,)
        _call(shell_link, _LINK_SET_PATH, str(target), argtypes=wstr)
        _call(shell_link, _LINK_SET_WORKING_DIRECTORY, str(target.parent), argtypes=wstr)
        _call(shell_link, _LINK_SET_DESCRIPTION, description, argtypes=wstr)
        _call(shell_link, _LINK_SET_ICON_LOCATION, str(target), 0,
              argtypes=(ctypes.c_wchar_p, ctypes.c_int))
        _call(shell_link, 0, ctypes.byref(_GUID(_IID_IPERSIST_FILE)), ctypes.byref(persist),
              argtypes=(ctypes.c_void_p, ctypes.c_void_p))          # QueryInterface
        _call(persist, _PERSIST_SAVE, str(link), True,
              argtypes=(ctypes.c_wchar_p, ctypes.c_int))
    finally:
        _release(persist)
        _release(shell_link)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def read_shortcut_target(link: Path) -> Path:
    """Where a .lnk points (used by tests and to check existing shortcuts)."""
    ole32 = ctypes.oledll.ole32
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    initialized = hr in (0, 1)
    shell_link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(_GUID(_CLSID_SHELL_LINK)), None,
                               _CLSCTX_INPROC_SERVER,
                               ctypes.byref(_GUID(_IID_ISHELL_LINK_W)),
                               ctypes.byref(shell_link))
        _call(shell_link, 0, ctypes.byref(_GUID(_IID_IPERSIST_FILE)), ctypes.byref(persist),
              argtypes=(ctypes.c_void_p, ctypes.c_void_p))
        _call(persist, 5, str(link), 0, argtypes=(ctypes.c_wchar_p, ctypes.c_ulong))  # Load
        buf = ctypes.create_unicode_buffer(32768)
        _call(shell_link, 3, buf, len(buf), None, 0,                                   # GetPath
              argtypes=(ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong))
        return Path(buf.value)
    finally:
        _release(persist)
        _release(shell_link)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()
