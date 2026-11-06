"""python3 -m tiktok_autopost.tests.test_transcode で実行 (pytest / ffmpeg 不要)。

ffmpeg は呼ばずに「何を変換し、何を絶対に変換しないか」を検証する。
ここで一番守りたいのは、横動画と尺超過が自動変換で投稿可能にならないこと。
"""

from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

from tiktok_autopost import transcode, video_check
from tiktok_autopost.video_check import VideoCheckError, VideoFacts

RULES = video_check.load_config()
FAKE = Path("/fake/clip.mp4")


def _facts(width, height, *, fps=30.0, duration=30.0, size=50_000_000, codec="h264"):
    return VideoFacts(
        width=width, height=height, duration_sec=duration, fps=fps,
        size_bytes=size, codec=codec, rotation=0,
    )


def _probe_json(*, width, height, fps="30/1", duration="30.0", size="50000000"):
    stream = {
        "codec_type": "video", "codec_name": "h264",
        "width": width, "height": height,
        "r_frame_rate": fps, "avg_frame_rate": fps, "duration": duration,
    }
    return {"streams": [stream], "format": {"duration": duration, "size": size}}


@contextmanager
def probe_returning(*payloads):
    """1 回目/2 回目… の video_check.check() が見る ffprobe 出力を順に差し込む。"""
    original = video_check._ffprobe
    seq = list(payloads)

    def fake(path):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    video_check._ffprobe = fake
    try:
        yield
    finally:
        video_check._ffprobe = original


@contextmanager
def ffmpeg_recording(sizes=None):
    """run_ffmpeg を差し替えて、呼ばれた回数と plan を記録する。"""
    calls = []
    original = transcode.run_ffmpeg

    def fake(src, dst, plan, **kw):
        calls.append(plan)
        return dst

    transcode.run_ffmpeg = fake
    try:
        yield calls
    finally:
        transcode.run_ffmpeg = original


# ------------------------------------------------- 分類: 直せる / 直せない

def test_landscape_is_never_fixable():
    """横動画は変換対象にしない。クロップで縦にしてはいけない。"""
    fixable, unfixable = transcode.classify(["縦型ではない (1920x1080, landscape)"])
    assert fixable == []
    assert len(unfixable) == 1


def test_duration_is_never_fixable():
    """10分超を自動で切るのは編集判断なので変換しない。"""
    fixable, unfixable = transcode.classify(["尺が 3.0-600.0秒 の範囲外 (900.0秒)"])
    assert fixable == []
    assert len(unfixable) == 1


def test_resolution_fps_and_size_are_fixable():
    fixable, unfixable = transcode.classify([
        "長辺が 4096px 超過 (4320x7680)",
        "短辺が 360px 未満 (200x400)",
        "fps が 23.0-60.0 の範囲外 (120.00)",
        "ファイルサイズが上限超過 (5000000000 > 4294967296 bytes)",
    ])
    assert len(fixable) == 4
    assert unfixable == []


def test_unknown_reason_is_treated_as_unfixable():
    """知らない理由を勝手に変換して投稿しない (止めて人に見せる)。"""
    fixable, unfixable = transcode.classify(["なにか新しい却下理由"])
    assert fixable == []
    assert unfixable == ["なにか新しい却下理由"]


# ------------------------------------------------------------- 変換計画

def test_8k_vertical_plans_down_to_1080x1920():
    """nov3_1.mp4 の実寸 (7680x4320 を縦に持った素材) が配信解像度に落ちる。"""
    plan = transcode.plan_for(_facts(4320, 7680, fps=60.0), RULES)
    assert (plan.width, plan.height) == (1080, 1920)
    # 60fps は TikTok の規定内 (23-60) なので触らない。手でやったときは
    # 念のため 30 に落としたが、落とす必要はなかった。
    assert plan.fps is None


def test_plan_keeps_aspect_ratio():
    plan = transcode.plan_for(_facts(2160, 2880), RULES)  # 3:4
    assert plan.height == 1920
    assert plan.width == 1440


def test_plan_dimensions_are_even():
    """yuv420p は奇数の縦横を取れない。"""
    plan = transcode.plan_for(_facts(1079, 4321), RULES)
    assert plan.width % 2 == 0
    assert plan.height % 2 == 0


def test_plan_upscales_when_short_side_is_below_minimum():
    plan = transcode.plan_for(_facts(200, 400), RULES)
    assert min(plan.width, plan.height) >= RULES["min_side_px"]


