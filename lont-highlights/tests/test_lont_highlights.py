"""Tests for lont_highlights.py. Instagram and its CDN are fakes plugged under
gallery-dl and the downloader, so the real code runs without the internet.

Run from the project folder:  python -m unittest discover -s lont-highlights/tests
"""
from __future__ import annotations

import contextlib
import csv
import functools
import io
import json
import logging
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

LONT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LONT_DIR))

import lont_highlights as lh  # noqa: E402

try:
    import requests
    from requests.adapters import BaseAdapter
    from gallery_dl import config as gdl_config
    from gallery_dl.extractor import common as gdl_common, instagram as gdl_instagram
    GRAPHQL = hasattr(gdl_instagram, "QUERIES")  # 1.32.14 and later read highlights through GraphQL
except ImportError:  # the tests that go through gallery-dl are skipped
    gdl_config = None
    BaseAdapter = object
    GRAPHQL = False

try:
    import pillow_heif
    from PIL import Image
except ImportError:
    pillow_heif = None

LATER = int(time.time()) + 300 * 86400
SESSIONID = "1500%3AsEcReTsEsSiOnVaLuE%3A27%3AAYcQvWxYz"  # URL-encoded, like a real export
CSRFTOKEN = "CsRfSeCrEtToKeN0123456789abcdef"
DS_USER_ID = "98765432123"
COOKIES_TXT = (
    "# Netscape HTTP Cookie File\n"
    f"#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t{LATER}\tsessionid\t{SESSIONID}\n"
    f".instagram.com\tTRUE\t/\tTRUE\t{LATER}\tcsrftoken\t{CSRFTOKEN}\n"
    f".instagram.com\tTRUE\t/\tTRUE\t{LATER}\tds_user_id\t{DS_USER_ID}\n"
)
SECRETS = [SESSIONID, urllib.parse.unquote(SESSIONID), CSRFTOKEN, DS_USER_ID]

USER = {"pk": "1500", "id": "1500", "username": "lont.sa", "full_name": "Lont", "is_private": False}
# The profile's circles as Instagram gives them, plus a ninth that is not in the list.
CIRCLES = [
    ("1001", "عن لونت", ["video", "image"]),
    ("1002", "غرف النوم", ["image", "image", "video"]),
    ("1003", "الأسئلة الشائعة", ["image"]),
    ("1004", "غرف المعيشة", ["video"]),
    ("1005", "طاولات", ["image", "video"]),
    ("1006", "آراء العملاء", ["image"]),
    ("1007", "التوصيل", ["video"]),
    ("1008", "صور على الطبيعة", ["image", "image"]),
    ("1009", "عروض", ["image"]),
]
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF"
MP4_HEAD = b"\x00\x00\x00\x18ftypisom"
HEIC_HEAD = b"\x00\x00\x00\x18ftypheic"


def hex_time(seconds: float) -> str:
    return format(int(seconds), "x")


PAGE = ('<html><head><link rel="preload" href="https://static.cdninstagram.com/rsrc.php/v4/app.js" as="script">'
        '<script type="application/json" id="__eqmc">{"l":"fakeLsd","f":"fakeDtsg"}</script></head>'
        '<body><script>{"profile_id":"1500","params":{"username":"lont.sa"}}</script></body></html>')


