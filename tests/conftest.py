"""Shared fixtures: small test clips generated with FFmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from videomerger.ffmpeg_locator import find_ffmpeg

PATHS = find_ffmpeg()


def make_clip(path: Path, seconds: float, size="640x360", fps=30,
              vcodec="libx264", audio=True, acodec="aac", rate=44100) -> Path:
    cmd = [str(PATHS.ffmpeg), "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
                "-c:a", acodec]
    cmd += ["-c:v", vcodec, "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    if PATHS is None:
        pytest.skip("FFmpeg not installed")
    d = tmp_path_factory.mktemp("clips")
    return {
        "a": make_clip(d / "a.mp4", 2),
        "b": make_clip(d / "it's b.mp4", 3),                # apostrophe in name
        "small": make_clip(d / "small.mkv", 2, size="320x240", fps=25,
                           vcodec="mpeg4", acodec="libopus", rate=48000),
        "silent": make_clip(d / "silent.mp4", 1.5, audio=False),
        "tall": make_clip(d / "tall.mp4", 1, size="360x640"),
        "long": make_clip(d / "long.mp4", 20),
    }
