"""End-to-end test of the built .exe as if on a PC that has never had FFmpeg.

    python tools/cleanpc_test.py dist/VideoMerger.exe <empty work folder>

* copies the .exe into a fresh folder (no Python, no project files next to it)
* runs it with a PATH that has only Windows' own folders (no FFmpeg, no Python)
  and a brand-new, empty LOCALAPPDATA
* clicks through it like a person: accept the FFmpeg download, add three
  videos through the file dialog, merge, check the result
* starts it a second time to make sure it doesn't ask for FFmpeg again

It drives the real window with pywinauto, so the app appears on screen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pywinauto import Desktop
from pywinauto.keyboard import send_keys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from videomerger import __version__  # noqa: E402
from videomerger.ffmpeg_locator import find_ffmpeg  # noqa: E402  (test machine's FFmpeg)

MAIN_TITLE = f"VideoMerger {__version__}"

WIN = os.environ.get("SystemRoot", r"C:\Windows")
CLEAN_PATH = ";".join([rf"{WIN}\system32", WIN, rf"{WIN}\System32\Wbem",
                       rf"{WIN}\System32\WindowsPowerShell\v1.0"])


def step(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_window(pid: int | None, title: str, timeout: float):
    """pid=None searches every process. (A one-file PyInstaller .exe is a small
    launcher process that starts the real app as a child process, so the
    window doesn't belong to the PID we launched.)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = (Desktop(backend="uia").windows(process=pid) if pid
                   else Desktop(backend="uia").windows())
        for w in windows:
            if w.window_text() == title:
                return w
            for child in w.children():     # Qt message boxes can be nested
                if child.window_text() == title and child.element_info.control_type == "Window":
                    return child
        time.sleep(0.25)
    raise TimeoutError(f"'{title}' didn't appear within {timeout:.0f}s")


def find(win, control_type: str, name: str | None = None):
    for c in win.descendants(control_type=control_type):
        if name is None or c.window_text() == name:
            return c
    raise LookupError(f"No {control_type} '{name}' in '{win.window_text()}'")


def click(win, name: str):
    button = find(win, "Button", name)
    try:
        # Press it through UI Automation: works even when Windows won't move
        # the mouse (display asleep, screen inactive).
        button.invoke()
    except Exception:
        button.click_input()


def snap(win, path: Path):
    try:
        win.set_focus()
        time.sleep(0.3)
        win.capture_as_image().save(path)
    except Exception as exc:     # screenshots are nice-to-have
        step(f"  (screenshot failed: {exc})")


def make_clips(folder: Path) -> list[Path]:
    ff = find_ffmpeg()
    specs = [("Beach day.mp4", "1280x720", 30, 6), ("Sunset.mp4", "1280x720", 30, 4),
             ("Phone clip.mov", "720x1280", 60, 3)]
    out = []
    for name, size, fps, secs in specs:
        path = folder / name
        subprocess.run([str(ff.ffmpeg), "-v", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={secs}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        str(path)], check=True)
        out.append(path)
    return out


