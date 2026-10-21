"""Tests for videos_to_photos.py, with real sample videos made by the same ffmpeg.

Run from the project folder:  python -m unittest discover -s lont-highlights/tests
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LONT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LONT_DIR))

import lont_highlights as lh  # noqa: E402
import videos_to_photos as vp  # noqa: E402

try:
    import imageio_ffmpeg
except ImportError:  # the tests are skipped
    imageio_ffmpeg = None

try:
    from PIL import Image
except ImportError:
    Image = None

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + bytes(40)
DOWNLOADED_AT = "2026-10-21T22:00:00Z"


def item(media_id: str, kind: str) -> dict:
    ext = "mp4" if kind == "video" else "jpg"
    return {"media_id": media_id, "type": kind, "url": f"https://cdn.fake/{media_id}.{ext}", "width": 720,
            "height": 1280}


RAW = {
    "version": 1, "profile": "lont.sa", "complete_list": True, "covers_from_tray": True, "order": ["1001", "1002"],
    "circles": {
        "1001": {"title": "عن لونت", "media_count": 2, "fetched_at_utc": DOWNLOADED_AT,
                 "cover": {"url": "https://cdn.fake/c1.jpg", "width": 150, "height": 150, "media": "c1"},
                 "items": [item("a1", "video"), item("a2", "image")]},
        "1002": {"title": "غرف النوم", "media_count": 3, "fetched_at_utc": DOWNLOADED_AT, "cover": None,
                 "items": [item("b1", "video"), item("b2", "video"), item("b3", "video")]},
    },
    "files": {
        "lont-hl-01-cover": {"file": "lont-hl-01-cover.jpg", "media": "c1"},
        "lont-hl-01-001": {"file": "lont-hl-01-001.mp4", "media": "a1"},
        "lont-hl-01-002": {"file": "lont-hl-01-002.jpg", "media": "a2"},
        "lont-hl-02-001": {"file": "lont-hl-02-001.mp4", "media": "b1"},
        "lont-hl-02-002": {"file": "lont-hl-02-002.mp4", "media": "b2"},
        "lont-hl-02-003": {"file": "lont-hl-02-003.mp4", "media": "b3"},
    },
}


@unittest.skipIf(imageio_ffmpeg is None, "imageio-ffmpeg is not installed")
class VideosToPhotosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = Path(tempfile.mkdtemp())
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

        def make(name: str, *args: str) -> None:
            subprocess.run([ffmpeg, "-v", "error", "-y", *args, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            str(cls.samples / name)], check=True)

        # a picture with music, like most of the highlights; and one that changes (a fade from black)
        make("still.mp4", "-f", "lavfi", "-i", "color=c=0x3366cc:s=720x1280:d=4:r=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "aac", "-shortest")
        make("moving.mp4", "-f", "lavfi", "-i", "color=c=white:s=720x1280:d=4:r=30,fade=t=in:st=0:d=4")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "lont-highlights"
        self.out = self.base / "output"
        self.out.mkdir(parents=True)
        (self.base / "raw_list.json").write_text(json.dumps(RAW, ensure_ascii=False), encoding="utf-8")
        for name in ("lont-hl-01-cover.jpg", "lont-hl-01-002.jpg"):
            (self.out / name).write_bytes(JPEG)
        for name, sample in (("lont-hl-01-001.mp4", "still"), ("lont-hl-02-001.mp4", "still"),
                             ("lont-hl-02-002.mp4", "moving"), ("lont-hl-02-003.mp4", "still")):
            shutil.copy(self.samples / f"{sample}.mp4", self.out / name)
        (self.out / "manifest.json").write_text(json.dumps({"downloaded_at_utc": DOWNLOADED_AT}), encoding="utf-8")

    def run_main(self, module, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            code = module.main([*args, "--folder", str(self.base)])
        return code, stdout.getvalue()

    def files(self, folder: Path) -> list[str]:
        return sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []

    def manifest(self) -> dict:
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))

    def report(self) -> list[list[str]]:
        with open(self.out / "report.csv", encoding="utf-8-sig", newline="") as fh:
            return list(csv.reader(fh))

    def test_dry_run_only_lists(self):
        before = self.files(self.out)
        code, out = self.run_main(vp, "--keep", "lont-hl-02-003", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("  lont-hl-01-001.mp4 -> lont-hl-01-001.jpg", out)
        self.assertIn("Staying videos: lont-hl-02-003.mp4.", out)
        self.assertNotIn("lont-hl-02-003.mp4 ->", out)
        self.assertEqual(self.files(self.out), before)

    def test_videos_become_pictures_except_the_kept_one(self):
        code, out = self.run_main(vp, "--keep", "lont-hl-02-003")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.isascii())
        self.assertEqual(self.files(self.out), [
            "lont-hl-01-001.jpg", "lont-hl-01-002.jpg", "lont-hl-01-cover.jpg", "lont-hl-02-001.jpg",
            "lont-hl-02-002.jpg", "lont-hl-02-003.mp4", "manifest.json", "report.csv"])
        self.assertEqual(self.files(self.out / "original-videos"),
                         ["lont-hl-01-001.mp4", "lont-hl-02-001.mp4", "lont-hl-02-002.mp4"])
        photo = self.out / "lont-hl-01-001.jpg"
        self.assertTrue(photo.read_bytes().startswith(b"\xff\xd8\xff"))
        if Image is not None:
            with Image.open(photo) as image:
                self.assertEqual((image.format, image.size), ("JPEG", (720, 1280)))
                red, green, blue = image.convert("RGB").getpixel((360, 640))
                self.assertLess(abs(blue - 0xcc) + abs(red - 0x33), 30)  # the picture's colour, not washed out
        self.assertIn("+ lont-hl-01-001.jpg (720x1280,", out)
        self.assertIn("! These videos move, so check their pictures: lont-hl-02-002.jpg", out)

        manifest = self.manifest()
        self.assertEqual(manifest["downloaded_at_utc"], DOWNLOADED_AT)
        self.assertEqual([(i["file"], i["type"]) for h in manifest["highlights"] for i in h["items"]], [
            ("lont-hl-01-001.jpg", "image"), ("lont-hl-01-002.jpg", "image"), ("lont-hl-02-001.jpg", "image"),
            ("lont-hl-02-002.jpg", "image"), ("lont-hl-02-003.mp4", "video")])
        rows = self.report()
        self.assertEqual(rows[1][:4], ["1", "عن لونت", "2", "0"])
        self.assertEqual(rows[2][:4], ["2", "غرف النوم", "2", "1"])
        self.assertEqual(json.loads((self.base / "raw_list.json").read_text(encoding="utf-8"))["files"]
                         ["lont-hl-02-001"], {"file": "lont-hl-02-001.jpg", "media": "b1"})

        code, out = self.run_main(vp, "--keep", "lont-hl-02-003")  # again: nothing left to do
        self.assertEqual(code, 0, out)
        self.assertIn("No video to turn into a picture.", out)

        code, out = self.run_main(lh)  # the downloader again: the pictures stay pictures
        self.assertEqual(code, 0, out)
        self.assertIn("Done: 6 of 6 files", out)
        self.assertEqual(self.files(self.out / "original-videos"),
                         ["lont-hl-01-001.mp4", "lont-hl-02-001.mp4", "lont-hl-02-002.mp4"])
        self.assertEqual([i["type"] for h in self.manifest()["highlights"] for i in h["items"]],
                         ["image", "image", "image", "image", "video"])
        self.assertEqual(self.report()[2][:4], ["2", "غرف النوم", "2", "1"])

    def test_a_wrong_name_after_keep_changes_nothing(self):
        before = self.files(self.out)
        code, out = self.run_main(vp, "--keep", "lont-hl-2-003")
        self.assertEqual(code, 2, out)
        self.assertIn("Not a video in", out)
        self.assertIn("lont-hl-2-003", out)
        self.assertEqual(self.files(self.out), before)

    def test_a_broken_video_stays_a_video_and_the_rest_go_on(self):
        (self.out / "lont-hl-02-001.mp4").write_bytes(b"not a video at all")
        code, out = self.run_main(vp, "--keep", "lont-hl-02-003")
        self.assertEqual(code, 1, out)
        self.assertIn("x lont-hl-02-001.mp4:", out)
        self.assertIn("Could not make a picture from: lont-hl-02-001.mp4", out)
        files = self.files(self.out)
        self.assertIn("lont-hl-02-001.mp4", files)
        self.assertNotIn("lont-hl-02-001.jpg", files)
        self.assertIn("lont-hl-02-002.jpg", files)
        self.assertEqual([i["type"] for h in self.manifest()["highlights"] for i in h["items"]],
                         ["image", "image", "video", "image", "video"])


if __name__ == "__main__":
    unittest.main()
