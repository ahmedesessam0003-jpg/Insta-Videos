#!/usr/bin/env python3
"""
Download the product videos of an Instagram profile into per-product folders.

The profile's posts (and optionally its reels tab) are scanned, and a video is
kept only when its caption mentions one of the products listed in
products.json. Part of the name is enough, e.g. "الوسمة" or "بخاخ الشعر".
Each video is saved inside its product's folder and named after that folder.
When a product has several videos they are numbered "(1)", "(2)", ... by date.

Usage:  python insta_videos.py --help
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import getpass
import itertools
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = ".insta-videos-state.json"
REPORT_FILE = "videos_report.csv"
COOKIE_FILES = ("cookies.txt", "cookies.json")
# Instagram's CDN throttles video downloads for full browser user agents;
# gallery-dl uses this bare one for the same reason.
VIDEO_USER_AGENT = "Mozilla/5.0"


class UserError(Exception):
    """An error whose message is meant for the user (printed without a traceback)."""


def log(message: str = "") -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Arabic-aware text matching
# ---------------------------------------------------------------------------

# Harakat, shadda, superscript alef, Quranic marks and tatweel.
_DIACRITICS = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
# Zero-width and bidi control characters that often hide inside captions.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_CHAR_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ة": "ه", "ى": "ي", "ی": "ي", "ئ": "ي", "ؤ": "و", "ک": "ك",
    "ء": None,
    "_": " ",  # hashtags: #معجون_الحنة -> معجون الحنه
    **{chr(0x0660 + d): str(d) for d in range(10)},  # Arabic-Indic digits
    **{chr(0x06F0 + d): str(d) for d in range(10)},  # Persian digits
})
_WORD = re.compile(r"[^\W_]+")

_CONJUNCTIONS = "وف"
_PREPOSITIONS = "بكل"
# Attached pronouns allowed after a keyword word: شفايفك، شعري، بلسمها ...
_SUFFIXES = ("", "ي", "ك", "كي", "كم", "كن", "ها", "هم", "هن", "نا")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _INVISIBLE.sub("", _DIACRITICS.sub("", text))
    return text.translate(_CHAR_MAP)


def tokenize(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def _without_al(word: str) -> set[str]:
    forms = {word}
    if word.startswith("ال") and len(word) >= 5:
        forms.add(word[2:])
    return forms


def caption_word_forms(word: str) -> set[str]:
    """All readings of a caption word once attached prefixes are removed.

    "والوسمه" -> {"والوسمه", "الوسمه", "وسمه"}, "للشعر" -> {..., "شعر"}.
    """
    bases = [word]
    if len(word) > 3 and word[0] in _CONJUNCTIONS:
        bases.append(word[1:])
    for base in list(bases):
        if len(base) > 3 and base[0] in _PREPOSITIONS:
            bases.append(base[1:])
        if len(base) > 4 and base.startswith("لل"):  # ل + ال
            bases.append(base[2:])
    forms: set[str] = set()
    for base in bases:
        forms |= _without_al(base)
    return forms


def _word_matches(keyword_forms: frozenset[str], caption_forms: set[str]) -> bool:
    return any(form.startswith(k) and form[len(k):] in _SUFFIXES
               for form in caption_forms for k in keyword_forms)


@dataclass(frozen=True)
class Keyword:
    text: str
    words: tuple[frozenset[str], ...]


def compile_keyword(text: str) -> Keyword | None:
    tokens = tokenize(text)
    if not tokens:
        return None
    return Keyword(text.strip(), tuple(frozenset(_without_al(t)) for t in tokens))


def find_keyword(keyword: Keyword, caption: list[set[str]]) -> int | None:
    """Index of the first caption word where `keyword` starts, or None."""
    size = len(keyword.words)
    for start in range(len(caption) - size + 1):
        if all(_word_matches(keyword.words[i], caption[start + i]) for i in range(size)):
            return start
    return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class Product:
    name: str
    keywords: list[Keyword]
    folder: str = ""  # actual folder name on disk, filled by resolve_folders()


@dataclass(frozen=True)
class Match:
    product: Product
    keyword: str
    position: int


def match_caption(caption: str, products: list[Product], mode: str = "first") -> list[Match]:
    """Products mentioned in `caption`, earliest mention first.

    In "first" mode only the product mentioned first is returned; a tie at the
    same word goes to the longer keyword ("بلسم الشفايف" beats "بلسم").
    """
    words = [caption_word_forms(t) for t in tokenize(caption)]
    found = []
    for order, product in enumerate(products):
        best = None
        for keyword in product.keywords:
            position = find_keyword(keyword, words)
            if position is None:
                continue
            rank = (position, -len(keyword.words))
            if best is None or rank < best[0]:
                best = (rank, keyword)
        if best is not None:
            rank, keyword = best
            found.append((rank, order, Match(product, keyword.text, rank[0])))
    found.sort(key=lambda item: item[:2])
    matches = [match for _, _, match in found]
    return matches[:1] if mode == "first" else matches


_SHORTCODE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)")
_USERNAME = re.compile(r"instagram\.com/([\w.]+)")


def to_shortcode(value: str) -> str:
    """Accept either a post link or a bare shortcode."""
    found = _SHORTCODE.search(value)
    return found.group(1) if found else value.strip().strip("/")


def to_username(value: str) -> str:
    """Accept either a profile link or a bare username."""
    found = _USERNAME.search(value)
    return (found.group(1) if found else value).strip().strip("/").lstrip("@")


@dataclass
class Config:
    profile: str
    output_dir: Path
    match_mode: str
    number_format: str
    exclude_posts: set[str]
    products: list[Product]
    unmatched_folder: str = ""  # folder for videos that match no product ("" = skip them)


DEFAULT_UNMATCHED_FOLDER = "من غير وصف"


def load_config(path: Path) -> Config:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise UserError(f"ملف الإعدادات غير موجود: {path}") from None
    except json.JSONDecodeError as exc:
        raise UserError(f"خطأ في صياغة ملف الإعدادات {path.name} (سطر {exc.lineno}): {exc.msg}") from None

    def as_list(value) -> list:
        return [value] if isinstance(value, str) else list(value or [])

    products, seen = [], set()
    for item in as_list(data.get("products")):
        if isinstance(item, str):  # a bare name is allowed as a shorthand
            item = {"name": item}
        if not isinstance(item, dict):
            raise UserError(f"عنصر غير صالح في products داخل {path.name}: {item!r}")
        name = str(item.get("name", "")).strip()
        if not name:
            raise UserError(f"يوجد منتج بدون اسم في {path.name}")
        if name in seen:
            raise UserError(f"اسم المنتج مكرر في {path.name}: {name}")
        seen.add(name)
        # Default keyword: the first two words of the name, e.g. "بودرة السدر".
        texts = as_list(item.get("keywords")) or [" ".join(name.split()[:2])]
        keywords = [k for k in (compile_keyword(str(t)) for t in texts) if k]
        if not keywords:
            raise UserError(f"لا توجد كلمات بحث صالحة للمنتج: {name}")
        products.append(Product(name, keywords))
    if not products:
        raise UserError(f"لا توجد منتجات في {path.name}")

    output_dir = Path(os.path.expandvars(str(data.get("output_dir") or "videos"))).expanduser()
    if not output_dir.is_absolute():
        output_dir = path.parent / output_dir
    unmatched = data.get("unmatched_folder", DEFAULT_UNMATCHED_FOLDER)
    return Config(
        profile=to_username(str(data.get("profile") or "")),
        output_dir=output_dir,
        match_mode=data.get("match_mode", "first"),
        number_format=data.get("number_format", "{name} ({n})"),
        exclude_posts={to_shortcode(str(p)) for p in as_list(data.get("exclude_posts"))},
        products=products,
        unmatched_folder=str(unmatched).strip() if unmatched else "",
    )


# ---------------------------------------------------------------------------
# Folders and file names
# ---------------------------------------------------------------------------

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """Make `name` usable as a Windows file/folder name."""
    name = re.sub(r"\s+", " ", _INVALID_CHARS.sub(" ", name)).strip().rstrip(". ")
    return name or "video"


def name_key(name: str) -> str:
    return " ".join(tokenize(name))


def resolve_folders(products: list[Product], out_dir: Path) -> list[str]:
    """Pick each product's folder, preferring folders that already exist.

    Existing folders are matched while ignoring diacritics, alef/ta-marbuta
    variants and spacing (then by close similarity), so that files get
    exactly the same name as the folder on disk. Returns messages to print.
    """
    existing = sorted(p.name for p in out_dir.iterdir()
                      if p.is_dir() and not p.name.startswith(".")) if out_dir.is_dir() else []
    by_key: dict[str, str] = {}
    for folder in existing:
        by_key.setdefault(name_key(folder), folder)
    used: set[str] = set()
    for product in products:
        folder = by_key.get(name_key(product.name))
        if folder and folder not in used:
            product.folder = folder
            used.add(folder)

    messages = []
    for product in products:
        if product.folder:
            messages.append(f"  ✓ مجلد موجود: {product.folder}")
            continue
        key = name_key(product.name)
        candidates = [(difflib.SequenceMatcher(None, key, name_key(f)).ratio(), f)
                      for f in existing if f not in used]
        ratio, folder = max(candidates, default=(0.0, ""))
        if ratio >= 0.85:
            messages.append(f"  ~ مجلد موجود (اسم قريب): {folder}  ←  {product.name}")
        else:
            folder = safe_name(product.name)
            messages.append(f"  + مجلد جديد: {folder}")
        product.folder = folder
        used.add(folder)
    return messages


def resolve_unmatched_folder(name: str, products: list[Product], out_dir: Path) -> str:
    """The folder for videos that match no product, reusing an existing one with the same name."""
    key = name_key(name)
    if key in {name_key(p.folder) for p in products}:
        raise UserError(f"اسم unmatched_folder «{name}» هو نفس اسم مجلد منتج؛ اختر اسماً آخر.")
    if out_dir.is_dir():
        for path in sorted(out_dir.iterdir()):
            if path.is_dir() and name_key(path.name) == key:
                return path.name
    return safe_name(name)


def plan_names(base: str, count: int, taken: set[str], number_format: str) -> list[str]:
    """File names for `count` videos of one product, oldest first.

    A single video gets the plain name; several videos are numbered.
    Names in `taken` (lower-case) belong to other files and are skipped.
    """
    if count == 1 and f"{base}.mp4".lower() not in taken:
        return [f"{base}.mp4"]
    names: list[str] = []
    for n in itertools.count(1):
        if len(names) == count:
            break
        name = safe_name(number_format.format(name=base, n=n)) + ".mp4"
        if name.lower() not in taken and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Instagram access
# ---------------------------------------------------------------------------

@dataclass
class PostRecord:
    shortcode: str
    date: dt.datetime  # UTC, naive
    caption: str
    kind: str  # "video", "carousel" or "image"
    get_videos: Callable[[], list[tuple[int, str]]]  # [(position in post, url)]

    @property
    def url(self) -> str:
        return f"https://www.instagram.com/p/{self.shortcode}/"


def _utc_naive(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(tzinfo=None)
    try:
        return _utc_naive(dt.datetime.fromisoformat(str(value)))
    except ValueError:
        return dt.datetime(1970, 1, 1)


def parse_cookie_file(path: Path) -> dict[str, str]:
    """Read Instagram cookies from a Netscape cookies.txt or a JSON export."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise UserError(f"تعذر قراءة ملف الكوكيز {path}: {exc}") from None
    cookies: dict[str, str] = {}
    if text.lstrip()[:1] in ("[", "{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise UserError(f"ملف الكوكيز {path.name} ليس JSON صالحاً") from None
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        for item in data:
            if isinstance(item, dict) and item.get("name") and \
                    "instagram" in str(item.get("domain", "instagram.com")):
                cookies[str(item["name"])] = str(item.get("value", ""))
    else:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and "instagram" in parts[0]:
                cookies[parts[5]] = parts[6]
    return cookies


def browser_cookies(spec: str) -> dict[str, str]:
    """Instagram cookies read straight from a browser ("firefox", "chrome:Profile 1")."""
    try:
        from gallery_dl import cookies as gdl_cookies
    except ImportError:
        raise UserError("مكتبة gallery-dl غير مثبتة. نفّذ: pip install -r requirements.txt") from None
    browser, _, profile = spec.partition(":")
    try:
        jar = gdl_cookies.load_cookies((browser.lower(), profile or None, None, None, ".instagram.com"))
    except Exception as exc:
        raise UserError(f"تعذر قراءة الكوكيز من المتصفح {browser}: {exc}\n"
                        "  جرّب تصدير الكوكيز إلى ملف cookies.txt بدلاً من ذلك (انظر README).") from None
    return {c.name: c.value for c in jar if "instagram.com" in (c.domain or "")}


class InstaloaderBackend:
    """Scans the profile with instaloader (works anonymously, with cookies or with a login)."""

    # Until the first post arrives a failed request is not retried: Instagram now
    # answers instaloader's first request with "429 Too Many Requests", and
    # instaloader would wait about 11 minutes before each retry for nothing.
    FIRST_ATTEMPTS, LATER_ATTEMPTS = 1, 3

    def __init__(self, profile: str, cookies: dict[str, str] | None, login: str | None):
        try:
            import instaloader
        except ImportError:
            raise UserError("مكتبة instaloader غير مثبتة. نفّذ: pip install -r requirements.txt") from None
        self.il = instaloader
        self.profile = profile
        self.loader = instaloader.Instaloader(
            download_pictures=False, download_videos=False, download_video_thumbnails=False,
            download_geotags=False, download_comments=False, save_metadata=False,
            compress_json=False, max_connection_attempts=self.FIRST_ATTEMPTS, request_timeout=60)
        if cookies:
            self._use_cookies(cookies)
        elif login:
            self._login(login)
        else:
            log("• التصفح بدون تسجيل دخول (قد يوقفه إنستجرام بعد أول المنشورات؛ "
                "الأفضل استخدام ملف الكوكيز cookies.txt)")

    def _use_cookies(self, cookies: dict[str, str]) -> None:
        self.loader.context.update_cookies(cookies)
        username = self.loader.test_login()
        if username:
            log(f"✓ تم تسجيل الدخول بالكوكيز (الحساب: {username})")
        else:
            log("! تعذر التأكد من صلاحية الكوكيز، سنحاول المتابعة بها على أي حال")
        # Any non-empty name makes instaloader use its logged-in API calls with these cookies.
        self.loader.context.username = username or cookies.get("ds_user_id") or "cookies"

    def _login(self, username: str) -> None:
        il, loader = self.il, self.loader
        try:
            loader.load_session_from_file(username)
            if loader.test_login():
                log(f"✓ تم استخدام الجلسة المحفوظة للحساب {username}")
                return
        except FileNotFoundError:
            pass
        for _ in range(3):
            password = getpass.getpass(f"كلمة سر إنستجرام للحساب {username} (لن تظهر أثناء الكتابة): ")
            try:
                loader.login(username, password)
            except il.TwoFactorAuthRequiredException:
                self._two_factor()
            except il.BadCredentialsException:
                log("✗ كلمة السر غير صحيحة، حاول مرة أخرى.")
                continue
            except il.LoginException as exc:
                raise UserError(f"فشل تسجيل الدخول: {exc}\n"
                                "  افتح إنستجرام من المتصفح وأكّد أن محاولة الدخول منك، "
                                "أو استخدم ملف الكوكيز cookies.txt بدلاً من كلمة السر.") from None
            break
        else:
            raise UserError("فشل تسجيل الدخول بعد 3 محاولات.")
        loader.save_session_to_file()
        log(f"✓ تم تسجيل الدخول وحفظ الجلسة للحساب {username} (لن تُطلب كلمة السر مرة أخرى)")

    def _two_factor(self) -> None:
        for _ in range(3):
            code = input("اكتب كود التحقق الثنائي (2FA): ").strip()
            try:
                self.loader.two_factor_login(code)
                return
            except self.il.BadCredentialsException:
                log("✗ الكود غير صحيح، حاول مرة أخرى.")
        raise UserError("فشل التحقق الثنائي بعد 3 محاولات.")

    def iter_posts(self, include_reels: bool) -> Iterator[PostRecord]:
        try:
            profile = self.il.Profile.from_username(self.loader.context, self.profile)
        except self.il.ProfileNotExistsException:
            raise UserError(f"الحساب {self.profile} غير موجود على إنستجرام.") from None
        log(f"• الحساب: {profile.username} (عدد المنشورات: {profile.mediacount})")
        seen = set()
        sources = [profile.get_posts]
        if include_reels:
            sources.append(profile.get_reels)
        for source in sources:
            for post in source():
                # Posts are coming through, so from now on a 429 is a real rate limit worth waiting for.
                self.loader.context.max_connection_attempts = self.LATER_ATTEMPTS
                if post.shortcode not in seen:
                    seen.add(post.shortcode)
                    yield self._record(post)

    @staticmethod
    def _record(post) -> PostRecord:
        typename = post.typename or ""
        kind = "carousel" if typename.endswith("Sidecar") else "video" if post.is_video else "image"

        def videos() -> list[tuple[int, str]]:
            if kind == "carousel":
                found = [(i, node.video_url) for i, node in enumerate(post.get_sidecar_nodes(), 1)
                         if node.is_video and node.video_url]
                if found:
                    return found
            if post.is_video and post.video_url:
                return [(1, post.video_url)]
            return []

        return PostRecord(post.shortcode, _utc_naive(post.date_utc), post.caption or "", kind, videos)


class GalleryDlBackend:
    """Scans the profile with gallery-dl (needs cookies of a logged-in account)."""

    def __init__(self, profile: str, cookies: dict[str, str] | None, max_posts: int | None = None):
        try:
            from gallery_dl import config, job
        except ImportError:
            raise UserError("مكتبة gallery-dl غير مثبتة. نفّذ: pip install -r requirements.txt") from None
        if not cookies:
            raise UserError("طريقة gallery-dl تحتاج كوكيز حساب مسجَّل الدخول: "
                            "ضع ملف cookies.txt بجانب السكربت أو استخدم --cookies أو --browser.")
        self.job = job
        self.profile = profile
        config.clear()
        config.set(("extractor",), "cookies", dict(cookies))
        if max_posts:  # stop paging there, since _extract() reads a whole section before returning
            config.set(("extractor", "instagram"), "max-posts", max_posts)
        logging.basicConfig(level=logging.INFO, format="  [gallery-dl] %(message)s")
        log("• gallery-dl ينتظر عدة ثوانٍ بين الطلبات لتجنب الحظر، لذلك قد يبدو متوقفاً قليلاً (هذا طبيعي)")

    def iter_posts(self, include_reels: bool) -> Iterator[PostRecord]:
        seen = set()
        for section in ("posts", "reels") if include_reels else ("posts",):
            files, error = self._extract(f"https://www.instagram.com/{self.profile}/{section}/")
            posts: dict[str, list[dict]] = {}
            for item in files:
                posts.setdefault(str(item.get("post_shortcode") or item.get("shortcode")), []).append(item)
            for shortcode, items in posts.items():
                if shortcode not in seen:
                    seen.add(shortcode)
                    yield self._record(shortcode, items)
            if error is not None:
                raise error

    def _extract(self, url: str) -> tuple[list[dict], Exception | None]:
        files: list[dict] = []

        class Collector(self.job.DataJob):
            def handle_url(self, url, kwdict):
                files.append(kwdict)
                if len(files) % 25 == 0:
                    log(f"  … الملفات التي جُمعت بياناتها حتى الآن: {len(files)}")

        collector = Collector(url, file=None)
        collector.run()
        return files, collector.exception

    @staticmethod
    def _record(shortcode: str, items: list[dict]) -> PostRecord:
        first = items[0]
        videos = [(int(item.get("num") or i), item["video_url"])
                  for i, item in enumerate(items, 1) if item.get("video_url")]
        if len(items) > 1 or first.get("sidecar_media_id"):
            kind = "carousel"
        else:
            kind = "video" if videos else "image"
        return PostRecord(shortcode, _utc_naive(first.get("post_date") or first.get("date")),
                          first.get("description") or "", kind, lambda: videos)


def explain_error(exc: Exception, backend: str, at_start: bool = False, suggest_other: bool = True) -> str:
    """The error with hints. `at_start`: no post was received yet; `suggest_other`:
    the other backend was not tried in this run."""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    hints = []
    if "could not be found" in low:  # gallery-dl reports any failed profile lookup this way
        hints.append("تأكد من اسم الحساب ومن اتصال الإنترنت ومن أن الكوكيز لحساب مسجَّل الدخول وغير منتهية.")
    if any(s in low for s in ("max retries exceeded", "proxyerror", "name resolution", "getaddrinfo",
                              "connection refused", "connection aborted", "timed out")):
        hints.append("تعذر الاتصال بإنستجرام: تأكد من اتصال الإنترنت وأن إنستجرام يفتح من المتصفح، "
                     "ثم أعد المحاولة.")
    else:
        if any(s in low for s in ("login", "401", "403", "unauthorized", "forbidden", "redirect")):
            hints.append("إنستجرام يطلب تسجيل الدخول: سجّل الدخول في المتصفح ثم صدّر ملف الكوكيز cookies.txt "
                         "من جديد (انظر README).")
        if any(s in low for s in ("429", "too many", "wait a few minutes", "rate limit")):
            if backend == "instaloader" and at_start:
                hints.append("هذا ليس حظراً بسبب كثرة الطلبات، والانتظار لا يحله: إنستجرام يرفض طريقة instaloader "
                             "من أول طلب (مشكلة معروفة حالياً)، والطريقة التي تعمل هي gallery-dl مع ملف الكوكيز. " +
                             ("ضع ملف cookies.txt بجانب السكربت وشغّله بدون --backend instaloader "
                              "لتُستخدم gallery-dl تلقائياً." if suggest_other else
                              "راجع خطأ gallery-dl المذكور أعلاه."))
                suggest_other = False
            else:
                hints.append("إنستجرام أوقف الطلبات مؤقتاً لكثرتها: انتظر من نصف ساعة لساعة ثم أعد التشغيل.")
        if "checkpoint" in low or "challenge" in low:
            hints.append("إنستجرام يطلب تأكيد الحساب: افتحه من المتصفح وأكّد أنك أنت ثم أعد المحاولة.")
        if suggest_other and backend == "gallery-dl":
            hints.append("يمكنك أيضاً تجربة الطريقة الأخرى: --backend instaloader")
        elif suggest_other:
            hints.append("يمكنك أيضاً تجربة الطريقة الأخرى: --backend gallery-dl (مع ملف الكوكيز).")
    return "\n".join([f"✗ توقف فحص الحساب: {text}"] + [f"  - {h}" for h in hints])


# ---------------------------------------------------------------------------
# Downloading and folder synchronisation
# ---------------------------------------------------------------------------

@dataclass
class Video:
    shortcode: str
    index: int
    date: dt.datetime
    url: str
    keyword: str

    @property
    def key(self) -> str:
        return f"{self.shortcode}_{self.index}"

    @property
    def stamp(self) -> str:
        return self.date.strftime("%Y-%m-%dT%H:%M:%S")


class State:
    """Remembers which files this script created, so re-runs resume and renumber safely."""

    def __init__(self, path: Path):
        self.path = path
        self.folders: dict[str, dict[str, dict]] = {}
        if path.is_file():
            try:
                self.folders = json.loads(path.read_text(encoding="utf-8")).get("folders", {})
            except (OSError, ValueError) as exc:
                raise UserError(f"ملف الحالة {path} تالف ({exc}). احذفه أو أصلحه ثم أعد التشغيل.") from None

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        folders = {name: files for name, files in self.folders.items() if files}
        tmp.write_text(json.dumps({"version": 1, "folders": folders}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)


class Downloader:
    def __init__(self):
        import requests
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": VIDEO_USER_AGENT, "Referer": "https://www.instagram.com/"})

    def fetch(self, url: str, dest: Path) -> None:
        part = dest.with_name(dest.name + ".part")
        for attempt in range(1, 4):
            try:
                self._fetch_once(url, part)
                os.replace(part, dest)
                return
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if attempt == 3 or status in (403, 404, 410):  # expired/invalid links won't recover
                    raise
                time.sleep(3 * attempt)
            finally:
                part.unlink(missing_ok=True)

    def _fetch_once(self, url: str, part: Path) -> None:
        with self.session.get(url, stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            if "text/html" in response.headers.get("Content-Type", ""):
                raise RuntimeError("الرابط أعاد صفحة ويب بدلاً من فيديو")
            with open(part, "wb") as fh:
                for chunk in response.iter_content(1 << 18):
                    fh.write(chunk)
        if part.stat().st_size == 0:
            raise RuntimeError("الملف فارغ")


def _set_mtime(path: Path, date: dt.datetime) -> None:
    stamp = date.replace(tzinfo=dt.timezone.utc).timestamp()
    try:
        os.utime(path, (stamp, stamp))
    except OSError:
        pass


def _existing_copy(out_dir: Path, state: State, key: str) -> Path | None:
    """A file downloaded earlier for the same video (in any product folder)."""
    for folder, managed in state.folders.items():
        info = managed.get(key)
        if info and (out_dir / folder / info["file"]).is_file():
            return out_dir / folder / info["file"]
    return None


def _rename(folder_dir: Path, old: str, new: str) -> None:
    try:
        os.rename(folder_dir / old, folder_dir / new)
    except OSError as exc:
        raise UserError(f"تعذرت إعادة تسمية {folder_dir / old}: {exc}\n"
                        "  أغلق أي برنامج يفتح هذا الفيديو ثم أعد التشغيل (سيكمل من حيث توقف).") from None


def sync_folder(out_dir: Path, folder: str, wanted: dict[str, Video], state: State,
                number_format: str, downloader: Downloader | None,
                results: dict[tuple[str, str], tuple[str, str]], dry_run: bool,
                unmatched: str = "", moved: set[str] | None = None) -> dict[str, int]:
    """Bring one product folder in line with the wanted videos.

    Videos are ordered by date and named "<folder>.mp4" (only one video) or
    "<folder> (1).mp4", "<folder> (2).mp4", ... Files downloaded earlier are
    renamed when the numbering changes; files the script did not create are
    never touched.

    A video found in the `unmatched` folder (videos matching no product) is
    moved from there instead of being copied, and its key is added to `moved`.
    A dry run moves nothing, so when it syncs the `unmatched` folder itself the
    keys in `moved` are treated as gone.
    """
    moved = set() if moved is None else moved
    folder_dir = out_dir / folder
    managed = state.folders.setdefault(folder, {})
    for key, info in list(managed.items()):  # forget files the user deleted
        if not (folder_dir / info["file"]).is_file():
            del managed[key]
    own_files = {info["file"].lower() for info in managed.values()}
    if dry_run and folder == unmatched:
        for key in moved:
            managed.pop(key, None)  # the state is never saved in a dry run
    new_keys = [key for key in wanted if key not in managed]
    counts = {"new": 0, "renamed": 0, "failed": 0, "total": 0}
    if not managed and not new_keys:
        return counts

    entries = sorted([(info["date"], key) for key, info in managed.items()] +
                     [(wanted[key].stamp, key) for key in new_keys])
    counts["total"] = len(entries)
    taken = ({p.name.lower() for p in folder_dir.iterdir()} - own_files) if folder_dir.is_dir() else set()
    names = plan_names(folder, len(entries), taken, number_format)
    target = {key: name for (_, key), name in zip(entries, names)}

    # Rename in two steps (to temporary names first) so numbers can shift freely.
    # The state is saved after every step, so an interrupted run can resume.
    moves = [key for key in managed if managed[key]["file"] != target[key]]
    counts["renamed"] = len(moves)
    for key in moves:
        log(f"  ↻ إعادة ترقيم: {managed[key]['file']}  ←  {target[key]}")
    if moves and not dry_run:
        for key in moves:
            temp = f".{uuid.uuid4().hex}.renaming"
            _rename(folder_dir, managed[key]["file"], temp)
            managed[key]["file"] = temp
            state.save()
        for key in moves:
            _rename(folder_dir, managed[key]["file"], target[key])
            managed[key]["file"] = target[key]
            state.save()

    for key in managed:
        results[(folder, key)] = (managed[key]["file"], "موجود مسبقاً")

    for _, key in entries:
        if key in managed:
            continue
        video, name = wanted[key], target[key]
        # A video that was saved as matching no product, but now matches this one.
        info = state.folders.get(unmatched, {}).get(key) if unmatched and folder != unmatched else None
        source = out_dir / unmatched / info["file"] if info else None
        moving = source is not None and source.is_file()
        if dry_run:
            if moving:
                moved.add(key)
            log(f"  {'→' if moving else '↓'} (تجربة) {folder}/{name}")
            results[(folder, key)] = (name, f"سيُنقل من «{unmatched}»" if moving else "سيتم تحميله")
            counts["new"] += 1
            continue
        dest = folder_dir / name
        folder_dir.mkdir(parents=True, exist_ok=True)
        try:
            if moving:
                os.replace(source, dest)
                del state.folders[unmatched][key]
                moved.add(key)
            elif (copy := _existing_copy(out_dir, state, key)) is not None:
                shutil.copy2(copy, dest)
            else:
                downloader.fetch(video.url, dest)
                _set_mtime(dest, video.date)
        except Exception as exc:
            action = "نقل" if moving else "تحميل"
            log(f"  ✗ فشل {action} {name}: {exc}")
            results[(folder, key)] = (name, f"فشل ال{action}: {exc}")
            counts["failed"] += 1
            continue
        if moving:
            log(f"  → {folder}/{name}  (نُقل من «{unmatched}»)")
        else:
            log(f"  ↓ {folder}/{name}  ({dest.stat().st_size / (1024 * 1024):.1f} MB)")
        managed[key] = {"file": name, "date": video.stamp,
                        "post": f"https://www.instagram.com/p/{video.shortcode}/",
                        "keyword": video.keyword}
        results[(folder, key)] = (name, f"نُقل من «{unmatched}»" if moving else "تم التحميل")
        counts["new"] += 1
        state.save()
    return counts


def write_report(path: Path, rows: list[dict]) -> None:
    columns = ["التاريخ", "رابط المنشور", "النوع", "المنتج (المجلد)", "الكلمة المطابقة",
               "اسم الملف", "الحالة", "الوصف"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:  # BOM so Excel shows Arabic
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

KIND_NAMES = {"video": "فيديو", "carousel": "ألبوم", "image": "صورة"}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="تحميل فيديوهات المنتجات من حساب إنستجرام وحفظ كل فيديو في مجلد المنتج باسمه.")
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "products.json",
                        help="ملف المنتجات والكلمات (الافتراضي: products.json بجانب السكربت)")
    parser.add_argument("--out", type=Path,
                        help="المجلد الذي يحتوي مجلدات المنتجات (يتجاوز output_dir في ملف الإعدادات)")
    parser.add_argument("--profile", help="اسم حساب إنستجرام (يتجاوز profile في ملف الإعدادات)")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--cookies", type=Path,
                      help="ملف الكوكيز المُصدَّر من المتصفح (cookies.txt أو JSON)")
    auth.add_argument("--browser", help="قراءة الكوكيز مباشرة من المتصفح: firefox أو chrome أو edge ...")
    auth.add_argument("--login", metavar="USERNAME",
                      help="تسجيل الدخول باسم المستخدم (تُطلب كلمة السر مرة واحدة وتُحفظ الجلسة)")
    parser.add_argument("--backend", choices=("auto", "gallery-dl", "instaloader"), default="auto",
                        help="طريقة قراءة إنستجرام. auto (الافتراضي): gallery-dl إذا وُجدت الكوكيز "
                             "ثم instaloader إذا لم تصل الأولى لأي منشور، وبدون كوكيز instaloader فقط")
    parser.add_argument("--reels", action="store_true",
                        help="فحص تبويب الريلز أيضاً (للريلز غير الظاهرة في شبكة المنشورات؛ أبطأ)")
    parser.add_argument("--max-posts", type=int, metavar="N", help="فحص أحدث N منشور فقط")
    parser.add_argument("--match", choices=("first", "all"),
                        help="first: الفيديو يذهب لأول منتج مذكور في الوصف (الافتراضي). "
                             "all: نسخة في مجلد كل منتج مذكور")
    parser.add_argument("--dry-run", action="store_true",
                        help="تجربة: عرض ما سيتم تحميله وكتابة التقرير بدون تحميل أي شيء")
    return parser.parse_args(argv)


def find_cookie_file() -> Path | None:
    for folder in dict.fromkeys((Path.cwd(), SCRIPT_DIR)):
        for name in COOKIE_FILES:
            if (folder / name).is_file():
                return folder / name
    return None


def backend_order(choice: str, has_cookies: bool) -> list[str]:
    """The ways to read Instagram, in the order they are tried.

    "auto" starts with gallery-dl when there are cookies, because Instagram now
    refuses instaloader's first request with a 429. The next way is tried only
    when the previous one found no post at all.
    """
    if choice != "auto":
        return [choice]
    return ["gallery-dl", "instaloader"] if has_cookies else ["instaloader"]


NO_MATCH = "لا يطابق أي منتج"


def scan_posts(posts: Iterator[PostRecord], config: Config, mode: str,
               wanted: dict[str, dict[str, Video]], post_rows: list,
               unmatched: str = "") -> tuple[int, Exception | None]:
    """Match each post's caption against the products.

    Videos matching no product go to the `unmatched` folder ("" = skip them).
    Fills `wanted` (videos per folder) and `post_rows`, entries of
    (post, [(folder, keyword)], videos, status) for the report.
    Returns the number of posts scanned and the error that stopped the scan, if any.
    """
    scanned = 0
    try:
        for post in posts:
            scanned += 1
            if scanned % 24 == 0:
                log(f"  … المنشورات التي تم فحصها حتى الآن: {scanned}")
            if post.kind == "image":
                continue
            if post.shortcode in config.exclude_posts:
                post_rows.append((post, [], [], "مستبعد (exclude_posts)"))
                continue
            matches = match_caption(post.caption, config.products, mode)
            if not matches and not unmatched:
                post_rows.append((post, [], [], NO_MATCH))
                continue
            targets = [(m.product.folder, m.keyword) for m in matches] or [(unmatched, "")]
            try:
                videos = post.get_videos()
            except Exception as exc:  # one broken post should not stop the scan
                post_rows.append((post, targets, [], f"تعذر جلب رابط الفيديو: {exc}"))
                log(f"  ✗ تعذر جلب فيديو المنشور {post.url}: {exc}")
                continue
            if not videos:
                post_rows.append((post, targets if matches else [], [], "لا يحتوي على فيديو"))
                continue
            post_rows.append((post, targets, videos, ""))
            for folder, keyword in targets:
                for index, url in videos:
                    video = Video(post.shortcode, index, post.date, url, keyword)
                    wanted[folder].setdefault(video.key, video)
            folders = " | ".join(folder for folder, _ in targets)
            log(f"  {'✓' if matches else '○'} {post.date:%Y-%m-%d} {post.url} (فيديوهات: {len(videos)}) → {folders}")
    except UserError:
        raise
    except Exception as exc:
        return scanned, exc
    return scanned, None


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    profile = to_username(args.profile or config.profile)
    if not profile:
        raise UserError("لم يتم تحديد حساب إنستجرام (profile في ملف الإعدادات أو --profile).")
    if args.max_posts is not None and args.max_posts < 1:
        raise UserError("--max-posts يجب أن يكون رقماً أكبر من صفر.")
    mode = args.match or config.match_mode
    if mode not in ("first", "all"):
        raise UserError('قيمة match_mode يجب أن تكون "first" أو "all".')
    try:
        valid_format = "{n}" in config.number_format and config.number_format.format(name="x", n=1)
    except (KeyError, IndexError, ValueError):
        valid_format = False
    if not valid_format:
        raise UserError('number_format يجب أن يحتوي على {n} وأن يستخدم {name} و{n} فقط '
                        '(مثال: "{name} ({n})").')
    out_dir = (args.out or config.output_dir).expanduser().resolve()
    products = config.products

    log(f"• مجلد المنتجات: {out_dir}")
    for message in resolve_folders(products, out_dir):
        log(message)
    unmatched = ""
    if config.unmatched_folder:
        unmatched = resolve_unmatched_folder(config.unmatched_folder, products, out_dir)
        log(f"  {'✓ مجلد موجود' if (out_dir / unmatched).is_dir() else '+ مجلد جديد'}"
            f" للفيديوهات التي لا تطابق أي منتج: {unmatched}")

    cookie_path = args.cookies
    if not (cookie_path or args.browser or args.login):
        cookie_path = find_cookie_file()
        if cookie_path:
            log(f"• استخدام ملف الكوكيز: {cookie_path}")
    cookies = None
    if cookie_path:
        cookies = parse_cookie_file(cookie_path)
    elif args.browser:
        cookies = browser_cookies(args.browser)
    if cookies is not None and "sessionid" not in cookies:
        raise UserError("الكوكيز لا تحتوي على sessionid الخاص بإنستجرام. "
                        "سجّل الدخول لإنستجرام في المتصفح ثم صدّر الكوكيز من جديد.")

    if args.backend == "gallery-dl" and args.login:
        raise UserError("طريقة gallery-dl لا تدعم --login؛ استخدم ملف الكوكيز أو --browser.")
    order = backend_order(args.backend, cookies is not None)

    folders = [p.folder for p in products] + ([unmatched] if unmatched else [])
    wanted: dict[str, dict[str, Video]] = {folder: {} for folder in folders}
    post_rows: list[tuple[PostRecord, list[tuple[str, str]], list[tuple[int, str]], str]] = []
    scanned, crawl_error = 0, None
    for position, name in enumerate(order):
        fallback = order[position + 1] if position + 1 < len(order) else None
        try:
            if name == "gallery-dl":
                backend = GalleryDlBackend(profile, cookies, args.max_posts)
            else:
                backend = InstaloaderBackend(profile, cookies, args.login)
        except Exception as exc:  # e.g. a missing library, or no connection while logging in
            message = str(exc) if isinstance(exc, UserError) else explain_error(exc, name).lstrip("✗ ")
            if fallback is None:
                raise UserError(message) from None
            log(f"✗ {message}\n• سنجرب الطريقة الأخرى: {fallback}")
            continue

        log(f"\n• فحص منشورات الحساب {profile} (الطريقة: {name}) ...")
        posts = itertools.islice(backend.iter_posts(args.reels), args.max_posts)
        scanned, crawl_error = scan_posts(posts, config, mode, wanted, post_rows, unmatched)
        if crawl_error is None:
            break
        log(explain_error(crawl_error, name, at_start=not scanned, suggest_other=len(order) == 1))
        if scanned:
            log("  سيتم تحميل ما تم العثور عليه حتى الآن؛ أعد التشغيل لاحقاً لإكمال الباقي.")
        elif fallback:
            log(f"• لم نصل لأي منشور بطريقة {name}، سنجرب الطريقة الأخرى: {fallback}")
            continue
        break

    found = sum(len(wanted[p.folder]) for p in products)
    extra = f" — فيديوهات لا تطابق أي منتج: {len(wanted[unmatched])}" if unmatched else ""
    log(f"\n• المنشورات التي تم فحصها: {scanned} — فيديوهات المنتجات: {found}{extra}")

    out_dir.mkdir(parents=True, exist_ok=True)
    state = State(out_dir / STATE_FILE)
    downloader = None if args.dry_run else Downloader()
    results: dict[tuple[str, str], tuple[str, str]] = {}
    moved: set[str] = set()  # videos moved from the unmatched folder into a product folder
    summary = []
    for folder in folders:  # the unmatched folder comes last, after videos were moved out of it
        counts = sync_folder(out_dir, folder, wanted[folder], state, config.number_format,
                             downloader, results, args.dry_run, unmatched, moved)
        if counts["total"] or counts["new"]:
            summary.append((folder, counts))

    rows = []
    for post, targets, videos, status in post_rows:
        base = {"التاريخ": f"{post.date:%Y-%m-%d %H:%M}", "رابط المنشور": post.url,
                "النوع": KIND_NAMES.get(post.kind, post.kind),
                "الوصف": " ".join(post.caption.split())[:500]}
        if not videos:
            rows.append({**base, "المنتج (المجلد)": " | ".join(folder for folder, _ in targets),
                         "الحالة": status})
            continue
        for folder, keyword in targets:
            for index, _ in videos:
                name, result = results.get((folder, f"{post.shortcode}_{index}"), ("", ""))
                rows.append({**base, "المنتج (المجلد)": folder, "الكلمة المطابقة": keyword,
                             "اسم الملف": name, "الحالة": result})
    report_path = out_dir / REPORT_FILE if scanned else None  # keep the last report if nothing was scanned
    if report_path:
        try:
            write_report(report_path, rows)
        except OSError as exc:  # e.g. the report is open in Excel
            log(f"! تعذر كتابة التقرير {report_path}: {exc}")
            report_path = None

    log("\n" + ("نتيجة التجربة (لم يتم تحميل شيء):" if args.dry_run else "الملخص:"))
    for folder, counts in summary:
        parts = [f"الإجمالي {counts['total']}", f"جديد {counts['new']}"]
        if counts["renamed"]:
            parts.append(f"أعيد ترقيمه {counts['renamed']}")
        if counts["failed"]:
            parts.append(f"فشل {counts['failed']}")
        log(f"  {folder}: " + "، ".join(parts))
    if not summary:
        log("  لا توجد فيديوهات مطابقة.")
    no_match = sum(1 for _, targets, videos, status in post_rows
                   if status == NO_MATCH or (unmatched and videos and targets == [(unmatched, "")]))
    if no_match and unmatched:
        log(f"  منشورات لا تطابق أي منتج: {no_match} (فيديوهاتها في مجلد «{unmatched}»؛ "
            "راجع التقرير لإضافة كلمات بحث)")
    elif no_match:
        log(f"  منشورات فيديو لم تطابق أي منتج: {no_match} (راجع التقرير لإضافة كلمات بحث)")
    if report_path:
        log(f"• التقرير: {report_path}")
    failed = sum(c["failed"] for _, c in summary)
    return 1 if crawl_error or failed else 0


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
        log(f"✗ {exc}")
        return 2
    except KeyboardInterrupt:
        log("\n✗ تم الإيقاف. ما تم تحميله محفوظ، وعند إعادة التشغيل سيكمل من حيث توقف.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