class FakeInstagram:
    """instagram.com and its CDN, answering like the real ones: the REST API that
    gallery-dl 1.32.13 asks, and the GraphQL one of later versions."""

    def __init__(self, circles=CIRCLES):
        self.circles = {cid: (title, kinds) for cid, title, kinds in circles}
        self.order = [cid for cid, _, _ in circles]
        self.version = 1     # part of every link; a new version is what a refreshed answer carries
        self.calls = []      # what was asked of Instagram: "tray", "reels:1001,1002", "user", "page:/" ...
        self.downloads = []  # CDN files sent
        self.fail = {}       # part of a call ("tray", "reels:1006", "cdn:1003001.jpg") -> status instead
        self.revoked = {}    # CDN file -> link version the CDN refuses
        self.heic = set()    # CDN files sent as HEIC
        self.renewed = {}    # circle -> number added to its story ids (new stories)
        self.on_call = None  # called with each call (to log or raise)

    def data_calls(self) -> list[str]:
        """The calls that read highlights (the rest are the account, and pages for GraphQL tokens)."""
        return [call for call in self.calls if call == "tray" or call.startswith("reels:")]

    def link(self, name: str) -> str:
        return f"https://cdn.fake/{name}?oe={hex_time(time.time() + 7 * 86400)}&oh=sig{self.version}"

    def cover(self, cid: str) -> dict:
        return {"cropped_image_version": {"url": self.link(f"cover{cid}.jpg"), "width": 150, "height": 150},
                "media_id": f"{cid}001_1500"}

    def reel(self, cid: str) -> dict:
        title, kinds = self.circles[cid]
        items = []
        for n, kind in enumerate(kinds, 1):
            pk = f"{cid}{n + self.renewed.get(cid, 0):03d}"
            item = {"pk": pk, "id": f"{pk}_1500", "taken_at": 1700000000 + n, "media_type": 1,
                    "original_width": 1080, "original_height": 1920,
                    "image_versions2": {"candidates": [{"url": self.link(f"{pk}.jpg"), "width": 1080,
                                                        "height": 1920}]}}
            if kind == "video":
                item["media_type"] = 2
                item["video_versions"] = [{"url": self.link(f"{pk}.mp4"), "width": 720, "height": 1280,
                                           "type": 101}]
            items.append(item)
        return {"id": f"highlight:{cid}", "title": title, "user": dict(USER), "created_at": 1690000000,
                "media_count": len(kinds), "cover_media": self.cover(cid), "items": items}

    def answer(self, request) -> tuple[int, dict, bytes]:
        parsed = urllib.parse.urlsplit(request.url)
        query = urllib.parse.parse_qs(parsed.query)
        body = request.body.decode() if isinstance(request.body, bytes) else request.body or ""
        form = urllib.parse.parse_qs(body) if request.method == "POST" else {}
        graphql = (form.get("fb_api_req_friendly_name") or [""])[0]
        if graphql == "PolarisStoriesV3ReelPageGalleryQuery":
            reel_ids = json.loads(form["variables"][0])["reel_ids"]
        else:
            reel_ids = query.get("reel_ids", [])
        call = ("cdn:" + parsed.path.rsplit("/", 1)[1] if parsed.netloc == "cdn.fake" else
                "script" if parsed.netloc == "static.cdninstagram.com" else
                "reels:" + ",".join(r.rpartition(":")[2] for r in reel_ids) if reel_ids else
                "tray" if parsed.path.endswith("/highlights_tray/") or "HighlightsTray" in graphql else
                "user" if parsed.path == "/web/search/topsearch/" or graphql == "PolarisProfilePageContentQuery" else
                "page:" + parsed.path)
        if not call.startswith("cdn:"):
            self.calls.append(call)
            if self.on_call:
                self.on_call(call)
        for part, status in self.fail.items():
            if part in call:
                if status in (301, 302):
                    return status, {"Location": "https://www.instagram.com/accounts/login/?next=%2F"}, b""
                if status == 200:  # how Instagram refuses a GraphQL query it no longer accepts
                    return self.json({"errors": [{"message": "A server error missing_required_variable_value "
                                                             "occured. Check server logs for details.",
                                                  "severity": "CRITICAL", "code": 1675012}]})
                return status, {}, b"{}"

        if call.startswith("cdn:"):
            name = call[4:]
            if int(query["oe"][0], 16) < time.time() or query["oh"][0] == f"sig{self.revoked.get(name)}":
                return 403, {"Content-Type": "text/plain"}, b"URL signature expired"
            self.downloads.append(name)
            if name in self.heic:
                return 200, {"Content-Type": "image/heic"}, heic_bytes()
            if name.endswith(".mp4"):
                return 200, {"Content-Type": "video/mp4"}, MP4_HEAD + name.encode() + bytes(50)
            return 200, {"Content-Type": "image/jpeg"}, JPEG_HEAD + name.encode() + bytes(50)
        if call == "script":  # where newer gallery-dl versions look up the GraphQL query ids
            from gallery_dl.extractor import instagram
            return 200, {"Content-Type": "text/javascript"}, "".join(
                f'__d("{name}_instagramRelayOperation",[],(function(a,b,c,d,e,f){{e.exports="{n}"}}),null);\n'
                for n, name in enumerate(getattr(instagram, "QUERIES", ()), 1)).encode()
        if call == "page:/accounts/login/":
            return 200, {"Content-Type": "text/html"}, b"<html>login</html>"
        if call.startswith("page:"):
            return 200, {"Content-Type": "text/html"}, PAGE.encode()
        if call == "user":
            return self.json({"data": {"user": dict(USER)}} if graphql else {"users": [{"user": dict(USER)}]})
        if call == "tray":
            tray = [self.reel(cid) | {"items": []} for cid in self.order]
            return self.json({"data": {"highlights": {"edges": [{"node": node} for node in tray]}}} if graphql
                             else {"tray": tray})
        if call.startswith("reels:"):
            reels = [self.reel(cid) for cid in call[6:].split(",") if cid in self.circles]
            if graphql:
                return self.json({"data": {"xdt_api__v1__feed__reels_media__connection": {
                    "edges": [{"node": reel} for reel in reels], "page_info": {"has_next_page": False}}}})
            return self.json({"reels_media": reels})
        return 404, {}, b"{}"

    @staticmethod
    def json(data) -> tuple[int, dict, bytes]:
        return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()


