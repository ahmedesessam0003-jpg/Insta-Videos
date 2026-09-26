import csv
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import insta_videos as iv  # noqa: E402

CONFIG_PATH = Path(iv.__file__).resolve().parent / "products.json"


def products():
    return iv.load_config(CONFIG_PATH).products


def first_match(caption):
    matches = iv.match_caption(caption, products(), "first")
    return matches[0].product.name if matches else None


def product_named(prefix):
    return next(p.name for p in products() if p.name.startswith(prefix))


class SourceHygieneTests(unittest.TestCase):
    def test_no_invisible_or_bidi_control_characters(self):
        import unicodedata
        root = CONFIG_PATH.parent
        for name in ("insta_videos.py", "products.json", "tests/test_insta_videos.py", "README.md"):
            text = (root / name).read_text(encoding="utf-8")
            hidden = sorted({f"U+{ord(c):04X}" for c in text if unicodedata.category(c) == "Cf"})
            self.assertEqual(hidden, [], name)


class NormalizeTests(unittest.TestCase):
    def test_zero_width_and_bidi_marks_are_ignored(self):
        self.assertEqual(iv.tokenize("الوس" + chr(0x200C) + "مة" + chr(0x200F)), ["الوسمه"])

    def test_diacritics_and_letter_variants(self):
        self.assertEqual(iv.normalize("الحنّة"), iv.normalize("الحنه"))
        self.assertEqual(iv.normalize("الأرز"), iv.normalize("الارز"))
        self.assertEqual(iv.normalize("مستشفى"), iv.normalize("مستشفي"))
        self.assertEqual(iv.normalize("الحناء"), iv.normalize("الحنا"))
        self.assertEqual(iv.normalize("ب٥"), "ب5")

    def test_hashtags_and_emoji_split_words(self):
        self.assertEqual(iv.tokenize("#معجون_الحنة🌿✨بودرة"), ["معجون", "الحنه", "بودره"])


class MatchingTests(unittest.TestCase):
    def test_every_product_name_matches_its_own_product(self):
        for product in products():
            with self.subTest(product=product.name):
                self.assertEqual(first_match(product.name), product.name)

    def test_partial_names_from_the_request(self):
        self.assertEqual(first_match("جربي الوسمة"), product_named("الوسمة"))
        self.assertEqual(first_match("بخاخ الشعر"), product_named("بخاخ"))
        self.assertEqual(first_match("ماسك البروتين"), product_named("ماسك"))
        self.assertEqual(first_match("معجون الحنة"), product_named("معجون الحنّة"))
        self.assertEqual(first_match("بودرة السدر"), product_named("بودرة السدر"))

    def test_spelling_variants(self):
        henna_paste = product_named("معجون الحنّة")
        for caption in ("معجون الحنّة الطبيعي", "#معجون_الحنه", "ومعجون حنة", "معجون الحناء الأصلي",
                        "عجينة الحنا"):
            with self.subTest(caption=caption):
                self.assertEqual(first_match(caption), henna_paste)
        wasma = product_named("الوسمة")
        for caption in ("بالوسمة", "والوسمه", "وسمة طبيعية", "#الوسمة"):
            with self.subTest(caption=caption):
                self.assertEqual(first_match(caption), wasma)
        self.assertEqual(first_match("مرطب شفايفك المفضل"), product_named("مرطب"))
        self.assertEqual(first_match("مرطب للشفايف"), product_named("مرطب"))
        self.assertEqual(first_match("اللبان الحوجري"), product_named("بخور"))

    def test_no_false_positives(self):
        for caption in ("عروض الموسم", "موسمه جديد", "بان الفرق من أول استخدام", "زيت نواة التمر",
                        "الشحم البقري", "منتجات طبيعية 100%", ""):
            with self.subTest(caption=caption):
                self.assertIsNone(first_match(caption))

    def test_similar_products_are_kept_apart(self):
        self.assertEqual(first_match("بودرة الحنة الحضرمية"), product_named("بودرة الحنة"))
        self.assertEqual(first_match("معجون السدر"), product_named("معجون السدر"))
        self.assertEqual(first_match("بودره السدر"), product_named("بودرة السدر"))

    def test_first_mode_prefers_earliest_then_longest(self):
        caption = "معجون الحنة الجديد 🌿 استخدميه بعد الشامبو والبلسم"
        self.assertEqual(first_match(caption), product_named("معجون الحنّة"))
        self.assertEqual(first_match("بلسم الشفايف بعسل المانوكا"), product_named("مرطب"))
        self.assertEqual(first_match("بلسم الشعر"), product_named("بلسم"))

    def test_all_mode_returns_every_product_in_order(self):
        caption = "معجون الحنة الجديد 🌿 استخدميه بعد الشامبو والبلسم"
        names = [m.product.name for m in iv.match_caption(caption, products(), "all")]
        self.assertEqual(names, [product_named("معجون الحنّة"), product_named("شامبو"), product_named("بلسم")])

    def test_default_keywords_are_first_two_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"profile": "x", "products": [{"name": "زيت الروزماري الطبيعي"}]}),
                            encoding="utf-8")
            config = iv.load_config(path)
            self.assertEqual([k.text for k in config.products[0].keywords], ["زيت الروزماري"])
            self.assertEqual(len(iv.match_caption("جديد: زيت روزماري", config.products)), 1)

    def test_hand_edited_config_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({
                "profile": "https://www.instagram.com/naturalroots.store/",
                "exclude_posts": "https://www.instagram.com/p/XYZ/",
                "products": ["زيت الروزماري الطبيعي", {"name": "الوسمة", "keywords": "الوسمة"}]}),
                encoding="utf-8")
            config = iv.load_config(path)
            self.assertEqual(config.profile, "naturalroots.store")
            self.assertEqual(config.exclude_posts, {"XYZ"})
            self.assertEqual([k.text for k in config.products[1].keywords], ["الوسمة"])
            self.assertEqual(iv.match_caption("منتجات طبيعية", config.products), [])


