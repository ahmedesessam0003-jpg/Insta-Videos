#!/usr/bin/env python3
"""
Turn the downloaded highlight videos that are really one picture with music
into pictures, for a store that shows them as pictures.

Each video in output/ becomes a JPG with the same name (lont-hl-04-001.mp4 ->
lont-hl-04-001.jpg): one frame from the middle of the video, at its full size.
The videos named with --keep stay videos. The original videos are moved to
output/original-videos/, never deleted, and manifest.json and report.csv are
written again, so they list the pictures.

Console messages are English only, like lont_highlights.py.

Usage:  python videos_to_photos.py --keep lont-hl-03-001 --dry-run   (only list)
        python videos_to_photos.py --keep lont-hl-03-001             (convert)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import lont_highlights as lh
from lont_highlights import UserError, say

ORIGINALS_DIR = "original-videos"
STILL_LIMIT = 10  # average difference (0-255) between two small frames still counted as the same picture


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg
    except ImportError:
        raise UserError("The video tool (imageio-ffmpeg) is not installed: start with "
                        "run_videos_to_photos.bat, which installs it.") from None
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_info(tool: str, video: Path) -> tuple[float, str]:
    """The length in seconds and the size ("720x1280") of a video, from ffmpeg's description."""
    result = subprocess.run([tool, "-hide_banner", "-i", str(video)], capture_output=True, text=True,
                            errors="replace")
    length = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    size = re.search(r"Video: .*?(\d{2,5})x(\d{2,5})", result.stderr)
    seconds = int(length[1]) * 3600 + int(length[2]) * 60 + float(length[3]) if length else 0.0
    return seconds, f"{size[1]}x{size[2]}" if size else "?"