def heic_bytes() -> bytes:
    if pillow_heif is None:
        return HEIC_HEAD + bytes(50)
    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buffer, "HEIF")
    return buffer.getvalue()


class Transport(BaseAdapter):
    """Sends gallery-dl's and the downloader's requests to FakeInstagram."""

    def __init__(self, site: FakeInstagram):
        super().__init__()
        self.site = site

    def send(self, request, **kwargs):
        status, headers, body = self.site.answer(request)
        response = requests.Response()
        response.status_code, response.url, response.request = status, request.url, request
        response.reason = {200: "OK", 302: "Found", 403: "Forbidden", 404: "Not Found",
                           429: "Too Many Requests"}.get(status, "")
        response.headers.update(headers)
        response._content, response._content_consumed = body, True
        response.raw, response.encoding = io.BytesIO(body), "utf-8"
        return response

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Order and names (no network at all)
# ---------------------------------------------------------------------------

def raw_with_titles(titles: list[str]) -> dict:
    return {"order": [str(i) for i in range(len(titles))],
            "circles": {str(i): {"title": title} for i, title in enumerate(titles)}}


class OrderTests(unittest.TestCase):
    LIST = [arabic for arabic, _ in lh.EXPECTED]

    def test_same_order_as_the_list_keeps_instagram_order(self):
        titles = self.LIST[:5] + ["عروض"] + self.LIST[5:] + ["جديد"]
        arrangement = lh.arrange(raw_with_titles(titles))
        self.assertFalse(arrangement.reordered)
        self.assertEqual([titles[int(cid)] for cid in arrangement.order], titles)
        self.assertEqual(arrangement.missing, [])

    def test_other_order_follows_the_list_and_extra_circles_go_last(self):
        titles = ["عروض", "التوصيل", "عن لونت", "طاولات", "غرف النوم", "الأسئلة الشائعة", "صور على الطبيعة",
                  "غرف المعيشة", "آراء العملاء"]
        arrangement = lh.arrange(raw_with_titles(titles))
        self.assertTrue(arrangement.reordered)
        self.assertEqual([titles[int(cid)] for cid in arrangement.order], self.LIST + ["عروض"])

    def test_titles_match_with_another_spelling_or_an_emoji(self):
        titles = ["عن لونت ✨", "غرفة النوم", "الاسئلة الشائعة ❓", "غرف المعيشه", "الطاولات", "آراء عملائنا",
                  "توصيل الطلبات", "صور ع الطبيعة"]
        arrangement = lh.arrange(raw_with_titles(titles))
        self.assertEqual(arrangement.matched, {str(i): i for i in range(8)})
        self.assertFalse(arrangement.reordered)

    def test_circles_missing_on_instagram_are_reported(self):
        titles = [t for t in self.LIST if t != "طاولات"]
        arrangement = lh.arrange(raw_with_titles(titles))
        self.assertEqual(arrangement.missing, [4])
        self.assertEqual(len(arrangement.order), 7)


