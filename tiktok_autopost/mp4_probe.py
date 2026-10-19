"""ffprobe が無い環境向けの、MP4 ボックスを直接読む最小パーサ。

投稿前ゲート (video_check) は ffprobe を前提にしているが、実行環境に
ffmpeg 一式が入っていないことがある。そこで落ちると「横動画を投稿しない」
という必須要件が機能しなくなるので、標準ライブラリだけで同じ事実
(表示上の幅・高さ、尺、fps、コーデック、回転) を取れるようにしておく。

扱うのは ISO BMFF (mp4 / m4v / mov)。それ以外は ValueError を投げる。
"""

from __future__ import annotations

import struct
from pathlib import Path

# 回転行列 (a, b, c, d) → 表示上の回転角
_ROTATIONS = {
    (1.0, 0.0, 0.0, 1.0): 0,
    (0.0, 1.0, -1.0, 0.0): 90,
    (-1.0, 0.0, 0.0, -1.0): 180,
    (0.0, -1.0, 1.0, 0.0): 270,
}

# stsd の 4CC から ffprobe 互換のコーデック名へ
_CODECS = {
    "avc1": "h264", "avc3": "h264",
    "hev1": "hevc", "hvc1": "hevc",
    "av01": "av1",
    "mp4v": "mpeg4",
    "mp4a": "aac",
}

_BRANDS = (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42",
           b"avc1", b"M4V ", b"M4A ", b"qt  ", b"dash", b"3gp4", b"3gp5")


def _iter_boxes(fh, start: int, end: int):
    """[start, end) 内の兄弟ボックスを (type, payload_start, payload_end) で返す。"""
    fh.seek(start)
    while fh.tell() + 8 <= end:
        offset = fh.tell()
        header = fh.read(8)
        if len(header) < 8:
            return
        size, raw_type = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            extra = fh.read(8)
            if len(extra) < 8:
                return
            size = struct.unpack(">Q", extra)[0]
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            return
        yield raw_type.decode("latin1"), offset + header_size, offset + size
        fh.seek(offset + size)


def _find(fh, start: int, end: int, path: tuple[str, ...]):
    """ネストしたボックスを名前の並びで引く。見つからなければ None。"""
    for box_type, body_start, body_end in _iter_boxes(fh, start, end):
        if box_type != path[0]:
            continue
        if len(path) == 1:
            return body_start, body_end
        found = _find(fh, body_start, body_end, path[1:])
        if found:
            return found
    return None


def _read_version(fh, at: int) -> int:
    fh.seek(at)
    version = fh.read(1)[0]
    fh.read(3)  # flags
    return version


def _read_timescale_duration(fh, at: int) -> tuple[int, int]:
    """mvhd / mdhd から (timescale, duration) を取る。"""
    version = _read_version(fh, at)
    if version == 1:
        fh.read(16)  # creation_time + modification_time
        timescale = struct.unpack(">I", fh.read(4))[0]
        duration = struct.unpack(">Q", fh.read(8))[0]
    else:
        fh.read(8)
        timescale = struct.unpack(">I", fh.read(4))[0]
        duration = struct.unpack(">I", fh.read(4))[0]
    return timescale, duration


def _read_tkhd(fh, at: int) -> tuple[float, float, int]:
    """tkhd から (width, height, rotation) を取る。"""
    version = _read_version(fh, at)
    fh.read(16 if version == 1 else 8)   # creation + modification
    fh.read(4)                           # track_ID
    fh.read(4)                           # reserved
    fh.read(8 if version == 1 else 4)    # duration
    fh.read(8)                           # reserved[2]
    fh.read(8)                           # layer, alternate_group, volume, reserved
    matrix = [struct.unpack(">i", fh.read(4))[0] for _ in range(9)]
    width = struct.unpack(">I", fh.read(4))[0] / 65536.0
    height = struct.unpack(">I", fh.read(4))[0] / 65536.0
    key = (matrix[0] / 65536.0, matrix[1] / 65536.0,
           matrix[3] / 65536.0, matrix[4] / 65536.0)
    return width, height, _ROTATIONS.get(key, 0)


