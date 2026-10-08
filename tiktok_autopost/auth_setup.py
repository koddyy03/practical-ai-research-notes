"""初回 OAuth。人間が手を動かすのはここ 1 回だけ (以後は自動更新)。

手順:
  1. 認可URLを出す
         python3 -m tiktok_autopost.auth_setup url
     -> 出た URL をスマホ/PC のブラウザで開いて「許可」を押す
  2. リダイレクト先 URL の ?code=... を渡してトークンに交換
         python3 -m tiktok_autopost.auth_setup exchange --code "<code>" \
             --token-state /path/to/token_state.json

リフレッシュトークンは発行から 365 日有効で、更新ごとにローテーションする。
つまり通常運用ではこのコマンドを年 1 回しか踏まない。

環境変数: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from .tiktok_api import (
    TikTokAPIError,
    TokenStore,
    build_authorize_url,
    exchange_code_for_token,
)

logger = logging.getLogger("tiktok_autopost.auth_setup")

DEFAULT_TOKEN_STATE = Path("token_state.json")


def _creds() -> tuple[str, str, str]:
    key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    redirect = os.environ.get("TIKTOK_REDIRECT_URI", "").strip()
    missing = [
        name
        for name, value in (
            ("TIKTOK_CLIENT_KEY", key),
            ("TIKTOK_CLIENT_SECRET", secret),
            ("TIKTOK_REDIRECT_URI", redirect),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"環境変数が未設定: {', '.join(missing)}")
    return key, secret, redirect


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("url", help="認可URLを表示する")

    ex = sub.add_parser("exchange", help="認可コードをトークンに交換して保存する")
    ex.add_argument("--code", required=True, help="リダイレクト先 URL の code パラメータ")
    ex.add_argument("--token-state", type=Path, default=DEFAULT_TOKEN_STATE)

    args = parser.parse_args(argv)
    key, secret, redirect = _creds()

    if args.cmd == "url":
        state = secrets.token_urlsafe(16)
        print(build_authorize_url(key, redirect, state))
        print(f"\n(csrf state: {state})", file=sys.stderr)
        return 0

    try:
        token_state = exchange_code_for_token(key, secret, args.code, redirect)
    except TikTokAPIError as exc:
        logger.error("トークン交換に失敗: %s", exc)
        return 1

    TokenStore(args.token_state).save(token_state)
    print(
        json.dumps(
            {
                "saved_to": str(args.token_state),
                "open_id": token_state.open_id,
                "scope": token_state.scope,
                "access_expires_at": token_state.access_expires_at,
                "has_refresh_token": bool(token_state.refresh_token),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
