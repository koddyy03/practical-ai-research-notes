"""ゲートで落ちた動画のうち、再エンコードで直せるものだけを直す。

Drive に上がってくる素材は 8K60 HEVC のような「TikTok が受け取れない
けれど中身は問題ない」ものが混ざる。これを毎回人が手で変換していると
週3の投稿が止まるので、直せる却下理由は自動で潰す。

ここで最も大事なのは **何を直さないか**:

  - 横動画は変換しない。クロップやパディングで縦枠に押し込めば
    「投稿できる」状態にはなるが、「横動画は投稿しない」という要件は
    画角の話であってファイル形式の話ではない。自動で縦にしてはいけない。
  - 尺が範囲外のものは変換しない。10分超を切るのは編集判断であって
    エンコード設定の問題ではない。

つまり直すのは「同じ絵・同じ尺のまま、容器と画素数だけ TikTok に
合わせれば通るもの」に限る。

使い方 (CLI):
    python3 -m tiktok_autopost.transcode /path/in.mp4 --workdir /path/out

    # 変換したらそのまま公開URLに載せる (投稿にそのURLを使う)
    python3 -m tiktok_autopost.transcode /path/in.mp4 --workdir /path/out --host
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import video_check
from .video_check import CheckResult, VideoCheckError, VideoFacts

logger = logging.getLogger("tiktok_autopost.transcode")

# 再エンコードで直せる却下理由。video_check.check() が作る日本語メッセージの
# 先頭と突き合わせる。ここに無い理由が 1 つでも混ざっていたら変換しない。
FIXABLE_PREFIXES = (
    "長辺が",       # 解像度上限超過 -> 縮小すれば通る
    "短辺が",       # 解像度下限未満 -> 拡大すれば通る
    "fps が",       # フレームレート範囲外 -> -r で合わせられる
    "ファイルサイズが",  # 容量超過 -> ビットレートを下げれば通る
)

# 絶対に変換で解決しない却下理由。該当したら人に投げる。
UNFIXABLE_PREFIXES = (
    "縦型ではない",
    "尺が",
    "解像度が読めない",
    "映像ストリームが無い",
)


class TranscodeError(Exception):
    """ffmpeg が無い、または変換しても基準を満たせない。"""


@dataclass
class TranscodePlan:
    """ffmpeg に渡す目標値。None の項目は元のまま触らない。"""

    width: int
    height: int
    fps: float | None
    crf: int
    video_bitrate_bps: int | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "crf": self.crf,
            "video_bitrate_bps": self.video_bitrate_bps,
            "reasons": self.reasons,
        }


def classify(rejections: list[str]) -> tuple[list[str], list[str]]:
    """却下理由を (変換で直せる, 直せない) に分ける。

    どちらの接頭辞にも当たらない未知の理由は「直せない」側に入れる。
    黙って変換して投稿してしまうより、止めて人に見せる方が安全。
    """
    fixable: list[str] = []
    unfixable: list[str] = []
    for reason in rejections:
        if reason.startswith(FIXABLE_PREFIXES):
            fixable.append(reason)
        else:
            unfixable.append(reason)
    return fixable, unfixable


def _even(value: int) -> int:
    """H.264 の yuv420p は偶数の縦横しか取れない。"""
    return max(2, value - (value % 2))


def plan_for(facts: VideoFacts, config: dict, reasons: list[str] | None = None) -> TranscodePlan:
    """却下された facts から、基準を満たす目標解像度/fps を決める。

    アスペクト比は変えない。横動画をここに渡してはいけない (呼び出し側で
    classify() が弾く) が、渡されても縦にはしない。
    """
    min_side = int(config.get("min_side_px", 360))
    max_side = int(config.get("max_side_px", 4096))
    # TikTok が実際に配信するのは 1080x1920 なので、上限いっぱいの 4096 まで
    # 残しても容量を食うだけ。変換するときは配信解像度まで落とす。
    target_long = int(config.get("transcode_target_long_side_px", 1920))

    width, height = facts.width, facts.height
    long_side = max(width, height)
    short_side = min(width, height)

    # 1) 長辺を目標値まで縮める (すでに小さければ触らない)
    scale = 1.0
    cap = min(max_side, target_long)
    if long_side > cap:
        scale = cap / long_side

    # 2) 縮めた結果 短辺が下限を割るなら、下限に合わせて引き上げ直す
    if short_side * scale < min_side:
        scale = min_side / short_side

    # 3) 引き上げた結果 長辺が上限を超えるなら、もう解像度では解決できない
    if long_side * scale > max_side:
        raise TranscodeError(
            f"アスペクト比が極端で {min_side}-{max_side}px の両立が不可能 "
            f"({width}x{height})"
        )

    new_w = _even(int(round(width * scale)))
    new_h = _even(int(round(height * scale)))

    fps_lo = float(config.get("min_fps", 23))
    fps_hi = float(config.get("max_fps", 60))
    target_fps: float | None = None
    if facts.fps <= 0 or not (fps_lo - 0.5 <= facts.fps <= fps_hi + 0.5):
        # 範囲外なら 30fps に寄せる。60 からは 1/2 間引き、下からは複製で埋まる。
        target_fps = float(config.get("transcode_target_fps", 30))
        if not (fps_lo <= target_fps <= fps_hi):
            target_fps = fps_lo

    return TranscodePlan(
        width=new_w,
        height=new_h,
        fps=target_fps,
        crf=int(config.get("transcode_crf", 20)),
        reasons=list(reasons or []),
    )


def _ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise TranscodeError(
            "ffmpeg が無いので再エンコードできない。素材を縦1080x1920/30fpsに"
            "書き出し直してもらう必要がある"
        )
    return found


def run_ffmpeg(src: Path, dst: Path, plan: TranscodePlan, *, timeout_sec: int = 3600) -> Path:
    """plan の通りに再エンコードする。音声は AAC 128k に統一。"""
    ffmpeg = _ffmpeg_path()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"scale={plan.width}:{plan.height}:flags=lanczos",
    ]
    if plan.fps:
        cmd += ["-r", str(plan.fps)]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p"]
    if plan.video_bitrate_bps:
        # 容量上限に収めたい場合は CRF ではなくビットレート指定に切り替える
        kbps = max(200, plan.video_bitrate_bps // 1000)
        cmd += [
            "-b:v", f"{kbps}k",
            "-maxrate", f"{int(kbps * 1.2)}k",
            "-bufsize", f"{kbps * 2}k",
        ]
    else:
        cmd += ["-crf", str(plan.crf)]
    cmd += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ]

    logger.info("ffmpeg: %dx%d fps=%s crf=%s bitrate=%s",
                plan.width, plan.height, plan.fps, plan.crf, plan.video_bitrate_bps)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError(f"ffmpeg がタイムアウトした ({timeout_sec}秒)") from exc
    if out.returncode != 0:
        raise TranscodeError(f"ffmpeg 失敗: {out.stderr.strip()[:400]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise TranscodeError("ffmpeg は成功したが出力が空")
    return dst


def _bitrate_to_fit(max_bytes: int, duration_sec: float) -> int:
    """容量上限に収まる映像ビットレート。音声 128k とコンテナ分を引く。"""
    if duration_sec <= 0:
        raise TranscodeError("尺が読めないので目標ビットレートを計算できない")
    total_bps = (max_bytes * 8) / duration_sec
    # コンテナのオーバーヘッドに 6% 残し、音声 128kbps を差し引く
    video_bps = int(total_bps * 0.94) - 128_000
    if video_bps < 200_000:
        raise TranscodeError(
            f"容量上限 {max_bytes} bytes に {duration_sec:.0f}秒 を収めるには"
            "ビットレートが低すぎる"
        )
    return video_bps


def ensure_postable(
    path: Path,
    config: dict | None = None,
    *,
    workdir: Path | None = None,
    max_attempts: int = 3,
) -> tuple[Path, CheckResult, TranscodePlan | None]:
    """ゲートを通るファイルを返す。必要なら再エンコードする。

    返り値は (投稿すべきファイル, 最終的なゲート結果, 使った変換計画 or None)。
    変換しても通らなければ VideoCheckError / TranscodeError を上げる。
    元ファイルは書き換えない。
    """
    config = config or video_check.load_config()
    result = video_check.check(path, config)
    if result.ok:
        return path, result, None

    fixable, unfixable = classify(result.rejections)
    if unfixable:
        # 横動画・尺超過はここで止まる。自動で投稿可能にはしない。
        raise VideoCheckError(
            "変換では直せない却下理由がある: " + "; ".join(unfixable)
        )
    if not fixable:
        raise VideoCheckError("却下理由を分類できなかった: " + "; ".join(result.rejections))

    workdir = workdir or path.parent
    workdir.mkdir(parents=True, exist_ok=True)
    plan = plan_for(result.facts, config, reasons=fixable)
    max_bytes = int(config.get("max_file_bytes", 4294967296))

    last: CheckResult | None = None
    for attempt in range(1, max_attempts + 1):
        dst = workdir / f"{path.stem}.tiktok{'' if attempt == 1 else f'.{attempt}'}.mp4"
        run_ffmpeg(path, dst, plan)
        last = video_check.check(dst, config)
        if last.ok:
            logger.info(
                "transcoded %s -> %s (%d -> %d bytes)",
                path.name, dst.name, result.facts.size_bytes, last.facts.size_bytes,
            )
            return dst, last, plan

        again, still_bad = classify(last.rejections)
        if still_bad:
            raise VideoCheckError(
                "変換後に直せない却下理由が出た: " + "; ".join(still_bad)
            )
        if attempt == max_attempts:
            break
        # 残っているのが容量だけなら、ビットレート指定に切り替えて詰め直す
        if all(r.startswith("ファイルサイズが") for r in again):
            plan = TranscodePlan(
                width=plan.width,
                height=plan.height,
                fps=plan.fps,
                crf=plan.crf,
                video_bitrate_bps=_bitrate_to_fit(max_bytes, last.facts.duration_sec),
                reasons=plan.reasons,
            )
        else:
            # 解像度/fps がまだ外れているのは計画の計算違い。黙って回さない。
            raise VideoCheckError(
                "変換計画が基準を満たせていない: " + "; ".join(again)
            )

    assert last is not None
    raise VideoCheckError(
        f"{max_attempts}回 変換しても基準を満たせなかった: " + "; ".join(last.rejections)
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="ゲート落ちした動画を投稿可能な形に再エンコードする")
    parser.add_argument("path", type=Path)
    parser.add_argument("--workdir", type=Path, default=None, help="出力先ディレクトリ")
    parser.add_argument("--config", type=Path, default=video_check.CONFIG_PATH)
    parser.add_argument(
        "--host", action="store_true",
        help="変換した場合、Late(Zernio) の保管に上げて公開URLも返す",
    )
    args = parser.parse_args(argv)

    config = video_check.load_config(args.config)
    try:
        out, result, plan = ensure_postable(args.path, config, workdir=args.workdir)
    except (VideoCheckError, TranscodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    payload = {
        "ok": True,
        "path": str(out),
        "transcoded": plan is not None,
        "plan": plan.as_dict() if plan else None,
        "facts": result.facts.as_dict(),
    }

    # 変換した動画は Drive 上に存在しないので、投稿に使える公開URLが必要。
    # 変換していないなら元の Drive リンクがそのまま使えるので何もしない。
    if args.host and plan is not None:
        from . import media_host

        try:
            hosted = media_host.host(out)
        except media_host.MediaHostError as exc:
            payload.update({"ok": False, "error": f"公開URLに載せられない: {exc}"})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        payload["public_url"] = hosted.public_url
        payload["hosted_key"] = hosted.key

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
