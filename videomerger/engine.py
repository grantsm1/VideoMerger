"""Core merge engine: inspect clips, pick a merge method, run FFmpeg.

Two methods:
  * COPY     - concat demuxer with stream copy. Seconds, lossless, but every
               clip must share codec, resolution, frame rate, audio format...
  * REENCODE - concat filter. Works with any mix of files; each clip is
               scaled/padded to one size and frame rate, then encoded to
               H.264 + AAC.

The engine has no GUI code, so it can be driven from the GUI, the CLI or tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Callable, Sequence

from .ffmpeg_locator import FFmpegPaths

# Hide the console window FFmpeg would otherwise pop up from a GUI app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Which codecs each output container can hold without re-encoding.
_COPY_SAFE = {
    ".mp4": ({"h264", "hevc", "av1", "mpeg4", "vp9"},
             {"aac", "mp3", "ac3", "eac3", "opus", "alac", "flac"}),
    ".mov": ({"h264", "hevc", "mpeg4", "prores", "mjpeg"},
             {"aac", "mp3", "ac3", "eac3", "alac", "pcm_s16le", "pcm_s24le"}),
    ".mkv": (None, None),  # Matroska holds practically anything
}

OUTPUT_FORMATS = (".mp4", ".mkv", ".mov")

# Quality presets for re-encoding (x264 CRF: lower = better/bigger).
QUALITY_CRF = {"high": 18, "standard": 21, "small": 26}

MAX_FPS = 60.0


class MergeError(Exception):
    """A problem the user should see (bad input, FFmpeg failure...)."""


class MergeCancelled(Exception):
    pass


class Method(str, Enum):
    AUTO = "auto"
    COPY = "copy"
    REENCODE = "reencode"


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ClipInfo:
    path: Path
    duration: float
    v_codec: str
    width: int          # as displayed (rotation already applied)
    height: int
    pix_fmt: str
    fps: Fraction
    rotation: int
    sar: str
    a_codec: str | None = None
    a_rate: int | None = None
    a_channels: int | None = None

    @property
    def has_audio(self) -> bool:
        return self.a_codec is not None

    def copy_signature(self) -> tuple:
        """Everything that must match across clips for a lossless join."""
        return (self.v_codec, self.width, self.height, self.pix_fmt,
                round(float(self.fps), 2), self.rotation, self.sar,
                self.a_codec, self.a_rate, self.a_channels)


def _parse_rate(text: str | None) -> Fraction:
    try:
        rate = Fraction(text or "0")
    except (ValueError, ZeroDivisionError):
        return Fraction(0)
    return rate


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(round(float(side["rotation"]))) % 360
    rotate = stream.get("tags", {}).get("rotate")
    return int(rotate) % 360 if rotate else 0


def probe(paths: FFmpegPaths, file: Path) -> ClipInfo:
    """Read the stream details of one file with ffprobe."""
    file = Path(file)
    if not file.is_file():
        raise MergeError(f"File not found: {file}")

    cmd = [str(paths.ffprobe), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(file)]
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise MergeError(f"Can't read {file.name}: {msg or 'not a media file'}")

    data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MergeError(f"{file.name} has no video stream.")

    duration = float(data.get("format", {}).get("duration") or
                     video.get("duration") or 0)
    if duration <= 0:
        raise MergeError(f"Can't determine the length of {file.name}.")

    fps = _parse_rate(video.get("avg_frame_rate"))
    if fps <= 0:
        fps = _parse_rate(video.get("r_frame_rate"))

    rotation = _rotation(video)
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if rotation in (90, 270):
        width, height = height, width

    return ClipInfo(
        path=file.resolve(),
        duration=duration,
        v_codec=video.get("codec_name", "?"),
        width=width,
        height=height,
        pix_fmt=video.get("pix_fmt", "?"),
        fps=fps,
        rotation=rotation,
        sar=video.get("sample_aspect_ratio", "1:1"),
        a_codec=audio.get("codec_name") if audio else None,
        a_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        a_channels=audio.get("channels") if audio else None,
    )


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

@dataclass
class MergeOptions:
    method: Method = Method.AUTO
    quality: str = "standard"           # key of QUALITY_CRF (re-encode only)


@dataclass
class MergePlan:
    clips: list[ClipInfo]
    output: Path
    method: Method                      # COPY or REENCODE, never AUTO
    reason: str                         # human-readable why
    total_duration: float
    width: int = 0                      # re-encode target
    height: int = 0
    fps: Fraction = field(default_factory=lambda: Fraction(30))


def copy_blockers(clips: Sequence[ClipInfo], output: Path) -> list[str]:
    """Reasons a lossless join isn't possible (empty list = it is)."""
    reasons: list[str] = []
    first = clips[0]
    for clip in clips[1:]:
        if clip.copy_signature() != first.copy_signature():
            reasons.append(f"{clip.path.name} doesn't match {first.path.name} "
                           f"({_describe(clip)} vs {_describe(first)})")
            break

    v_ok, a_ok = _COPY_SAFE.get(output.suffix.lower(), (set(), set()))
    if v_ok is not None and first.v_codec not in v_ok:
        reasons.append(f"{first.v_codec} video can't go in a {output.suffix} file")
    if a_ok is not None and first.has_audio and first.a_codec not in a_ok:
        reasons.append(f"{first.a_codec} audio can't go in a {output.suffix} file")
    return reasons


