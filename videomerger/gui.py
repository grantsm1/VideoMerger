"""PySide6 desktop GUI."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (QCloseEvent, QDragEnterEvent, QDragMoveEvent, QDropEvent, QIcon,
                           QKeySequence, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

from . import APP_NAME, __version__, install
from .downloader import (SOURCES, DownloadCancelled, DownloadError, download_ffmpeg,
                         installed_info)
from .engine import (OUTPUT_FORMATS, ClipInfo, MergeCancelled, MergeError, MergeOptions,
                     MergePlan, Method, copy_blockers, plan_merge, probe, run_merge)
from .ffmpeg_locator import FFmpegPaths, find_ffmpeg, managed_ffmpeg_dir

VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv",
              ".mts", ".m2ts", ".ts", ".3gp", ".mpg", ".mpeg", ".vob", ".ogv"}
VIDEO_FILTER = ("Videos (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTS)) + ");;All files (*)")

METHOD_CHOICES = [("Automatic (recommended)", Method.AUTO),
                  ("Lossless only (fast, clips must match)", Method.COPY),
                  ("Always re-encode", Method.REENCODE)]
QUALITY_CHOICES = [("High", "high"), ("Standard", "standard"), ("Smaller file", "small")]
FORMAT_CHOICES = [("MP4 (plays everywhere)", ".mp4"),
                  ("MKV", ".mkv"),
                  ("MOV (Apple)", ".mov")]


def fmt_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def describe_clip(c: ClipInfo) -> str:
    audio = "" if c.has_audio else " · no audio"
    return (f"{c.path.name}    —    {fmt_time(c.duration)} · {c.width}×{c.height} · "
            f"{float(c.fps):.4g} fps · {c.v_codec}{audio}")


def friendly_mismatch(clips: list[ClipInfo]) -> str | None:
    """Plain-English version of why clips can't be joined losslessly."""
    first = clips[0]
    for clip in clips[1:]:
        diffs = []
        if (clip.width, clip.height) != (first.width, first.height):
            diffs.append("size")
        if round(float(clip.fps), 2) != round(float(first.fps), 2):
            diffs.append("frame rate")
        if clip.v_codec != first.v_codec or clip.pix_fmt != first.pix_fmt:
            diffs.append("video format")
        if clip.rotation != first.rotation:
            diffs.append("rotation")
        if clip.has_audio != first.has_audio:
            diffs.append("sound (one has audio, the other doesn't)")
        elif (clip.a_codec, clip.a_rate, clip.a_channels) != \
                (first.a_codec, first.a_rate, first.a_channels):
            diffs.append("audio format")
        if diffs:
            listed = diffs[0] if len(diffs) == 1 else ", ".join(diffs[:-1]) + " and " + diffs[-1]
            return f"{clip.path.name} has a different {listed} than {first.path.name}."
    return None


# --------------------------------------------------------------------------- #
# File list: accepts files from Explorer and reorders by dragging
# --------------------------------------------------------------------------- #

class ClipList(QListWidget):
    files_dropped = Signal(list)
    order_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)

    def _is_external(self, event) -> bool:
        return event.source() is not self and event.mimeData().hasUrls()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._is_external(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent):
        if self._is_external(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if self._is_external(event):
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(event)
            self.order_changed.emit()

    def clips(self) -> list[ClipInfo]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]


# --------------------------------------------------------------------------- #
# Background merge
# --------------------------------------------------------------------------- #

class MergeWorker(QObject):
    progress = Signal(float)
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, paths: FFmpegPaths, plan: MergePlan, options: MergeOptions):
        super().__init__()
        self.paths, self.plan, self.options = paths, plan, options
        self.cancel_event = threading.Event()

    @Slot()
    def run(self):
        try:
            out = run_merge(self.paths, self.plan, self.options,
                            on_progress=self.progress.emit, cancel=self.cancel_event)
            self.succeeded.emit(str(out))
        except MergeCancelled:
            self.cancelled.emit()
        except MergeError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # never let the worker thread die silently
            self.failed.emit(f"Unexpected error: {exc!r}")
        finally:
            self.finished.emit()


