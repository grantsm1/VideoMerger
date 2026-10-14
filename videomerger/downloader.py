"""First-run FFmpeg download.

Downloads a Windows FFmpeg build, checks its SHA-256 against the checksum the
publisher posts, makes sure it runs and can encode H.264, then installs only
ffmpeg.exe, ffprobe.exe and the license files into
%LOCALAPPDATA%\\VideoMerger\\ffmpeg. If the first source fails the next one
is tried.

VideoMerger never ships FFmpeg itself; the user's PC fetches it directly from
the publisher, together with its license.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from . import APP_NAME, __version__
from .ffmpeg_locator import FFmpegPaths, app_data_dir, managed_ffmpeg_dir

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_CHUNK = 256 * 1024
_TIMEOUT = 30          # seconds without data before giving up
_INFO_FILE = "source.json"
_EXTRA_FILES = {"license", "license.txt", "readme.txt"}

# (phase, bytes_done, bytes_total or None). Phases: "download", "verify", "install"
ProgressCallback = Callable[[str, int, "int | None"], None]


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class Source:
    name: str             # shown to the user
    url: str
    checksum_url: str
    approx_mb: int
    homepage: str


SOURCES: tuple[Source, ...] = (
    Source("gyan.dev (FFmpeg release, essentials build)",
           "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
           "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256",
           110, "https://www.gyan.dev/ffmpeg/builds/"),
    Source("BtbN on GitHub (FFmpeg latest, GPL build)",
           "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
           "ffmpeg-master-latest-win64-gpl.zip",
           "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256",
           190, "https://github.com/BtbN/FFmpeg-Builds"),
)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def installed_info() -> dict | None:
    """Details about the FFmpeg this app downloaded, or None."""
    try:
        return json.loads((managed_ffmpeg_dir() / _INFO_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def download_ffmpeg(on_progress: ProgressCallback | None = None,
                    cancel: threading.Event | None = None,
                    sources: Sequence[Source] | None = None) -> FFmpegPaths:
    """Download and install FFmpeg, trying each source in turn. Blocking."""
    cancel = cancel or threading.Event()
    problems: list[str] = []
    for source in sources or SOURCES:
        try:
            return _install_from(source, on_progress, cancel)
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            problems.append(f"• {source.name}: {exc}")
    raise DownloadError("FFmpeg couldn't be downloaded.\n\n" + "\n".join(problems))


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def _install_from(source: Source, on_progress: ProgressCallback | None,
                  cancel: threading.Event) -> FFmpegPaths:
    root = app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    # Work inside the app folder so the final move is a same-drive rename.
    work = Path(tempfile.mkdtemp(prefix="download_", dir=root))
    try:
        expected = _fetch_checksum(source)
        zip_path = work / "ffmpeg.zip"
        actual = _download(source.url, zip_path, on_progress, cancel)
        if actual.lower() != expected.lower():
            raise DownloadError("the download is damaged or was changed "
                                "(checksum doesn't match). Please try again.")

        if on_progress:
            on_progress("verify", 0, None)
        staging = work / "ffmpeg"
        _extract(zip_path, staging)
        version = _check_runs(staging / "bin")
        _write_info(staging, source, version, actual)

        if cancel.is_set():
            raise DownloadCancelled()
        if on_progress:
            on_progress("install", 0, None)
        _swap_in(staging, managed_ffmpeg_dir())
    finally:
        shutil.rmtree(work, ignore_errors=True)

    bin_dir = managed_ffmpeg_dir() / "bin"
    return FFmpegPaths(bin_dir / "ffmpeg.exe", bin_dir / "ffprobe.exe")


def _ssl_contexts() -> list[tuple[str, ssl.SSLContext]]:
    """Ways to check HTTPS certificates, tried in order.

    Python's default check only reads the certificates already stored in
    Windows. A freshly installed Windows stores very few and fetches the rest
    on demand, but only when Windows' own verification asks for them, so the
    default check fails with CERTIFICATE_VERIFY_FAILED on new PCs.

    1. truststore: verify through Windows itself (same as Edge), which also
       fetches missing root certificates.
    2. certifi: Mozilla's list of trusted authorities, bundled with the app,
       in case Windows can't fetch roots (e.g. blocked by a company policy).
    """
    import certifi
    import truststore

    return [("Windows", truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)),
            ("bundled", ssl.create_default_context(cafile=certifi.where()))]


def _is_cert_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or \
        "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _open(url: str):
    request = urllib.request.Request(
        url, headers={"User-Agent": f"{APP_NAME}/{__version__}"})
    contexts = _ssl_contexts() if url.lower().startswith("https:") else [("none", None)]
    cert_errors: list[str] = []
    for label, context in contexts:
        try:
            return urllib.request.urlopen(request, timeout=_TIMEOUT, context=context)
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"the server answered {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            if _is_cert_error(exc):
                cert_errors.append(f"{label}: {getattr(exc, 'reason', exc)}")
                continue          # try the next way of checking certificates
            reason = getattr(exc, "reason", exc)
            raise DownloadError(f"couldn't connect ({reason}). "
                                "Check your internet connection.") from exc
    raise DownloadError("couldn't verify the website's security certificate ("
                        + "; ".join(cert_errors) + "). If you use antivirus "
                        "\"HTTPS scanning\" or a company network, that may be the cause.")


def _fetch_checksum(source: Source) -> str:
    with _open(source.checksum_url) as resp:
        text = resp.read(1024 * 1024).decode("utf-8", "replace")
    zip_name = source.url.rsplit("/", 1)[-1]
    for line in text.splitlines():
        parts = line.split()
        if not parts or not _HEX64.match(parts[0]):
            continue
        # Either a bare hash, or "hash  filename" lines (sha256sum format).
        if len(parts) == 1 or parts[-1].lstrip("*") == zip_name:
            return parts[0]
    raise DownloadError("couldn't read the published checksum")


def _download(url: str, dest: Path, on_progress: ProgressCallback | None,
              cancel: threading.Event) -> str:
    """Stream to `dest`, return its SHA-256 hex digest."""
    digest = hashlib.sha256()
    done = 0
    last_report = 0.0
    with _open(url) as resp, open(dest, "wb") as out:
        length = resp.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        while True:
            if cancel.is_set():
                raise DownloadCancelled()
            try:
                chunk = resp.read(_CHUNK)
            except OSError as exc:
                raise DownloadError(f"the connection dropped ({exc})") from exc
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if on_progress and now - last_report > 0.1:
                on_progress("download", done, total)
                last_report = now
    if total is not None and done != total:
        raise DownloadError("the download stopped before it finished")
    if on_progress:
        on_progress("download", done, total or done)
    return digest.hexdigest()


def _extract(zip_path: Path, dest: Path) -> None:
    """Copy out bin/ffmpeg.exe, bin/ffprobe.exe and top-level license files.

    Destination names are chosen here, never taken from the archive, so a
    malicious archive can't write outside `dest`.
    """
    (dest / "bin").mkdir(parents=True)
    found: set[str] = set()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.replace("\\", "/").split("/")
                base = parts[-1].lower()
                if base in ("ffmpeg.exe", "ffprobe.exe") and len(parts) >= 2 \
                        and parts[-2].lower() == "bin" and base not in found:
                    target = dest / "bin" / base
                elif base in _EXTRA_FILES and len(parts) <= 2:
                    target = dest / parts[-1]
                else:
                    continue
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
                found.add(base)
    except zipfile.BadZipFile as exc:
        raise DownloadError("the downloaded file isn't a valid zip") from exc
    missing = {"ffmpeg.exe", "ffprobe.exe"} - found
    if missing:
        raise DownloadError(f"the download doesn't contain {', '.join(sorted(missing))}")


def _check_runs(bin_dir: Path) -> str:
    """Make sure the new FFmpeg starts and can do what we need. Returns version."""
    def run(exe: str, *args: str) -> str:
        try:
            proc = subprocess.run([str(bin_dir / exe), "-hide_banner", *args],
                                  capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DownloadError(f"{exe} won't start ({exc})") from exc
        if proc.returncode != 0:
            raise DownloadError(f"{exe} won't start (exit code {proc.returncode})")
        return proc.stdout.decode("utf-8", "replace")

    first_line = run("ffmpeg.exe", "-version").splitlines()[0]
    run("ffprobe.exe", "-version")
    if "libx264" not in run("ffmpeg.exe", "-encoders"):
        raise DownloadError("this FFmpeg build can't encode H.264 video")
    match = re.match(r"ffmpeg version (\S+)", first_line)
    return match.group(1) if match else first_line


def _write_info(staging: Path, source: Source, version: str, sha256: str) -> None:
    info = {"version": version, "source": source.name, "url": source.url,
            "homepage": source.homepage, "sha256": sha256,
            "installed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (staging / _INFO_FILE).write_text(json.dumps(info, indent=2), "utf-8")


def _rename(src: Path, dst: Path, attempts: int = 20) -> None:
    """Rename a folder, retrying briefly.

    Right after an .exe inside a folder has run (or been written), Windows
    Defender and the process loader can hold it open for a moment, which makes
    the rename fail with "Access is denied".
    """
    for attempt in range(attempts):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)


def _swap_in(staging: Path, target: Path) -> None:
    """Replace any existing install in one move, keeping it if something fails."""
    old = target.with_name(target.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        try:
            _rename(target, old)
        except OSError as exc:
            raise DownloadError("the current FFmpeg is in use. Close any merges "
                                "in progress and try again.") from exc
    try:
        _rename(staging, target)
    except OSError as exc:
        if old.exists():
            _rename(old, target)   # put the previous version back
        raise DownloadError(f"couldn't install FFmpeg ({exc})") from exc
    shutil.rmtree(old, ignore_errors=True)
