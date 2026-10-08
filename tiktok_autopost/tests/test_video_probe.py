"""python3 -m tiktok_autopost.tests.test_video_probe で実行 (pytest / ffprobe 不要)。

ffprobe の JSON を差し込んで、向き判定・回転補正・fps 解析を決定的に検証する。
実機の ffprobe に依存しないので、どの環境でも同じ結果になる。
横動画を 1 本も投稿しないことが要件なので、ここは厚く見る。
"""

from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

from tiktok_autopost import video_check

RULES = video_check.load_config()
FAKE = Path("/fake/clip.mp4")


def _probe_json(*, width, height, rotation=None, rotation_in_tags=False,
                fps="30/1", duration="8.0", size="4000000", codec="h264"):
    stream = {
        "codec_type": "video",
        "codec_name": codec,
        "width": width,
        "height": height,
        "r_frame_rate": fps,
        "avg_frame_rate": fps,
        "duration": duration,
    }
    if rotation is not None:
        if rotation_in_tags:
            stream["tags"] = {"rotate": str(rotation)}
        else:
            stream["side_data_list"] = [{"side_data_type": "Display Matrix",
                                         "rotation": rotation}]
    return {
        "streams": [{"codec_type": "audio", "codec_name": "aac"}, stream],
        "format": {"duration": duration, "size": size, "format_name": "mov,mp4"},
    }


@contextmanager
def probe_returning(payload):
    original = video_check._ffprobe
    video_check._ffprobe = lambda path: payload
    try:
        yield
    finally:
        video_check._ffprobe = original


def _check(payload):
    with probe_returning(payload):
        return video_check.check(FAKE, RULES)


# ---------------------------------------------------------------- orientation

def test_portrait_passes():
    result = _check(_probe_json(width=1080, height=1920))
    assert result.ok, result.rejections
    assert result.facts.as_dict()["orientation"] == "portrait"


def test_landscape_rejected():
    result = _check(_probe_json(width=1920, height=1080))
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_the_actual_pv_dimensions_are_rejected():
    """ドライブに混入した 960x540 の PV が確実に落ちること。"""
    result = _check(_probe_json(width=960, height=540))
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_square_rejected():
    result = _check(_probe_json(width=1080, height=1080))
    assert not result.ok
    assert result.facts.as_dict()["orientation"] == "square"
    assert any("縦型" in r for r in result.rejections), result.rejections


# ---------------------------------------------------------------- rotation

def test_rotation_90_in_side_data_makes_it_portrait():
    """コンテナ上 1920x1080 でも rotate=90 なら表示は縦なので通す。"""
    result = _check(_probe_json(width=1920, height=1080, rotation=90))
    assert result.ok, result.rejections
    assert result.facts.width == 1080 and result.facts.height == 1920


def test_rotation_minus_90_makes_it_portrait():
    result = _check(_probe_json(width=1920, height=1080, rotation=-90))
    assert result.ok, result.rejections
    assert result.facts.is_portrait


def test_rotation_270_makes_it_portrait():
    result = _check(_probe_json(width=1920, height=1080, rotation=270))
    assert result.ok, result.rejections
    assert result.facts.is_portrait


def test_rotation_in_tags_is_honoured():
    result = _check(_probe_json(width=1920, height=1080, rotation=90,
                                rotation_in_tags=True))
    assert result.ok, result.rejections
    assert result.facts.is_portrait


def test_rotation_180_does_not_swap():
    """180 度は上下反転なので横のまま。通してはいけない。"""
    result = _check(_probe_json(width=1920, height=1080, rotation=180))
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_rotation_90_on_portrait_source_becomes_landscape():
    """縦素材に 90 度回転が付いていれば表示は横。落とすこと。"""
    result = _check(_probe_json(width=1080, height=1920, rotation=90))
    assert not result.ok
    assert any("縦型" in r for r in result.rejections), result.rejections


def test_float_rotation_parses():
    result = _check(_probe_json(width=1920, height=1080, rotation=-90.0))
    assert result.facts.rotation == -90
    assert result.facts.is_portrait


def test_garbage_rotation_is_ignored():
    payload = _probe_json(width=1080, height=1920)
    payload["streams"][1]["tags"] = {"rotate": "not-a-number"}
    result = _check(payload)
    assert result.facts.rotation == 0
    assert result.ok, result.rejections


# ---------------------------------------------------------------- fps / 尺 / サイズ

def test_fractional_fps_parses():
    result = _check(_probe_json(width=1080, height=1920, fps="30000/1001"))
    assert 29.9 < result.facts.fps < 30.0, result.facts.fps
    assert result.ok, result.rejections


def test_fps_too_low_rejected():
    result = _check(_probe_json(width=1080, height=1920, fps="15/1"))
    assert not result.ok
    assert any("fps" in r for r in result.rejections), result.rejections


def test_fps_too_high_rejected():
    result = _check(_probe_json(width=1080, height=1920, fps="120/1"))
    assert not result.ok
    assert any("fps" in r for r in result.rejections), result.rejections


def test_60fps_boundary_passes():
    result = _check(_probe_json(width=1080, height=1920, fps="60/1"))
    assert result.ok, result.rejections


def test_zero_fps_falls_back_then_rejects():
    payload = _probe_json(width=1080, height=1920, fps="0/0")
    result = _check(payload)
    assert not result.ok
    assert any("fps" in r for r in result.rejections), result.rejections


def test_too_short_rejected():
    result = _check(_probe_json(width=1080, height=1920, duration="2.0"))
    assert not result.ok
    assert any("尺" in r for r in result.rejections), result.rejections


def test_too_long_rejected():
    result = _check(_probe_json(width=1080, height=1920, duration="900"))
    assert not result.ok
    assert any("尺" in r for r in result.rejections), result.rejections


def test_short_side_below_360_rejected():
    result = _check(_probe_json(width=320, height=640))
    assert not result.ok
    assert any("短辺" in r for r in result.rejections), result.rejections


def test_oversize_file_rejected():
    result = _check(_probe_json(width=1080, height=1920, size=str(2 * 1024**3)))
    assert not result.ok
    assert any("サイズ" in r for r in result.rejections), result.rejections


def test_multiple_rejections_all_reported():
    result = _check(_probe_json(width=640, height=360, duration="1.0", fps="10/1"))
    assert not result.ok
    assert len(result.rejections) >= 3, result.rejections


# ---------------------------------------------------------------- 異常系

def test_no_video_stream_raises():
    payload = {"streams": [{"codec_type": "audio"}], "format": {}}
    with probe_returning(payload):
        try:
            video_check.check(FAKE, RULES)
        except video_check.VideoCheckError as exc:
            assert "映像ストリーム" in str(exc)
            return
    raise AssertionError("映像ストリームが無ければ例外にすべき")


def test_zero_resolution_raises():
    with probe_returning(_probe_json(width=0, height=0)):
        try:
            video_check.check(FAKE, RULES)
        except video_check.VideoCheckError as exc:
            assert "解像度" in str(exc)
            return
    raise AssertionError("解像度 0 は例外にすべき")


def test_require_portrait_can_be_turned_off_in_config():
    """設定ファイルだけで挙動が変わること (コードを触らず更新できる要件)。"""
    relaxed = dict(RULES, require_portrait=False)
    with probe_returning(_probe_json(width=1920, height=1080)):
        result = video_check.check(FAKE, relaxed)
    assert result.ok, result.rejections


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
