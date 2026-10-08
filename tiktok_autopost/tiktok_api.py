"""TikTok Content Posting API クライアント (Direct Post / FILE_UPLOAD)。

Higgsfield の仲介ツールを介さず、自前の TikTok 開発者アプリで直接投稿する。
依存は標準ライブラリのみ (サンドボックスに pip できないため requests は使わない)。

フロー:
  1. refresh_access_token()  -> アクセストークン更新 (リフレッシュトークンは毎回ローテーションする)
  2. query_creator_info()    -> 公開範囲の選択肢と最大尺を取得 (TikTok 側の必須手順)
  3. init_direct_post()      -> publish_id と upload_url を取得
  4. upload_file()           -> 分割して PUT
  5. poll_until_done()       -> PUBLISH_COMPLETE まで待つ

重要な制約:
  - 審査 (audit) を通していないアプリは公開範囲が強制的に非公開になる。
    privacy_level に PUBLIC_TO_EVERYONE を投げても拒否される。
  - リフレッシュトークンは更新ごとに新しい値が返り、古い値は使えなくなる。
    コンテナが毎回作り直される環境では、必ず TokenStore で永続化すること。
  - コメント投稿 API は Content Posting API に無い。ファーストコメントは別手段。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tiktok_autopost.tiktok_api")

API_BASE = "https://open.tiktokapis.com"
TOKEN_URL = f"{API_BASE}/v2/oauth/token/"
CREATOR_INFO_URL = f"{API_BASE}/v2/post/publish/creator_info/query/"
INIT_URL = f"{API_BASE}/v2/post/publish/video/init/"
STATUS_URL = f"{API_BASE}/v2/post/publish/status/fetch/"
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"

SCOPES = ("user.info.basic", "video.publish")

MIN_CHUNK_BYTES = 5 * 1024 * 1024        # 5MB
MAX_CHUNK_BYTES = 64 * 1024 * 1024       # 64MB
MAX_FINAL_CHUNK_BYTES = 128 * 1024 * 1024  # 最終チャンクのみ 128MB まで
MAX_CHUNK_COUNT = 1000

TERMINAL_OK = "PUBLISH_COMPLETE"
TERMINAL_BAD = {"FAILED"}


class TikTokAPIError(Exception):
    """API がエラーを返した、または想定外の応答。"""

    def __init__(self, message: str, *, code: str = "", http_status: int = 0, log_id: str = ""):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.log_id = log_id


# ---------------------------------------------------------------- token store


@dataclass
class TokenState:
    """アクセストークンとリフレッシュトークンの保存形。

    refresh_token は更新ごとに値が変わるので、save() を必ず通す。
    """

    access_token: str = ""
    refresh_token: str = ""
    access_expires_at: float = 0.0
    open_id: str = ""
    scope: str = ""

    @property
    def access_token_valid(self) -> bool:
        # 60秒の余裕を持たせる
        return bool(self.access_token) and time.time() < (self.access_expires_at - 60)

    def as_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": self.access_expires_at,
            "open_id": self.open_id,
            "scope": self.scope,
        }


class TokenStore:
    """JSON ファイル 1 枚でトークンを持ち回す。

    このコンテナは使い捨てなので、呼び出し側がこのファイルを
    Google Drive 等の永続先と同期させる責任を持つ。
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> TokenState:
        if not self.path.exists():
            return TokenState()
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return TokenState(
            access_token=raw.get("access_token", ""),
            refresh_token=raw.get("refresh_token", ""),
            access_expires_at=float(raw.get("access_expires_at") or 0),
            open_id=raw.get("open_id", ""),
            scope=raw.get("scope", ""),
        )

    def save(self, state: TokenState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state.as_dict(), fh, indent=2)
        tmp.replace(self.path)
        logger.info("token state saved: %s", self.path)


# ---------------------------------------------------------------- http helpers


def _request(
    url: str,
    *,
    method: str = "POST",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = 120,
) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise TikTokAPIError(f"接続できない ({url}): {exc.reason}") from exc