class HelperTests(unittest.TestCase):
    def test_links(self):
        self.assertEqual(iv.to_shortcode("https://www.instagram.com/reel/C1a-B_c2/?igsh=x"), "C1a-B_c2")
        self.assertEqual(iv.to_shortcode("https://www.instagram.com/naturalroots.store/p/Abc123/"), "Abc123")
        self.assertEqual(iv.to_shortcode("Abc123"), "Abc123")
        self.assertEqual(iv.to_username("https://www.instagram.com/naturalroots.store/"), "naturalroots.store")
        self.assertEqual(iv.to_username("@naturalroots.store"), "naturalroots.store")

    def test_plan_names(self):
        self.assertEqual(iv.plan_names("منتج", 1, set(), "{name} ({n})"), ["منتج.mp4"])
        self.assertEqual(iv.plan_names("منتج", 3, set(), "{name} ({n})"),
                         ["منتج (1).mp4", "منتج (2).mp4", "منتج (3).mp4"])
        self.assertEqual(iv.plan_names("منتج", 1, {"منتج.mp4"}, "{name} ({n})"), ["منتج (1).mp4"])
        self.assertEqual(iv.plan_names("منتج", 2, {"منتج (1).mp4"}, "{name} {n}"),
                         ["منتج 1.mp4", "منتج 2.mp4"])
        self.assertEqual(iv.plan_names("منتج", 2, {"منتج (1).mp4"}, "{name} ({n})"),
                         ["منتج (2).mp4", "منتج (3).mp4"])

    def test_safe_name(self):
        self.assertEqual(iv.safe_name('a/b:c*d?. '), "a b c d")

    def test_cookie_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            netscape = Path(tmp) / "cookies.txt"
            netscape.write_text(
                "# Netscape HTTP Cookie File\n"
                ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tabc\n"
                "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t123%3Axyz\n"
                ".example.com\tTRUE\t/\tFALSE\t0\tother\tzzz\n", encoding="utf-8")
            self.assertEqual(iv.parse_cookie_file(netscape), {"csrftoken": "abc", "sessionid": "123%3Axyz"})
            exported = Path(tmp) / "cookies.json"
            exported.write_text(json.dumps([
                {"domain": ".instagram.com", "name": "sessionid", "value": "s"},
                {"domain": ".facebook.com", "name": "c_user", "value": "f"}]), encoding="utf-8")
            self.assertEqual(iv.parse_cookie_file(exported), {"sessionid": "s"})


