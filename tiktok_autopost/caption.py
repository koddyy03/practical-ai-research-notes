"""Caption generation + validation for the "Can I Silk Your Hair?" TikTok series.

広告に見えた時点で数字が死ぬので、キャプションで商品を売る言葉を
1つでも出さないことが最優先。生成 (generate_*) と検証 (validate_*) は
別関数で、build_post() は必ず検証を通してから結果を返す (バイパス不可)。

使い方 (CLI):
    python -m tiktok_autopost.caption \
        --location "The Grove" \
        --subject-note "pink hair" \
        --outcome "she said yes"

検証だけ走らせる (外部で生成したキャプションのチェック):
    python -m tiktok_autopost.caption --validate-only \
        --caption "she said yes at the grove" \
        --first-comment "she almost said no"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tiktok_autopost.caption")

CONFIG_PATH = Path(__file__).parent / "config" / "caption_rules.json"

_URL_RE = re.compile(r"(https?://|www\.|\S+\.(com|net|org|io|co|shop|store)\b)", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")


class CaptionValidationError(Exception):
    """検証違反。黙って修正せず、投稿を止めるために投げる。"""


@dataclass
class PostText:
    caption: str
    hashtags: list[str]
    first_comment: str

    @property
    def full_caption(self) -> str:
        return f"{self.caption} {' '.join(self.hashtags)}"

    def as_dict(self) -> dict:
        return {
            "caption": self.caption,
            "hashtags": self.hashtags,
            "full_caption": self.full_caption,
            "first_comment": self.first_comment,
        }


@dataclass
class VideoInput:
    location: str = ""
    subject_note: str = ""
    outcome: str = ""
    extra_outcome: str = field(default="", metadata={"doc": "動画の外側の情報。ファーストコメント用"})


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- generation

def generate_caption(video: VideoInput, config: dict) -> str:
    """動画の中で起きたことだけを1行・全小文字・ピリオドなしで書く。

    search_terms のどれかを自然に含められる形を優先するが、
    不自然になるくらいなら outcome そのままを使う (rule 4 は努力目標、
    検証セクションの必須チェックではない)。
    """
    location = video.location.strip().lower()
    outcome = video.outcome.strip().lower().rstrip(".")
    subject = video.subject_note.strip().lower()

    candidates: list[str] = []
    if outcome and location:
        candidates.append(f"{outcome} at {location}")
    if subject:
        candidates.append(f"asked the girl with {subject} if i could silk her hair")
    if outcome:
        candidates.append(f"{outcome} and her hair after hair oil")
        candidates.append(outcome)
    candidates.append("asked a stranger if i could silk her hair")

    max_chars = config["max_caption_chars"]
    search_terms = config["search_terms"]

    fitting = [c for c in candidates if len(c) <= max_chars]
    if not fitting:
        raise CaptionValidationError(
            f"no caption candidate fits within {max_chars} chars: {candidates}"
        )
    # search_term を含む候補を優先、なければ先頭 (= 最も事実に近い形)
    for c in fitting:
        if any(term in c for term in search_terms):
            return c
    if not any(any(term in c for term in search_terms) for c in fitting):
        logger.warning("no search term fit naturally; using plain outcome caption")
    return fitting[0]


def generate_first_comment(video: VideoInput) -> str:
    """動画の外側の情報を1行だけ。"""
    text = (video.extra_outcome or video.outcome).strip().lower().rstrip(".")
    return text


# ---------------------------------------------------------------- validation

def _find_banned_words(text: str, banned: list[str]) -> list[str]:
    lowered = text.lower()
    return [w for w in banned if w.lower() in lowered]


def validate_caption(caption: str, hashtags: list[str], config: dict) -> None:
    """違反したら CaptionValidationError。黙って修正しない。"""
    problems: list[str] = []

    if "\n" in caption:
        problems.append("caption must be a single line")
    if caption != caption.lower():
        problems.append("caption must be all lowercase")
    if caption.rstrip().endswith("."):
        problems.append("caption must not end with a period")
    if len(caption) > config["max_caption_chars"]:
        problems.append(
            f"caption is {len(caption)} chars (max {config['max_caption_chars']}, hashtags excluded)"
        )

    hit = _find_banned_words(caption, config["banned_words"])
    if hit:
        problems.append(f"banned words in caption: {hit}")

    if _HASHTAG_RE.search(caption):
        problems.append("hashtags must not appear inside the caption body")
    if _URL_RE.search(caption):
        problems.append("caption must not contain a URL")

    run = config["max_consecutive_uppercase"]
    if re.search(rf"[A-Z]{{{run + 1},}}", caption):
        problems.append(f"more than {run} consecutive uppercase letters in caption")

    if len(hashtags) > config["max_total_hashtags"]:
        problems.append(
            f"{len(hashtags)} hashtags (max {config['max_total_hashtags']})"
        )
    banned_tags = {t.lower() for t in config["banned_hashtags"]}
    bad_tags = [t for t in hashtags if t.lower() in banned_tags]
    if bad_tags:
        problems.append(f"banned hashtags: {bad_tags}")
    if hashtags != config["fixed_hashtags"]:
        problems.append(
            f"hashtags must be exactly {config['fixed_hashtags']} in that order, got {hashtags}"
        )

    if problems:
        raise CaptionValidationError("; ".join(problems))


def validate_first_comment(comment: str, config: dict) -> None:
    problems: list[str] = []
    if "\n" in comment:
        problems.append("first comment must be a single line")
    if not comment.strip():
        problems.append("first comment is empty")
    if _URL_RE.search(comment):
        problems.append("first comment must not contain a URL")
    hit = _find_banned_words(comment, config["first_comment_banned_terms"])
    if hit:
        problems.append(f"banned terms in first comment (no product/purchase/link): {hit}")
    hit = _find_banned_words(comment, config["banned_words"])
    if hit:
        problems.append(f"banned words in first comment: {hit}")
    if problems:
        raise CaptionValidationError("; ".join(problems))


# ---------------------------------------------------------------- entrypoint

def build_post(video: VideoInput, config: dict | None = None) -> PostText:
    """生成 → 検証 → ログ出力。検証はバイパス不可。"""
    config = config or load_config()
    caption = generate_caption(video, config)
    first_comment = generate_first_comment(video)
    post = PostText(caption=caption, hashtags=list(config["fixed_hashtags"]), first_comment=first_comment)
    validate_caption(post.caption, post.hashtags, config)
    validate_first_comment(post.first_comment, config)
    logger.info("post text ready for review:\n%s", json.dumps(post.as_dict(), indent=2, ensure_ascii=False))
    return post


def check_post(caption: str, first_comment: str, config: dict | None = None) -> PostText:
    """外部 (LLMなど) で生成済みのキャプションを検証だけする。"""
    config = config or load_config()
    post = PostText(caption=caption, hashtags=list(config["fixed_hashtags"]), first_comment=first_comment)
    validate_caption(post.caption, post.hashtags, config)
    validate_first_comment(post.first_comment, config)
    logger.info("post text ready for review:\n%s", json.dumps(post.as_dict(), indent=2, ensure_ascii=False))
    return post


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--location", default="")
    p.add_argument("--subject-note", default="")
    p.add_argument("--outcome", default="")
    p.add_argument("--extra-outcome", default="", help="ファーストコメント用。省略時は outcome を使う")
    p.add_argument("--validate-only", action="store_true", help="--caption / --first-comment を検証だけする")
    p.add_argument("--caption", default="")
    p.add_argument("--first-comment", default="")
    args = p.parse_args(argv)

    try:
        if args.validate_only:
            post = check_post(args.caption, args.first_comment)
        else:
            video = VideoInput(
                location=args.location,
                subject_note=args.subject_note,
                outcome=args.outcome,
                extra_outcome=args.extra_outcome,
            )
            post = build_post(video)
    except CaptionValidationError as e:
        logger.error("VALIDATION FAILED — do not post: %s", e)
        return 1

    print(json.dumps(post.as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