def _post_json(url: str, access_token: str, payload: dict, *, timeout: int = 120) -> dict:
    status, _, raw = _request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        body=json.dumps(payload).encode("utf-8"),
        timeout=timeout,
    )
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise TikTokAPIError(
            f"JSON ではない応答 (HTTP {status}): {raw[:300]!r}", http_status=status
        ) from exc

    err = parsed.get("error") or {}
    code = str(err.get("code") or "")
    # TikTok は成功時も error.code = "ok" を返す
    if code and code not in ("ok", "", "0"):
        raise TikTokAPIError(
            f"{code}: {err.get('message') or '(no message)'}",
            code=code,
            http_status=status,
            log_id=str(err.get("log_id") or ""),
        )
    if status >= 400:
        raise TikTokAPIError(f"HTTP {status}: {raw[:300]!r}", http_status=status)
    return parsed


# ---------------------------------------------------------------- oauth


def build_authorize_url(client_key: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "scope": ",".join(SCOPES),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _token_call(form: dict) -> dict:
    status, _, raw = _request(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
        },
        body=urllib.parse.urlencode(form).encode("utf-8"),
    )
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise TikTokAPIError(
            f"トークン応答が JSON ではない (HTTP {status}): {raw[:300]!r}", http_status=status
        ) from exc
    if parsed.get("error"):
        raise TikTokAPIError(
            f"{parsed.get('error')}: {parsed.get('error_description') or ''}",
            code=str(parsed.get("error")),
            http_status=status,
            log_id=str(parsed.get("log_id") or ""),
        )
    if "access_token" not in parsed:
        raise TikTokAPIError(f"access_token が無い応答: {parsed}", http_status=status)
    return parsed


def _state_from_token_response(parsed: dict) -> TokenState:
    return TokenState(
        access_token=parsed["access_token"],
        refresh_token=parsed.get("refresh_token", ""),
        access_expires_at=time.time() + float(parsed.get("expires_in") or 0),
        open_id=parsed.get("open_id", ""),
        scope=parsed.get("scope", ""),
    )


