"""python3 -m tiktok_autopost.tests.test_media_host で実行 (pytest / ネットワーク不要)。

urllib を差し替えて、署名付きアップロードの手順を検証する。
一番守りたいのは **署名付きURLへの PUT に Authorization を付けないこと**
(付けると R2 の署名検証が落ちて投稿が失敗する) と、
**公開URLの検証を省かないこと** (404 のURLを Late に渡すと静かに失敗する)。
"""

from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

from tiktok_autopost import media_host
from tiktok_autopost.media_host import MediaHostError

SIGNED = {
    "uploadUrl": "https://late-media.example.r2.cloudflarestorage.com/temp/x_t.mp4?X-Amz-Signature=abc",
    "publicUrl": "https://media.zernio.com/temp/x_t.mp4",
    "key": "temp/x_t.mp4",
    "type": "video",
}


class _Resp:
    def __init__(self, body=b"", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body


@contextmanager
def requests_recorded(*, size=1234, public_size=None, fail_on=None):
    """_request を差し替えて、呼ばれた (method, url, headers) を記録する。"""
    calls = []
    original = media_host._request

    def fake(url, *, method, headers, data=None, timeout=60):
        calls.append({"method": method, "url": url, "headers": dict(headers)})
        if fail_on and fail_on(method, url):
            raise MediaHostError(f"{method} failed (test)")
        if method == "POST":
            return _Resp(json.dumps(SIGNED).encode())
        if method == "PUT":
            return _Resp(b"")
        if method == "HEAD":
            n = size if public_size is None else public_size
            return _Resp(b"", {"Content-Length": str(n)})
        return _Resp(b"")

    media_host._request = fake
    try:
        yield calls
    finally:
        media_host._request = original


def _tmp_video(tmp: Path, size=1234) -> Path:
    p = tmp / "t.mp4"
    p.write_bytes(b"\0" * size)
    return p


def _tmpdir() -> Path:
    import tempfile
    return Path(tempfile.mkdtemp())


# -------------------------------------------------- 署名リクエスト

def test_request_upload_sends_filename_and_content_type():
    with requests_recorded() as calls:
        data = media_host.request_upload("t.mp4", "video/mp4")
    assert data["publicUrl"] == SIGNED["publicUrl"]
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/media")


def test_request_upload_rejects_response_without_urls():
    original = media_host._request
    media_host._request = lambda url, **kw: _Resp(json.dumps({"key": "x"}).encode())
    try:
        media_host.request_upload("t.mp4", "video/mp4")
    except MediaHostError as exc:
        assert "uploadUrl" in str(exc)
        return
    finally:
        media_host._request = original
    raise AssertionError("MediaHostError が上がらなかった")


# ------------------------------------- 署名URLに Authorization を付けない

def test_put_does_not_send_authorization_header():
    """これが付くと R2 の署名検証が落ちる。最重要。"""
    tmp = _tmpdir()
    path = _tmp_video(tmp)
    with requests_recorded(size=path.stat().st_size) as calls:
        media_host.host(path)
    puts = [c for c in calls if c["method"] == "PUT"]
    assert len(puts) == 1
    for name in puts[0]["headers"]:
        assert name.lower() != "authorization", f"PUT に {name} が付いている"


def test_post_does_send_authorization_header():
    """逆に zernio.com 宛の POST には必要 (プロキシが実鍵に差し替える)。"""
    tmp = _tmpdir()
    path = _tmp_video(tmp)
    with requests_recorded(size=path.stat().st_size) as calls:
        media_host.host(path)
    post = [c for c in calls if c["method"] == "POST"][0]
    assert any(k.lower() == "authorization" for k in post["headers"])


def test_put_sends_content_length():
    tmp = _tmpdir()
    path = _tmp_video(tmp, size=4096)
    with requests_recorded(size=4096) as calls:
        media_host.host(path)
    put = [c for c in calls if c["method"] == "PUT"][0]
    assert put["headers"]["Content-Length"] == "4096"


# -------------------------------------------------- 公開URLの検証

def test_host_verifies_public_url_after_upload():
    tmp = _tmpdir()
    path = _tmp_video(tmp, size=777)
    with requests_recorded(size=777) as calls:
        hosted = media_host.host(path)
    assert hosted.public_url == SIGNED["publicUrl"]
    assert [c["method"] for c in calls] == ["POST", "PUT", "HEAD"]


def test_host_fails_when_public_url_size_differs():
    """途中で切れたアップロードを成功扱いにしない。"""
    tmp = _tmpdir()
    path = _tmp_video(tmp, size=1000)
    with requests_recorded(size=1000, public_size=400):
        try:
            media_host.host(path)
        except MediaHostError as exc:
            assert "サイズが合わない" in str(exc), str(exc)
            return
    raise AssertionError("MediaHostError が上がらなかった")


def test_host_fails_when_public_url_is_unreachable():
    tmp = _tmpdir()
    path = _tmp_video(tmp)
    with requests_recorded(size=path.stat().st_size, fail_on=lambda m, u: m == "HEAD"):
        try:
            media_host.host(path)
        except MediaHostError:
            return
    raise AssertionError("MediaHostError が上がらなかった")


def test_host_fails_when_put_fails():
    tmp = _tmpdir()
    path = _tmp_video(tmp)
    with requests_recorded(size=path.stat().st_size, fail_on=lambda m, u: m == "PUT") as calls:
        try:
            media_host.host(path)
        except MediaHostError:
            assert not any(c["method"] == "HEAD" for c in calls), "PUT 失敗後に検証へ進んでいる"
            return
    raise AssertionError("MediaHostError が上がらなかった")


# -------------------------------------------------- 入力の検査

def test_missing_file_is_refused_without_any_request():
    with requests_recorded() as calls:
        try:
            media_host.host(Path("/fake/nope.mp4"))
        except MediaHostError as exc:
            assert "ファイルが無い" in str(exc)
            assert calls == [], "存在しないファイルで署名URLを取りにいっている"
            return
    raise AssertionError("MediaHostError が上がらなかった")


def test_empty_file_is_refused_without_any_request():
    tmp = _tmpdir()
    path = tmp / "empty.mp4"
    path.write_bytes(b"")
    with requests_recorded() as calls:
        try:
            media_host.host(path)
        except MediaHostError as exc:
            assert "空" in str(exc)
            assert calls == []
            return
    raise AssertionError("MediaHostError が上がらなかった")


def test_content_type_by_suffix():
    assert media_host.content_type_for(Path("a.mp4")) == "video/mp4"
    assert media_host.content_type_for(Path("a.mov")) == "video/quicktime"
    assert media_host.content_type_for(Path("a.webm")) == "video/webm"
    assert media_host.content_type_for(Path("a.bin")) == "video/mp4"


def test_api_base_is_zernio():
    """getlate.dev は zernio.com に 301 するので、最初から zernio を使う。"""
    assert media_host.API_BASE.startswith("https://zernio.com/api/v1")


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