def grab(tool: str, video: Path, seconds: float, *output: str) -> bytes:
    result = subprocess.run([tool, "-v", "error", "-y", "-ss", f"{seconds:.3f}", "-i", str(video),
                             "-frames:v", "1", *output], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(" ".join(result.stderr.decode(errors="replace").split())[-300:] or "ffmpeg failed")
    return result.stdout


def moves(tool: str, video: Path, length: float) -> bool:
    """True when the picture changes during the video (frames at 20% and 80%, made tiny and grey)."""
    tiny = ("-vf", "scale=32:32", "-f", "rawvideo", "-pix_fmt", "gray", "-")
    try:
        first, last = (grab(tool, video, length * share, *tiny) for share in (0.2, 0.8))
    except RuntimeError:
        return False  # can't tell
    if len(first) != 32 * 32 or len(last) != 32 * 32:
        return False
    return sum(abs(a - b) for a, b in zip(first, last)) / (32 * 32) > STILL_LIMIT


def to_photo(tool: str, video: Path, photo: Path) -> tuple[str, bool]:
    """Save the middle frame of `video` as the JPG `photo`; returns its size and whether the video moves."""
    length, size = video_info(tool, video)
    part = photo.with_name(photo.name + ".part")
    try:
        grab(tool, video, length / 2, "-q:v", "2", "-f", "image2", "-c:v", "mjpeg", str(part))
        if not part.is_file() or part.stat().st_size == 0:
            raise RuntimeError("ffmpeg made no picture")
        os.replace(part, photo)
    finally:
        part.unlink(missing_ok=True)
    return size, moves(tool, video, length)


def set_aside(video: Path, folder: Path) -> Path:
    folder.mkdir(exist_ok=True)
    dest, n = folder / video.name, 1
    while dest.exists():
        dest, n = folder / f"{video.stem} ({n}){video.suffix}", n + 1
    os.replace(video, dest)
    return dest


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Turn the downloaded highlight videos into pictures.")
    parser.add_argument("--keep", nargs="+", default=[], metavar="NAME",
                        help="videos that stay videos, e.g. lont-hl-03-001")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be done")
    parser.add_argument("--folder", type=Path, default=lh.LONT_DIR, help=argparse.SUPPRESS)  # for the tests
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    base = args.folder.resolve()
    out_dir, raw_path = base / lh.OUTPUT_DIR, base / lh.RAW_FILE
    say(f"Videos to pictures: {lh.shown(out_dir, base)}" + (" (dry run: nothing is changed)" if args.dry_run else ""))
    if not raw_path.is_file() or not out_dir.is_dir():
        raise UserError("Nothing is downloaded yet: run run_lont.bat first.")
    raw = lh.load_raw(raw_path)
    arrangement = lh.arrange(raw)

    videos = []  # (plan, file) for every story that is a video in output/
    for position, circle_id in enumerate(arrangement.order, 1):
        for plan in lh.planned_files(raw["circles"][circle_id], position):
            path = lh.saved_file(out_dir, raw["files"], plan)
            if plan.kind != "cover" and path is not None and path.suffix.lower() in lh.VIDEO_SUFFIXES:
                videos.append((plan, path))
    keep = {Path(name.strip()).stem.lower() for name in args.keep if name.strip()}
    unknown = sorted(keep - {plan.stem for plan, _ in videos})
    if unknown:
        raise UserError(f"Not a video in {lh.shown(out_dir, base)}: {', '.join(unknown)}. "
                        "Check the name (like lont-hl-03-001); nothing was changed.")
    todo = [(plan, path) for plan, path in videos if plan.stem not in keep]
    kept = [path.name for plan, path in videos if plan.stem in keep]
    say(f"- Videos: {len(videos)}. Staying videos: {', '.join(kept) or 'none'}.")
    if not todo:
        say("- No video to turn into a picture.")
        return 0
    if args.dry_run:
        for plan, path in todo:
            say(f"  {path.name} -> {plan.stem}.jpg")
        say(f"Dry run: nothing was changed. Run without --dry-run to make the {len(todo)} pictures.")
        return 0

    tool = ffmpeg()
    originals = out_dir / ORIGINALS_DIR
    manifest_path = out_dir / lh.MANIFEST_FILE
    try:
        downloaded_at = json.loads(manifest_path.read_text(encoding="utf-8")).get("downloaded_at_utc")
    except (OSError, ValueError, AttributeError):
        downloaded_at = None
    say(f"- Making {len(todo)} pictures: one frame from the middle of each video ...")
    moving, failed = [], []
    try:
        for plan, video in todo:
            photo = out_dir / (plan.stem + ".jpg")
            try:
                size, moved = to_photo(tool, video, photo)
                set_aside(video, originals)
            except Exception as exc:  # one broken video should not stop the others
                if video.is_file():
                    photo.unlink(missing_ok=True)  # never a picture and its video side by side
                failed.append(video.name)
                say(f"  x {video.name}: {lh.describe(exc)}")
                continue
            raw["files"][plan.stem] = {"file": photo.name, "media": plan.media}
            lh.save_raw(raw_path, raw)
            say(f"  + {photo.name} ({size}, {lh.size_text(photo.stat().st_size)})")
            if moved:
                moving.append(photo.name)
    finally:  # also after Ctrl+C: the lists show what is in output/ now
        lh.write_manifest(manifest_path, raw, arrangement, out_dir, downloaded_at)
        lh.write_report(out_dir / lh.REPORT_FILE, lh.report_rows(raw, arrangement, out_dir, None, False), base)

    say()
    say(f"Done: {len(todo) - len(failed)} pictures made in {lh.shown(out_dir, base)}.")
    say(f"- The original videos are kept in {lh.shown(originals, base)}.")
    say("- manifest.json and report.csv now list the pictures.")
    if moving:
        say(f"! These videos move, so check their pictures: {', '.join(moving)}")
    if failed:
        say(f"! Could not make a picture from: {', '.join(failed)} (they stay videos). Run this again to retry.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except ValueError:
                pass
    args = parse_args(argv)
    try:
        return run(args)
    except UserError as exc:
        say(f"\nSTOPPED: {exc}")
        return 2
    except KeyboardInterrupt:
        say("\nStopped. The pictures made so far are kept; run this again to finish.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
