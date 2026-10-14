"""Downloader tests against local file:// "servers" built from the installed FFmpeg.

The real internet download is covered by tests/test_downloader_live.py
(opt-in, because it fetches ~110 MB).
"""

from __future__ import annotations

import hashlib
import io
import os
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from videomerger import downloader
from videomerger.downloader import (DownloadCancelled, DownloadError, Source,
                                    download_ffmpeg, installed_info)
from videomerger.ffmpeg_locator import find_ffmpeg, managed_ffmpeg_dir

from conftest import PATHS

pytestmark = pytest.mark.skipif(PATHS is None, reason="needs an FFmpeg to package")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fake_source(folder: Path, name: str, members: dict[str, Path | bytes],
                 checksum: str | None = None, bare_hash: bool = False) -> Source:
    """Write a zip + checksum file and return a Source pointing at them."""
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for arcname, content in members.items():
            if isinstance(content, Path):
                zf.write(content, arcname)
            else:
                zf.writestr(arcname, content)
    digest = checksum or _sha256(zip_path)
    sums = folder / f"{name}.sha256"
    sums.write_text(digest if bare_hash else
                    f"{'0' * 64}  other-file.zip\n{digest}  {zip_path.name}\n")
    return Source(f"fake {name}", zip_path.as_uri(), sums.as_uri(), 1, "https://example.invalid")


def _good_members() -> dict[str, Path | bytes]:
    return {
        "ffmpeg-9-test/bin/ffmpeg.exe": PATHS.ffmpeg,
        "ffmpeg-9-test/bin/ffprobe.exe": PATHS.ffprobe,
        "ffmpeg-9-test/LICENSE": b"GPL text",
        "ffmpeg-9-test/doc/readme.html": b"not copied",
        "../evil.txt": b"must not escape",
    }


@pytest.fixture(scope="session")
def servers(tmp_path_factory):
    d = tmp_path_factory.mktemp("server")
    return {
        "good": _fake_source(d, "good", _good_members(), bare_hash=True),
        "good_list": _fake_source(d / "list", "good", _good_members()),
        "tampered": _fake_source(d, "tampered", _good_members(), checksum="ab" * 32),
        "no_probe": _fake_source(d, "no_probe",
                                 {"x/bin/ffmpeg.exe": PATHS.ffmpeg, "x/LICENSE": b"GPL"}),
        "missing": Source("fake missing", (d / "nope.zip").as_uri(),
                          (d / "nope.sha256").as_uri(), 1, "https://example.invalid"),
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEOMERGER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIDEOMERGER_ONLY_MANAGED_FFMPEG", "1")
    return tmp_path / "home"


def test_install(home, servers):
    assert find_ffmpeg() is None
    phases = []
    paths = download_ffmpeg(lambda p, d, t: phases.append(p), sources=[servers["good"]])

    assert paths.ffmpeg.parent == managed_ffmpeg_dir() / "bin"
    assert subprocess.run([str(paths.ffprobe), "-version"], capture_output=True).returncode == 0
    assert find_ffmpeg() == paths
    assert {"download", "verify", "install"} <= set(phases)
    assert (managed_ffmpeg_dir() / "LICENSE").read_bytes() == b"GPL text"
    assert not (home / "evil.txt").exists() and not (home.parent / "evil.txt").exists()
    assert installed_info()["source"] == "fake good"
    # The temporary download folder is cleaned up.
    assert sorted(p.name for p in home.iterdir()) == ["ffmpeg"]


def test_checksum_list_format(home, servers):
    download_ffmpeg(sources=[servers["good_list"]])
    assert find_ffmpeg() is not None


def test_tampered_download_is_rejected_and_next_source_used(home, servers):
    download_ffmpeg(sources=[servers["tampered"], servers["good"]])
    assert installed_info()["source"] == "fake good"


def test_all_sources_fail(home, servers):
    with pytest.raises(DownloadError) as err:
        download_ffmpeg(sources=[servers["tampered"], servers["missing"], servers["no_probe"]])
    message = str(err.value)
    assert "checksum doesn't match" in message
    assert "couldn't connect" in message
    assert "doesn't contain ffprobe.exe" in message
    assert find_ffmpeg() is None
    assert not any(home.iterdir())


def test_cancel(home, servers):
    cancel = threading.Event()
    with pytest.raises(DownloadCancelled):
        download_ffmpeg(lambda *a: cancel.set(), cancel, sources=[servers["good"]])
    assert find_ffmpeg() is None
    assert not any(home.iterdir())


def test_reinstall_replaces_existing(home, servers):
    download_ffmpeg(sources=[servers["good"]])
    (managed_ffmpeg_dir() / "marker").write_text("old")
    download_ffmpeg(sources=[servers["good_list"]])
    assert not (managed_ffmpeg_dir() / "marker").exists()
    assert find_ffmpeg() is not None
    assert sorted(p.name for p in home.iterdir()) == ["ffmpeg"]


# ---- HTTPS certificate handling -------------------------------------------- #

def _cert_error():
    return urllib.error.URLError(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "unable to get local issuer certificate"))


def test_https_uses_windows_verification_first(monkeypatch):
    import truststore
    contexts = []

    def fake_urlopen(request, timeout, context):
        contexts.append(context)
        return io.BytesIO(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert downloader._open("https://example.com/x").read() == b"ok"
    assert len(contexts) == 1 and isinstance(contexts[0], truststore.SSLContext)


def test_certificate_failure_falls_back_to_bundled_list(monkeypatch):
    # The bug found in Windows Sandbox: a fresh Windows can't verify the site.
    contexts = []

    def fake_urlopen(request, timeout, context):
        contexts.append(context)
        if len(contexts) == 1:
            raise _cert_error()
        return io.BytesIO(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert downloader._open("https://example.com/x").read() == b"ok"
    assert len(contexts) == 2


def test_certificate_failure_everywhere_says_so(monkeypatch):
    def fake_urlopen(request, timeout, context):
        raise _cert_error()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(DownloadError) as err:
        downloader._open("https://example.com/x")
    assert "security certificate" in str(err.value)
    assert "internet connection" not in str(err.value)


def test_offline_is_not_retried(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout, context):
        calls.append(context)
        raise urllib.error.URLError(OSError(11001, "getaddrinfo failed"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(DownloadError) as err:
        downloader._open("https://example.com/x")
    assert len(calls) == 1 and "internet connection" in str(err.value)


# ---- GUI flow ------------------------------------------------------------- #

def test_gui_first_run_download(home, servers, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from videomerger.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(downloader, "SOURCES", (servers["good"],))

    def auto_accept(box):
        # Press the "Download" button instead of waiting for a person.
        accept = next(b for b in box.buttons() if box.buttonRole(b) == QMessageBox.AcceptRole)
        accept.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", auto_accept)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)

    w = MainWindow(None)
    w.show()
    assert w.get_ffmpeg_btn.isVisible()
    assert "isn't installed" in w.status_label.text()
    assert w.ffmpeg_action.text() == "Download FFmpeg…"

    w.get_ffmpeg_btn.click()   # prompt -> DownloadDialog (real, modal) -> installed

    assert w.paths is not None and w.paths.ffmpeg.parent == managed_ffmpeg_dir() / "bin"
    assert not w.get_ffmpeg_btn.isVisible()
    assert w.ffmpeg_action.text() == "Update FFmpeg…"
    assert "at least two" in w.status_label.text()
    w.close()
    w.deleteLater()
    app.processEvents()