class NamingTests(unittest.TestCase):
    def test_file_names(self):
        items = [{"media_id": str(n), "url": f"https://cdn.fake/{n}", "type": "image"} for n in range(12)]
        circle = {"cover": {"url": "https://cdn.fake/c", "media": "c"}, "items": items}
        names = [plan.stem for plan in lh.planned_files(circle, 1)]
        self.assertEqual(names[:3], ["lont-hl-01-cover", "lont-hl-01-001", "lont-hl-01-002"])
        self.assertEqual(names[-1], "lont-hl-01-012")
        self.assertEqual([plan.stem for plan in lh.planned_files({"items": items[:1]}, 10)], ["lont-hl-10-001"])

    def test_extension_comes_from_the_content_type(self):
        self.assertEqual(lh.extension("video/mp4", MP4_HEAD, "video"), ".mp4")
        self.assertEqual(lh.extension("image/jpeg", JPEG_HEAD, "image"), ".jpg")
        self.assertEqual(lh.extension("image/webp", b"RIFF\0\0\0\0WEBP", "cover"), ".webp")
        self.assertEqual(lh.extension("image/heic", HEIC_HEAD, "image"), ".heic")
        # a missing or vague Content-Type: the first bytes decide
        self.assertEqual(lh.extension("application/octet-stream", JPEG_HEAD, "image"), ".jpg")
        self.assertEqual(lh.extension("", MP4_HEAD, "video"), ".mp4")
        self.assertEqual(lh.extension("image/jpeg", HEIC_HEAD, "image"), ".heic")

    def test_link_expiry_is_read_from_the_link(self):
        self.assertTrue(lh.link_expired(f"https://cdn.fake/a.jpg?oe={hex_time(time.time() - 60)}&oh=x"))
        self.assertFalse(lh.link_expired(f"https://cdn.fake/a.jpg?oe={hex_time(time.time() + 86400)}&oh=x"))
        self.assertFalse(lh.link_expired("https://cdn.fake/a.jpg"))


# ---------------------------------------------------------------------------
# Whole runs, through gallery-dl
# ---------------------------------------------------------------------------