class ResolveFoldersTests(unittest.TestCase):
    def test_existing_folders_are_reused_with_their_exact_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "معجون الحنة الطبيعي بالقسط الهندي والبلميط المنشاري وزيت نواة التمر").mkdir()  # no shadda
            (out / "بودرة السدر اليمنيه من ناتشورال روتس").mkdir()  # ه instead of ة
            (out / "بخاخ الشعر الطبيعي المدعم بالخلايا الجذعية المستخلصه من التفاح ").mkdir()
            (out / "ديودرنت الشحم البقرى").mkdir()
            items = products()
            iv.resolve_folders(items, out)
            folders = {p.name: p.folder for p in items}
            self.assertEqual(folders[product_named("معجون الحنّة")],
                             "معجون الحنة الطبيعي بالقسط الهندي والبلميط المنشاري وزيت نواة التمر")
            self.assertEqual(folders[product_named("بودرة السدر")], "بودرة السدر اليمنيه من ناتشورال روتس")
            self.assertEqual(folders[product_named("ديودرنت")], "ديودرنت الشحم البقرى")
            self.assertTrue(folders[product_named("بخاخ")].startswith("بخاخ الشعر الطبيعي المدعم"))
            self.assertEqual(folders[product_named("الوسمة")], product_named("الوسمة"))
            self.assertEqual(len(set(folders.values())), len(items))

    def test_close_but_different_names_do_not_steal_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "بودرة الحنة الحضرمية").mkdir()
            items = products()
            iv.resolve_folders(items, out)
            folders = {p.name: p.folder for p in items}
            self.assertEqual(folders[product_named("بودرة الحنة")], "بودرة الحنة الحضرمية")
            self.assertEqual(folders[product_named("بودرة السدر")], product_named("بودرة السدر"))


class FakeDownloader:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def fetch(self, url, dest):
        self.calls.append(url)
        if url in self.fail:
            raise RuntimeError("boom")
        dest.write_bytes(url.encode())


def video(shortcode, day, index=1):
    return iv.Video(shortcode, index, dt.datetime(2024, 1, day, 12), f"https://cdn/{shortcode}_{index}.mp4", "k")


class SyncFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.state = iv.State(self.out / iv.STATE_FILE)

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, folder, videos, downloader=None, dry_run=False):
        results = {}
        counts = iv.sync_folder(self.out, folder, {v.key: v for v in videos}, self.state, "{name} ({n})",
                                downloader or FakeDownloader(), results, dry_run)
        return counts, results

    def files(self, folder):
        return sorted(p.name for p in (self.out / folder).iterdir())

    def content(self, folder, name):
        return (self.out / folder / name).read_text()

    def test_single_video_then_more_videos_get_numbered_by_date(self):
        self.sync("منتج", [video("B", 10)])
        self.assertEqual(self.files("منتج"), ["منتج.mp4"])

        counts, _ = self.sync("منتج", [video("C", 20), video("A", 5), video("B", 10)])
        self.assertEqual(self.files("منتج"), ["منتج (1).mp4", "منتج (2).mp4", "منتج (3).mp4"])
        self.assertEqual(self.content("منتج", "منتج (1).mp4"), "https://cdn/A_1.mp4")
        self.assertEqual(self.content("منتج", "منتج (2).mp4"), "https://cdn/B_1.mp4")
        self.assertEqual(self.content("منتج", "منتج (3).mp4"), "https://cdn/C_1.mp4")
        self.assertEqual((counts["new"], counts["renamed"], counts["total"]), (2, 1, 3))

        # Re-running with nothing new downloads nothing.
        downloader = FakeDownloader()
        counts, _ = self.sync("منتج", [video("A", 5), video("B", 10), video("C", 20)], downloader)
        self.assertEqual(downloader.calls, [])
        self.assertEqual((counts["new"], counts["renamed"]), (0, 0))

        # The state survives a restart.
        reloaded = iv.State(self.out / iv.STATE_FILE)
        self.assertEqual(sorted(reloaded.folders["منتج"]), ["A_1", "B_1", "C_1"])

    def test_foreign_files_are_never_touched(self):
        (self.out / "منتج").mkdir()
        (self.out / "منتج" / "منتج.mp4").write_text("user file")
        (self.out / "منتج" / "صورة.jpg").write_text("image")
        self.sync("منتج", [video("A", 1)])
        self.assertEqual(self.content("منتج", "منتج.mp4"), "user file")
        self.assertEqual(self.files("منتج"), ["صورة.jpg", "منتج (1).mp4", "منتج.mp4"])

    def test_deleted_file_is_downloaded_again_and_failures_retry_next_run(self):
        self.sync("منتج", [video("A", 1), video("B", 2)], FakeDownloader(fail={"https://cdn/B_1.mp4"}))
        self.assertEqual(self.files("منتج"), ["منتج (1).mp4"])
        (self.out / "منتج" / "منتج (1).mp4").unlink()
        downloader = FakeDownloader()
        self.sync("منتج", [video("A", 1), video("B", 2)], downloader)
        self.assertEqual(sorted(downloader.calls), ["https://cdn/A_1.mp4", "https://cdn/B_1.mp4"])
        self.assertEqual(self.files("منتج"), ["منتج (1).mp4", "منتج (2).mp4"])

    def test_carousel_videos_and_copies_between_folders(self):
        downloader = FakeDownloader()
        self.sync("أ", [video("A", 1, 1), video("A", 1, 3)], downloader)
        self.sync("ب", [video("A", 1, 3)], downloader)
        self.assertEqual(self.files("أ"), ["أ (1).mp4", "أ (2).mp4"])
        self.assertEqual(self.content("ب", "ب.mp4"), "https://cdn/A_3.mp4")
        self.assertEqual(len(downloader.calls), 2)  # the second folder got a copy

    def test_dry_run_changes_nothing(self):
        counts, results = self.sync("منتج", [video("A", 1)], dry_run=True)
        self.assertFalse((self.out / "منتج").exists())
        self.assertFalse((self.out / iv.STATE_FILE).exists())
        self.assertEqual(counts["new"], 1)
        self.assertEqual(results[("منتج", "A_1")][0], "منتج.mp4")


