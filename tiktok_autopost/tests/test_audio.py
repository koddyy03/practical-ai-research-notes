"""音入れ / 切り出しの検証。ffmpeg が必要(無ければ自動スキップ)。

素材は実ファイルを落とさずに ffmpeg で合成する。検証したいのは
「無音を入力すると音が付いて出てくる」「切り出しても音が残る」こと。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from tiktok_autopost import audio

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))


def _make(path: Path, *, seconds: float, silent: bool) -> Path:
    """1080x1920/30fps の縦動画を作る。silent=True なら実質無音の音声トラック付き。"""
    # 無音側は「トラックは有るが中身が無い」= 実際に起きた壊れ方を再現する
    atrack = "anullsrc=r=44100:cl=stereo" if silent else "sine=f=440:r=44100"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size=1080x1920:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"{atrack}:d={seconds}",
         "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(path)],
        check=True, capture_output=True,
    )
    return path


def _make_bed(path: Path, *, seconds: float = 3.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=f=220:r=44100:d={seconds}",
         "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_probe_flags_silent_source():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        v = _make(Path(td) / "s.mp4", seconds=4, silent=True)
        assert audio.probe(v)["is_silent"] is True


def test_probe_passes_loud_source():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        v = _make(Path(td) / "l.mp4", seconds=4, silent=False)
        assert audio.probe(v)["is_silent"] is False


def test_mux_makes_a_silent_video_audible():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _make(d / "s.mp4", seconds=5, silent=True)
        bed = _make_bed(d / "bed.m4a", seconds=2)  # 尺より短い -> ループされる
        out = audio.mux(src, d / "out.mp4", bed=bed)
        got = audio.probe(out)
        assert got["is_silent"] is False, got
        # 尺は変えない
        assert abs(got["duration_sec"] - 5.0) < 0.2, got
        # 狙いの音量帯に乗っている
        assert -30 < got["audio_mean_dbfs"] < -8, got


def test_mux_keeps_video_untouched():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _make(d / "s.mp4", seconds=4, silent=True)
        bed = _make_bed(d / "bed.m4a")
        before = audio.video_check.probe(src)
        after = audio.video_check.probe(audio.mux(src, d / "o.mp4", bed=bed))
        assert (before.width, before.height) == (after.width, after.height)
        assert abs(before.fps - after.fps) < 0.1
        assert before.codec == after.codec  # 映像は再エンコードせずコピー


def test_clip_keeps_audio_from_long_source():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _make(d / "long.mp4", seconds=20, silent=False)
        out = audio.clip(src, d / "cut.mp4", start=5, duration=6)
        got = audio.probe(out)
        assert got["is_silent"] is False, got
        assert abs(got["duration_sec"] - 6.0) < 0.2, got


def test_clip_rejects_range_past_end():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _make(d / "s.mp4", seconds=5, silent=False)
        try:
            audio.clip(src, d / "o.mp4", start=4, duration=10)
        except audio.AudioError as exc:
            assert "尺を超えている" in str(exc)
        else:
            raise AssertionError("範囲外の切り出しが通ってしまった")


def test_clip_rejects_bad_args():
    if not HAVE_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = _make(d / "s.mp4", seconds=5, silent=False)
        for kw in ({"start": 0, "duration": 0}, {"start": -1, "duration": 2}):
            try:
                audio.clip(src, d / "o.mp4", **kw)
            except audio.AudioError:
                pass
            else:
                raise AssertionError(f"不正な引数が通ってしまった: {kw}")


if __name__ == "__main__":
    import traceback
    fails = 0
    if not HAVE_FFMPEG:
        print("ffmpeg が無いのでスキップ")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception:
                fails += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    raise SystemExit(1 if fails else 0)
