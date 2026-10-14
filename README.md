# VideoMerger

A small Windows app for joining video files into one, using FFmpeg.

- Drag videos in, put them in order, click **Merge**.
- If the clips match (same camera/phone settings) they're joined **losslessly in
  seconds**. Otherwise they're re-encoded to one size and frame rate, and the app
  says why in plain English.
- Output as MP4, MKV or MOV. Quality and merge method are under **Advanced**.
- FFmpeg isn't bundled: on first launch the app offers to download it (about
  110 MB from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), with
  [BtbN's GitHub builds](https://github.com/BtbN/FFmpeg-Builds) as a fallback),
  checks the SHA-256 checksum, and saves it in `%LOCALAPPDATA%\VideoMerger\ffmpeg`.
  If FFmpeg is already on the PATH, that one is used instead.

## For people you share it with

1. Download `VideoMerger.exe` (single file, no install needed) and run it.
2. Windows may show **"Windows protected your PC"** because the file isn't
   code-signed yet: click **More info → Run anyway**.
3. Click **Download** when asked for FFmpeg (one time only).

To uninstall: delete `VideoMerger.exe` and the folder `%LOCALAPPDATA%\VideoMerger`.

## Developing

Requires Python 3.12 (`winget install Python.Python.3.12`).

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m videomerger                 # run the GUI
.venv\Scripts\python.exe -m videomerger.cli a.mp4 b.mp4 -o out.mp4
.venv\Scripts\python.exe -m pytest tests                # tests
```

Useful environment variables:

| Variable | Effect |
|---|---|
| `VIDEOMERGER_ONLY_MANAGED_FFMPEG=1` | Ignore FFmpeg on PATH (try the first-run download on a dev PC) |
| `VIDEOMERGER_HOME=<folder>` | Use another folder instead of `%LOCALAPPDATA%\VideoMerger` |
| `VIDEOMERGER_LIVE_TESTS=1` | Also run `tests/test_downloader_live.py` (real downloads) |

## Building the .exe

```powershell
.\build.ps1                # tests, then dist\VideoMerger.exe (+ .sha256)
.\build.ps1 -CleanPcTest   # also run the exe as on a PC that never had FFmpeg
```

The clean-PC test copies the exe to an empty folder, runs it with only Windows'
own folders on the PATH and an empty AppData, then clicks through the real
window: download FFmpeg, add three videos, merge, check the output, relaunch.

## Testing on a truly clean Windows (Windows Sandbox)

Windows Sandbox (Windows 10/11 Pro) is a throwaway copy of Windows: nothing
installed, wiped when closed.

1. Turn it on once: **Turn Windows features on or off → Windows Sandbox**, then restart.
2. `.\tools\make_sandbox.ps1` creates labelled sample videos and `sandbox\VideoMerger.wsb`.
3. Double-click `sandbox\VideoMerger.wsb`. The exe is placed on the Sandbox
   desktop marked as downloaded from the internet (so you see the same
   SmartScreen warning as your users), and a checklist opens in Notepad.
4. The **Test videos** folder on the Sandbox desktop is shared with
   `sandbox\test-videos`, so merged results come back to your PC.

## Layout

| Path | What |
|---|---|
| `videomerger/engine.py` | Probe clips, choose lossless vs re-encode, run FFmpeg with progress/cancel |
| `videomerger/downloader.py` | First-run FFmpeg download, checksum, install |
| `videomerger/ffmpeg_locator.py` | Where to find FFmpeg |
| `videomerger/gui.py` | PySide6 window |
| `videomerger/cli.py` | Command-line front end |
| `VideoMerger.spec`, `build.ps1` | PyInstaller build |
| `tools/` | Icon generator, clean-PC end-to-end test |

## Licensing note

VideoMerger downloads FFmpeg on the user's PC rather than shipping it. FFmpeg
builds that include x264 are GPL-licensed; the license file is saved next to
the downloaded FFmpeg. If you ever bundle FFmpeg inside your download instead,
GPL obligations (offering the source) apply to that copy.
