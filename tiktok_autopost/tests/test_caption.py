"""python -m tiktok_autopost.tests.test_caption で実行 (pytest不要)。"""

from __future__ import annotations

import sys
import traceback

from tiktok_autopost.caption import (
    CaptionValidationError,
    VideoInput,
    build_post,
    check_post,
    load_config,
    validate_caption,
    validate_first_comment,
)

CONFIG = load_config()
FIXED = CONFIG["fixed_hashtags"]


def expect_error(fn, *args, contains: str = ""):
    try:
        fn(*args)
    except CaptionValidationError as e:
        assert contains.lower() in str(e).lower(), f"expected '{contains}' in error, got: {e}"
        return
    raise AssertionError(f"expected CaptionValidationError from {fn.__name__}{args}")


def test_spec_examples_pass():
    for caption in [
        "she said yes at the grove",
        "her hair after 30 seconds of hair oil",
        "asked a stranger if i could silk her hair",
    ]:
        validate_caption(caption, list(FIXED), CONFIG)


def test_build_post_happy_path():
    post = build_post(VideoInput(location="The Grove", subject_note="pink hair", outcome="she said yes"))
    assert post.caption == post.caption.lower()
    assert len(post.caption) <= 60
    assert post.hashtags == FIXED
    assert post.first_comment == "she said yes"


def test_first_comment_uses_extra_outcome():
    post = build_post(VideoInput(outcome="she said yes", extra_outcome="she almost said no"))
    assert post.first_comment == "she almost said no"


def test_banned_word_rejected():
    expect_error(validate_caption, "this formula works on frizzy hair", list(FIXED), CONFIG, contains="banned words")


def test_banned_word_case_insensitive_partial():
    expect_error(validate_caption, "my Heat Protectant routine", list(FIXED), CONFIG, contains="banned words")


def test_banned_hashtag_rejected():
    tags = FIXED[:3] + ["#fyp"]
    expect_error(validate_caption, "she said yes", tags, CONFIG, contains="banned hashtags")


def test_too_many_hashtags_rejected():
    tags = FIXED + ["#hair", "#losangeles"]
    expect_error(validate_caption, "she said yes", tags, CONFIG, contains="hashtags")


def test_hashtag_in_body_rejected():
    expect_error(validate_caption, "she said yes #hairoil", list(FIXED), CONFIG, contains="inside the caption")


def test_url_rejected():
    expect_error(validate_caption, "she said yes silktherich.com", list(FIXED), CONFIG, contains="url")


def test_consecutive_uppercase_rejected():
    expect_error(validate_caption, "she said YES at the grove", list(FIXED), CONFIG, contains="uppercase")


def test_over_60_chars_rejected():
    expect_error(validate_caption, "a" * 61, list(FIXED), CONFIG, contains="chars")


def test_wrong_tag_order_rejected():
    tags = list(reversed(FIXED))
    expect_error(validate_caption, "she said yes", tags, CONFIG, contains="in that order")


def test_first_comment_link_rejected():
    expect_error(validate_first_comment, "full video at silktherich.com", CONFIG, contains="url")


def test_first_comment_product_rejected():
    expect_error(validate_first_comment, "the hair oil is from silk the rich", CONFIG, contains="banned terms")


def test_first_comment_purchase_funnel_rejected():
    expect_error(validate_first_comment, "shop now while it lasts", CONFIG, contains="banned")


def test_first_comment_ok():
    validate_first_comment("she almost said no", CONFIG)


def test_check_post_validates_external_caption():
    check_post("she said yes at the grove", "she almost said no")
    try:
        check_post("link in bio for the reveal", "she almost said no")
    except CaptionValidationError:
        return
    raise AssertionError("check_post must validate external captions")


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
