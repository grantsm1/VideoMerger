"""End-to-end tests: generate small clips with FFmpeg, merge them, check results."""

from __future__ import annotations

import threading

import pytest

from videomerger.engine import (MergeCancelled, MergeError, MergeOptions, Method,
                                merge_files, plan_merge, probe, run_merge)

from conftest import PATHS

pytestmark = pytest.mark.skipif(PATHS is None, reason="FFmpeg not installed")


def test_probe(clips):
    info = probe(PATHS, clips["small"])
    assert (info.width, info.height, info.v_codec) == (320, 240, "mpeg4")
    assert info.a_codec == "opus" and abs(info.duration - 2) < 0.2


def test_matching_clips_use_lossless_copy(clips, tmp_path):
    out = tmp_path / "copy.mp4"
    progress = []
    plan = merge_files(PATHS, [clips["a"], clips["b"]], out, on_progress=progress.append)
    assert plan.method == Method.COPY
    result = probe(PATHS, out)
    assert abs(result.duration - 5) < 0.3
    assert result.has_audio and progress[-1] == 1.0


def test_mixed_clips_reencode(clips, tmp_path):
    out = tmp_path / "mixed.mp4"
    files = [clips["a"], clips["small"], clips["silent"], clips["tall"]]
    plan = merge_files(PATHS, files, out)
    assert plan.method == Method.REENCODE
    result = probe(PATHS, out)
    assert (result.width, result.height) == (640, 360)
    assert result.v_codec == "h264" and result.a_codec == "aac"
    assert abs(result.duration - 6.5) < 0.4


def test_no_audio_anywhere(clips, tmp_path):
    out = tmp_path / "silent.mkv"
    plan = merge_files(PATHS, [clips["silent"], clips["silent"]], out,
                       MergeOptions(method=Method.REENCODE))
    result = probe(PATHS, out)
    assert plan.method == Method.REENCODE and not result.has_audio
    assert abs(result.duration - 3) < 0.3


def test_opus_audio_blocks_copy_into_mov(clips, tmp_path):
    info = probe(PATHS, clips["small"])
    plan = plan_merge([info, info], tmp_path / "x.mov")
    assert plan.method == Method.REENCODE
    with pytest.raises(MergeError):
        plan_merge([info, info], tmp_path / "x.mov", MergeOptions(method=Method.COPY))


def test_bad_inputs(clips, tmp_path):
    info = probe(PATHS, clips["a"])
    with pytest.raises(MergeError):
        plan_merge([info], tmp_path / "x.mp4")               # only one clip
    with pytest.raises(MergeError):
        plan_merge([info, info], clips["a"])                 # overwrite an input
    with pytest.raises(MergeError):
        plan_merge([info, info], tmp_path / "x.avi")         # unsupported format
    junk = tmp_path / "junk.mp4"
    junk.write_text("not a video")
    with pytest.raises(MergeError):
        probe(PATHS, junk)


def _leftovers(folder):
    return [p.name for p in folder.iterdir() if ".merging" in p.name]


def test_cancel_keeps_existing_file(clips, tmp_path):
    # Bug found in Windows Sandbox: cancelling a merge that replaces a file
    # used to delete the earlier, finished video.
    out = tmp_path / "keep.mp4"
    out.write_bytes(b"an earlier finished video")
    cancel = threading.Event()
    with pytest.raises(MergeCancelled):
        merge_files(PATHS, [clips["long"], clips["small"]], out,
                    MergeOptions(method=Method.REENCODE),
                    lambda f: cancel.set() if f > 0.05 else None, cancel)
    assert out.read_bytes() == b"an earlier finished video"
    assert _leftovers(tmp_path) == []


def test_failed_merge_keeps_existing_file(clips, tmp_path):
    out = tmp_path / "keep.mp4"
    out.write_bytes(b"an earlier finished video")
    victim = tmp_path / "goes missing.mp4"
    victim.write_bytes(clips["b"].read_bytes())
    plan = plan_merge([probe(PATHS, clips["a"]), probe(PATHS, victim)], out)
    victim.unlink()                     # input disappears after it was added
    # FFmpeg itself reports success here and just drops the clip; the
    # length check has to catch it.
    with pytest.raises(MergeError, match="part of it is missing"):
        run_merge(PATHS, plan)
    assert out.read_bytes() == b"an earlier finished video"
    assert _leftovers(tmp_path) == []


def test_successful_merge_replaces_existing_file(clips, tmp_path):
    out = tmp_path / "replace.mp4"
    out.write_bytes(b"old")
    merge_files(PATHS, [clips["a"], clips["b"]], out)
    assert abs(probe(PATHS, out).duration - 5) < 0.3
    assert _leftovers(tmp_path) == []


def test_cancel_removes_partial_output(clips, tmp_path):
    out = tmp_path / "cancel.mp4"
    cancel = threading.Event()

    def on_progress(fraction):
        if fraction > 0.05:
            cancel.set()

    with pytest.raises(MergeCancelled):
        merge_files(PATHS, [clips["long"], clips["small"]], out,
                    MergeOptions(method=Method.REENCODE), on_progress, cancel)
    assert not out.exists()