def _describe(c: ClipInfo) -> str:
    audio = f"{c.a_codec} {c.a_rate}Hz {c.a_channels}ch" if c.has_audio else "no audio"
    return f"{c.v_codec} {c.width}x{c.height} {float(c.fps):.2f}fps, {audio}"


def _even(n: int) -> int:
    return n - (n % 2)


def plan_merge(clips: Sequence[ClipInfo], output: Path,
               options: MergeOptions | None = None) -> MergePlan:
    options = options or MergeOptions()
    output = Path(output).resolve()

    if len(clips) < 2:
        raise MergeError("Pick at least two videos to merge.")
    if output.suffix.lower() not in OUTPUT_FORMATS:
        raise MergeError(f"Output must be one of: {', '.join(OUTPUT_FORMATS)}")
    if any(c.path == output for c in clips):
        raise MergeError("The output file can't be one of the input files.")
    if options.quality not in QUALITY_CRF:
        raise MergeError(f"Unknown quality '{options.quality}'.")

    total = sum(c.duration for c in clips)
    blockers = copy_blockers(clips, output)

    if options.method == Method.COPY:
        if blockers:
            raise MergeError("Can't merge losslessly: " + "; ".join(blockers))
        method, reason = Method.COPY, "Lossless join (forced)."
    elif options.method == Method.REENCODE:
        method, reason = Method.REENCODE, "Re-encoding (forced)."
    elif blockers:
        method, reason = Method.REENCODE, "Re-encoding because " + "; ".join(blockers)
    else:
        method, reason = Method.COPY, "All clips match, so they can be joined losslessly."

    plan = MergePlan(list(clips), output, method, reason, total)
    if method == Method.REENCODE:
        # Use the biggest clip's frame size and the highest frame rate, so
        # nothing gets downscaled or choppy. Smaller clips get black bars.
        biggest = max(clips, key=lambda c: c.width * c.height)
        plan.width, plan.height = _even(biggest.width), _even(biggest.height)
        best_fps = max((c.fps for c in clips if c.fps > 0), default=Fraction(30))
        plan.fps = min(best_fps, Fraction(MAX_FPS)).limit_denominator(1001)
    return plan


# --------------------------------------------------------------------------- #
# Command building
# --------------------------------------------------------------------------- #

def _concat_list_line(path: Path) -> str:
    # Concat-demuxer quoting: wrap in '...' and turn ' into '\''
    return "file '" + str(path).replace("'", "'\\''") + "'\n"


def _container_flags(output: Path) -> list[str]:
    if output.suffix.lower() in (".mp4", ".mov"):
        return ["-movflags", "+faststart"]  # lets players start before full download
    return []


def build_copy_command(paths: FFmpegPaths, plan: MergePlan, list_file: Path,
                       dest: Path | None = None) -> list[str]:
    """`dest` is where FFmpeg writes (default: the plan's output)."""
    dest = dest or plan.output
    cmd = [str(paths.ffmpeg), "-hide_banner", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    if plan.clips[0].v_codec == "hevc" and plan.output.suffix.lower() in (".mp4", ".mov"):
        cmd += ["-tag:v", "hvc1"]  # so Apple devices/QuickTime will play it
    return cmd + _container_flags(plan.output) + [
        "-progress", "pipe:1", "-nostats", str(dest)]


def build_reencode_command(paths: FFmpegPaths, plan: MergePlan,
                           options: MergeOptions, dest: Path | None = None) -> list[str]:
    """`dest` is where FFmpeg writes (default: the plan's output)."""
    dest = dest or plan.output
    w, h, fps = plan.width, plan.height, plan.fps
    any_audio = any(c.has_audio for c in plan.clips)

    cmd = [str(paths.ffmpeg), "-hide_banner", "-nostdin", "-y"]
    for clip in plan.clips:
        cmd += ["-i", str(clip.path)]

    parts: list[str] = []
    labels: list[str] = []
    for i, clip in enumerate(plan.clips):
        parts.append(
            f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"fps={fps.numerator}/{fps.denominator},format=yuv420p[v{i}]")
        labels.append(f"[v{i}]")
        if any_audio:
            dur = f"{clip.duration:.3f}"
            if clip.has_audio:
                # Normalise format and pad/trim to the clip length to keep sync.
                parts.append(
                    f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:"
                    f"channel_layouts=stereo,apad,atrim=duration={dur}[a{i}]")
            else:
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur}[a{i}]")
            labels.append(f"[a{i}]")

    n = len(plan.clips)
    a = 1 if any_audio else 0
    out_labels = "[outv][outa]" if any_audio else "[outv]"
    parts.append(f"{''.join(labels)}concat=n={n}:v=1:a={a}{out_labels}")

    cmd += ["-filter_complex", ";".join(parts), "-map", "[outv]"]
    if any_audio:
        cmd += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "medium",
            "-crf", str(QUALITY_CRF[options.quality]), "-pix_fmt", "yuv420p"]
    return cmd + _container_flags(plan.output) + [
        "-progress", "pipe:1", "-nostats", str(dest)]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

