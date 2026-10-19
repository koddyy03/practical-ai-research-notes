"""python3 -m tiktok_autopost.tests.test_posting で実行 (pytest不要)。

ffmpeg がある環境では実際に小さい動画を生成して縦型ゲートまで通す。
無ければその分はスキップする (チャンク計算とトークン保存は常に検証)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from tiktok_autopost import tiktok_api, video_check
from tiktok_autopost.post_video import GateFailure, run_gates

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
RULES = video_check.load_config()

MB = 1024 * 1024


# ---------------------------------------------------------------- chunk plan

def test_small_file_is_single_chunk():
    plan = tiktok_api.plan_chunks(3 * MB)
    assert plan.total_chunk_count == 1, plan
    assert plan.chunk_size == 3 * MB
    assert plan.ranges == [(0, 3 * MB - 1)]


def test_mid_file_is_single_chunk():
    size = 40 * MB
    plan = tiktok_api.plan_chunks(size)
    assert plan.total_chunk_count == 1
    assert plan.ranges[-1][1] == size - 1


def test_large_file_last_chunk_absorbs_remainder():
    size = 200 * MB
    plan = tiktok_api.plan_chunks(size)
    # floor(200/64) = 3
    assert plan.total_chunk_count == 3, plan.total_chunk_count
    assert plan.chunk_size == 64 * MB
    # 範囲が隙間なく連続し、全体を覆う
    assert plan.ranges[0][0] == 0
    assert plan.ranges[-1][1] == size - 1
    for (_, prev_end), (next_start, _) in zip(plan.ranges, plan.ranges[1:]):
        assert next_start == prev_end + 1, plan.ranges
    final_len = plan.ranges[-1][1] - plan.ranges[-1][0] + 1
    assert final_len == 72 * MB, final_len
    assert final_len <= tiktok_api.MAX_FINAL_CHUNK_BYTES


def test_exact_multiple_has_even_chunks():
    size = 1024 * MB  # 1GB, 64MB で割り切れる
    plan = tiktok_api.plan_chunks(size)
    assert plan.total_chunk_count == 16, plan.total_chunk_count
    lengths = {end - start + 1 for start, end in plan.ranges}
    assert lengths == {64 * MB}, lengths


def test_ranges_cover_every_byte():
    for size in (1, 5 * MB - 1, 5 * MB, 64 * MB + 3, 130 * MB, 999 * MB):
        plan = tiktok_api.plan_chunks(size)
        covered = sum(end - start + 1 for start, end in plan.ranges)
        assert covered == size, (size, covered, plan.ranges)
        final_len = plan.ranges[-1][1] - plan.ranges[-1][0] + 1
        assert final_len <= tiktok_api.MAX_FINAL_CHUNK_BYTES, (size, final_len)
        assert 1 <= plan.total_chunk_count <= tiktok_api.MAX_CHUNK_COUNT


def test_zero_size_rejected():
    for bad in (0, -1):
        try:
            tiktok_api.plan_chunks(bad)
        except ValueError:
            continue
        raise AssertionError(f"plan_chunks({bad}) should raise")


# ---------------------------------------------------------------- token store

def test_token_store_roundtrip_and_rotation():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "token_state.json"
        store = tiktok_api.TokenStore(path)
        assert store.load().refresh_token == ""  # 無ければ空で返る

        first = tiktok_api.TokenState(
            access_token="a1", refresh_token="r1",
            access_expires_at=time.time() + 3600, open_id="oid", scope="video.publish",
        )
        store.save(first)
        assert store.load().refresh_token == "r1"
        assert store.load().access_token_valid

        # ローテーション後の値で上書きされること
        store.save(tiktok_api.TokenState(
            access_token="a2", refresh_token="r2", access_expires_at=time.time() + 3600,
        ))
        assert store.load().refresh_token == "r2"


def test_expired_access_token_is_not_valid():
    state = tiktok_api.TokenState(access_token="a", refresh_token="r",
                                  access_expires_at=time.time() - 1)
    assert not state.access_token_valid
    # 期限内でも残り 60 秒未満は無効扱い
    assert not tiktok_api.TokenState(
        access_token="a", refresh_token="r", access_expires_at=time.time() + 30
    ).access_token_valid


def test_refresh_without_token_raises():
    try:
        tiktok_api.refresh_access_token("k", "s", "")
    except tiktok_api.TikTokAPIError as exc:
        assert "refresh_token" in str(exc)
        return
    raise AssertionError("空の refresh_token は例外にすべき")


def test_authorize_url_has_scopes():
    url = tiktok_api.build_authorize_url("ck", "https://example.com/cb", "st")
    assert "client_key=ck" in url
    assert "video.publish" in url
    assert "response_type=code" in url
    assert "state=st" in url


# ---------------------------------------------------------------- video gate

def _make_video(path: Path, width: int, height: int, seconds: int = 4, fps: int = 30) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}:duration={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def test_portrait_passes_landscape_rejected():
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tall = Path(tmp) / "tall.mp4"
        wide = Path(tmp) / "wide.mp4"
        _make_video(tall, 720, 1280)
        _make_video(wide, 1280, 720)

        good = video_check.check(tall, RULES)
        assert good.ok, good.rejections
        assert good.facts.is_portrait

        bad = video_check.check(wide, RULES)
        assert not bad.ok
        assert any("縦型" in r for r in bad.rejections), bad.rejections


def test_square_rejected():
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        square = Path(tmp) / "square.mp4"
        _make_video(square, 720, 720)
        result = video_check.check(square, RULES)
        assert not result.ok
        assert result.facts.as_dict()["orientation"] == "square"


def test_too_short_rejected():
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        short = Path(tmp) / "short.mp4"
        _make_video(short, 720, 1280, seconds=1)
        result = video_check.check(short, RULES)
        assert not result.ok
        assert any("尺" in r for r in result.rejections), result.rejections


def test_small_side_rejected():
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tiny = Path(tmp) / "tiny.mp4"
        _make_video(tiny, 180, 320)
        result = video_check.check(tiny, RULES)
        assert not result.ok
        assert any("短辺" in r for r in result.rejections), result.rejections


def test_rotation_metadata_counts_as_portrait():
    """コンテナ上は横でも rotate=90 なら表示は縦。取り違えないこと。"""
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.mp4"
        rotated = Path(tmp) / "rotated.mp4"
        _make_video(src, 1280, 720)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src),
             "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rotated)],
            check=True, capture_output=True,
        )
        facts = video_check.probe(rotated)
        if facts.rotation == 0:
            print("     (skip: この ffmpeg は rotate メタデータを保持しない)")
            return
        assert facts.is_portrait, facts.as_dict()


# ---------------------------------------------------------------- gates

def test_gate_rejects_missing_file():
    try:
        run_gates(Path("/nonexistent/clip.mp4"), "she said yes at the grove", "", RULES)
    except GateFailure as exc:
        assert "動画ファイルが無い" in str(exc)
        return
    raise AssertionError("存在しないファイルはゲートで落ちるべき")


def test_gate_rejects_bad_extension():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.avi"
        path.write_bytes(b"x")
        try:
            run_gates(path, "she said yes at the grove", "", RULES)
        except GateFailure as exc:
            assert "拡張子" in str(exc)
            return
    raise AssertionError("avi はゲートで落ちるべき")


def test_gate_rejects_selling_caption_before_any_api_call():
    """キャプション違反は投稿前に止まる (広告に見えた時点で数字が死ぬため)。"""
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tall = Path(tmp) / "tall.mp4"
        _make_video(tall, 720, 1280)
        try:
            run_gates(tall, "link in bio shop now", "", RULES)
        except GateFailure as exc:
            assert "キャプション検証違反" in str(exc), exc
            return
    raise AssertionError("売り込みキャプションはゲートで落ちるべき")


def test_gate_happy_path_returns_full_caption():
    if not HAVE_FFMPEG:
        print("     (skip: ffmpeg なし)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tall = Path(tmp) / "tall.mp4"
        _make_video(tall, 720, 1280)
        gated = run_gates(tall, "she said yes at the grove", "she almost said no", RULES)
        assert gated["video"]["orientation"] == "portrait"
        assert gated["post"]["full_caption"].startswith("she said yes at the grove #")
        assert "#silktherich" in gated["post"]["full_caption"]


def test_config_is_loadable_and_portrait_required():
    assert RULES["require_portrait"] is True
    assert RULES["privacy_level"] == "PUBLIC_TO_EVERYONE"
    json.dumps(RULES)  # シリアライズできる


# ---------------------------------------------------------------- 別環境からの受け渡し

def test_env_var_seeds_token_when_file_missing():
    """別環境で初回認可した場合、環境変数だけで起動できること。"""
    import os
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "absent.json"
        os.environ["TIKTOK_REFRESH_TOKEN"] = "seeded-rt"
        try:
            state = tiktok_api.TokenStore(path).load()
        finally:
            del os.environ["TIKTOK_REFRESH_TOKEN"]
        assert state.refresh_token == "seeded-rt"
        # アクセストークンは無いので必ず refresh が走る
        assert not state.access_token_valid


def test_file_wins_over_env_var():
    """ファイルがあれば環境変数は使わない(ローテーション後の値を失わないため)。"""
    import os
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "token.json"
        store = tiktok_api.TokenStore(path)
        store.save(tiktok_api.TokenState(access_token="a", refresh_token="from-file",
                                         access_expires_at=time.time() + 3600))
        os.environ["TIKTOK_REFRESH_TOKEN"] = "from-env"
        try:
            assert store.load().refresh_token == "from-file"
        finally:
            del os.environ["TIKTOK_REFRESH_TOKEN"]


def test_parses_camelcase_token_file():
    """他実装が書いた camelCase のトークンJSONも読めること。"""
    state = tiktok_api.parse_token_file({
        "accessToken": "at", "refreshToken": "rt", "openId": "oid", "scopes": "video.publish",
    })
    assert state.access_token == "at"
    assert state.refresh_token == "rt"
    assert state.open_id == "oid"


def test_expires_in_only_is_treated_as_expired():
    """相対秒しか無い形式は発行時刻が不明なので使い回さない。"""
    state = tiktok_api.parse_token_file({
        "access_token": "at", "refresh_token": "rt", "expires_in": 86400,
    })
    assert state.refresh_token == "rt"
    assert not state.access_token_valid, "発行時刻不明のアクセストークンを有効扱いしてはいけない"


def test_absolute_expiry_is_honoured():
    state = tiktok_api.parse_token_file({
        "access_token": "at", "refresh_token": "rt",
        "access_expires_at": time.time() + 3600,
    })
    assert state.access_token_valid


# ---------------------------------------------------------------- 認証情報の置き場所

def test_credentials_read_from_file():
    """環境変数欄は全員に見えるので、認証情報はファイル側から読めること。"""
    key, secret = tiktok_api.load_credentials(
        {"client_key": "ck-file", "client_secret": "cs-file", "refresh_token": "rt"}
    )
    assert (key, secret) == ("ck-file", "cs-file")


def test_credentials_file_wins_over_env():
    import os
    os.environ["TIKTOK_CLIENT_KEY"] = "ck-env"
    os.environ["TIKTOK_CLIENT_SECRET"] = "cs-env"
    try:
        key, secret = tiktok_api.load_credentials(
            {"client_key": "ck-file", "client_secret": "cs-file"}
        )
        assert (key, secret) == ("ck-file", "cs-file")
    finally:
        del os.environ["TIKTOK_CLIENT_KEY"], os.environ["TIKTOK_CLIENT_SECRET"]


def test_credentials_fall_back_to_env():
    import os
    os.environ["TIKTOK_CLIENT_KEY"] = "ck-env"
    os.environ["TIKTOK_CLIENT_SECRET"] = "cs-env"
    try:
        assert tiktok_api.load_credentials({}) == ("ck-env", "cs-env")
    finally:
        del os.environ["TIKTOK_CLIENT_KEY"], os.environ["TIKTOK_CLIENT_SECRET"]


def test_missing_credentials_name_what_is_missing():
    import os
    for v in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"):
        os.environ.pop(v, None)
    try:
        tiktok_api.load_credentials({"client_key": "ck"})
    except tiktok_api.TikTokAPIError as exc:
        assert "client_secret" in str(exc)
        assert "client_key" not in str(exc), "揃っている方を欠落として報告しない"
        return
    raise AssertionError("client_secret が無ければ例外にすべき")


def test_save_keeps_credentials_in_the_same_file():
    """トークン更新で client_secret を消してしまわないこと。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "creds.json"
        path.write_text(json.dumps({
            "client_key": "ck", "client_secret": "cs", "refresh_token": "rt-old",
        }), encoding="utf-8")
        store = tiktok_api.TokenStore(path)
        assert store.load().refresh_token == "rt-old"

        # ローテーション後を書き戻す
        store.save(tiktok_api.TokenState(
            access_token="at", refresh_token="rt-new",
            access_expires_at=time.time() + 3600,
        ))

        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["refresh_token"] == "rt-new"
        assert after["client_key"] == "ck", "認証情報が消えている"
        assert after["client_secret"] == "cs", "認証情報が消えている"
        assert tiktok_api.load_credentials(store.raw()) == ("ck", "cs")


def test_token_file_with_bom_is_readable():
    """Driveやエディタ経由だとBOMが付くことがある。それで落ちないこと。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "creds.json"
        body = json.dumps({"client_key": "ck", "client_secret": "cs",
                           "refresh_token": "rt"})
        path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        store = tiktok_api.TokenStore(path)
        assert store.load().refresh_token == "rt"
        assert tiktok_api.load_credentials(store.raw()) == ("ck", "cs")


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed  (ffmpeg={'yes' if HAVE_FFMPEG else 'no'})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