class DownloaderTests(unittest.TestCase):
    def test_fetch_from_local_server(self):
        import functools
        import http.server
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "v.mp4").write_bytes(b"\x00\x01video" * 50000)
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
            handler.log_message = lambda *a: None
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                downloader = iv.Downloader()
                self.assertEqual(downloader.session.headers["User-Agent"], "Mozilla/5.0")  # not throttled
                downloader.session.trust_env = False  # never route the local server through a proxy
                dest = root / "out.mp4"
                downloader.fetch(f"{base}/v.mp4", dest)
                self.assertEqual(dest.read_bytes(), (root / "v.mp4").read_bytes())
                with self.assertRaises(Exception):
                    downloader.fetch(f"{base}/missing.mp4", root / "missing.mp4")
                self.assertEqual(sorted(p.name for p in root.iterdir()), ["out.mp4", "v.mp4"])
            finally:
                server.shutdown()
                server.server_close()


class FakeBackend:
    posts = []

    def __init__(self, *args, **kwargs):
        pass

    def iter_posts(self, include_reels):
        yield from self.posts


def record(shortcode, day, caption, kind="video", urls=None):
    videos = [(i, u) for i, u in enumerate(urls or [f"https://cdn/{shortcode}.mp4"], 1)]
    return iv.PostRecord(shortcode, dt.datetime(2024, 2, day), caption, kind, lambda: videos)


def fake_backend(name, used, posts=(), error=None):
    """A backend class that records its use, yields `posts`, then raises `error`."""
    class Backend:
        def __init__(self, *args, **kwargs):
            used.append(name)

        def iter_posts(self, include_reels):
            yield from posts
            if error is not None:
                raise error
    return Backend


