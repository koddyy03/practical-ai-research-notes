"""無音素材の検出。pytest / ffprobe 不要。

編集アプリが元音声をミュートして書き出すと、音声トラックは存在するのに
中身が -60dBFS 付近の空トラックになる。実際にそれが4本投稿されてしまったので、
ゲートで警告を出す。投稿は止めない (止めてよいのは
ゲート不合格 / 内容重複 / ブランドPV の3つだけ)。
"""

from __future__ import annotations

from pathlib import Path

from tiktok_autopost import video_check

RULES = video_check.load_config()


def _facts(**over) -> video_check.VideoFacts:
    base = dict(
        width=1080, height=1920, duration_sec=7.7, fps=30.0,
        size_bytes=14_809_540, codec="hevc", rotation=0,
        audio_codec="aac", audio_max_dbfs=-3.6, audio_mean_dbfs=-17.1,
    )
    base.update(over)
    return video_check.VideoFacts(**base)


def _check(facts: video_check.VideoFacts) -> video_check.CheckResult:
    original = video_check.probe
    video_check.probe = lambda _path: facts
    try:
        return video_check.check(Path("dummy.mp4"), RULES)
    finally:
        video_check.probe = original


def test_normal_audio_passes_without_warning():
    # 実測値: 11/16 に投稿したカメラ直出し素材 (peak -3.6 / mean -17.1)
    r = _check(_facts())
    assert r.ok and not r.warnings, r.warnings


def test_muted_export_warns_but_does_not_block():
    # 実測値: chasebank.mp4 (peak -59.9 / mean -73.3)
    r = _check(_facts(audio_max_dbfs=-59.9, audio_mean_dbfs=-73.3))
    assert r.ok, r.rejections          # 投稿は止めない
    assert not r.rejections
    assert len(r.warnings) == 1
    assert "無音" in r.warnings[0]


def test_near_silent_export_warns():
    # 実測値: stock.mp4 (peak -39.6 / mean -55.0) — 聞こえないが -50 では拾えない
    r = _check(_facts(audio_max_dbfs=-39.6, audio_mean_dbfs=-55.0))
    assert r.ok and len(r.warnings) == 1, r.warnings


def test_no_audio_track_warns():
    r = _check(_facts(audio_codec=None, audio_max_dbfs=None, audio_mean_dbfs=None))
    assert r.ok
    assert any("音声トラックが無い" in w for w in r.warnings), r.warnings


def test_unmeasurable_loudness_does_not_warn():
    # ffmpeg が無い環境では測れない。測れないことを無音扱いにはしない。
    r = _check(_facts(audio_max_dbfs=None, audio_mean_dbfs=None))
    assert r.ok and not r.warnings, r.warnings


def test_warning_can_be_switched_off():
    rules = dict(RULES, warn_silent_audio=False)
    original = video_check.probe
    video_check.probe = lambda _p: _facts(audio_max_dbfs=-59.9, audio_mean_dbfs=-73.3)
    try:
        r = video_check.check(Path("dummy.mp4"), rules)
    finally:
        video_check.probe = original
    assert not r.warnings


if __name__ == "__main__":
    import traceback
    fails = 0
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