def test_plan_leaves_fps_alone_when_already_in_range():
    plan = transcode.plan_for(_facts(4320, 7680, fps=30.0), RULES)
    assert plan.fps is None


def test_plan_pulls_120fps_into_range():
    plan = transcode.plan_for(_facts(1080, 1920, fps=120.0), RULES)
    assert RULES["min_fps"] <= plan.fps <= RULES["max_fps"]


def test_extreme_aspect_ratio_is_refused():
    """短辺下限と長辺上限を同時に満たせない形は変換しない。"""
    try:
        transcode.plan_for(_facts(10, 100000), RULES)
    except transcode.TranscodeError:
        return
    raise AssertionError("TranscodeError が上がらなかった")


# ------------------------------------------------------- ensure_postable

def test_already_postable_file_is_not_transcoded():
    ok = _probe_json(width=1080, height=1920)
    with probe_returning(ok), ffmpeg_recording() as calls:
        path, result, plan = transcode.ensure_postable(FAKE, RULES)
    assert path == FAKE
    assert plan is None
    assert calls == []
    assert result.ok


def test_oversized_resolution_is_transcoded_and_rechecked():
    too_big = _probe_json(width=4320, height=7680, fps="60/1")
    fixed = _probe_json(width=1080, height=1920)
    with probe_returning(too_big, fixed), ffmpeg_recording() as calls:
        path, result, plan = transcode.ensure_postable(FAKE, RULES, workdir=Path("/fake"))
    assert len(calls) == 1
    assert plan is not None
    assert result.ok
    assert path != FAKE
    assert path.name.endswith(".mp4")


def test_landscape_is_not_transcoded_even_though_it_could_be():
    """横動画に ffmpeg を一度も走らせないこと。"""
    landscape = _probe_json(width=1920, height=1080)
    with probe_returning(landscape), ffmpeg_recording() as calls:
        try:
            transcode.ensure_postable(FAKE, RULES)
        except VideoCheckError as exc:
            assert "縦型ではない" in str(exc), str(exc)
            assert calls == [], "横動画に変換を走らせてはいけない"
            return
    raise AssertionError("VideoCheckError が上がらなかった")


def test_landscape_plus_fixable_reason_still_refuses():
    """直せる理由が混ざっていても、横動画なら全体を止める。"""
    both = _probe_json(width=7680, height=4320, fps="60/1")
    with probe_returning(both), ffmpeg_recording() as calls:
        try:
            transcode.ensure_postable(FAKE, RULES)
        except VideoCheckError:
            assert calls == []
            return
    raise AssertionError("VideoCheckError が上がらなかった")


def test_too_long_video_is_not_transcoded():
    long_clip = _probe_json(width=4320, height=7680, duration="900.0", fps="60/1")
    with probe_returning(long_clip), ffmpeg_recording() as calls:
        try:
            transcode.ensure_postable(FAKE, RULES)
        except VideoCheckError as exc:
            assert "尺が" in str(exc), str(exc)
            assert calls == []
            return
    raise AssertionError("VideoCheckError が上がらなかった")


def test_transcode_can_be_disabled_in_config():
    """設定で止められること (コードを触らず戻せる要件)。"""
    off = dict(RULES, transcode_enabled=False)
    assert off["transcode_enabled"] is False


def test_size_only_rejection_retries_with_a_bitrate_cap():
    """1 回目が容量超過なら、ビットレート指定で詰め直す。"""
    huge = _probe_json(width=4320, height=7680, fps="60/1", size="9000000000")
    still_huge = _probe_json(width=1080, height=1920, size="5000000000")
    fits = _probe_json(width=1080, height=1920, size="80000000")
    with probe_returning(huge, still_huge, fits), ffmpeg_recording() as calls:
        path, result, plan = transcode.ensure_postable(FAKE, RULES, workdir=Path("/fake"))
    assert len(calls) == 2, calls
    assert calls[0].video_bitrate_bps is None
    assert calls[1].video_bitrate_bps and calls[1].video_bitrate_bps > 0
    assert result.ok


def test_bitrate_to_fit_refuses_impossible_targets():
    try:
        transcode._bitrate_to_fit(1_000_000, 600.0)
    except transcode.TranscodeError:
        return
    raise AssertionError("TranscodeError が上がらなかった")


def test_bitrate_to_fit_leaves_room_for_audio_and_container():
    max_bytes, duration = 100_000_000, 60.0
    bps = transcode._bitrate_to_fit(max_bytes, duration)
    assert bps + 128_000 < (max_bytes * 8) / duration


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