@unittest.skipIf(gdl_config is None, "gallery-dl is not installed")
class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.base = self.project / "lont-highlights"
        self.out = self.base / "output"
        self.base.mkdir()
        (self.project / "cookies.txt").write_text(COOKIES_TXT, encoding="utf-8")
        self.site = FakeInstagram()
        self.console = ""
        transport = Transport(self.site)

        class TestInstagram(lh.Instagram):  # gallery-dl without its waits and its cache file
            def __init__(self, cookies):
                super().__init__(cookies, transport=transport)
                gdl_config.set(("extractor",), "sleep-request", 0)
                gdl_config.set(("extractor", "instagram"), "user-cache", "memory")
                gdl_config.set(("cache",), "file", ":memory:")

        gdl_common.CACHE_MEMORY.clear()
        lh._SECRETS.clear()
        for patch in (mock.patch.object(lh, "Instagram", TestInstagram),
                      mock.patch.object(lh, "Fetcher", functools.partial(lh.Fetcher, transport=transport)),
                      mock.patch("time.sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *args: str) -> tuple[int, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        root.handlers.clear()  # gallery-dl's log goes to the captured stderr too
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = lh.main([*args, "--folder", str(self.base)])
        finally:
            root.handlers[:], root.level = handlers, level
        text = stdout.getvalue() + stderr.getvalue()
        self.console += text
        return code, text

    def raw(self) -> dict:
        return json.loads((self.base / "raw_list.json").read_text(encoding="utf-8"))

    def report(self) -> list[list[str]]:
        with open(self.out / "report.csv", encoding="utf-8-sig", newline="") as fh:
            return list(csv.reader(fh))

    def media(self) -> list[str]:
        return sorted(p.name for p in self.out.glob("lont-hl-*")) if self.out.is_dir() else []

    def test_dry_run_reads_the_data_once_and_downloads_nothing(self):
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        # the circles, then their stories 5 at a time: nothing more
        self.assertEqual(self.site.data_calls(), ["tray", "reels:1001,1002,1003,1004,1005", "reels:1006,1007,1008,1009"])
        self.assertEqual(self.site.downloads, [])
        self.assertEqual(self.media(), [])
        raw = self.raw()
        self.assertEqual(raw["order"], [cid for cid, _, _ in CIRCLES])
        self.assertTrue(all(raw["circles"][cid]["cover"] for cid in raw["order"]))
        self.assertIn("- Order: the same as your list.", out)
        self.assertRegex(out, r"\n  02  2 Bedrooms +3 +2 +1  yes\n")
        self.assertRegex(out, r"\n  09  extra \(not in your list\) +1 +1 +0  yes\n")
        rows = self.report()
        self.assertEqual(rows[0], lh.REPORT_COLUMNS)
        self.assertEqual(rows[2][:4], ["2", "غرف النوم", "2", "1"])
        self.assertIn("مش في قايمتك", rows[9][4])

        self.site.calls.clear()
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.calls, [])  # raw_list.json answers
        self.assertIn("is complete, so Instagram is not asked again", out)

    def test_download_names_files_in_order_and_writes_the_manifest(self):
        self.run_main("--dry-run")
        self.site.calls.clear()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.calls, [])  # downloading needs no API request
        self.assertEqual(self.media()[:6], ["lont-hl-01-001.mp4", "lont-hl-01-002.jpg", "lont-hl-01-cover.jpg",
                                            "lont-hl-02-001.jpg", "lont-hl-02-002.jpg", "lont-hl-02-003.mp4"])
        self.assertEqual(len(self.media()), 9 + 14)  # covers + stories
        self.assertTrue((self.out / "lont-hl-01-001.mp4").read_bytes().startswith(MP4_HEAD + b"1001001.mp4"))
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["profile"], "lont.sa")
        first = manifest["highlights"][0]
        self.assertEqual({k: first[k] for k in ("position", "title", "cover_file")},
                         {"position": 1, "title": "عن لونت", "cover_file": "lont-hl-01-cover.jpg"})
        self.assertEqual(first["items"], [
            {"position": 1, "file": "lont-hl-01-001.mp4", "type": "video", "width": 720, "height": 1280},
            {"position": 2, "file": "lont-hl-01-002.jpg", "type": "image", "width": 1080, "height": 1920}])
        self.assertEqual([h["title"] for h in manifest["highlights"]], [title for _, title, _ in CIRCLES])
        self.assertTrue(all(row[4].startswith("اتحمّلت كاملة") for row in self.report()[1:]))

    def test_another_order_on_instagram_uses_the_list_order(self):
        self.site.order = ["1002", "1001"] + self.site.order[2:]
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("! Instagram gave the circles in another order than your list.", out)
        self.assertTrue((self.out / "lont-hl-01-001.mp4").read_bytes().startswith(MP4_HEAD + b"1001001.mp4"))
        self.assertTrue((self.out / "lont-hl-02-003.mp4").read_bytes().startswith(MP4_HEAD + b"1002003.mp4"))

    def test_a_new_run_skips_what_is_already_downloaded(self):
        self.run_main()
        (self.out / "lont-hl-02-002.jpg").unlink()
        self.site.calls.clear()
        self.site.downloads.clear()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.downloads, ["1002002.jpg"])
        self.assertEqual(self.site.calls, [])
        self.assertIn("= 3 file(s) were already downloaded", out)
        self.assertFalse(list(self.out.glob("*.part")))

    def test_429_while_reading_stops_and_the_next_run_asks_only_for_the_rest(self):
        self.site.fail["reels:1006"] = 429  # the second group of circles
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 3, out)
        self.assertIn("429 Too Many Requests", out)
        self.assertIn("Wait one hour, then run run_lont.bat again", out)
        self.assertEqual(self.site.calls[-1], "reels:1006,1007,1008,1009")  # nothing was asked after the 429
        raw = self.raw()
        self.assertEqual([cid for cid in raw["order"] if raw["circles"][cid]["items"] is None],
                         ["1006", "1007", "1008", "1009"])

        self.site.fail.clear()
        self.site.calls.clear()
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.data_calls(), ["reels:1006", "reels:1007", "reels:1008", "reels:1009"])
        self.assertEqual(self.raw()["order"], [cid for cid, _, _ in CIRCLES])

    def test_429_while_downloading_stops_and_the_next_run_continues(self):
        self.site.fail["cdn:1003001.jpg"] = 429
        code, out = self.run_main()
        self.assertEqual(code, 3, out)
        self.assertIn("Wait one hour", out)
        self.assertIn("lont-hl-03-cover.jpg", self.media())
        self.assertNotIn("lont-hl-04-001.mp4", self.media())
        rows = self.report()
        self.assertTrue(rows[1][4].startswith("اتحمّلت كاملة"))
        self.assertTrue(rows[3][4].startswith("ناقص 1 من 2"))
        self.assertTrue(rows[4][4].startswith("لسه ماتحمّلتش"))
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertIsNone(manifest["highlights"][2]["items"][0]["file"])

        self.site.fail.clear()
        self.site.downloads.clear()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.downloads[0], "1003001.jpg")
        self.assertNotIn("1001001.mp4", self.site.downloads)
        self.assertEqual(len(self.media()), 23)

    def test_expired_links_are_renewed_for_that_circle_only(self):
        self.run_main("--dry-run")
        raw = self.raw()
        circle = raw["circles"]["1004"]
        for link in circle["items"] + [circle["cover"]]:
            link["url"] = link["url"].replace("oe=", f"oe={hex_time(time.time() - 3600)}&old=")
        (self.base / "raw_list.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.site.calls.clear()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.data_calls(), ["reels:1004"])
        self.assertIn("lont-hl-04-001.mp4", self.media())
        self.assertIn("lont-hl-04-cover.jpg", self.media())  # the circle's answer renews its cover too

    def test_a_refused_link_renews_its_circle_once(self):
        self.run_main("--dry-run")
        self.site.revoked["1005002.mp4"] = 1  # the saved link stops working ...
        self.site.version = 2                  # ... and a new answer has a working one
        self.site.calls.clear()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.data_calls(), ["reels:1005"])
        self.assertIn("lont-hl-05-002.mp4", self.media())

        self.site.fail["cdn:1006001.jpg"] = 403  # refused even after renewing: reported, the rest goes on
        (self.out / "lont-hl-06-001.jpg").unlink()
        code, out = self.run_main()
        self.assertEqual(code, 1, out)
        self.assertIn("x lont-hl-06-001: the link no longer works (HTTP 403)", out)
        self.assertTrue(self.report()[6][4].startswith("ناقص 1 من 2"))

    def test_a_circle_changed_on_instagram_moves_its_old_files_aside(self):
        self.run_main()
        self.site.circles["1002"] = ("غرف النوم", ["video", "image"])  # Lont put new stories in it,
        self.site.renewed["1002"] = 100
        raw = self.raw()
        for item in raw["circles"]["1002"]["items"]:  # the saved links ran out,
            item["url"] = item["url"].replace("oe=", f"oe={hex_time(time.time() - 3600)}&old=")
        (self.base / "raw_list.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        (self.out / "lont-hl-02-002.jpg").unlink()  # and one file is still missing
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual([name for name in self.media() if name.startswith("lont-hl-02-")],
                         ["lont-hl-02-001.mp4", "lont-hl-02-002.jpg", "lont-hl-02-cover.jpg"])
        self.assertTrue((self.out / "lont-hl-02-001.mp4").read_bytes().startswith(MP4_HEAD + b"1002101.mp4"))
        self.assertEqual(sorted(p.name for p in (self.out / "old").iterdir()),
                         ["lont-hl-02-001.jpg", "lont-hl-02-003.mp4"])
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["file"] for item in manifest["highlights"][1]["items"]],
                         ["lont-hl-02-001.mp4", "lont-hl-02-002.jpg"])

    def test_heic_pictures_become_jpg(self):
        if pillow_heif is None:
            self.skipTest("pillow-heif is not installed")
        self.site.heic.add("1003001.jpg")
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("lont-hl-03-001.jpg", self.media())
        with Image.open(self.out / "lont-hl-03-001.jpg") as image:
            self.assertEqual((image.format, image.size), ("JPEG", (64, 48)))

    def test_heic_is_kept_when_the_converter_is_missing(self):
        self.site.heic.add("1003001.jpg")
        with mock.patch.object(lh, "heic_to_jpg", return_value=False):
            code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("lont-hl-03-001.heic", self.media())
        self.assertIn("HEIC picture kept as .heic", out)

    def test_a_refused_account_lookup_still_reads_the_highlights(self):
        self.site.fail["user"] = 200  # what stopped the first real run: the number comes from the profile page
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.site.data_calls(), ["tray", "reels:1001,1002,1003,1004,1005", "reels:1006,1007,1008,1009"])
        self.assertTrue(self.raw()["complete_list"])

    def test_instagram_answer_is_shown_when_it_refuses_a_step(self):
        self.site.fail["tray"] = 200
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("STOPPED: gallery-dl stopped: KeyError:", out)
        if GRAPHQL:  # newer gallery-dl versions log Instagram's answer, which is shown in one line
            self.assertIn("  Instagram's answer: {\"errors\": [{\"message\": \"A server error "
                          "missing_required_variable_value", out)
        self.assertNotIn("Server response", out)  # gallery-dl's other debug messages stay hidden
        self.assertTrue(out.isascii())

    def test_cookies_rejected_by_instagram(self):
        self.site.fail["tray"] = 302  # Instagram sends the browser to its login page
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("Instagram did not accept the cookies", out)
        self.assertIn('"Get cookies.txt LOCALLY"', out)

    def test_no_usable_cookies_stop_before_asking_instagram(self):
        (self.project / "cookies.txt").write_text(COOKIES_TXT.replace("sessionid", "other"), encoding="utf-8")
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("cookies.txt in the project folder: no Instagram login in it (no sessionid)", out)
        self.assertIn("put it in the lont-highlights folder", out)
        self.assertEqual(self.site.calls, [])

        (self.project / "cookies.txt").unlink()
        code, out = self.run_main("--dry-run")
        self.assertIn("lont-highlights", out)
        self.assertIn("not found", out)

        expired = COOKIES_TXT.replace(str(LATER), str(int(time.time()) - 86400))
        (self.base / "cookies.txt").write_text(expired, encoding="utf-8")
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 2, out)
        self.assertIn("the Instagram login in it ran out on", out)

        (self.base / "cookies.txt").write_text(COOKIES_TXT, encoding="utf-8")  # a new one in lont-highlights
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0, out)

    def test_ctrl_c_inside_gallery_dl_stops_and_keeps_the_data(self):
        def interrupt(call):
            if call.startswith("reels:1006"):
                raise KeyboardInterrupt
        self.site.on_call = interrupt
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 130, out)
        raw = self.raw()
        self.assertEqual(len([cid for cid in raw["order"] if raw["circles"][cid]["items"] is not None]), 5)

    def test_no_cookie_value_appears_in_any_output(self):
        def leak(call):  # gallery-dl, or an error, could mention a cookie
            logging.getLogger("instagram").warning("echo %s %s", SESSIONID, CSRFTOKEN)
        self.site.on_call = leak
        real_answer = self.site.answer

        def broken_cdn(request):
            if "1004001.mp4" in request.url:
                raise requests.ConnectionError(f"connection reset (token {CSRFTOKEN}, user {DS_USER_ID})")
            return real_answer(request)
        self.site.answer = broken_cdn
        self.run_main("--dry-run")
        code, _ = self.run_main()
        self.assertEqual(code, 1)  # the broken file is reported
        self.assertIn("***", self.console)
        self.assertTrue(self.console.isascii(), "the console is English only")
        outputs = [p for p in self.project.rglob("*") if p.is_file() and p.name != "cookies.txt"]
        self.assertIn(self.base / "raw_list.json", outputs)
        self.assertIn(self.out / "report.csv", outputs)
        for secret in SECRETS:
            self.assertNotIn(secret, self.console)
            for path in outputs:
                self.assertNotIn(secret.encode(), path.read_bytes(), path)
        self.assertIn("cookies.txt", (LONT_DIR / ".gitignore").read_text(encoding="utf-8").split())


def tearDownModule():
    if gdl_config is not None:
        from gallery_dl import cache
        if cache.DATABASE is not None:  # the tests' in-memory cache
            cache.DATABASE.close()


if __name__ == "__main__":
    unittest.main()
