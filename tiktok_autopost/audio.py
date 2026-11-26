"""無音素材に音を入れる / 長尺素材から音ごと切り出す。

背景(2026-11-26): Sammy が Drive に上げるのは素材で、そこから投稿分を選んで
音を入れるのは Claude の担当になった。11月の8本を実測したところ、10秒未満に
切り詰められた素材は音声トラックが実質無音(ピーク -60dBFS 前後)で、30秒超の
長回しだけ音が生きていた。したがって優先順位は:

  1. 長回し素材から切り出す  -> 本物の街の音がそのまま乗る (clip)
  2. それが無い無音素材だけ   -> 音を足す                  (mux)

音を足す場合、ベッドは動画の尺に合わせてループ/カットし、投稿に耐える音量まで
正規化する。TikTok 側でも正規化されるので狙いは平均 -16dBFS 前後。

使い方 (CLI):
    python3 -m tiktok_autopost.audio clip  src.mp4 out.mp4 --start 12.5 --duration 7
    python3 -m tiktok_autopost.audio mux   src.mp4 out.mp4 --bed music.m4a
    python3 -m tiktok_autopost.audio probe src.mp4
終了コード 0 = 成功、1 = 失敗(理由を stdout の JSON に入れる)。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import video_check

# 足した音の狙い。TikTok 側でも正規化されるので上げすぎない。
TARGET_MEAN_DBFS = -16.0


class AudioError(Exception):
    """音の加工ができない。"""


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise AudioError("ffmpeg が無いので音の加工ができない")
    return found


def _run(cmd: list[str], *, timeout: int = 1800) -> None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg がタイムアウトした ({timeout}秒)") from exc
    if out.returncode != 0:
        raise AudioError(f"ffmpeg 失敗: {out.stderr.strip()[-400:]}")


def probe(path: Path) -> dict:
    """音の状態を返す。投稿前に必ずこれで確認する。"""
    facts = video_check.probe(path)
    cfg = video_check.load_config()
    d = facts.as_dict()
    peak, mean = facts.audio_max_dbfs, facts.audio_mean_dbfs
    silent = (not facts.has_audio_track) or (
        (peak is not None and peak < float(cfg.get("silent_audio_max_dbfs", -30.0)))
        or (mean is not None and mean < float(cfg.get("silent_audio_mean_dbfs", -45.0)))
    )
    return {
        "duration_sec": d["duration_sec"],
        "audio_codec": d["audio_codec"],
        "audio_max_dbfs": peak,
        "audio_mean_dbfs": mean,
        "is_silent": silent,
    }


def clip(src: Path, dst: Path, *, start: float, duration: float) -> Path:
    """長尺素材から start 秒から duration 秒を切り出す。音はそのまま持っていく。

    入力に生きた音があれば、切り出した側にも生きた音が残る。これが本命の経路。
    キーフレーム合わせのズレを避けるため、`-ss` はデコード後に適用する。
    """
    if duration <= 0:
        raise AudioError("duration は正の秒数で指定する")
    if start < 0:
        raise AudioError("start は 0 以上で指定する")
    src_facts = probe(src)
    if start + duration > src_facts["duration_sec"] + 0.05:
        raise AudioError(
            f"切り出し範囲が素材の尺を超えている "
            f"(start={start} + duration={duration} > {src_facts['duration_sec']}秒)"
        )
    _run([
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-ss", str(start), "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ])
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("切り出しは成功したが出力が空")
    return dst


def mux(
    src: Path,
    dst: Path,
    *,
    bed: Path,
    bed_gain_db: float = 0.0,
    keep_original: bool = False,
    original_volume: float = 0.35,
) -> Path:
    """`bed` の音を動画に入れる。映像は再エンコードしない(コピー)。

    - ベッドは動画の尺までループし、末尾でフェードアウトする。
    - `loudnorm` で狙いの音量に正規化するので、素材の音量差を気にしなくてよい。
    - `keep_original=True` なら元の音を `original_volume` 倍で下に残す。
      ただし元が無音(= 本来の用途)なら残しても意味が無いので既定は False。
    """
    src_facts = probe(src)
    dur = src_facts["duration_sec"]
    if dur <= 0:
        raise AudioError("尺が読めないのでベッドの長さを決められない")
    fade = min(0.6, dur / 4)

    bed_chain = (
        f"volume={bed_gain_db}dB,"
        f"loudnorm=I={TARGET_MEAN_DBFS}:TP=-1.5:LRA=11,"
        f"atrim=0:{dur},"
        f"afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}"
    )

    cmd = [
        _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-stream_loop", "-1", "-i", str(bed),
    ]
    if keep_original and src_facts["audio_codec"]:
        filt = (
            f"[1:a]{bed_chain}[bed];"
            f"[0:a]volume={original_volume}[orig];"
            f"[orig][bed]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    else:
        filt = f"[1:a]{bed_chain}[aout]"
    cmd += [
        "-filter_complex", filt,
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(dst),
    ]
    _run(cmd)
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("音入れは成功したが出力が空")

    out = probe(dst)
    if out["is_silent"]:
        raise AudioError(
            f"音を入れたのに無音判定のまま "
            f"(ピーク {out['audio_max_dbfs']} / 平均 {out['audio_mean_dbfs']})。ベッドを確認する"
        )
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="無音素材への音入れ / 長尺からの切り出し")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="音の状態を見る")
    p.add_argument("src", type=Path)

    c = sub.add_parser("clip", help="長尺から音ごと切り出す")
    c.add_argument("src", type=Path)
    c.add_argument("dst", type=Path)
    c.add_argument("--start", type=float, required=True)
    c.add_argument("--duration", type=float, required=True)

    m = sub.add_parser("mux", help="音を入れる")
    m.add_argument("src", type=Path)
    m.add_argument("dst", type=Path)
    m.add_argument("--bed", type=Path, required=True)
    m.add_argument("--bed-gain-db", type=float, default=0.0)
    m.add_argument("--keep-original", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "probe":
            payload = {"ok": True, **probe(args.src)}
        elif args.cmd == "clip":
            out = clip(args.src, args.dst, start=args.start, duration=args.duration)
            payload = {"ok": True, "path": str(out), **probe(out)}
        else:
            out = mux(
                args.src, args.dst, bed=args.bed,
                bed_gain_db=args.bed_gain_db, keep_original=args.keep_original,
            )
            payload = {"ok": True, "path": str(out), **probe(out)}
    except (AudioError, video_check.VideoCheckError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
