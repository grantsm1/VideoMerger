"""Create labelled sample videos for manual testing (e.g. in Windows Sandbox).

    python tools/make_test_videos.py [output folder]     (default: sandbox/test-videos)

Each clip shows its number and name in big text and plays a different tone, so
you can tell at a glance whether the merged video has the clips in the right order.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from videomerger.ffmpeg_locator import find_ffmpeg  # noqa: E402

FONT = "C\\:/Windows/Fonts/arialbd.ttf"

# name, size, fps, seconds, pattern, video codec, audio codec (None = silent), tone Hz
CLIPS = [
    ("01 Beach day.mp4", "1280x720", 30, 6, "testsrc2", "libx264", "aac", 440),
    ("02 Sunset.mp4", "1280x720", 30, 5, "smptehdbars", "libx264", "aac", 550),
    ("03 Phone clip (vertical).mov", "720x1280", 60, 4, "testsrc2", "libx264", "aac", 660),
    ("04 Old camera.mkv", "640x480", 25, 4, "smptebars", "mpeg4", "libmp3lame", 330),
    ("05 Silent screen recording.mp4", "1920x1080", 30, 3, "rgbtestsrc", "libx264", None, 0),
]


def make(ffmpeg: Path, folder: Path, name, size, fps, secs, pattern, vcodec, acodec, tone):
    label = Path(name).stem.replace("'", "")
    text = (f"drawtext=fontfile='{FONT}':text='{label}':fontsize=min(w\\,h)/15:fontcolor=white:"
            "box=1:boxcolor=black@0.6:boxborderw=20:x=(w-text_w)/2:y=(h-text_h)/2")
    timer = (f"drawtext=fontfile='{FONT}':text='%{{pts\\:hms}}':fontsize=min(w\\,h)/24:"
             "fontcolor=yellow:box=1:boxcolor=black@0.6:x=20:y=20")
    cmd = [str(ffmpeg), "-v", "error", "-y",
           "-f", "lavfi", "-i", f"{pattern}=size={size}:rate={fps}:duration={secs}"]
    if acodec:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=44100:duration={secs}"]
    cmd += ["-vf", f"{text},{timer}", "-c:v", vcodec, "-pix_fmt", "yuv420p"]
    if vcodec == "mpeg4":
        cmd += ["-q:v", "4"]
    if acodec:
        cmd += ["-c:a", acodec, "-shortest"]
    cmd.append(str(folder / name))
    subprocess.run(cmd, check=True)


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sandbox" / "test-videos"
    folder.mkdir(parents=True, exist_ok=True)
    paths = find_ffmpeg()
    if paths is None:
        print("FFmpeg is needed to create the samples.", file=sys.stderr)
        return 1
    for clip in CLIPS:
        make(paths.ffmpeg, folder, *clip)
        print(f"  {clip[0]}")
    (folder / "Not a video.mp4").write_text("This is a text file pretending to be a video.\n")
    print(f"  Not a video.mp4   (for checking the error message)\nSaved in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