ProgressCallback = Callable[[float], None]   # receives 0.0 .. 1.0


def run_merge(paths: FFmpegPaths, plan: MergePlan,
              options: MergeOptions | None = None,
              on_progress: ProgressCallback | None = None,
              cancel: threading.Event | None = None) -> Path:
    """Run the plan. Blocks until done; call from a worker thread in a GUI.

    Raises MergeCancelled if `cancel` gets set, MergeError if FFmpeg fails.

    FFmpeg writes to a temporary file next to the output, which only replaces
    the output once it's complete. So a cancelled or failed merge leaves an
    existing file of the same name untouched, and never leaves half a video.
    """
    options = options or MergeOptions()
    cancel = cancel or threading.Event()
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    # Same folder (so the final rename is instant) and same extension (FFmpeg
    # picks the container format from it).
    partial = plan.output.with_name(f"{plan.output.stem}.merging{plan.output.suffix}")

    list_file: Path | None = None
    try:
        if plan.method == Method.COPY:
            fd, name = tempfile.mkstemp(prefix="videomerger_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(_concat_list_line(c.path) for c in plan.clips)
            list_file = Path(name)
            cmd = build_copy_command(paths, plan, list_file, dest=partial)
        else:
            cmd = build_reencode_command(paths, plan, options, dest=partial)

        _run_ffmpeg(cmd, plan.total_duration, on_progress, cancel)
        _check_length(paths, partial, plan.total_duration)
        try:
            os.replace(partial, plan.output)
        except OSError as exc:
            raise MergeError(f"Couldn't save {plan.output.name}. Is it open in "
                             f"another program, such as a video player? ({exc})") from exc
    finally:
        partial.unlink(missing_ok=True)
        if list_file:
            list_file.unlink(missing_ok=True)

    if on_progress:
        on_progress(1.0)
    return plan.output


def _check_length(paths: FFmpegPaths, result: Path, expected: float) -> None:
    """Make sure no clip went missing.

    FFmpeg's concat demuxer reports success even when it can't read one of
    the files (e.g. it was moved or deleted after being added): it just
    leaves that clip out. So measure the result instead of trusting it.
    """
    try:
        actual = probe(paths, result).duration
    except MergeError as exc:
        raise MergeError(f"The merged video couldn't be read back: {exc}") from exc
    if actual < expected - max(1.0, expected * 0.02):
        raise MergeError(
            f"The merged video came out {_mmss(actual)} long instead of {_mmss(expected)}, "
            "so part of it is missing. One of the files may have been moved, deleted or "
            "damaged after it was added. Remove it and add it again, then retry.")


def _mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02}"


def _run_ffmpeg(cmd: list[str], total: float,
                on_progress: ProgressCallback | None,
                cancel: threading.Event) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)

    # Drain stderr in the background (keeps FFmpeg from blocking on a full
    # pipe) and remember the tail for error messages.
    err_tail: deque[str] = deque(maxlen=30)
    err_thread = threading.Thread(
        target=lambda: err_tail.extend(
            line.decode("utf-8", "replace").rstrip() for line in proc.stderr),
        daemon=True)
    err_thread.start()

    # Cancel watcher: kill FFmpeg as soon as the event is set.
    def watch_cancel():
        while proc.poll() is None:
            if cancel.wait(0.2):
                proc.kill()
                return
    threading.Thread(target=watch_cancel, daemon=True).start()

    for raw in proc.stdout:
        key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
        if key == "out_time_us" and on_progress and total > 0 and value.isdigit():
            on_progress(min(int(value) / 1_000_000 / total, 0.999))

    proc.wait()
    err_thread.join(timeout=2)

    if cancel.is_set():
        raise MergeCancelled()
    if proc.returncode != 0:
        detail = "\n".join(line for line in err_tail if line) or "unknown error"
        raise MergeError(f"FFmpeg failed (exit code {proc.returncode}):\n{detail}")


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #

def merge_files(paths: FFmpegPaths, files: Sequence[Path], output: Path,
                options: MergeOptions | None = None,
                on_progress: ProgressCallback | None = None,
                cancel: threading.Event | None = None) -> MergePlan:
    """Probe, plan and run in one call. Returns the plan that was used."""
    clips = [probe(paths, Path(f)) for f in files]
    plan = plan_merge(clips, output, options)
    run_merge(paths, plan, options, on_progress, cancel)
    return plan
