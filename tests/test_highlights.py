import csv
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import highlights as hl  # noqa: E402
import insta_videos as iv  # noqa: E402

ROOT = Path(hl.__file__).resolve().parent

# Exactly as it was copied from WhatsApp, with the store owner's spelling.
COPIED = """\
[11:40 am, 02/10/2026] .: نمشي بالنظام السابق ٤ فيديوهات متكررة؟
[11:40 am, 02/10/2026] Reem: https://www.instagram.com/reel/DdkOOy4tV48/?stkn=MXZkMG4yNnQ4bG1m
[11:40 am, 02/10/2026] Reem: هذا لياقه وقف التساقط
للبخاخ
ليرنامج التحول
[11:41 am, 02/10/2026] Reem: لياقه الديتوكس والترميم
[11:41 am, 02/10/2026] Reem: https://www.instagram.com/reel/DdtvtUQPoBD/?stkn=N2E3ZW5zZzV1bnJw
"""


def parse(text, sender=None):
    messages, skipped = hl.pick_messages(hl.read_messages(text), sender)
    items, warnings = hl.collect_items(messages)
    return [(i.number, i.code, i.notes) for i in items], skipped, warnings


class SourceHygieneTests(unittest.TestCase):
    def test_no_invisible_or_bidi_control_characters(self):
        import unicodedata
        for name in ("highlights.py", "tests/test_highlights.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            hidden = sorted({f"U+{ord(c):04X}" for c in text if unicodedata.category(c) == "Cf"})
            self.assertEqual(hidden, [], name)


class ChatTests(unittest.TestCase):
    def test_copied_from_whatsapp(self):
        items, skipped, warnings = parse(COPIED)
        self.assertEqual(items, [
            (1, "DdkOOy4tV48", ["هذا لياقه وقف التساقط", "للبخاخ", "ليرنامج التحول", "لياقه الديتوكس والترميم"]),
            (2, "DdtvtUQPoBD", [])])
        self.assertEqual(skipped, ["رسائل «.» لم تُحسب (1)"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("الرابط رقم 2 ليس بعده أي كلام", warnings[0])

    def test_android_export(self):
        text = (
            "02/10/2026, 11:38 am - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.\n"
            "02/10/2026, 11:39\u202fam - Ahmed: ابعتيلي الفيديوهات https://www.instagram.com/p/MINE/\n"
            "02/10/2026, 11:40\u202fam - Reem: https://www.instagram.com/p/AAA111/ لباقة وقف التساقط\n"
            "و للبخاخ\n"
            "02/10/2026, 11:41\u202fam - Reem: <Media omitted>\n"
            "02/10/2026, 11:42\u202fam - Reem: https://instagram.com/reel/BBB222?igsh=xyz\n"
            "https://www.Instagram.com/reels/CCC333/\n"
            "- للشامبو\n"
            "02/10/2026, 11:43\u202fam - Reem: This message was deleted\n"
            "02/10/2026, 11:44\u202fam - Reem: https://www.instagram.com/p/AAA111/\n"
            "وكمان لبرنامج التحول <This message was edited>\n"
            "02/10/2026, 11:45\u202fam - Reem: https://www.instagram.com/naturalroots.store/\n"
            "02/10/2026, 11:46\u202fam - Reem: https://www.instagram.com/share/reel/BAxyz123/\n"
            "للبلسم\n")
        items, skipped, warnings = parse(text)
        self.assertEqual(items, [
            (1, "AAA111", ["لباقة وقف التساقط", "و للبخاخ", "وكمان لبرنامج التحول"]),
            (2, "BBB222", []),
            (3, "CCC333", ["للشامبو"]),
            (4, "", ["للبلسم"])])
        self.assertEqual(skipped, ["رسائل «Ahmed» لم تُحسب (1)"])
        self.assertTrue(any("مكرر" in w and "رقم 1" in w for w in warnings))
        self.assertTrue(any("naturalroots.store" in w for w in warnings))
        self.assertTrue(any("رقم 2 ليس بعده" in w for w in warnings))

    def test_arabic_android_export(self):
        date = "\u200f٢\u200f/١٠\u200f/٢٠٢٦"
        text = (f"{date}، ١١:٤٠ ص - ريم: https://www.instagram.com/reel/DDD444/\n"
                f"{date}، ١١:٤١ ص - ريم: لباقة الديتوكس\n"
                f"{date}، ١١:٤٢ ص - أحمد: تمام\n")
        items, skipped, _ = parse(text)
        self.assertEqual(items, [(1, "DDD444", ["لباقة الديتوكس"])])
        self.assertEqual(skipped, ["رسائل «أحمد» لم تُحسب (1)"])

    def test_copied_on_a_phone_and_text_before_a_link(self):
        text = ("[2/10, 11:40 am] Reem: https://www.instagram.com/reel/HHH888/ لباقة وقف التساقط\n"
                "[2/10, 11:41 am] Reem: وللشامبو https://www.instagram.com/reel/III999/ للبلسم\n")
        items, _, warnings = parse(text)
        self.assertEqual(items, [(1, "HHH888", ["لباقة وقف التساقط", "وللشامبو"]), (2, "III999", ["للبلسم"])])
        self.assertEqual(warnings, ["«وللشامبو» مكتوب قبل الرابط رقم 2 في نفس السطر، فحُسب للرابط رقم 1: تأكد أنه له"])

    def test_iphone_export(self):
        text = ("[02/10/2026, 11:40:12 AM] Reem: https://www.instagram.com/reel/EEE555/\n"
                "\u200e[02/10/2026, 11:40:30 AM] Reem: \u200eimage omitted\n"
                "[02/10/2026, 11:41:00 AM] Reem: لماسك البروتين\n")
        items, _, _ = parse(text)
        self.assertEqual(items, [(1, "EEE555", ["لماسك البروتين"])])

    def test_plain_text(self):
        text = ("الفيديوهات:\n"
                "https://www.instagram.com/reel/FFF666/\nلباقة كذا\n"
                "https://www.instagram.com/p/GGG777/ للمنتج كذا\n")
        items, skipped, warnings = parse(text)
        self.assertEqual(items, [(1, "FFF666", ["لباقة كذا"]), (2, "GGG777", ["للمنتج كذا"])])
        self.assertEqual(skipped, [])
        self.assertEqual(warnings, ["كلام قبل أول رابط لم يُحسب: الفيديوهات"])

    def test_sender_option(self):
        items, skipped, _ = parse(COPIED, sender="ree")
        self.assertEqual([i[1] for i in items], ["DdkOOy4tV48", "DdtvtUQPoBD"])
        with self.assertRaises(iv.UserError) as caught:
            parse(COPIED, sender="Mona")
        self.assertIn("Reem", str(caught.exception))

    def test_text_encodings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.txt"
            text = COPIED.replace("٤", "4")  # Windows' Arabic "ANSI" has no Arabic-Indic digits
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1256"):
                path.write_bytes(text.encode(encoding))
                self.assertEqual(hl.read_text(path), text, encoding)

    def test_file_names(self):
        self.assertEqual(hl.file_names(7, 2, [1]), {1: "HL-07.mp4"})
        self.assertEqual(hl.file_names(12, 2, [3, 1]), {1: "HL-12-1.mp4", 3: "HL-12-2.mp4"})
        self.assertEqual(hl.file_names(5, 3, [1]), {1: "HL-005.mp4"})


class FakeGalleryDl(iv.GalleryDlBackend):
    """gallery-dl with Instagram replaced by `posts`: link -> metadata of the post's files, or an error."""
    posts = {}
    opened = []

    def __init__(self, profile, cookies, max_posts=None):
        assert cookies["sessionid"]

    def _extract(self, url):
        FakeGalleryDl.opened.append(url)
        value = FakeGalleryDl.posts.get(url, [])
        return ([], value) if isinstance(value, Exception) else (value, None)


class FakeDownloader:
    calls = []

    def fetch(self, url, dest):
        FakeDownloader.calls.append(url)
        dest.write_bytes(url.encode())


class NotFoundError(Exception):
    pass


class AbortExtraction(Exception):
    pass


def meta(code, num=1, video=True, album=False, caption="وصف المنشور"):
    item = {"post_shortcode": code, "num": num, "description": caption, "post_date": dt.datetime(2026, 9, 1, 10),
            "video_url": f"https://cdn/{code}_{num}.mp4" if video else None}
    if album:
        item["sidecar_media_id"] = 9
    return item


def post_url(code):
    return f"https://www.instagram.com/p/{code}/"


def chat(*lines):
    return "".join(f"[10:0{i % 10} am, 02/10/2026] Reem: {line}\n" for i, line in enumerate(lines))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.library = base / "Roots content"
        (self.library / "منتج").mkdir(parents=True)
        (self.library / "منتج" / "منتج.mp4").write_text("library LIB1")
        (self.library / iv.STATE_FILE).write_text(json.dumps({"version": 1, "folders": {"منتج": {
            "LIB1_1": {"file": "منتج.mp4", "date": "2026-09-01T10:00:00", "post": post_url("LIB1"), "keyword": "k"}}}},
            ensure_ascii=False), encoding="utf-8")
        self.config = base / "products.json"
        self.config.write_text(json.dumps({"profile": "x", "output_dir": str(self.library),
                                           "products": [{"name": "منتج"}]}, ensure_ascii=False), encoding="utf-8")
        self.cookies = base / "cookies.txt"
        self.cookies.write_text(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tx\n")
        self.chat = base / "chat.txt"
        self.root = base / hl.DEFAULT_OUT
        self.upload = self.root / hl.UPLOAD_DIR
        FakeGalleryDl.posts = {
            post_url("AAA"): [meta("AAA")],
            post_url("CAR"): [meta("CAR", 1, album=True), meta("CAR", 2, video=False, album=True),
                              meta("CAR", 3, album=True)],
            post_url("IMG"): [meta("IMG", video=False)],
            post_url("LIB1"): [meta("LIB1")],
            post_url("GONE"): NotFoundError("Requested post could not be found"),
            post_url("NEW"): [meta("NEW")],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, text, *extra):
        self.chat.write_text(text, encoding="utf-8")
        FakeGalleryDl.opened, FakeDownloader.calls = [], []
        out = []
        with mock.patch.object(iv, "GalleryDlBackend", FakeGalleryDl), \
                mock.patch.object(iv, "Downloader", FakeDownloader), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = hl.main([str(self.chat), "--config", str(self.config), "--cookies", str(self.cookies), *extra])
        return code, "\n".join(out)

    def files(self, folder=None):
        folder = folder or self.upload
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def content(self, name):
        return (self.upload / name).read_text()

    def rows(self):
        with open(self.root / hl.LIST_FILE, encoding="utf-8-sig", newline="") as fh:
            return [(r["رقم"], r["اسم الملف"], r["الحالة"]) for r in csv.DictReader(fh)]

    def prompt(self):
        return (self.root / hl.PROMPT_FILE).read_text(encoding="utf-8-sig")

    FIRST = ("https://www.instagram.com/reel/AAA/ لباقة وقف التساقط",
             "https://www.instagram.com/p/CAR/?igsh=1\nللبخاخ",
             "https://www.instagram.com/p/IMG/ للشامبو",
             "https://www.instagram.com/reel/LIB1/ لبرنامج التحول",
             "https://www.instagram.com/reel/GONE/ للبلسم")

    def test_downloads_resumes_and_follows_changes_to_the_chat(self):
        code, output = self.run_script(chat(*self.FIRST))
        self.assertEqual(code, 1)  # one link failed
        self.assertEqual(FakeGalleryDl.opened, [post_url(c) for c in ("AAA", "CAR", "IMG", "LIB1", "GONE")])
        self.assertEqual(FakeDownloader.calls, ["https://cdn/AAA_1.mp4", "https://cdn/CAR_1.mp4",
                                                "https://cdn/CAR_3.mp4"])
        self.assertEqual(self.files(), ["HL-01.mp4", "HL-02-1.mp4", "HL-02-2.mp4", "HL-04.mp4"])
        self.assertEqual(self.content("HL-02-2.mp4"), "https://cdn/CAR_3.mp4")
        self.assertEqual(self.content("HL-04.mp4"), "library LIB1")  # copied, not downloaded
        rows = self.rows()
        self.assertEqual([r[:2] for r in rows], [("1", "HL-01.mp4"), ("2", "HL-02-1.mp4"), ("2", "HL-02-2.mp4"),
                                                 ("3", ""), ("4", "HL-04.mp4"), ("5", "")])
        self.assertEqual(rows[3][2], "المنشور ليس فيه فيديو")
        self.assertEqual(rows[4][2], "نُسخ من مجلد المنتجات")
        self.assertIn("Requested post could not be found", rows[5][2])
        prompt = self.prompt()
        self.assertIn("HL-01: لباقة وقف التساقط\n", prompt)
        self.assertIn("HL-02-1 و HL-02-2: للبخاخ\n", prompt)
        self.assertIn("HL-04: لبرنامج التحول\n", prompt)
        self.assertIn("4 فيديو جديد", prompt)
        self.assertIn("أقصى حاجة 4 فيديوهات", prompt)
        self.assertIn("«كل الباقات» أو «الباقات كلها» يبقى الفيديو لكل الباقات والبرامج", prompt)
        self.assertNotIn("HL-03", prompt)
        self.assertNotIn("HL-05", prompt)
        self.assertIn("لم يكتمل: 1", output)

        # The post is back: only that link is opened again.
        FakeGalleryDl.posts[post_url("GONE")] = [meta("GONE")]
        code, _ = self.run_script(chat(*self.FIRST), "--per-circle", "3")
        self.assertEqual(code, 0)
        self.assertEqual(FakeGalleryDl.opened, [post_url("GONE")])
        self.assertEqual(FakeDownloader.calls, ["https://cdn/GONE_1.mp4"])
        self.assertEqual(self.files(), ["HL-01.mp4", "HL-02-1.mp4", "HL-02-2.mp4", "HL-04.mp4", "HL-05.mp4"])
        self.assertIn("HL-05: للبلسم\n", self.prompt())
        self.assertIn("أقصى حاجة 3 فيديوهات", self.prompt())

        # The first link leaves the chat and one is added at the end: the others are renumbered.
        changed = self.FIRST[1:] + ("https://www.instagram.com/reel/NEW/ للديودرنت",)
        code, output = self.run_script(chat(*changed))
        self.assertEqual(code, 0)
        self.assertEqual(FakeGalleryDl.opened, [post_url("NEW")])
        self.assertEqual(FakeDownloader.calls, ["https://cdn/NEW_1.mp4"])
        self.assertEqual(self.files(), ["HL-01-1.mp4", "HL-01-2.mp4", "HL-03.mp4", "HL-04.mp4", "HL-05.mp4"])
        self.assertEqual(self.content("HL-01-1.mp4"), "https://cdn/CAR_1.mp4")
        self.assertEqual(self.content("HL-03.mp4"), "library LIB1")
        self.assertEqual(self.content("HL-04.mp4"), "https://cdn/GONE_1.mp4")
        self.assertEqual(self.content("HL-05.mp4"), "https://cdn/NEW_1.mp4")
        self.assertEqual(self.files(self.root / hl.REMOVED_DIR), ["HL-01.mp4"])
        self.assertIn("تغيّرت أرقام 4 فيديو", output)
        self.assertIn("HL-05: للديودرنت\n", self.prompt())

    def test_files_the_script_did_not_make_are_never_touched(self):
        self.run_script(chat(self.FIRST[0]))
        (self.upload / "HL-02.mp4").write_text("user file")
        code, output = self.run_script(chat(self.FIRST[0], "https://www.instagram.com/reel/NEW/ x"))
        self.assertEqual(code, 2)
        self.assertIn("HL-02.mp4", output)
        self.assertEqual(self.content("HL-02.mp4"), "user file")
        self.assertEqual(self.files(), ["HL-01.mp4", "HL-02.mp4"])

    def test_a_login_problem_stops_at_once(self):
        FakeGalleryDl.posts[post_url("AAA")] = AbortExtraction(
            "HTTP redirect to login page (https://www.instagram.com/accounts/login/)")
        code, output = self.run_script(chat(*self.FIRST))
        self.assertEqual(code, 1)
        self.assertEqual(FakeGalleryDl.opened, [post_url("AAA")])
        self.assertIn("cookies.txt", output)
        self.assertEqual([r[2] for r in self.rows()][1:], ["لم يُفتح (توقف التشغيل قبله)"] * 4)
        self.assertFalse((self.root / hl.PROMPT_FILE).exists())

    def test_three_unexplained_failures_in_a_row_stop(self):
        for n in range(5):
            FakeGalleryDl.posts[post_url(f"X{n}")] = RuntimeError("KeyError: 'items'")
        code, _ = self.run_script(chat(*[f"https://www.instagram.com/reel/X{n}/ y" for n in range(5)]))
        self.assertEqual(code, 1)
        self.assertEqual(len(FakeGalleryDl.opened), 3)

    def test_deleted_posts_do_not_stop_the_others(self):
        gone = [f"https://www.instagram.com/reel/GONE{n}/ y" for n in range(4)]
        for n in range(4):
            FakeGalleryDl.posts[post_url(f"GONE{n}")] = NotFoundError("Requested post could not be found")
        code, _ = self.run_script(chat(*gone, self.FIRST[0]))
        self.assertEqual(code, 1)
        self.assertEqual(len(FakeGalleryDl.opened), 5)
        self.assertEqual(self.files(), ["HL-05.mp4"])

    def test_a_share_link_to_a_post_already_listed(self):
        FakeGalleryDl.posts["https://www.instagram.com/share/reel/BAzz/"] = [meta("AAA")]
        text = chat("https://www.instagram.com/reel/AAA/ للبخاخ", "https://instagram.com/share/reel/BAzz/ وللشامبو")
        code, _ = self.run_script(text)
        self.assertEqual(code, 0)
        self.assertEqual(self.files(), ["HL-01.mp4"])
        self.assertEqual(self.rows()[1], ("2", "", "نفس فيديو الرابط رقم 1"))
        self.assertIn("HL-01: للبخاخ / وللشامبو\n", self.prompt())
        code, _ = self.run_script(text)  # the share link is not opened again
        self.assertEqual((code, FakeGalleryDl.opened), (0, []))

    def test_dry_run_reads_the_chat_only(self):
        code, output = self.run_script(chat(*self.FIRST), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(FakeGalleryDl.opened, [])
        self.assertFalse(self.upload.exists())
        self.assertEqual(len(self.rows()), 5)
        self.assertIn("05  GONE  ←  للبلسم", output)

    def test_cookies_are_needed_to_open_links(self):
        self.chat.write_text(chat(self.FIRST[0]), encoding="utf-8")
        with mock.patch.object(iv, "find_cookie_file", lambda: None), mock.patch("builtins.print") as printed:
            code = hl.main([str(self.chat), "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertIn("cookies.txt", str(printed.call_args_list[-1]))


@unittest.skipUnless(importlib.util.find_spec("gallery_dl"), "gallery-dl not installed")
class GalleryDlLinkTests(unittest.TestCase):
    """Opens links through gallery-dl's real extractor code with a faked Instagram API."""

    def test_post_and_share_links(self):
        from gallery_dl.extractor import instagram

        def image(url):
            return {"image_versions2": {"candidates": [{"url": url, "width": 1080, "height": 1350}]}}

        def video(url):
            return {"video_versions": [{"url": url, "width": 720, "height": 1280, "type": 101}]}

        user = {"pk": "1", "username": "naturalroots.store", "full_name": "Natural Roots"}
        posts = {
            "R1": {"pk": "11", "code": "R1", "taken_at": 1700000200, "caption": {"text": "الوسمة"}, "user": user,
                   **image("https://cdn/r1.jpg"), **video("https://cdn/r1.mp4")},
            "C1": {"pk": "12", "code": "C1", "taken_at": 1700000100, "caption": None, "user": user,
                   **image("https://cdn/c1.jpg"), "carousel_media": [
                       {"pk": "121", **image("https://cdn/c1a.jpg")},
                       {"pk": "122", **image("https://cdn/c1b.jpg"), **video("https://cdn/c1b.mp4")}]},
        }
        asked = []

        def media(api, shortcode):
            asked.append(shortcode)
            yield posts[shortcode]

        def request_location(extractor, url, **kwargs):
            asked.append(url)
            return "https://www.instagram.com/reel/R1/"

        with mock.patch.object(instagram.InstagramAPI, "media", media), \
                mock.patch.object(instagram.InstagramPostExtractor, "request_location", request_location), \
                mock.patch.object(instagram.InstagramExtractor, "request",
                                  side_effect=AssertionError("unexpected request")), \
                mock.patch("builtins.print"):
            backend = iv.GalleryDlBackend("", {"sessionid": "s"})
            album, error = hl.open_link(backend, hl.Item(1, "https://www.instagram.com/p/C1/?igsh=x", "C1"))
            self.assertIsNone(error)
            shared, error = hl.open_link(backend, hl.Item(2, "instagram.com/share/reel/BAxyz/", ""))
            self.assertIsNone(error)
        self.assertEqual((album.shortcode, album.kind, album.get_videos()),
                         ("C1", "carousel", [(2, "https://cdn/c1b.mp4")]))
        self.assertEqual((shared.shortcode, shared.kind, shared.caption, shared.get_videos()),
                         ("R1", "video", "الوسمة", [(1, "https://cdn/r1.mp4")]))
        self.assertEqual(asked, ["C1", "https://www.instagram.com/share/reel/BAxyz/", "R1"])


if __name__ == "__main__":
    unittest.main()