def exchange_code_for_token(
    client_key: str, client_secret: str, code: str, redirect_uri: str
) -> TokenState:
    """初回の認可コード -> トークン交換。人間が 1 回だけ踏む経路。"""
    parsed = _token_call(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": urllib.parse.unquote(code),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )
    return _state_from_token_response(parsed)


def refresh_access_token(client_key: str, client_secret: str, refresh_token: str) -> TokenState:
    """リフレッシュトークンでアクセストークンを更新する。

    応答には新しい refresh_token が入る。古い値は無効になるので、
    返り値を必ず保存すること。
    """
    if not refresh_token:
        raise TikTokAPIError("refresh_token が無い。初回認可 (auth_setup) が必要。")
    parsed = _token_call(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )
    state = _state_from_token_response(parsed)
    if not state.refresh_token:
        # 応答に無ければ既存の値を保つ (仕様上は必ず返るが、落とさない)
        state.refresh_token = refresh_token
    return state


# ---------------------------------------------------------------- chunk plan


@dataclass
class ChunkPlan:
    video_size: int
    chunk_size: int
    total_chunk_count: int
    ranges: list[tuple[int, int]] = field(default_factory=list)


def plan_chunks(video_size: int) -> ChunkPlan:
    """TikTok の分割ルールに合わせてチャンクを割る。

    - total_chunk_count = floor(video_size / chunk_size)
    - 各チャンクは 5MB-64MB、最終チャンクのみ残りを飲んで 128MB まで
    - 5MB 未満は分割せず 1 チャンク
    """
    if video_size <= 0:
        raise ValueError("video_size must be positive")

    if video_size <= MAX_CHUNK_BYTES:
        # 64MB 以下は 1 チャンク (5MB 未満もここ)
        chunk_size = video_size
    else:
        chunk_size = MAX_CHUNK_BYTES

    total = video_size // chunk_size
    if total < 1:
        total = 1
    if total > MAX_CHUNK_COUNT:
        raise ValueError(
            f"チャンク数が上限超過 ({total} > {MAX_CHUNK_COUNT}): ファイルが大きすぎる"
        )

    ranges: list[tuple[int, int]] = []
    for index in range(total):
        start = index * chunk_size
        # 最終チャンクは残り全部を飲む
        end = video_size - 1 if index == total - 1 else start + chunk_size - 1
        ranges.append((start, end))

    final_len = ranges[-1][1] - ranges[-1][0] + 1
    if final_len > MAX_FINAL_CHUNK_BYTES:
        raise ValueError(f"最終チャンクが 128MB を超える ({final_len} bytes)")

    return ChunkPlan(
        video_size=video_size, chunk_size=chunk_size, total_chunk_count=total, ranges=ranges
    )


# ---------------------------------------------------------------- posting


def query_creator_info(access_token: str) -> dict:
    """公開範囲の選択肢・最大尺・各種 disabled を取得。

    TikTok の規約で、投稿前にこれを呼んで最新値を使うことが求められている。
    """
    parsed = _post_json(CREATOR_INFO_URL, access_token, {})
    return parsed.get("data") or {}


def init_direct_post(
    access_token: str,
    *,
    caption: str,
    plan: ChunkPlan,
    privacy_level: str,
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    video_cover_timestamp_ms: int = 1000,
    is_aigc: bool = False,
) -> dict:
    payload = {
        "post_info": {
            "title": caption,
            "privacy_level": privacy_level,
            "disable_comment": disable_comment,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            "video_cover_timestamp_ms": video_cover_timestamp_ms,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "is_aigc": is_aigc,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": plan.video_size,
            "chunk_size": plan.chunk_size,
            "total_chunk_count": plan.total_chunk_count,
        },
    }
    parsed = _post_json(INIT_URL, access_token, payload)
    data = parsed.get("data") or {}
    if not data.get("publish_id") or not data.get("upload_url"):
        raise TikTokAPIError(f"init の応答に publish_id / upload_url が無い: {parsed}")
    return data


def upload_file(upload_url: str, path: Path, plan: ChunkPlan, *, mime: str = "video/mp4") -> None:
    """計画どおりに順番に PUT する。順序は守る必要がある。"""
    with open(path, "rb") as fh:
        for index, (start, end) in enumerate(plan.ranges, start=1):
            fh.seek(start)
            length = end - start + 1
            chunk = fh.read(length)
            if len(chunk) != length:
                raise TikTokAPIError(
                    f"チャンク {index} の読み出しが不足 ({len(chunk)} != {length})"
                )
            status, _, raw = _request(
                upload_url,
                method="PUT",
                headers={
                    "Content-Type": mime,
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{end}/{plan.video_size}",
                },
                body=chunk,
                timeout=600,
            )
            if status not in (200, 201, 202, 204, 206, 308):
                raise TikTokAPIError(
                    f"チャンク {index}/{plan.total_chunk_count} の PUT 失敗 "
                    f"(HTTP {status}): {raw[:300]!r}",
                    http_status=status,
                )
            logger.info(
                "uploaded chunk %d/%d (%d bytes, HTTP %d)",
                index, plan.total_chunk_count, length, status,
            )


def fetch_status(access_token: str, publish_id: str) -> dict:
    parsed = _post_json(STATUS_URL, access_token, {"publish_id": publish_id})
    return parsed.get("data") or {}


def poll_until_done(
    access_token: str,
    publish_id: str,
    *,
    interval_sec: int = 10,
    max_attempts: int = 30,
) -> dict:
    """PUBLISH_COMPLETE まで待つ。FAILED なら例外で止める。"""
    last: dict = {}
    for attempt in range(1, max_attempts + 1):
        last = fetch_status(access_token, publish_id)
        status = str(last.get("status") or "")
        logger.info("publish status (%d/%d): %s", attempt, max_attempts, status or "(unknown)")
        if status == TERMINAL_OK:
            return last
        if status in TERMINAL_BAD:
            raise TikTokAPIError(
                f"TikTok 側で投稿に失敗: {status} "
                f"reason={last.get('fail_reason') or last.get('failure_reason') or '(不明)'}"
            )
        if attempt < max_attempts:
            time.sleep(interval_sec)
    raise TikTokAPIError(
        f"{max_attempts} 回ポーリングしても完了しなかった。最終状態: {last}"
    )
