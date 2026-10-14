"""Find the ffmpeg/ffprobe executables.

Search order:
  1. The app's own FFmpeg folder (where the first-run downloader will put it):
     %LOCALAPPDATA%\\VideoMerger\\ffmpeg\\bin
  2. An "ffmpeg\\bin" folder next to the program (for portable/bundled setups)
  3. The system PATH
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME


@dataclass(frozen=True)
class FFmpegPaths:
    ffmpeg: Path
    ffprobe: Path


def app_data_dir() -> Path:
    # VIDEOMERGER_HOME lets tests (or portable setups) use another folder.
    if override := os.environ.get("VIDEOMERGER_HOME"):
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def _only_managed() -> bool:
    """Set VIDEOMERGER_ONLY_MANAGED_FFMPEG=1 to ignore FFmpeg on PATH, e.g. to
    try the first-run download on a PC that already has FFmpeg."""
    return os.environ.get("VIDEOMERGER_ONLY_MANAGED_FFMPEG") == "1"


def managed_ffmpeg_dir() -> Path:
    """Folder the downloader installs FFmpeg into."""
    return app_data_dir() / "ffmpeg"


def _program_dir() -> Path:
    # When frozen by PyInstaller, sys.executable is the .exe itself.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _candidate_bin_dirs() -> list[Path]:
    dirs = [managed_ffmpeg_dir() / "bin"]
    if not _only_managed():
        dirs.append(_program_dir() / "ffmpeg" / "bin")
    return dirs


def find_ffmpeg() -> FFmpegPaths | None:
    """Return paths to ffmpeg and ffprobe, or None if either is missing."""
    for bin_dir in _candidate_bin_dirs():
        ffmpeg = bin_dir / "ffmpeg.exe"
        ffprobe = bin_dir / "ffprobe.exe"
        if ffmpeg.is_file() and ffprobe.is_file():
            return FFmpegPaths(ffmpeg, ffprobe)

    if _only_managed():
        return None
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return FFmpegPaths(Path(ffmpeg), Path(ffprobe))
    return None
