"""変換した動画を公開URLに載せる。Late(Zernio) 自身のメディア保管を使う。

ゲートを通すために再エンコードした動画は Drive 上に存在しないので、Late に
渡す公開URLをどこかに作らなければならない。当初これを Higgsfield の CDN で
やっていたが、Higgsfield が落ちると**変換が必要な動画は投稿そのものが
できなくなる**。投稿経路の単一障害点を、投稿に無関係なサービスに置いていた。

Late 自身が署名付きアップロードを持っているので、そちらを使う。これで
動画の保管先と投稿先が同じサービスになり、Higgsfield は「内容把握
(キャプション生成の材料)」だけの依存に下がる。そこが落ちてもキャプションを
人が書けば投稿はできる。

手順 (2026-11-06に実地で確認):
    POST /api/v1/media {filename, contentType}
        -> {uploadUrl(署名付きPUT, 1時間有効), publicUrl, key, type}
    PUT uploadUrl  (本文=動画。**Authorization を付けてはいけない**: 署名が壊れる)
    GET publicUrl  (取得できることを必ず確認する)

使い方 (CLI):
    python3 -m tiktok_autopost.media_host /path/out.mp4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tiktok_autopost.media_host")

API_BASE = "https://zernio.com/api/v1"

CONTENT_TYPE_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


class MediaHostError(Exception):
    """署名URLが取れない、PUT が失敗した、または公開URLが読めない。"""


@dataclass
class HostedMedia:
    public_url: str
    key: str
    size_bytes: int

    def as_dict(self) -> dict:
        return {"public_url": self.public_url, "key": self.key, "size_bytes": self.size_bytes}


def content_type_for(path: Path) -> str:
    return CONTENT_TYPE_BY_SUFFIX.get(path.suffix.lower(), "video/mp4")


def _request(url: str, *, method: str, headers: dict, data=None, timeout: int = 60):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise MediaHostError(f"{method} {url.split('?')[0]} -> {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise MediaHostError(f"{method} {url.split('?')[0]} に到達できない: {exc.reason}") from exc


def request_upload(filename: str, content_type: str) -> dict:
    """署名付きアップロードURLを発行してもらう。

    APIキーは環境のネットワークシークレットでプロキシが注入するので、
    ここでは Bearer の値はダミーのままでよい (実際の鍵はコードに持たない)。
    """
    payload = json.dumps({"filename": filename, "contentType": content_type}).encode()
    resp = _request(
        f"{API_BASE}/media",
        method="POST",
        headers={"Authorization": "Bearer dummy", "Content-Type": "application/json"},
        data=payload,
    )
    data = json.loads(resp.read().decode())
    for key in ("uploadUrl", "publicUrl"):
        if not data.get(key):
            raise MediaHostError(f"署名URLの応答に {key} が無い: {data}")
    return data


def put_bytes(upload_url: str, path: Path, content_type: str, *, timeout: int = 1800) -> None:
    """署名付きURLへ本体を送る。

    **Authorization ヘッダを付けないこと。** 署名に含まれないヘッダを足すと
    R2 側で署名検証が落ちる。プロキシが注入するのは zernio.com 宛だけなので、
    cloudflarestorage.com 宛のこの PUT には付かない。
    """
    size = path.stat().st_size
    with open(path, "rb") as fh:
        _request(
            upload_url,
            method="PUT",
            headers={"Content-Type": content_type, "Content-Length": str(size)},
            data=fh,
            timeout=timeout,
        )


def verify(public_url: str, expected_bytes: int, *, timeout: int = 60) -> int:
    """公開URLが本当に読めて、サイズが合っているかを確かめる。

    ここを省くと、404 を返すURLを Late に渡して投稿が静かに失敗する。
    """
    resp = _request(public_url, method="HEAD", headers={}, timeout=timeout)
    raw = resp.headers.get("Content-Length")
    try:
        got = int(raw)
    except (TypeError, ValueError):
        raise MediaHostError(f"公開URLが Content-Length を返さない: {public_url}")
    if got != expected_bytes:
        raise MediaHostError(
            f"公開URLのサイズが合わない (期待 {expected_bytes}, 実際 {got}): {public_url}"
        )
    return got


def host(path: Path, *, timeout: int = 1800) -> HostedMedia:
    """ローカルの動画を公開URLに載せて返す。検証まで通す。"""
    if not path.exists():
        raise MediaHostError(f"ファイルが無い: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise MediaHostError(f"ファイルが空: {path}")

    ctype = content_type_for(path)
    signed = request_upload(path.name, ctype)
    logger.info("uploading %s (%d bytes) -> %s", path.name, size, signed["publicUrl"])
    put_bytes(signed["uploadUrl"], path, ctype, timeout=timeout)
    verify(signed["publicUrl"], size)
    logger.info("hosted: %s", signed["publicUrl"])
    return HostedMedia(public_url=signed["publicUrl"], key=signed.get("key", ""), size_bytes=size)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="動画を Late(Zernio) の保管に上げて公開URLを返す")
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    try:
        hosted = host(args.path)
    except MediaHostError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, **hosted.as_dict()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