class BackendChoiceTests(unittest.TestCase):
    def test_backend_order(self):
        self.assertEqual(iv.backend_order("auto", True), ["gallery-dl", "instaloader"])
        self.assertEqual(iv.backend_order("auto", False), ["instaloader"])
        self.assertEqual(iv.backend_order("instaloader", True), ["instaloader"])
        self.assertEqual(iv.backend_order("gallery-dl", True), ["gallery-dl"])

    def run_with(self, gallery_dl, instaloader, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            cookies = Path(tmp) / "cookies.txt"
            cookies.write_text(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\ts\n", encoding="utf-8")
            out = Path(tmp) / "out"
            argv = ["--out", str(out), "--cookies", str(cookies), "--dry-run", *extra]
            lines = []
            with mock.patch.object(iv, "GalleryDlBackend", gallery_dl), \
                    mock.patch.object(iv, "InstaloaderBackend", instaloader), \
                    mock.patch.object(iv, "log", lambda message="": lines.append(message)):
                code = iv.main(argv)
            report = (out / iv.REPORT_FILE).is_file()
        return code, "\n".join(lines), report

    def test_falls_back_when_the_first_backend_finds_nothing(self):
        used = []
        code, output, report = self.run_with(
            fake_backend("gallery-dl", used, error=RuntimeError("Requested user could not be found")),
            fake_backend("instaloader", used, posts=[record("P1", 1, "الوسمة")]))
        self.assertEqual((code, used, report), (0, ["gallery-dl", "instaloader"], True))
        self.assertIn("سنجرب الطريقة الأخرى: instaloader", output)
        self.assertNotIn("--backend", output)  # the other way was tried automatically

    def test_no_fallback_once_posts_were_found(self):
        used = []
        code, output, report = self.run_with(
            fake_backend("gallery-dl", used, posts=[record("P1", 1, "الوسمة")], error=RuntimeError("429")),
            fake_backend("instaloader", used))
        self.assertEqual((code, used, report), (1, ["gallery-dl"], True))
        self.assertIn("سيتم تحميل ما تم العثور عليه", output)

    def test_an_explicit_backend_is_the_only_one_tried(self):
        used = []
        code, output, report = self.run_with(
            fake_backend("gallery-dl", used),
            fake_backend("instaloader", used, error=RuntimeError("429 Too Many Requests")),
            ["--backend", "instaloader"])
        self.assertEqual((code, used, report), (1, ["instaloader"], False))
        self.assertIn("gallery-dl", output)

    def test_error_hints(self):
        blocked = RuntimeError("JSON Query to api/v1/users/web_profile_info/: 429 Too Many Requests")
        text = iv.explain_error(blocked, "instaloader", at_start=True)
        self.assertIn("ليس حظراً", text)
        self.assertNotIn("انتظر", text)
        self.assertIn("gallery-dl", text)
        text = iv.explain_error(blocked, "instaloader", at_start=False)
        self.assertIn("انتظر", text)
        text = iv.explain_error(RuntimeError("HttpError: 429 Too Many Requests"), "gallery-dl",
                                at_start=True, suggest_other=False)
        self.assertIn("انتظر", text)
        self.assertNotIn("--backend", text)


class RunTests(unittest.TestCase):
    def test_end_to_end(self):
        FakeBackend.posts = [
            record("P5", 5, "✨ الوسمة الطبيعية ✨ #الوسمة"),
            record("P4", 4, "صورة للمحل", kind="image"),
            record("P3", 3, "معجون الحنّة + بودرة السدر", kind="carousel",
                   urls=["https://cdn/P3a.mp4", "https://cdn/P3b.mp4"]),
            record("P2", 2, "فيديو بدون اسم منتج"),
            record("P1", 1, "بخاخ الشعر من ناتشورال روتس"),
            record("P0", 1, "الوسمة (منشور مستبعد)"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            (out / "الوسمة الطبيعية لتغطية الشيب").mkdir(parents=True)
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            config["exclude_posts"] = ["https://www.instagram.com/p/P0/"]
            config_path = Path(tmp) / "products.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            argv = ["--config", str(config_path), "--out", str(out)]
            with mock.patch.object(iv, "InstaloaderBackend", FakeBackend), \
                    mock.patch.object(iv, "Downloader", FakeDownloader), \
                    mock.patch.object(iv, "find_cookie_file", lambda: None), \
                    mock.patch("builtins.print"):
                self.assertEqual(iv.main(argv + ["--dry-run"]), 0)
                self.assertEqual(sorted(p.name for p in out.iterdir()),
                                 sorted(["الوسمة الطبيعية لتغطية الشيب", iv.REPORT_FILE]))
                self.assertEqual(iv.main(argv), 0)

            henna = product_named("معجون الحنّة")
            self.assertEqual(sorted(p.name for p in (out / henna).iterdir()),
                             [f"{henna} (1).mp4", f"{henna} (2).mp4"])
            wasma = "الوسمة الطبيعية لتغطية الشيب"
            self.assertEqual([p.name for p in (out / wasma).iterdir()], [f"{wasma}.mp4"])
            spray = product_named("بخاخ")
            self.assertEqual([p.name for p in (out / spray).iterdir()], [f"{spray}.mp4"])
            self.assertFalse((out / product_named("بودرة السدر")).exists())  # "first" mode

            with open(out / iv.REPORT_FILE, encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            statuses = {(r["رابط المنشور"].split("/")[-2], r["اسم الملف"]): r["الحالة"] for r in rows}
            self.assertEqual(statuses[("P5", f"{wasma}.mp4")], "تم التحميل")
            self.assertEqual(statuses[("P2", "")], "لا يطابق أي منتج")
            self.assertEqual(statuses[("P0", "")], "مستبعد (exclude_posts)")
            self.assertNotIn("P4", {key for key, _ in statuses})


@unittest.skipUnless(__import__("importlib").util.find_spec("instaloader"), "instaloader not installed")
class InstaloaderRecordTests(unittest.TestCase):
    def test_records_from_real_instaloader_posts(self):
        import instaloader
        context = instaloader.Instaloader().context
        caption = {"edges": [{"node": {"text": "معجون الحنة"}}]}
        video_post = instaloader.Post(context, {
            "shortcode": "V1", "__typename": "GraphVideo", "is_video": True, "video_url": "https://cdn/v.mp4",
            "taken_at_timestamp": 1700000000, "edge_media_to_caption": caption})
        sidecar = instaloader.Post(context, {
            "shortcode": "S1", "__typename": "GraphSidecar", "is_video": False, "taken_at_timestamp": 1700000000,
            "edge_media_to_caption": caption, "edge_sidecar_to_children": {"edges": [
                {"node": {"is_video": False, "display_url": "https://cdn/i.jpg"}},
                {"node": {"is_video": True, "display_url": "https://cdn/t.jpg", "video_url": "https://cdn/s.mp4"}}]}})
        rec = iv.InstaloaderBackend._record(video_post)
        self.assertEqual((rec.kind, rec.caption, rec.get_videos()), ("video", "معجون الحنة", [(1, "https://cdn/v.mp4")]))
        self.assertEqual(rec.date, dt.datetime(2023, 11, 14, 22, 13, 20))
        rec = iv.InstaloaderBackend._record(sidecar)
        self.assertEqual((rec.kind, rec.get_videos()), ("carousel", [(2, "https://cdn/s.mp4")]))

        logged_in = instaloader.Post.from_iphone_struct(context, {
            "code": "R1", "pk": "1", "media_type": 2, "taken_at": 1700000000, "caption": {"text": "الوسمة"},
            "has_liked": False, "like_count": 0, "video_versions": [{"url": "https://cdn/r.mp4"}],
            "video_duration": 10, "view_count": 1, "image_versions2": {"candidates": [{"url": "https://cdn/r.jpg"}]}})
        rec = iv.InstaloaderBackend._record(logged_in)
        self.assertEqual((rec.kind, rec.caption), ("video", "الوسمة"))


@unittest.skipUnless(__import__("importlib").util.find_spec("instaloader"), "instaloader not installed")
class InstaloaderBackendTests(unittest.TestCase):
    """Runs the backend through instaloader's real paging code with faked Instagram responses."""

    USER = {"id": "123", "username": "naturalroots.store"}

    def fake_network(self, backend, pages):
        context = backend.loader.context
        queries = []

        def doc_id_graphql_query(doc_id, variables, referer=None):
            queries.append(variables.get("after"))
            return pages.pop(0)

        return queries, [
            mock.patch.object(context, "get_json", return_value=pages.pop(0)),
            mock.patch.object(context, "doc_id_graphql_query", doc_id_graphql_query),
            mock.patch.object(context, "graphql_query", side_effect=AssertionError("unexpected request")),
            mock.patch.object(context, "head", side_effect=AssertionError("unexpected request")),
            mock.patch("builtins.print"),
        ]

    def collect(self, backend, pages):
        queries, patches = self.fake_network(backend, pages)
        for patch in patches:
            patch.start()
        try:
            records = list(backend.iter_posts(False))
            return queries, [(r.shortcode, r.kind, r.caption, r.get_videos()) for r in records]
        finally:
            mock.patch.stopall()

    def test_anonymous_profile_with_two_pages(self):
        def node(code, typename, caption, **extra):
            return {"node": {"shortcode": code, "__typename": typename, "is_video": typename == "GraphVideo",
                             "taken_at_timestamp": 1700000000, "display_url": "https://cdn/x.jpg",
                             "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]}, **extra}}

        first = {"count": 3, "page_info": {"has_next_page": True, "end_cursor": "CURSOR"}, "edges": [
            node("V1", "GraphVideo", "الوسمة", video_url="https://cdn/v1.mp4"),
            node("I1", "GraphImage", "صورة")]}
        second = {"count": 3, "page_info": {"has_next_page": False, "end_cursor": None}, "edges": [
            node("S1", "GraphSidecar", "بخاخ الشعر", edge_sidecar_to_children={"edges": [
                {"node": {"is_video": False, "display_url": "https://cdn/a.jpg"}},
                {"node": {"is_video": True, "display_url": "https://cdn/b.jpg", "video_url": "https://cdn/s1.mp4"}}]})]}
        with mock.patch("builtins.print"):
            backend = iv.InstaloaderBackend("naturalroots.store", None, None)
        self.assertEqual(backend.loader.context.max_connection_attempts, 1)
        queries, records = self.collect(backend, [
            {"data": {"user": {**self.USER, "edge_owner_to_timeline_media": first}}},
            {"data": {"user": {"edge_owner_to_timeline_media": second}}}])
        self.assertEqual(backend.loader.context.max_connection_attempts, 3)  # posts came, retry again
        self.assertEqual(queries, ["CURSOR"])
        self.assertEqual(records, [
            ("V1", "video", "الوسمة", [(1, "https://cdn/v1.mp4")]),
            ("I1", "image", "صورة", []),
            ("S1", "carousel", "بخاخ الشعر", [(2, "https://cdn/s1.mp4")])])

    def test_logged_in_with_cookies(self):
        import instaloader
        with mock.patch.object(instaloader.Instaloader, "test_login", return_value=None), \
                mock.patch("builtins.print"):
            backend = iv.InstaloaderBackend("naturalroots.store", {"sessionid": "s", "ds_user_id": "42"}, None)
        self.assertTrue(backend.loader.context.is_logged_in)

        def image(url):
            return {"image_versions2": {"candidates": [{"url": url}]}}

        video = {"code": "R1", "pk": "1", "media_type": 2, "taken_at": 1700000000,
                 "caption": {"text": "معجون السدر"}, "has_liked": False, "like_count": 0,
                 "video_versions": [{"url": "https://cdn/r1.mp4"}], "video_duration": 9, "view_count": 1,
                 **image("https://cdn/r1.jpg")}
        carousel = {"code": "C1", "pk": "2", "media_type": 8, "taken_at": 1700000100, "caption": None,
                    "has_liked": False, "like_count": 0, **image("https://cdn/c1.jpg"), "carousel_media": [
                        {"media_type": 2, "video_versions": [{"url": "https://cdn/c1a.mp4"}],
                         **image("https://cdn/c1a.jpg")},
                        {"media_type": 1, **image("https://cdn/c1b.jpg")}]}
        timeline = {"page_info": {"has_next_page": False, "end_cursor": None},
                    "edges": [{"node": video}, {"node": carousel}]}
        queries, records = self.collect(backend, [
            {"data": {"user": {**self.USER, "edge_owner_to_timeline_media": {"count": 2}}}},
            {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": timeline}}])
        self.assertEqual(queries, [None])
        self.assertEqual(records, [
            ("R1", "video", "معجون السدر", [(1, "https://cdn/r1.mp4")]),
            ("C1", "carousel", "", [(1, "https://cdn/c1a.mp4")])])

    def test_a_429_on_the_first_request_stops_at_once(self):
        import instaloader
        import requests
        with mock.patch("builtins.print"):
            backend = iv.InstaloaderBackend("naturalroots.store", None, None)
        response = requests.Response()
        response.status_code, response.reason, response._content = 429, "Too Many Requests", b""
        response.url = "https://www.instagram.com/api/v1/users/web_profile_info/?username=naturalroots.store"
        context = backend.loader.context
        with mock.patch.object(context._session, "get", return_value=response) as get, \
                mock.patch.object(context, "do_sleep"), \
                mock.patch.object(instaloader.RateController, "sleep", side_effect=AssertionError("waited")), \
                mock.patch("builtins.print"):
            with self.assertRaises(instaloader.ConnectionException) as caught:
                next(backend.iter_posts(False))
        self.assertEqual(get.call_count, 1)
        self.assertIn("429", str(caught.exception))
        self.assertIn("ليس حظراً", iv.explain_error(caught.exception, "instaloader", at_start=True))


@unittest.skipUnless(__import__("importlib").util.find_spec("gallery_dl"), "gallery-dl not installed")
class GalleryDlBackendTests(unittest.TestCase):
    """Runs the backend through gallery-dl's real extractor code with a faked Instagram API."""

    def records(self, max_posts):
        from gallery_dl.extractor import instagram

        def image(url):
            return {"image_versions2": {"candidates": [{"url": url, "width": 1080, "height": 1350}]}}

        def video(url):
            return {"video_versions": [{"url": url, "width": 720, "height": 1280, "type": 101}]}

        user = {"pk": "1", "username": "naturalroots.store", "full_name": "Natural Roots"}
        feed = [
            {"pk": "11", "code": "R1", "taken_at": 1700000200, "caption": {"text": "الوسمة"}, "user": user,
             **image("https://cdn/r1.jpg"), **video("https://cdn/r1.mp4")},
            {"pk": "12", "code": "C1", "taken_at": 1700000100, "caption": None, "user": user,
             **image("https://cdn/c1.jpg"), "carousel_media": [
                 {"pk": "121", **image("https://cdn/c1a.jpg")},
                 {"pk": "122", **image("https://cdn/c1b.jpg"), **video("https://cdn/c1b.mp4")}]},
            {"pk": "13", "code": "P1", "taken_at": 1700000000, "caption": {"text": "صورة"}, "user": user,
             **image("https://cdn/p1.jpg")},
        ]
        pulled = []

        def user_feed(api, handle):
            for post in feed:
                pulled.append(post["code"])
                yield post

        with mock.patch.object(instagram.InstagramAPI, "user_feed", user_feed), \
                mock.patch.object(instagram.InstagramExtractor, "request",
                                  side_effect=AssertionError("unexpected request")), \
                mock.patch("builtins.print"):
            backend = iv.GalleryDlBackend("naturalroots.store", {"sessionid": "s"}, max_posts)
            records = [(r.shortcode, r.kind, r.caption, r.date, r.get_videos())
                       for r in backend.iter_posts(False)]
        return pulled, records

    def test_posts(self):
        pulled, records = self.records(None)
        self.assertEqual(pulled, ["R1", "C1", "P1"])
        self.assertEqual(records, [
            ("R1", "video", "الوسمة", dt.datetime(2023, 11, 14, 22, 16, 40), [(1, "https://cdn/r1.mp4")]),
            ("C1", "carousel", "", dt.datetime(2023, 11, 14, 22, 15), [(2, "https://cdn/c1b.mp4")]),
            ("P1", "image", "صورة", dt.datetime(2023, 11, 14, 22, 13, 20), [])])

    def test_max_posts_stops_paging_early(self):
        pulled, records = self.records(2)
        self.assertEqual(pulled, ["R1", "C1"])
        self.assertEqual([r[0] for r in records], ["R1", "C1"])


class GalleryDlRecordTests(unittest.TestCase):
    def test_records_from_gallery_dl_metadata(self):
        date = dt.datetime(2024, 3, 1, 8)
        items = [{"post_shortcode": "C1", "description": "بودرة السدر", "post_date": date, "num": 1,
                  "sidecar_media_id": 5, "video_url": None},
                 {"post_shortcode": "C1", "description": "بودرة السدر", "post_date": date, "num": 2,
                  "sidecar_media_id": 5, "video_url": "https://cdn/c.mp4"}]
        rec = iv.GalleryDlBackend._record("C1", items)
        self.assertEqual((rec.kind, rec.date, rec.caption, rec.get_videos()),
                         ("carousel", date, "بودرة السدر", [(2, "https://cdn/c.mp4")]))
        rec = iv.GalleryDlBackend._record("R1", [{"description": "", "date": date, "num": 1,
                                                   "video_url": "https://cdn/r.mp4"}])
        self.assertEqual(rec.kind, "video")


if __name__ == "__main__":
    unittest.main()
