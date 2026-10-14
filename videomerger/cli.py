"""Command-line front end, handy for testing the engine without the GUI.

    python -m videomerger.cli clip1.mp4 clip2.mkv -o merged.mp4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import (QUALITY_CRF, MergeError, MergeOptions, Method,
                     plan_merge, probe, run_merge)
from .ffmpeg_locator import find_ffmpeg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="videomerger", description="Merge video files.")
    parser.add_argument("inputs", nargs="+", type=Path, help="videos, in order")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--method", choices=[m.value for m in Method], default="auto")
    parser.add_argument("--quality", choices=list(QUALITY_CRF), default="standard")
    parser.add_argument("--dry-run", action="store_true", help="show the plan only")
    args = parser.parse_args(argv)

    paths = find_ffmpeg()
    if paths is None:
        print("FFmpeg not found.", file=sys.stderr)
        return 2

    options = MergeOptions(Method(args.method), args.quality)
    try:
        clips = [probe(paths, f) for f in args.inputs]
        plan = plan_merge(clips, args.output, options)
        print(f"Method: {plan.method.value} - {plan.reason}")
        if args.dry_run:
            return 0
        run_merge(paths, plan, options,
                  on_progress=lambda f: print(f"\r{f:6.1%}", end="", flush=True))
        print(f"\nDone: {plan.output}")
        return 0
    except MergeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
