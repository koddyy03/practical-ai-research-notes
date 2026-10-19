"""端から端まで: 動画ゲート -> キャプション検証 -> TikTok 投稿 -> 完了待ち。

ゲートを 1 つでも落ちたら API を一切叩かずに止める。
キャプション検証 (caption.py) も投稿前に必ず通す (バイパス不可)。

使い方:
    python3 -m tiktok_autopost.post_video \
        --video /tmp/clip.mp4 \
        --caption "she said yes at the grove" \
        --first-comment "she almost said no" \
        --token-state /tmp/token_state.json

    # 投稿せず検証だけ (API を叩かない)
    python3 -m tiktok_autopost.post_video --video ... --caption ... --dry-run

終了コード: 0 = 投稿完了 / 2 = ゲート落ち (投稿していない) / 1 = API エラー

環境変数: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import tiktok_api, video_check
from .caption import CaptionValidationError, check_post
from .caption import load_config as load_caption_config

logger = logging.getLogger("tiktok_autopost.post_video")

MIME_BY_SUFFIX = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


class GateFailure(Exception):
    """投稿前チェックで落ちた。API は叩いていない。"""


def _mime_for(path: Path) -> str:
    return MIME_BY_SUFFIX.get(path.suffix.lower(), "video/mp4")


def run_gates(video: Path, caption: str, first_comment: str, rules: dict) -> dict:
    """動画とテキストの両方を検証する。落ちたら GateFailure。"""
    if not video.exists():
        raise GateFailure(f"動画ファイルが無い: {video}")
    if video.suffix.lower() not in MIME_BY_SUFFIX:
        raise GateFailure(f"対応していない拡張子: {video.suffix} (mp4/mov/webm のみ)")

    try:
        result = video_check.check(video, rules)
    except video_check.VideoCheckError as exc:
        raise GateFailure(f"動画を解析できない: {exc}") from exc
    if not result.ok:
        raise GateFailure("動画ゲート却下: " + "; ".join(result.rejections))

    try:
        post = check_post(caption, first_comment, load_caption_config())
    except CaptionValidationError as exc:
        raise GateFailure(f"キャプション検証違反: {exc}") from exc

    return {"video": result.facts.as_dict(), "post": post.as_dict()}


def publish(
    video: Path,
    full_caption: str,
    *,
    token_state_path: Path,
    rules: dict,
) -> dict:
    store = tiktok_api.TokenStore(token_state_path)
    state = store.load()
    client_key, client_secret = tiktok_api.load_credentials(store.raw())

    # アクセストークンは 24h で切れる。毎回更新してリフレッシュトークンを保存し直す。
    if not state.access_token_valid:
        logger.info("refreshing access token")
        state = tiktok_api.refresh_access_token(client_key, client_secret, state.refresh_token)
        store.save(state)

    # TikTok の規約上、投稿前に creator_info を取って最新値を使う必要がある
    creator = tiktok_api.query_creator_info(state.access_token)
    logger.info("creator info: %s", json.dumps(creator, ensure_ascii=False))

    wanted_privacy = str(rules.get("privacy_level", "PUBLIC_TO_EVERYONE"))
    allowed = creator.get("privacy_level_options") or []
    if allowed and wanted_privacy not in allowed:
        raise tiktok_api.TikTokAPIError(
            f"公開範囲 {wanted_privacy} は今のアカウント/アプリでは選べない。"
            f"選択可能: {allowed}。審査未通過のアプリは非公開に強制される。"
        )

    max_dur = creator.get("max_video_post_duration_sec")
    if max_dur:
        facts = video_check.probe(video)
        if facts.duration_sec > float(max_dur):
            raise tiktok_api.TikTokAPIError(
                f"尺 {facts.duration_sec:.1f}秒 がアカウント上限 {max_dur}秒 を超過"
            )

    size = video.stat().st_size
    plan = tiktok_api.plan_chunks(size)
    logger.info(
        "chunk plan: size=%d chunk_size=%d count=%d",
        plan.video_size, plan.chunk_size, plan.total_chunk_count,
    )

    data = tiktok_api.init_direct_post(
        state.access_token,
        caption=full_caption,
        plan=plan,
        privacy_level=wanted_privacy,
        disable_comment=bool(rules.get("disable_comment", False)),
        disable_duet=bool(rules.get("disable_duet", False)),
        disable_stitch=bool(rules.get("disable_stitch", False)),
        video_cover_timestamp_ms=int(rules.get("video_cover_timestamp_ms", 1000)),
        is_aigc=bool(rules.get("is_aigc", False)),
    )
    publish_id = data["publish_id"]
    logger.info("publish_id=%s", publish_id)

    tiktok_api.upload_file(data["upload_url"], video, plan, mime=_mime_for(video))

    final = tiktok_api.poll_until_done(
        state.access_token,
        publish_id,
        interval_sec=int(rules.get("status_poll_interval_sec", 10)),
        max_attempts=int(rules.get("status_poll_max_attempts", 30)),
    )
    return {"publish_id": publish_id, "status": final, "creator_info": creator}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--caption", required=True, help="ハッシュタグを含まない本文")
    parser.add_argument("--first-comment", default="", help="検証するだけ。API では投稿できない")
    parser.add_argument("--token-state", type=Path, default=Path("token_state.json"))
    parser.add_argument("--config", type=Path, default=video_check.CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true", help="ゲートだけ通して API は叩かない")
    args = parser.parse_args(argv)

    rules = video_check.load_config(args.config)

    try:
        gated = run_gates(args.video, args.caption, args.first_comment, rules)
    except GateFailure as exc:
        logger.error("DO NOT POST — %s", exc)
        print(json.dumps({"ok": False, "stage": "gate", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    full_caption = gated["post"]["full_caption"]

    if args.dry_run:
        print(json.dumps({"ok": True, "stage": "gate", "dry_run": True, **gated}, ensure_ascii=False, indent=2))
        return 0

    try:
        result = publish(args.video, full_caption, token_state_path=args.token_state, rules=rules)
    except tiktok_api.TikTokAPIError as exc:
        logger.error("投稿に失敗: %s", exc)
        print(json.dumps(
            {"ok": False, "stage": "publish", "error": str(exc), "code": exc.code},
            ensure_ascii=False, indent=2,
        ))
        return 1

    print(json.dumps({"ok": True, "stage": "publish", **gated, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
