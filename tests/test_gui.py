"""Drive the main window headlessly: add files, reorder, merge, cancel."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from videomerger.engine import Method, probe
from videomerger.gui import MainWindow

from conftest import PATHS


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch):
    # Message boxes would block a headless test; answer them automatically.
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    w = MainWindow(PATHS)
    w.settings.clear()
    w.show()
    yield w
    w.close()
    w.deleteLater()
    QTest.qWait(20)   # let Qt actually destroy it before the next test


def names(w):
    return [c.path.name for c in w.list.clips()]


def wait_for(condition, timeout_ms=60_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if condition():
            return True
        QTest.qWait(50)   # keeps the Qt event loop running
    return condition()


def wait_until_idle(w, timeout=60_000):
    assert wait_for(lambda: not w._running(), timeout)


def test_matching_files_merge_losslessly(window, clips, tmp_path):
    window.add_files([clips["a"], clips["b"]])
    assert names(window) == ["a.mp4", "it's b.mp4"]
    assert window.output_edit.text().endswith("merged.mp4")    # suggested next to clip 1
    assert window.plan.method == Method.COPY and window.merge_btn.isEnabled()

    out = tmp_path / "gui_copy.mp4"
    window.output_edit.setText(str(out))
    window._on_output_edited(str(out))
    window.merge_btn.click()
    wait_until_idle(window)
    assert window.progress.value() == 1000
    assert abs(probe(PATHS, out).duration - 5) < 0.3


def test_reorder_and_remove(window, clips):
    window.add_files([clips["a"], clips["b"], clips["tall"]])
    window.list.setCurrentRow(2)
    window.up_btn.click()
    assert names(window) == ["a.mp4", "tall.mp4", "it's b.mp4"]
    window.list.setCurrentRow(0)
    window.down_btn.click()
    assert names(window) == ["tall.mp4", "a.mp4", "it's b.mp4"]
    window.list.setCurrentRow(1)
    window.remove_btn.click()
    assert names(window) == ["tall.mp4", "it's b.mp4"]


def test_mixed_files_reencode_and_format_switch(window, clips, tmp_path):
    window.add_files([clips["a"], clips["small"], clips["silent"]])
    assert window.plan.method == Method.REENCODE
    assert "re-encoded" in window.status_label.text()

    window.format_combo.setCurrentIndex(window.format_combo.findData(".mkv"))
    assert window.output_edit.text().endswith(".mkv")
    window.output_edit.setText(str(tmp_path / "gui_mixed.mov"))
    window._on_output_edited("")
    assert window.format_combo.currentData() == ".mov"

    window.merge_btn.click()
    wait_until_idle(window)
    result = probe(PATHS, tmp_path / "gui_mixed.mov")
    assert result.v_codec == "h264" and abs(result.duration - 5.5) < 0.4


def test_lossless_only_explains_and_offers_fix(window, clips):
    # What the Sandbox tester ran into: "Lossless only" was selected, so the
    # vertical clip blocked the merge with a jargon-filled message.
    window.add_files([clips["a"], clips["tall"]])
    window.method_combo.setCurrentIndex(window.method_combo.findData(Method.COPY))

    assert not window.merge_btn.isEnabled()
    text = window.status_label.text()
    assert "Lossless only" in text and "tall.mp4 has a different size" in text
    assert "h264" not in text                          # no jargon
    assert window.use_auto_btn.isVisible()
    assert "Lossless only" in window.adv_toggle.text()  # visible even when folded

    window.use_auto_btn.click()
    assert window.method_combo.currentData() == Method.AUTO
    assert window.merge_btn.isEnabled() and window.plan.method == Method.REENCODE
    assert not window.use_auto_btn.isVisible()
    assert window.adv_toggle.text() == "Advanced"


def test_bad_file_is_skipped(window, clips, tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_text("nope")
    window.add_files([clips["a"], junk])
    assert names(window) == ["a.mp4"]
    assert not window.merge_btn.isEnabled()
    assert "at least two" in window.status_label.text()


def test_dropping_a_folder_adds_its_videos(window, clips):
    window.add_files([clips["a"].parent])
    assert len(names(window)) == 6


def test_cancel(window, clips, tmp_path):
    out = tmp_path / "gui_cancel.mp4"
    window.add_files([clips["long"], clips["small"]])
    window.output_edit.setText(str(out))
    window._on_output_edited("")
    window.merge_btn.click()
    assert window.cancel_btn.isVisible() and not window.add_btn.isEnabled()
    assert wait_for(lambda: window.progress.value() > 30, 30_000)
    window.cancel_btn.click()
    wait_until_idle(window)
    assert window.progress_label.text() == "Cancelled"
    assert not out.exists() and window.merge_btn.isVisible()