def _sample_count(fh, trak_start: int, trak_end: int) -> int:
    """stsz を優先し、無ければ stts を合算してサンプル数を出す。"""
    stsz = _find(fh, trak_start, trak_end, ("mdia", "minf", "stbl", "stsz"))
    if stsz:
        fh.seek(stsz[0] + 4)          # version + flags を飛ばす
        fh.read(4)                    # sample_size
        count = struct.unpack(">I", fh.read(4))[0]
        if count:
            return count
    stts = _find(fh, trak_start, trak_end, ("mdia", "minf", "stbl", "stts"))
    if stts:
        fh.seek(stts[0] + 4)
        entries = struct.unpack(">I", fh.read(4))[0]
        total = 0
        for _ in range(min(entries, 100000)):
            chunk = fh.read(8)
            if len(chunk) < 8:
                break
            total += struct.unpack(">II", chunk)[0]
        return total
    return 0


def _codec(fh, trak_start: int, trak_end: int) -> str:
    stsd = _find(fh, trak_start, trak_end, ("mdia", "minf", "stbl", "stsd"))
    if not stsd:
        return "unknown"
    fh.seek(stsd[0] + 8)   # version/flags + entry_count
    fh.read(4)             # entry size
    raw = fh.read(4).decode("latin1", "replace")
    return _CODECS.get(raw, raw)


def _handler(fh, trak_start: int, trak_end: int) -> str:
    hdlr = _find(fh, trak_start, trak_end, ("mdia", "hdlr"))
    if not hdlr:
        return ""
    fh.seek(hdlr[0] + 8)   # version/flags + pre_defined
    return fh.read(4).decode("latin1", "replace")


def probe_mp4(path: Path) -> dict:
    """ffprobe の -show_streams/-show_format と同じ形の dict を返す。

    video_check._ffprobe() の戻り値をそのまま差し替えられるようにしてある。
    """
    path = Path(path)
    file_size = path.stat().st_size

    with open(path, "rb") as fh:
        head = _find(fh, 0, file_size, ("ftyp",))
        if not head:
            raise ValueError("ftyp ボックスが無い (MP4 ではない)")
        fh.seek(head[0])
        brand = fh.read(4)
        if brand not in _BRANDS:
            # 未知のブランドでも moov が読めれば続行する
            pass

        moov = _find(fh, 0, file_size, ("moov",))
        if not moov:
            raise ValueError("moov ボックスが無い")

        mvhd = _find(fh, moov[0], moov[1], ("mvhd",))
        if not mvhd:
            raise ValueError("mvhd ボックスが無い")
        movie_scale, movie_duration = _read_timescale_duration(fh, mvhd[0])
        movie_sec = movie_duration / movie_scale if movie_scale else 0.0

        streams = []
        for box_type, trak_start, trak_end in _iter_boxes(fh, moov[0], moov[1]):
            if box_type != "trak":
                continue
            kind = _handler(fh, trak_start, trak_end)
            if kind not in ("vide", "soun"):
                continue

            mdhd = _find(fh, trak_start, trak_end, ("mdia", "mdhd"))
            track_scale, track_duration = 0, 0
            if mdhd:
                track_scale, track_duration = _read_timescale_duration(fh, mdhd[0])
            track_sec = track_duration / track_scale if track_scale else 0.0

            stream = {
                "codec_type": "video" if kind == "vide" else "audio",
                "codec_name": _codec(fh, trak_start, trak_end),
                "duration": str(track_sec or movie_sec),
            }

            if kind == "vide":
                tkhd = _find(fh, trak_start, trak_end, ("tkhd",))
                if not tkhd:
                    raise ValueError("映像トラックに tkhd が無い")
                width, height, rotation = _read_tkhd(fh, tkhd[0])
                # video_check 側が回転を見て入れ替えるので、ここでは
                # ffprobe と同じく「格納されている向き」のまま返す。
                stream["width"] = int(round(width))
                stream["height"] = int(round(height))
                if rotation:
                    stream["side_data_list"] = [{"rotation": -rotation}]
                frames = _sample_count(fh, trak_start, trak_end)
                scale, duration = track_scale, track_duration
                if not (scale and duration):
                    scale, duration = movie_scale, movie_duration
                # ffprobe と同じく整数どうしの比で返す。小数を分母に置くと
                # 受け取り側の Fraction() が解釈できず fps 不明になる。
                if frames and scale and duration:
                    stream["avg_frame_rate"] = f"{frames * scale}/{duration}"
                else:
                    stream["avg_frame_rate"] = "0/0"

            streams.append(stream)

    if not any(s["codec_type"] == "video" for s in streams):
        raise ValueError("映像ストリームが無い")

    return {
        "streams": streams,
        "format": {"duration": str(movie_sec), "size": str(file_size)},
    }