def probe_duration(path: Path) -> float:
    ff = find_ffmpeg()
    out = subprocess.run([str(ff.ffprobe), "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def launch(exe: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen([str(exe)], env=env, cwd=exe.parent)


def main() -> int:
    exe_src, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    shutil.rmtree(work, ignore_errors=True)
    app_dir, appdata, videos, shots = (work / d for d in ("app", "LocalAppData", "Videos", "shots"))
    for d in (app_dir, appdata, videos, shots):
        d.mkdir(parents=True)
    exe = app_dir / "VideoMerger.exe"
    shutil.copy2(exe_src, exe)
    make_clips(videos)

    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("VIDEOMERGER", "PYTHON", "VIRTUAL_ENV", "QT_"))}
    env["PATH"] = CLEAN_PATH
    env["LOCALAPPDATA"] = str(appdata)
    assert shutil.which("ffmpeg", path=CLEAN_PATH) is None, "FFmpeg still reachable on PATH"

    # ---- first launch ---------------------------------------------------- #
    step("Launching VideoMerger.exe (clean PATH, empty AppData)")
    t0 = time.monotonic()
    proc = launch(exe, env)
    try:
        main_win = wait_window(None, MAIN_TITLE, 60)
        pid = main_win.process_id()
        step(f"Main window up after {time.monotonic() - t0:.1f}s")

        prompt = wait_window(pid, "FFmpeg needed", 15)
        step("First-run prompt shown")
        snap(prompt, shots / "1_prompt.png")
        click(prompt, "Download")

        t1 = time.monotonic()
        try:
            progress = wait_window(pid, "Downloading FFmpeg", 10)
            time.sleep(2)
            snap(progress, shots / "2_downloading.png")
        except TimeoutError:
            pass
        ready = wait_window(pid, "FFmpeg is ready", 300)
        step(f"FFmpeg installed after {time.monotonic() - t1:.0f}s")
        click(ready, "OK")

        # Desktop/Start menu offer. Answer "Not now": on a real PC this test
        # must not put shortcuts on the user's actual desktop (that part is
        # covered by tests/test_install.py and the Sandbox checklist).
        offer = wait_window(pid, "Add to Desktop", 15)
        step("Desktop shortcut offer shown")
        snap(offer, shots / "1b_shortcut_offer.png")
        click(offer, "Not now")

        bin_dir = appdata / "VideoMerger" / "ffmpeg" / "bin"
        assert (bin_dir / "ffmpeg.exe").is_file() and (bin_dir / "ffprobe.exe").is_file()
        info = json.loads((bin_dir.parent / "source.json").read_text())
        step(f"Installed FFmpeg {info['version']} from {info['source']}")

        step("Adding videos through the Add files dialog")
        click(main_win, "Add files…")
        dlg = wait_window(pid, "Add videos", 15)
        time.sleep(0.5)
        # Type the folder into "File name" to open it, then pick all three files.
        send_keys(str(videos), with_spaces=True)
        send_keys("{ENTER}")
        time.sleep(1)
        names = " ".join(f'"{p.name}"' for p in sorted(videos.iterdir()))
        send_keys("%n")   # Alt+N: focus the File name box
        send_keys(names, with_spaces=True)
        send_keys("{ENTER}")
        time.sleep(2)

        items = find(main_win, "List").descendants(control_type="ListItem")
        step(f"List has {len(items)} files")
        assert len(items) == 3, [i.window_text() for i in items]
        snap(main_win, shots / "3_files_added.png")

        out_box = find(main_win, "Edit")
        output = Path(out_box.get_value())
        step(f"Saving to {output}")

        click(main_win, "Merge")
        t2 = time.monotonic()
        time.sleep(1.5)
        snap(main_win, shots / "4_merging.png")
        done = wait_window(pid, "Merge complete", 180)
        step(f"Merge finished in {time.monotonic() - t2:.1f}s")
        snap(done, shots / "5_done.png")
        click(done, "Close")

        duration = probe_duration(output)
        step(f"Output: {output.name}, {output.stat().st_size / 1e6:.1f} MB, {duration:.1f}s long")
        assert abs(duration - 13) < 0.5, duration
        main_win.close()
        proc.wait(15)
    finally:
        if proc.poll() is None:
            proc.kill()

    # ---- second launch: FFmpeg should already be there --------------------- #
    step("Launching a second time")
    t0 = time.monotonic()
    proc = launch(exe, env)
    try:
        main_win = wait_window(None, MAIN_TITLE, 60)
        pid = main_win.process_id()
        step(f"Main window up after {time.monotonic() - t0:.1f}s")
        try:
            wait_window(pid, "FFmpeg needed", 5)
            raise AssertionError("Asked to download FFmpeg again")
        except TimeoutError:
            step("No download prompt (FFmpeg found)")
        main_win.close()
        proc.wait(15)
    finally:
        if proc.poll() is None:
            proc.kill()

    step("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
