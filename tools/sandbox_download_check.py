"""Unattended download check for a brand-new Windows (run inside Windows Sandbox).

Built into a small console .exe by tools/sandbox_check.ps1, which starts a
Sandbox that runs it, saves the report to sandbox/results/download_check.txt,
and shuts the Sandbox down.

Order matters: the plain Python check runs first, before anything has made
Windows fetch extra root certificates, so it shows whether the original bug
reproduces on this fresh system.
"""

from __future__ import annotations

import os
import ssl
import sys
import tempfile
import traceback
import urllib.request
from pathlib import Path

from videomerger.downloader import SOURCES, _ssl_contexts, download_ffmpeg, installed_info

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("download_check.txt")
lines: list[str] = []


def log(msg: str):
    print(msg, flush=True)
    lines.append(msg)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def try_fetch(label: str, context) -> bool:
    ok = True
    for source in SOURCES:
        try:
            with urllib.request.urlopen(source.checksum_url, timeout=30, context=context) as r:
                log(f"  [{label}] {source.homepage}: OK ({r.status})")
        except Exception as exc:
            ok = False
            log(f"  [{label}] {source.homepage}: FAILED - {getattr(exc, 'reason', exc)}")
    return ok


def main() -> int:
    log(f"Windows {sys.getwindowsversion().build}, user {os.environ.get('USERNAME')}")
    log("1. Old method (Python's default check) - expected to FAIL on a fresh Windows:")
    old_ok = try_fetch("default", ssl.create_default_context())

    contexts = dict(_ssl_contexts())
    log("2. Bundled certificate list on its own:")
    bundled_ok = try_fetch("bundled", contexts["bundled"])
    log("3. Windows' own verification on its own:")
    windows_ok = try_fetch("Windows", contexts["Windows"])

    log("4. Full FFmpeg download exactly as the app does it:")
    os.environ["VIDEOMERGER_HOME"] = tempfile.mkdtemp()
    try:
        download_ffmpeg()
        info = installed_info()
        log(f"  OK - FFmpeg {info['version']} from {info['source']}")
        full_ok = True
    except Exception:
        log("  FAILED\n" + traceback.format_exc())
        full_ok = False

    log("")
    log(f"Bug reproduced with old method: {'yes' if not old_ok else 'no (store already had roots)'}")
    log(f"RESULT: {'PASS' if (full_ok and bundled_ok and windows_ok) else 'FAIL'}")
    return 0 if full_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("CRASHED\n" + traceback.format_exc())
        sys.exit(2)
