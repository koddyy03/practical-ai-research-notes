"""投稿前の動画ゲート。ffprobe で実寸を見て、縦型以外は落とす。

横動画を投稿しないことが要件なので、回転メタデータ (rotate=90 等) を
考慮した「表示上の」幅・高さで判定する。コンテナ上は 1920x1080 でも
rotate=-90 が付いていれば実際には縦なので、そこを取り違えない。

使い方 (CLI):
    python3 -m tiktok_autopost.video_check /path/to/video.mp4
終了コード 0 = 投稿可、1 = 却下 (理由を stdout の JSON に入れる)。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config" / "post_rules.json"


class VideoCheckError(Exception):
    """ffprobe が読めない等、判定自体ができない場合。"""


@dataclass
class VideoFacts:
    width: int
    height: int
    duration_sec: float
    fps: float
    size_bytes: int
    codec: str
    rotation: int

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

    def as_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "orientation": "portrait" if self.is_portrait else ("square" if self.width == self.height else "landscape"),
            "duration_sec": round(self.duration_sec, 2),
            "fps": round(self.fps, 2),
            "size_bytes": self.size_bytes,
            "codec": self.codec,
            "rotation": self.rotation,
        }


@dataclass
class CheckResult:
    ok: bool
    facts: VideoFacts
    rejections: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "rejections": self.rejections, "facts": self.facts.as_dict()}


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        # ffmpeg 一式が入っていない実行環境がある。そこでゲートごと落ちると
        # 「横動画を投稿しない」という必須要件が効かなくなるので、
        # 標準ライブラリだけの MP4 パーサで同じ事実を取りにいく。
        return _fallback_probe(path)
    except subprocess.TimeoutExpired as exc:
        raise VideoCheckError("ffprobe がタイムアウトした") from exc
    if out.returncode != 0:
        raise VideoCheckError(f"ffprobe 失敗: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout)


def _fallback_probe(path: Path) -> dict:
    from . import mp4_probe

    try:
        return mp4_probe.probe_mp4(path)
    except Exception as exc:  # noqa: BLE001 - 判定不能として一本化する
        raise VideoCheckError(
            f"ffprobe が無く、MP4 として読むこともできない: {exc}"
        ) from exc


def _parse_rotation(stream: dict) -> int:
    """side_data_list か tags.rotate から回転角を取る。見つからなければ 0。"""
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                continue
    raw = (stream.get("tags") or {}).get("rotate")
    if raw is not None:
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            pass
    return 0


def _parse_fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "0"):
            continue
        try:
            value = float(Fraction(raw))
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return 0.0


def probe(path: Path) -> VideoFacts:
    data = _ffprobe(path)
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise VideoCheckError("映像ストリームが無い")
    stream = streams[0]

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise VideoCheckError("解像度が読めない")

    rotation = _parse_rotation(stream)
    # 90/270 度回転は表示上 縦横が入れ替わる
    if abs(rotation) % 180 == 90:
        width, height = height, width

    fmt = data.get("format") or {}
    duration = 0.0
    for raw in (stream.get("duration"), fmt.get("duration")):
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break

    try:
        size_bytes = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if size_bytes <= 0:
        size_bytes = path.stat().st_size

    return VideoFacts(
        width=width,
        height=height,
        duration_sec=duration,
        fps=_parse_fps(stream),
        size_bytes=size_bytes,
        codec=str(stream.get("codec_name") or "unknown"),
        rotation=rotation,
    )


def check(path: Path, config: dict | None = None) -> CheckResult:
    config = config or load_config()
    facts = probe(path)
    bad: list[str] = []

    if config.get("require_portrait", True) and not facts.is_portrait:
        bad.append(
            f"縦型ではない ({facts.width}x{facts.height}, "
            f"{facts.as_dict()['orientation']})"
        )

    min_side = int(config.get("min_side_px", 360))
    if min(facts.width, facts.height) < min_side:
        bad.append(f"短辺が {min_side}px 未満 ({facts.width}x{facts.height})")

    lo = float(config.get("min_duration_sec", 3))
    hi = float(config.get("max_duration_sec", 600))
    if facts.duration_sec <= 0:
        bad.append("尺が読めない")
    elif not (lo <= facts.duration_sec <= hi):
        bad.append(f"尺が {lo}-{hi}秒 の範囲外 ({facts.duration_sec:.1f}秒)")

    fps_lo = float(config.get("min_fps", 23))
    fps_hi = float(config.get("max_fps", 60))
    if facts.fps <= 0:
        bad.append("fps が読めない")
    elif not (fps_lo - 0.5 <= facts.fps <= fps_hi + 0.5):
        bad.append(f"fps が {fps_lo}-{fps_hi} の範囲外 ({facts.fps:.2f})")

    max_bytes = int(config.get("max_file_bytes", 1073741824))
    if facts.size_bytes > max_bytes:
        bad.append(
            f"ファイルサイズが上限超過 ({facts.size_bytes} > {max_bytes} bytes)"
        )

    return CheckResult(ok=not bad, facts=facts, rejections=bad)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="投稿前の動画ゲート (縦型/尺/fps/サイズ)")
    parser.add_argument("path", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)

    try:
        result = check(args.path, load_config(args.config))
    except VideoCheckError as exc:
        print(json.dumps({"ok": False, "rejections": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
