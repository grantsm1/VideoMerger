# PyInstaller build recipe. Build with:  build.ps1   (or: pyinstaller VideoMerger.spec)
# -*- mode: python ; coding: utf-8 -*-

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct,
                                                 VSVersionInfo)

ROOT = Path(SPECPATH)
version = re.search(r'__version__ = "([^"]+)"',
                    (ROOT / "videomerger" / "__init__.py").read_text()).group(1)
nums = tuple(int(n) for n in version.split(".")) + (0,) * (4 - len(version.split(".")))

# Shown in the .exe's Properties > Details tab. Having proper version info also
# makes antivirus tools less suspicious of the file.
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=nums, prodvers=nums),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("ProductName", "VideoMerger"),
            StringStruct("FileDescription", "VideoMerger - merge video files with FFmpeg"),
            StringStruct("FileVersion", version),
            StringStruct("ProductVersion", version),
            StringStruct("OriginalFilename", "VideoMerger.exe"),
            StringStruct("InternalName", "VideoMerger"),
            StringStruct("LegalCopyright", ""),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# Qt pieces this app never uses; leaving them out shrinks the .exe.
EXCLUDES = [
    "tkinter", "unittest", "pydoc", "pytest", "pywinauto",
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSvg", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtMultimedia", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtDBus", "PySide6.QtSql",
]

a = Analysis(
    ["run_app.py"],
    pathex=[str(ROOT)],
    datas=[("assets/icon.ico", "assets")],
    excludes=EXCLUDES,
    noarchive=False,
)

# Drop Qt translation files and plugins that a plain widgets app doesn't need.
UNNEEDED = ("translations", "qtvirtualkeyboard", "qmltooling", "networkinformation",
            "tls", "multimedia", "position", "sqldrivers", "opengl32sw.dll")
a.datas = [d for d in a.datas if not any(u in d[0].replace("\\", "/") for u in UNNEEDED)]
a.binaries = [b for b in a.binaries if not any(u in b[0].replace("\\", "/") for u in UNNEEDED)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="VideoMerger",
    icon="assets/icon.ico",
    version=version_info,
    console=False,          # GUI app: no black console window
    upx=False,              # UPX-packed files trip antivirus far more often
    disable_windowed_traceback=False,
)
