"""Install-to-user-folder and shortcut tests (real .lnk files in temp folders)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from videomerger import install
from videomerger.install import InstallError, InstallState


@pytest.fixture
def places(tmp_path, monkeypatch):
    """Point Desktop, Start menu and the install folder at temp folders."""
    dirs = {name: tmp_path / name for name in ("Desktop", "StartMenu", "Programs", "Downloads")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setattr(install, "desktop_dir", lambda: dirs["Desktop"])
    monkeypatch.setattr(install, "start_menu_dir", lambda: dirs["StartMenu"])
    monkeypatch.setattr(install, "install_dir", lambda: dirs["Programs"] / "VideoMerger")
    return dirs


def fake_exe(folder, content=b"MZ fake VideoMerger build 1"):
    exe = folder / "VideoMerger.exe"
    exe.write_bytes(content)
    return exe


def test_install_from_downloads(places):
    downloaded = fake_exe(places["Downloads"])
    assert install.install_state(downloaded) == InstallState.NOT_INSTALLED
    assert not install.shortcuts_exist()

    result = install.install(downloaded)

    target = places["Programs"] / "VideoMerger" / "VideoMerger.exe"
    assert result.copied and result.exe == target
    assert target.read_bytes() == downloaded.read_bytes()
    assert install.shortcuts_exist()
    for link in (places["Desktop"] / "VideoMerger.lnk", places["StartMenu"] / "VideoMerger.lnk"):
        assert install.read_shortcut_target(link) == target   # points at the installed copy
    assert install.install_state(downloaded) == InstallState.SAME
    assert install.install_state(target) == InstallState.THIS_COPY


def test_shortcuts_survive_deleting_the_download(places):
    downloaded = fake_exe(places["Downloads"])
    install.install(downloaded)
    downloaded.unlink()
    link = places["Desktop"] / "VideoMerger.lnk"
    assert install.read_shortcut_target(link).is_file()


def test_newer_download_updates_installed_copy(places):
    install.install(fake_exe(places["Downloads"], b"MZ version 1"))
    newer = fake_exe(places["Downloads"], b"MZ version 2, a bit longer")
    assert install.install_state(newer) == InstallState.DIFFERENT
    install.install(newer)
    assert (places["Programs"] / "VideoMerger" / "VideoMerger.exe").read_bytes() == \
        b"MZ version 2, a bit longer"


def test_running_installed_copy_just_recreates_shortcuts(places):
    install.install(fake_exe(places["Downloads"]))
    (places["Desktop"] / "VideoMerger.lnk").unlink()          # user deleted the icon
    result = install.install(places["Programs"] / "VideoMerger" / "VideoMerger.exe")
    assert not result.copied and install.shortcuts_exist()


def test_installed_copy_in_use_is_left_intact(places):
    install.install(fake_exe(places["Downloads"], b"MZ version 1"))
    target = places["Programs"] / "VideoMerger" / "VideoMerger.exe"
    with open(target, "rb"):            # like the installed app being open
        with pytest.raises(InstallError, match="close it"):
            install.install(fake_exe(places["Downloads"], b"MZ version 2"))
    assert target.read_bytes() == b"MZ version 1"
    assert not (target.parent / "VideoMerger.exe.new").exists()


def test_real_known_folders_resolve():
    # The real Windows lookups (no monkeypatching): just make sure they work.
    assert install.desktop_dir().is_dir()
    assert install.start_menu_dir().is_dir()
    assert install.install_dir().parent.name == "Programs"


# ---- GUI ------------------------------------------------------------------- #

@pytest.fixture
def gui(places, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    from videomerger.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    asked = []
    answer = {"button": "Add shortcuts"}

    def fake_exec(box):
        asked.append(box.text())
        for b in box.buttons():
            if b.text() == answer["button"]:
                b.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    windows = []
    first = {"done": False}

    def make(exe):
        w = MainWindow(object(), exe=exe)   # FFmpeg isn't needed for these tests
        if not first["done"]:               # start clean, but keep settings between
            w.settings.clear()              # "launches" within one test
            first["done"] = True
        windows.append(w)
        return w

    yield make, asked, answer
    for w in windows:
        w.settings.clear()
        w.close()
        w.deleteLater()
    app.processEvents()


def test_first_run_offer_installs(gui, places):
    make, asked, answer = gui
    w = make(fake_exe(places["Downloads"]))
    assert w.install_action.isVisible()
    w.maybe_offer_install()
    assert asked == ["Add VideoMerger to your desktop and Start menu?"]
    assert install.shortcuts_exist()

    w.maybe_offer_install()                # already installed: no second question
    assert len(asked) == 1


def test_dont_ask_again_is_remembered(gui, places):
    make, asked, answer = gui
    answer["button"] = "Don't ask again"
    exe = fake_exe(places["Downloads"])
    make(exe).maybe_offer_install()
    make(exe).maybe_offer_install()        # a later launch
    assert len(asked) == 1 and not install.shortcuts_exist()


def test_not_now_asks_again_next_time(gui, places):
    make, asked, answer = gui
    answer["button"] = "Not now"
    exe = fake_exe(places["Downloads"])
    make(exe).maybe_offer_install()
    make(exe).maybe_offer_install()
    assert len(asked) == 2 and not install.shortcuts_exist()


def test_offer_to_update_older_install(gui, places):
    make, asked, answer = gui
    answer["button"] = "Update"
    install.install(fake_exe(places["Downloads"], b"MZ old"))
    make(fake_exe(places["Downloads"], b"MZ new version")).maybe_offer_install()
    assert asked == ["Update the installed VideoMerger to this version?"]
    assert (places["Programs"] / "VideoMerger" / "VideoMerger.exe").read_bytes() == b"MZ new version"


def test_no_offer_when_running_from_source(gui):
    make, asked, answer = gui
    w = make(None)
    w.exe = None                           # what running `python -m videomerger` gives
    w.maybe_offer_install()
    assert asked == []