# --------------------------------------------------------------------------- #
# FFmpeg download
# --------------------------------------------------------------------------- #

class DownloadWorker(QObject):
    progress = Signal(str, int, int)    # phase, done, total (-1 = unknown)
    succeeded = Signal(object)          # FFmpegPaths
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.cancel_event = threading.Event()

    @Slot()
    def run(self):
        try:
            paths = download_ffmpeg(
                on_progress=lambda phase, done, total: self.progress.emit(
                    phase, done, -1 if total is None else total),
                cancel=self.cancel_event)
            self.succeeded.emit(paths)
        except DownloadCancelled:
            self.cancelled.emit()
        except DownloadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Unexpected error: {exc!r}")
        finally:
            self.finished.emit()


class DownloadDialog(QDialog):
    """Modal progress window for the FFmpeg download. exec() returns Accepted on success."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Downloading FFmpeg")
        self.setMinimumWidth(440)
        self.paths: FFmpegPaths | None = None
        self.error: str | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.title = QLabel("Downloading FFmpeg…")
        self.title.setStyleSheet("font-weight: 600;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.detail = QLabel("Connecting…")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.detail, stretch=1)
        row.addWidget(self.cancel_btn)
        layout.addWidget(self.title)
        layout.addWidget(self.bar)
        layout.addLayout(row)

        self.thread = QThread(self)
        self.worker = DownloadWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self._outcome = QDialog.Rejected

    def exec(self) -> int:
        # Start once the dialog's event loop is running, so a very fast
        # finish can't call done() before exec() begins.
        QTimer.singleShot(0, self.thread.start)
        return super().exec()

    @Slot(str, int, int)
    def _on_progress(self, phase: str, done: int, total: int):
        if phase == "download":
            mb = done / 1_048_576
            if total > 0:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(done / total * 1000))
                self.detail.setText(f"{mb:.1f} of {total / 1_048_576:.0f} MB")
            else:
                self.bar.setRange(0, 0)   # busy indicator
                self.detail.setText(f"{mb:.1f} MB")
        else:
            self.bar.setRange(0, 0)
            self.cancel_btn.setEnabled(phase != "install")
            self.title.setText("Checking the download…" if phase == "verify"
                               else "Installing FFmpeg…")
            self.detail.setText("Almost done")

    @Slot(object)
    def _on_succeeded(self, paths: FFmpegPaths):
        self.paths = paths
        self._outcome = QDialog.Accepted

    @Slot(str)
    def _on_failed(self, message: str):
        self.error = message

    @Slot()
    def _on_thread_finished(self):
        self.done(self._outcome)

    def reject(self):
        # Cancel / Esc / close button: stop the download; the thread finishing
        # closes the dialog. Ignore while installing so a half-swap can't happen.
        if not self.cancel_btn.isEnabled():
            return
        if self.thread.isRunning():
            self.cancel_btn.setEnabled(False)
            self.detail.setText("Cancelling…")
            self.worker.cancel_event.set()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.thread.isRunning():
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)


def offer_ffmpeg_download(parent: QWidget, update: bool = False) -> FFmpegPaths | None:
    """Ask, download, report. Returns the new paths, or None if it didn't happen."""
    source = SOURCES[0]
    intro = ("Download the latest FFmpeg?" if update else
             f"{APP_NAME} uses FFmpeg, a free and open-source video tool, to merge "
             "videos. It isn't on this computer yet.")
    box = QMessageBox(QMessageBox.Question, "FFmpeg needed" if not update else "Update FFmpeg",
                      intro, parent=parent)
    box.setInformativeText(
        f"It's about {source.approx_mb} MB, downloaded from {source.homepage} and saved in "
        f"your user folder ({managed_ffmpeg_dir()}). You only need to do this once.\n\n"
        "FFmpeg is licensed under the GNU GPL; its license is saved alongside it.")
    download = box.addButton("Download", QMessageBox.AcceptRole)
    box.addButton("Not now", QMessageBox.RejectRole)
    box.setDefaultButton(download)
    box.exec()
    if box.clickedButton() is not download:
        return None

    dialog = DownloadDialog(parent)
    if dialog.exec() == QDialog.Accepted and dialog.paths:
        info = installed_info() or {}
        QMessageBox.information(parent, "FFmpeg is ready",
                                f"FFmpeg {info.get('version', '')} was installed. "
                                "You can merge videos now.")
        return dialog.paths
    if dialog.error:
        err = QMessageBox(QMessageBox.Warning, "Download failed",
                          "FFmpeg couldn't be downloaded. Check your internet connection "
                          "and try again from Help → Download FFmpeg.", parent=parent)
        err.setDetailedText(dialog.error)
        err.exec()
    return None


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self, paths: FFmpegPaths | None, exe: Path | None = None):
        """`exe` is the VideoMerger.exe to install for shortcuts; by default the
        running .exe, or None when running from source (no install offered)."""
        super().__init__()
        self.paths = paths
        self.exe = exe or install.running_exe()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.plan: MergePlan | None = None
        self.thread: QThread | None = None
        self.worker: MergeWorker | None = None
        self.started_at = 0.0
        self._output_touched = False   # user chose an output path themselves

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(780, 600)
        self.setMinimumSize(560, 460)
        self._build_ui()
        self._build_menu()
        self._refresh()

    # ---- FFmpeg ----------------------------------------------------------- #

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")
        self.install_action = file_menu.addAction("Add to Desktop and Start menu…")
        self.install_action.triggered.connect(lambda: self.install_shortcuts())
        self.install_action.setVisible(self.exe is not None)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)

        help_menu = self.menuBar().addMenu("&Help")
        self.ffmpeg_action = help_menu.addAction("Download FFmpeg…")
        self.ffmpeg_action.triggered.connect(self.download_ffmpeg)
        help_menu.addSeparator()
        help_menu.addAction(f"About {APP_NAME}", self._about)

    def download_ffmpeg(self):
        if self._running():
            return
        paths = offer_ffmpeg_download(self, update=self.paths is not None)
        if paths:
            self.set_paths(paths)

    def set_paths(self, paths: FFmpegPaths | None):
        self.paths = paths
        self._refresh()

    # ---- first run, desktop + Start menu shortcuts ------------------------- #

    def first_run(self):
        """Called once the window is on screen: offer FFmpeg, then shortcuts."""
        if self.paths is None:
            self.download_ffmpeg()
        self.maybe_offer_install()

    def maybe_offer_install(self):
        """Ask (until the user says "don't ask again") to add shortcuts, or to
        update the installed copy when this exe is a different version."""
        if self.exe is None or self.settings.value("install/dont_ask", False, type=bool):
            return
        try:
            state = install.install_state(self.exe)
            if state == install.InstallState.THIS_COPY or (
                    state == install.InstallState.SAME and install.shortcuts_exist()):
                return
            target = install.installed_exe()
        except (OSError, install.InstallError):
            return   # never let the offer get in the way of using the app

        update = state == install.InstallState.DIFFERENT
        box = QMessageBox(QMessageBox.Question,
                          "Update VideoMerger" if update else "Add to Desktop",
                          "Update the installed VideoMerger to this version?" if update
                          else f"Add {APP_NAME} to your desktop and Start menu?", parent=self)
        box.setInformativeText(
            f"{APP_NAME} will be copied to {target.parent} so the shortcuts keep "
            "working even if you delete the file you downloaded. "
            "No administrator rights are needed.")
        yes = box.addButton("Update" if update else "Add shortcuts", QMessageBox.AcceptRole)
        box.addButton("Not now", QMessageBox.RejectRole)
        never = box.addButton("Don't ask again", QMessageBox.DestructiveRole)
        box.setDefaultButton(yes)
        box.exec()
        if box.clickedButton() is yes:
            self.install_shortcuts()
        elif box.clickedButton() is never:
            self.settings.setValue("install/dont_ask", True)

    def install_shortcuts(self) -> bool:
        if self.exe is None:
            return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = install.install(self.exe)
        except install.InstallError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Couldn't add shortcuts", str(exc))
            return False
        QApplication.restoreOverrideCursor()
        text = f"{APP_NAME} is now on your desktop and in the Start menu."
        if result.copied:
            text += (f"\n\nYou can delete the copy you downloaded:\n{self.exe}")
        QMessageBox.information(self, "Shortcuts added", text)
        return True

    def _about(self):
        info = installed_info()
        if self.paths is None:
            ffmpeg = "FFmpeg: not installed"
        elif info and self.paths.ffmpeg.parent == managed_ffmpeg_dir() / "bin":
            ffmpeg = (f"FFmpeg {info['version']}, downloaded from {info['homepage']} "
                      f"on {info['installed'][:10]}")
        else:
            ffmpeg = f"FFmpeg: using {self.paths.ffmpeg}"
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<b>{APP_NAME} {__version__}</b><p>Merge video files with FFmpeg.</p>"
                          f"<p>{ffmpeg}</p><p>FFmpeg is a separate program by the FFmpeg "
                          "developers, licensed under the GNU GPL/LGPL "
                          "(<a href='https://ffmpeg.org/legal.html'>ffmpeg.org/legal</a>).</p>")

    # ---- layout ---------------------------------------------------------- #

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Videos to merge")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        hint = QLabel("Drag files here, then drag them up or down to set the order.")
        hint.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(title)
        layout.addWidget(hint)

        self.list = ClipList()
        self.list.files_dropped.connect(self.add_files)
        self.list.order_changed.connect(self._refresh)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.list, stretch=1)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("Add files…")
        self.remove_btn = QPushButton("Remove")
        self.up_btn = QPushButton("Move up")
        self.down_btn = QPushButton("Move down")
        self.clear_btn = QPushButton("Clear")
        self.add_btn.clicked.connect(self._browse_inputs)
        self.remove_btn.clicked.connect(self._remove_selected)
        self.up_btn.clicked.connect(lambda: self._move_selected(-1))
        self.down_btn.clicked.connect(lambda: self._move_selected(+1))
        self.clear_btn.clicked.connect(self._clear)
        for b in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            buttons.addWidget(b)
        buttons.addStretch()
        self.total_label = QLabel()
        buttons.addWidget(self.total_label)
        buttons.addWidget(self.clear_btn)
        layout.addLayout(buttons)
        QShortcut(QKeySequence.Delete, self.list, activated=self._remove_selected)

        # Output
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Save as:"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Choose where to save the merged video")
        self.output_edit.textEdited.connect(self._on_output_edited)
        self.output_btn = QPushButton("Browse…")
        self.output_btn.clicked.connect(self._browse_output)
        out_row.addWidget(self.output_edit, stretch=1)
        out_row.addWidget(self.output_btn)
        layout.addLayout(out_row)

        # Advanced (collapsed by default)
        self.adv_toggle = QToolButton()
        self.adv_toggle.setText("Advanced")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.adv_toggle.setArrowType(Qt.RightArrow)
        self.adv_toggle.setAutoRaise(True)
        self.adv_toggle.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.adv_toggle)

        self.adv_box = QGroupBox()
        form = QFormLayout(self.adv_box)
        self.format_combo = self._combo(FORMAT_CHOICES)
        self.method_combo = self._combo(METHOD_CHOICES)
        self.quality_combo = self._combo(QUALITY_CHOICES)
        self.quality_combo.setCurrentIndex(1)
        self.quality_combo.setToolTip("Only used when the videos have to be re-encoded.")
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        self.method_combo.currentIndexChanged.connect(self._refresh)
        self.quality_combo.currentIndexChanged.connect(self._refresh)
        form.addRow("Format:", self.format_combo)
        form.addRow("Merge method:", self.method_combo)
        form.addRow("Quality:", self.quality_combo)
        self.adv_box.setVisible(False)
        layout.addWidget(self.adv_box)

        # Plan / status
        status_row = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.get_ffmpeg_btn = QPushButton("Download FFmpeg…")
        self.get_ffmpeg_btn.clicked.connect(self.download_ffmpeg)
        # Shown when "Lossless only" blocks a merge: one click to fix it.
        self.use_auto_btn = QPushButton("Switch to Automatic")
        self.use_auto_btn.clicked.connect(
            lambda: self.method_combo.setCurrentIndex(self.method_combo.findData(Method.AUTO)))
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.get_ffmpeg_btn)
        status_row.addWidget(self.use_auto_btn)
        layout.addLayout(status_row)

        # Progress + actions
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_label = QLabel()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        self.merge_btn = QPushButton("Merge")
        self.merge_btn.setDefault(True)
        self.merge_btn.setMinimumWidth(110)
        self.merge_btn.setStyleSheet("font-weight: 600; padding: 6px 18px;")
        self.merge_btn.clicked.connect(self._start_merge)
        bottom.addWidget(self.progress, stretch=1)
        bottom.addWidget(self.progress_label)
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.merge_btn)
        layout.addLayout(bottom)
        self._set_running(False)

        self.setCentralWidget(root)

    @staticmethod
    def _combo(choices) -> QComboBox:
        combo = QComboBox()
        for label, value in choices:
            combo.addItem(label, value)
        return combo

    def _toggle_advanced(self, on: bool):
        self.adv_toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.adv_box.setVisible(on)

    def _update_advanced_label(self):
        """Show changed settings next to "Advanced" so they aren't hidden when folded."""
        changed = []
        if self.method_combo.currentData() != Method.AUTO:
            changed.append(self.method_combo.currentText().split(" (")[0])
        if self.quality_combo.currentData() != "standard":
            changed.append(f"{self.quality_combo.currentText()} quality")
        self.adv_toggle.setText("Advanced" + (f"  ·  {', '.join(changed)}" if changed else ""))

    # ---- adding / arranging files --------------------------------------- #

    def _browse_inputs(self):
        start = self.settings.value("last_input_dir", str(Path.home() / "Videos"))
        files, _ = QFileDialog.getOpenFileNames(self, "Add videos", start, VIDEO_FILTER)
        if files:
            self.settings.setValue("last_input_dir", str(Path(files[0]).parent))
            self.add_files([Path(f) for f in files])

    @Slot(list)
    def add_files(self, paths: list[Path]):
        if self._running():
            return
        if self.paths is None:
            self.download_ffmpeg()
            if self.paths is None:
                return
        expanded: list[Path] = []
        for p in paths:
            if p.is_dir():   # a dropped folder adds the videos inside it
                expanded += sorted(f for f in p.iterdir()
                                   if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
            else:
                expanded.append(p)

        problems: list[str] = []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for path in expanded:
                try:
                    clip = probe(self.paths, path)
                except MergeError as exc:
                    problems.append(str(exc))
                    continue
                item = QListWidgetItem(describe_clip(clip))
                item.setData(Qt.UserRole, clip)
                item.setToolTip(str(clip.path))
                self.list.addItem(item)
        finally:
            QApplication.restoreOverrideCursor()

        self._suggest_output()
        self._refresh()
        if problems:
            QMessageBox.warning(self, "Some files were skipped",
                                "These files couldn't be added:\n\n" + "\n".join(problems))

    def _remove_selected(self):
        if self._running():
            return
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._refresh()

    def _move_selected(self, step: int):
        rows = sorted(self.list.row(i) for i in self.list.selectedItems())
        if not rows:
            return
        if (step < 0 and rows[0] == 0) or (step > 0 and rows[-1] == self.list.count() - 1):
            return
        for row in (rows if step < 0 else reversed(rows)):
            item = self.list.takeItem(row)
            self.list.insertItem(row + step, item)
            item.setSelected(True)
        self._refresh()

    def _clear(self):
        self.list.clear()
        self._refresh()

    # ---- output path ------------------------------------------------------ #

    def _output_path(self) -> Path | None:
        text = self.output_edit.text().strip().strip('"')
        return Path(text) if text else None

    def _suggest_output(self):
        """Fill in 'merged.mp4' next to the first clip until the user picks something."""
        if self._output_touched or self.list.count() == 0:
            return
        first = self.list.clips()[0].path
        ext = self.format_combo.currentData()
        candidate = first.parent / f"merged{ext}"
        n = 2
        while candidate.exists():
            candidate = first.parent / f"merged ({n}){ext}"
            n += 1
        self.output_edit.setText(str(candidate))

    def _browse_output(self):
        current = self._output_path()
        start = str(current) if current else str(Path.home() / "Videos" / "merged.mp4")
        filters = ";;".join(f"{label} (*{ext})" for label, ext in FORMAT_CHOICES)
        selected = next(f for f in filters.split(";;") if self.format_combo.currentData() in f)
        path, _ = QFileDialog.getSaveFileName(self, "Save merged video as", start, filters, selected)
        if path:
            self._output_touched = True
            self.output_edit.setText(path)
            self._sync_format_from_path()
            self._refresh()

    def _on_output_edited(self, _text: str):
        self._output_touched = True
        self._sync_format_from_path()
        self._refresh()

    def _sync_format_from_path(self):
        path = self._output_path()
        if path and path.suffix.lower() in OUTPUT_FORMATS:
            self.format_combo.blockSignals(True)
            self.format_combo.setCurrentIndex(self.format_combo.findData(path.suffix.lower()))
            self.format_combo.blockSignals(False)

    def _on_format_changed(self):
        path = self._output_path()
        if path:
            self.output_edit.setText(str(path.with_suffix(self.format_combo.currentData())))
        self._refresh()

    # ---- planning --------------------------------------------------------- #

    def _options(self) -> MergeOptions:
        return MergeOptions(self.method_combo.currentData(), self.quality_combo.currentData())

    def _refresh(self):
        """Recompute the plan whenever anything changes and show what will happen."""
        clips = self.list.clips()
        total = sum(c.duration for c in clips)
        self.total_label.setText(f"{len(clips)} files · {fmt_time(total)}" if clips else "")
        self._update_buttons()

        self.plan = None
        self.use_auto_btn.setVisible(False)
        self._update_advanced_label()
        self.get_ffmpeg_btn.setVisible(self.paths is None)
        self.ffmpeg_action.setText("Update FFmpeg…" if self.paths else "Download FFmpeg…")
        self.ffmpeg_action.setEnabled(not self._running())
        if self.paths is None:
            self._set_status("FFmpeg isn't installed yet. It's needed to read and merge "
                             "videos, and only takes a minute to download.", error=True)
        elif len(clips) < 2:
            self._set_status("Add at least two videos to get started.")
        elif self._output_path() is None:
            self._set_status("Choose where to save the merged video.")
        else:
            try:
                self.plan = plan_merge(clips, self._output_path(), self._options())
            except MergeError as exc:
                options = self._options()
                if options.method == Method.COPY and copy_blockers(clips, self._output_path()):
                    # Lossless only, but the clips don't match: say why in plain
                    # words, point at the hidden setting, and offer the fix.
                    why = (friendly_mismatch(clips)
                           or str(exc).removeprefix("Can't merge losslessly: "))
                    self._set_status(
                        "These videos can't be joined without re-encoding, and "
                        "“Lossless only” is selected under Advanced.\n"
                        f"Reason: {why}\n"
                        "Switch to Automatic to re-encode them, or remove the clip "
                        "that doesn't match.", error=True)
                    self.use_auto_btn.setVisible(not self._running())
                else:
                    self._set_status(str(exc), error=True)
            else:
                if self.plan.method == Method.COPY:
                    self._set_status("✔ The videos match, so they'll be joined without "
                                     "re-encoding. This is fast and keeps the original quality.")
                else:
                    detail = (friendly_mismatch(clips)
                              or self.plan.reason.removeprefix("Re-encoding because "))
                    self._set_status(
                        f"The videos will be re-encoded to {self.plan.width}×{self.plan.height} "
                        f"at {float(self.plan.fps):.4g} fps. This takes longer.\n"
                        f"Reason: {detail}")
        self.merge_btn.setEnabled(self.plan is not None and not self._running())

    def _set_status(self, text: str, error: bool = False):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #c62828;" if error else "")

    def _update_buttons(self):
        busy = self._running()
        has_sel = bool(self.list.selectedItems())
        for b in (self.remove_btn, self.up_btn, self.down_btn):
            b.setEnabled(has_sel and not busy)
        self.clear_btn.setEnabled(self.list.count() > 0 and not busy)

    # ---- merging ---------------------------------------------------------- #

    def _running(self) -> bool:
        return self.thread is not None

    def _set_running(self, running: bool):
        for w in (self.add_btn, self.output_edit, self.output_btn, self.adv_box):
            w.setEnabled(not running)
        self.list.setDragDropMode(QAbstractItemView.NoDragDrop if running
                                  else QAbstractItemView.InternalMove)
        self.list.setAcceptDrops(not running)
        self.cancel_btn.setVisible(running)
        self.cancel_btn.setEnabled(running)
        self.merge_btn.setVisible(not running)
        self._update_buttons()

    def _start_merge(self):
        self._refresh()
        if self.plan is None:
            return
        out = self.plan.output
        if out.exists():
            answer = QMessageBox.question(self, "Replace file?",
                                          f"{out.name} already exists.\nDo you want to replace it?")
            if answer != QMessageBox.Yes:
                return

        self.thread = QThread(self)
        self.worker = MergeWorker(self.paths, self.plan, self._options())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)

        self.started_at = time.monotonic()
        self.progress.setValue(0)
        self.progress_label.setText("Starting…")
        self._set_running(True)
        self.thread.start()

    def _cancel(self):
        if self.worker:
            self.cancel_btn.setEnabled(False)
            self.progress_label.setText("Cancelling…")
            self.worker.cancel_event.set()

    @Slot(float)
    def _on_progress(self, fraction: float):
        self.progress.setValue(int(fraction * 1000))
        elapsed = time.monotonic() - self.started_at
        text = f"{fraction:.0%}"
        if 0.03 < fraction < 1 and elapsed > 3:
            text += f" · about {fmt_time(elapsed / fraction - elapsed)} left"
        self.progress_label.setText(text)

    @Slot(str)
    def _on_succeeded(self, output: str):
        self.progress.setValue(1000)
        self.progress_label.setText(f"Done in {fmt_time(time.monotonic() - self.started_at)}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Merge complete")
        box.setText(f"Your video is ready:\n{output}")
        show = box.addButton("Show in folder", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is show:
            subprocess.Popen(["explorer", "/select,", output])

    @Slot(str)
    def _on_failed(self, message: str):
        self.progress.setValue(0)
        self.progress_label.setText("Failed")
        box = QMessageBox(QMessageBox.Critical, "Merge failed",
                          "The videos couldn't be merged.", parent=self)
        box.setDetailedText(message)
        box.exec()

    @Slot()
    def _on_cancelled(self):
        self.progress.setValue(0)
        self.progress_label.setText("Cancelled")

    @Slot()
    def _on_thread_finished(self):
        self.thread.deleteLater()
        self.worker.deleteLater()
        self.thread = self.worker = None
        self._set_running(False)
        self._suggest_output()   # avoid offering the file we just wrote
        self._refresh()

    def closeEvent(self, event: QCloseEvent):
        if self._running():
            answer = QMessageBox.question(self, "Stop merging?",
                                          "A merge is still running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel_event.set()
            self.thread.quit()
            self.thread.wait(5000)
        event.accept()


def resource_path(relative: str) -> Path:
    """Find bundled files both from source and inside the PyInstaller .exe."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent
    return Path(base) / relative


def main() -> int:
    if sys.platform == "win32":
        # Group the taskbar button under our own icon instead of python.exe's.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{APP_NAME}.{APP_NAME}.{__version__}")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = resource_path("assets/icon.ico")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    paths = find_ffmpeg()
    window = MainWindow(paths)
    window.show()
    # Offer FFmpeg / shortcuts once the window is on screen.
    QTimer.singleShot(0, window.first_run)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
