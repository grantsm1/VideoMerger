"""Real downloads from the internet. Opt-in, since each source is 100+ MB:

    set VIDEOMERGER_LIVE_TESTS=1
    python -m pytest tests/test_downloader_live.py -s
"""

from __future__ import annotations

import os
import subprocess

import pytest

from videomerger.downloader import SOURCES, download_ffmpeg, installed_info

pytestmark = pytest.mark.skipif(os.environ.get("VIDEOMERGER_LIVE_TESTS") != "1",
                                reason="set VIDEOMERGER_LIVE_TESTS=1 to download for real")


@pytest.mark.parametrize("which", [0, 1], ids=["windows-verify", "bundled-list"])
@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.name.split()[0])
def test_each_certificate_check_works_alone(source, which):
    """Both ways of checking HTTPS certificates must reach every source."""
    import urllib.request

    from videomerger.downloader import _ssl_contexts

    label, context = _ssl_contexts()[which]
    with urllib.request.urlopen(source.checksum_url, timeout=30, context=context) as resp:
        assert resp.status == 200


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.name.split()[0])
def test_real_download(source, tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEOMERGER_HOME", str(tmp_path))
    last = {"done": 0}

    def on_progress(phase, done, total):
        if phase == "download":
            last["done"] = done

    paths = download_ffmpeg(on_progress, sources=[source])
    out = subprocess.run([str(paths.ffmpeg), "-hide_banner", "-encoders"],
                         capture_output=True, text=True)
    assert "libx264" in out.stdout
    info = installed_info()
    print(f"\n{source.name}: FFmpeg {info['version']}, {last['done'] / 1e6:.0f} MB")
