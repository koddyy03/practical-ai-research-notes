"""python3 -m tiktok_autopost.tests.test_mp4_probe で実行 (pytest / ffprobe 不要)。

ffmpeg が入っていない実行環境では video_check が ffprobe を呼べない。そこで
ゲートごと落ちると「横動画は投稿しない」という必須要件が効かなくなるので、
標準ライブラリだけの MP4 パーサ (mp4_probe) にフォールバックしている。
ここではそのフォールバック経路を、合成した実バイト列で検証する。
"""

from __future__ import annotations

import struct
import sys
import tempfile
import traceback
from pathlib import Path

from tiktok_autopost import mp4_probe, video_check

RULES = video_check.load_config()

IDENTITY = (0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
ROT90 = (0, 0x00010000, 0, -0x00010000, 0, 0, 0, 0, 0x40000000)
ROT270 = (0, -0x00010000, 0, 0x00010000, 0, 0, 0, 0, 0x40000000)
ROT180 = (-0x00010000, 0, 0, 0, -0x00010000, 0, 0, 0, 0x40000000)


# ---------------------------------------------------------------- mp4 builder

def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _mvhd(timescale: int, duration: int) -> bytes:
    return _box(b"mvhd", bytes(4) + bytes(8) + struct.pack(">II", timescale, duration))


def _mdhd(timescale: int, duration: int) -> bytes:
    return _box(b"mdhd", bytes(4) + bytes(8) + struct.pack(">II", timescale, duration))


def _hdlr(kind: bytes) -> bytes:
    return _box(b"hdlr", bytes(4) + bytes(4) + kind + bytes(12))


def _tkhd(width: int, height: int, matrix=IDENTITY) -> bytes:
    body = bytes(4)                   # version 0 + flags
    body += bytes(8)                  # creation + modification
    body += struct.pack(">I", 1)      # track_ID
    body += bytes(4)                  # reserved
    body += struct.pack(">I", 0)      # duration
    body += bytes(8)                  # reserved[2]
    body += bytes(8)                  # layer, alternate_group, volume, reserved
    body += b"".join(struct.pack(">i", v) for v in matrix)
    body += struct.pack(">II", width << 16, height << 16)
    return _box(b"tkhd", body)


def _stsd(fmt: bytes) -> bytes:
    entry = struct.pack(">I", 16) + fmt + bytes(8)
    return _box(b"stsd", bytes(4) + struct.pack(">I", 1) + entry)


def _stsz(count: int) -> bytes:
    return _box(b"stsz", bytes(4) + struct.pack(">II", 0, count))


def _stts(count: int, delta: int) -> bytes:
    return _box(b"stts", bytes(4) + struct.pack(">I", 1) + struct.pack(">II", count, delta))


def _video_trak(width, height, matrix, fmt, frames, timescale, duration,
                sample_table="stsz") -> bytes:
    if sample_table == "stts":
        counts = _stts(frames, duration // frames if frames else 1)
    else:
        counts = _stsz(frames)
    stbl = _box(b"stbl", _stsd(fmt) + counts)
    mdia = _box(b"mdia", _mdhd(timescale, duration) + _hdlr(b"vide")
                + _box(b"minf", stbl))
    return _box(b"trak", _tkhd(width, height, matrix) + mdia)


def _audio_trak() -> bytes:
    stbl = _box(b"stbl", _stsd(b"mp4a") + _stsz(100))
    mdia = _box(b"mdia", _mdhd(44100, 44100) + _hdlr(b"soun") + _box(b"minf", stbl))
    return _box(b"trak", _tkhd(0, 0) + mdia)


def build_mp4(path: Path, width=1080, height=1920, matrix=IDENTITY, fmt=b"avc1",
              seconds=10.0, fps=30.0, pad_to=0, video=True, audio=True,
              sample_table="stsz") -> Path:
    timescale = 600
    duration = int(seconds * timescale)
    frames = int(round(seconds * fps))
    moov = _mvhd(timescale, duration)
    if video:
        moov += _video_trak(width, height, matrix, fmt, frames, timescale,
                            duration, sample_table)
    if audio:
        moov += _audio_trak()
    data = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomavc1")
    data += _box(b"moov", moov)
    if pad_to > len(data) + 8:
        data += _box(b"free", bytes(pad_to - len(data) - 8))
    path.write_bytes(data)
    return path


_TMP = Path(tempfile.mkdtemp(prefix="mp4probe-"))
_counter = [0]


def _clip(**kwargs) -> Path:
    _counter[0] += 1
    return build_mp4(_TMP / f"clip{_counter[0]}.mp4", **kwargs)


def _check(**kwargs):
    return video_check.check(_clip(**kwargs), RULES)


# ---------------------------------------------------------------- orientation

def test_portrait_passes_without_ffprobe():
    result = _check(width=1080, height=1920)
    assert result.ok, result.rejections
    assert result.facts.as_dict()["orientation"] == "portrait"


def test_landscape_rejected_without_ffprobe():
    result = _check(width=1920, height=1080)
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_the_actual_pv_dimensions_are_rejected():
    """ドライブに実際に混入していた 960x540 の PV が、ffprobe 無しでも落ちること。"""
    result = _check(width=960, height=540)
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_square_rejected():
    result = _check(width=1080, height=1080)
    assert not result.ok
    assert result.facts.as_dict()["orientation"] == "square"


# ---------------------------------------------------------------- rotation

def test_rotation_90_matrix_makes_it_portrait():
    result = _check(width=1920, height=1080, matrix=ROT90)
    assert result.ok, result.rejections
    assert result.facts.width == 1080 and result.facts.height == 1920


def test_rotation_270_matrix_makes_it_portrait():
    result = _check(width=1920, height=1080, matrix=ROT270)
    assert result.ok, result.rejections
    assert result.facts.is_portrait


def test_rotation_180_does_not_swap():
    """180 度は上下反転なので横のまま。通してはいけない。"""
    result = _check(width=1920, height=1080, matrix=ROT180)
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_rotation_90_on_portrait_source_becomes_landscape():
    """縦素材に 90 度回転が付いていれば表示は横。落とすこと。"""
    result = _check(width=1080, height=1920, matrix=ROT90)
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


# ---------------------------------------------------------------- facts

def test_duration_and_fps_are_read():
    facts = video_check.probe(_clip(seconds=83.0, fps=30.0))
    assert abs(facts.duration_sec - 83.0) < 0.05, facts.duration_sec
    assert abs(facts.fps - 30.0) < 0.1, facts.fps


def test_fps_falls_back_to_stts_when_stsz_is_empty():
    facts = video_check.probe(_clip(seconds=10.0, fps=30.0, sample_table="stts"))
    assert abs(facts.fps - 30.0) < 0.1, facts.fps


def test_file_size_comes_from_disk():
    clip = _clip(pad_to=50000)
    assert video_check.probe(clip).size_bytes == clip.stat().st_size == 50000


def test_codec_names_are_normalised():
    assert video_check.probe(_clip(fmt=b"avc1")).codec == "h264"
    assert video_check.probe(_clip(fmt=b"hvc1")).codec == "hevc"
    assert video_check.probe(_clip(fmt=b"av01")).codec == "av1"


# ---------------------------------------------------------------- other gates

def test_short_side_below_minimum_is_rejected():
    result = _check(width=200, height=400)
    assert not result.ok
    assert any("短辺" in r for r in result.rejections), result.rejections


def test_duration_out_of_range_is_rejected():
    assert any("尺が" in r for r in _check(seconds=1.0).rejections)
    assert any("尺が" in r for r in _check(seconds=900.0).rejections)


def test_fps_out_of_range_is_rejected():
    assert any("fps" in r for r in _check(fps=12.0).rejections)


def test_oversized_file_is_rejected():
    tight = dict(RULES, max_file_bytes=10)
    result = video_check.check(_clip(), tight)
    assert not result.ok
    assert any("ファイルサイズ" in r for r in result.rejections), result.rejections


# ---------------------------------------------------------------- failure modes

def test_non_mp4_raises_rather_than_passing():
    """読めないものを「縦だった」ことにしないこと。"""
    path = _TMP / "garbage.mp4"
    path.write_bytes(b"this is not an mp4 at all" * 10)
    try:
        video_check.probe(path)
    except video_check.VideoCheckError:
        return
    raise AssertionError("MP4 でないファイルは例外にすべき")


def test_video_without_track_raises():
    try:
        video_check.probe(_clip(video=False))
    except video_check.VideoCheckError:
        return
    raise AssertionError("映像トラックが無ければ例外にすべき")


def test_probe_mp4_returns_ffprobe_shaped_dict():
    """video_check._ffprobe と差し替え可能な形で返ること。"""
    clip = _clip(width=1080, height=1920)
    data = mp4_probe.probe_mp4(clip)
    assert {"streams", "format"} <= set(data)
    video = [s for s in data["streams"] if s["codec_type"] == "video"]
    assert len(video) == 1
    assert video[0]["width"] == 1080 and video[0]["height"] == 1920
    assert int(data["format"]["size"]) == clip.stat().st_size


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
