#!/usr/bin/env python3
"""
Download all story highlights of instagram.com/lont.sa, in the order of the
profile, to move them to the Lont Shopify store.

It works like the Natural Roots scripts in the folder above (gallery-dl with
the cookies.txt of a logged-in account), in two steps:

1. Data: gallery-dl reads the circles (title, cover, and every story in order
   with its type, link and size) without downloading anything. The answer is
   kept in raw_list.json, so a re-run does not ask Instagram again.
2. Download: the files come straight from Instagram's CDN, which needs no API
   requests, into output/ as lont-hl-01-cover.jpg, lont-hl-01-001.mp4, ...
   with manifest.json and report.csv. Files that are already there are
   skipped, so a stopped run continues where it stopped.

The console messages are English only, because the Windows console cannot show
Arabic; the Arabic titles go to the UTF-8 files.

Usage:  python lont_highlights.py --dry-run   (data only: the table of circles)
        python lont_highlights.py             (data if needed, then download)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

LONT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(LONT_DIR.parent))

import insta_videos as iv  # noqa: E402  (cookies, User-Agent, Arabic matching)
from insta_videos import UserError  # noqa: E402

PROFILE = "lont.sa"
COOKIE_FILE = "cookies.txt"
RAW_FILE = "raw_list.json"
OUTPUT_DIR = "output"
OLD_DIR = "old"  # files that no longer match their circle are moved here, never deleted
MANIFEST_FILE = "manifest.json"
REPORT_FILE = "report.csv"
PREFIX = "lont-hl"

# The circles as the profile shows them: the title on Instagram, and a name for the console.
EXPECTED = [
    ("عن لونت", "About Lont"),
    ("غرف النوم", "Bedrooms"),
    ("الأسئلة الشائعة", "FAQ"),
    ("غرف المعيشة", "Living rooms"),
    ("طاولات", "Tables"),
    ("آراء العملاء", "Customer reviews"),
    ("التوصيل", "Delivery"),
    ("صور على الطبيعة", "Real-life photos"),
]
REPORT_COLUMNS = ["الترتيب", "الدايرة", "عدد الصور", "عدد الفيديو", "الحالة"]

COOKIE_HELP = (
    "What to do:\n"
    "  1. In Chrome or Edge, open https://www.instagram.com and make sure you are logged in.\n"
    '  2. Click the "Get cookies.txt LOCALLY" extension, then "Export".\n'
    "  3. Name the file cookies.txt and put it in the lont-highlights folder (next to run_lont.bat).\n"
    "  4. Run run_lont.bat again.\n"
    "  Never send or upload cookies.txt: it opens your Instagram account.")


class RateLimited(Exception):
    """Instagram or its CDN answered "429 Too Many Requests"."""


class LinkGone(Exception):
    """The CDN refused a link (403/404/410): it expired, or the story was removed."""


class RateLimitStop(UserError):
    def __init__(self):
        super().__init__(
            'Instagram answered "429 Too Many Requests": it wants a break.\n'
            "  Everything received so far is saved.\n"
            "  Wait one hour, then run run_lont.bat again. It continues from where it stopped.")


# ---------------------------------------------------------------------------
# Console: English only, and never a cookie value
# ---------------------------------------------------------------------------

_SECRETS: list[str] = []
_QUERY = re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*")
_NETWORK = ("max retries exceeded", "proxyerror", "name resolution", "getaddrinfo", "connection refused",
            "connection aborted", "connectionerror", "timed out", "timeout", "sslerror")


def remember_secrets(cookies: dict[str, str]) -> None:
    forms = set()
    for value in cookies.values():
        forms |= {value, urllib.parse.unquote(value), urllib.parse.quote(value, safe="")}
    _SECRETS[:] = sorted((form for form in forms if len(form) >= 8), key=len, reverse=True)


def hide_secrets(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


def say(message: str = "") -> None:
    print(hide_secrets(message), flush=True)


def describe(exc: BaseException) -> str:
    """An error in one line, without cookie values or the signed part of links."""
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return hide_secrets(_QUERY.sub(r"\1?...", " ".join(text.split())))


class _HideSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = hide_secrets(record.getMessage()), None
        return True


class _LastAnswer(logging.Handler):
    """Keeps what Instagram answered when gallery-dl could not understand it (a debug message)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = ""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Server response"):
            self.text = message.partition("\n")[2]


ANSWER = _LastAnswer()


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="  [gallery-dl] %(message)s")
    for handler in logging.getLogger().handlers:
        if handler.level == logging.NOTSET:
            handler.setLevel(logging.INFO)  # gallery-dl's debug messages stay out of the console
        if not any(isinstance(f, _HideSecrets) for f in handler.filters):
            handler.addFilter(_HideSecrets())
    instagram = logging.getLogger("instagram")
    instagram.setLevel(logging.DEBUG)  # but Instagram's answer is kept, to show it if a step fails
    if ANSWER not in instagram.handlers:
        instagram.addHandler(ANSWER)


def answer_line(text: str, size: int = 300) -> str:
    """Instagram's answer in one short English-only line, without cookie values."""
    text = hide_secrets(" ".join(text.split())).encode("ascii", "replace").decode()
    return text if len(text) <= size else text[:size] + " ..."


def shown(path: Path, base: Path) -> str:
    """A path for the console, from the project folder (whose full path may not be English)."""
    try:
        return str(path.relative_to(base.parent))
    except ValueError:
        return str(path)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value) -> int | None:
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def size_text(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def sessionid_expiry(path: Path) -> float | None:
    """When the Instagram login in a Netscape cookies.txt runs out (None: no date in the file)."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.strip().removeprefix("#HttpOnly_").split("\t")
        if len(parts) >= 7 and "instagram" in parts[0] and parts[5] == "sessionid":
            try:
                return float(parts[4]) or None
            except ValueError:
                return None
    return None


def load_cookies(base: Path) -> dict[str, str]:
    """The cookies of the first good cookies.txt: the one in lont-highlights, then the project's."""
    problems = []
    for path, where in ((base / COOKIE_FILE, shown(base / COOKIE_FILE, base)),
                        (base.parent / COOKIE_FILE, f"{COOKIE_FILE} in the project folder")):
        if not path.is_file():
            problems.append(f"{where}: not found")
            continue
        try:
            cookies = iv.parse_cookie_file(path)
        except UserError:  # its message is in Arabic
            problems.append(f"{where}: could not be read")
            continue
        if not cookies.get("sessionid"):
            problems.append(f"{where}: no Instagram login in it (no sessionid)")
            continue
        expiry = sessionid_expiry(path)
        if expiry and expiry < time.time():
            day = dt.datetime.fromtimestamp(expiry, dt.timezone.utc).strftime("%Y-%m-%d")
            problems.append(f"{where}: the Instagram login in it ran out on {day}")
            continue
        remember_secrets(cookies)
        say(f"- Cookies: {where} (Instagram login found)")
        return cookies
    raise UserError("No usable Instagram cookies.\n" + "".join(f"  - {p}\n" for p in problems) + COOKIE_HELP)


# ---------------------------------------------------------------------------
# Instagram, through gallery-dl
# ---------------------------------------------------------------------------

class Instagram:
    """gallery-dl set up like in the Natural Roots scripts (the cookies, and its own
    6-12 seconds between requests), except that the first "429 Too Many Requests"
    stops the run instead of gallery-dl waiting and asking again."""

    def __init__(self, cookies: dict[str, str], transport=None):
        try:
            from gallery_dl import config, extractor, job
            from gallery_dl.extractor.message import Message
        except ImportError:
            raise UserError("gallery-dl is not installed: start the tool with run_lont.bat, "
                            "which installs it.") from None
        self._find, self._job, self._message = extractor.find, job, Message
        config.clear()
        config.set(("extractor",), "cookies", dict(cookies))
        # One mp4 with sound for each video: the link gallery-dl downloads itself.
        config.set(("extractor", "instagram"), "videos", "merged")
        # Covers come from Instagram's cover_media below, not as an extra story.
        config.set(("extractor", "instagram"), "covers", False)
        self._transport = transport  # the tests' stand-in for the network
        self._session = None
        self.rate_limited = ""  # the address that answered 429
        self.requests = 0
        setup_logging()

    def highlights(self, profile: str):
        """All circles: (tray, circles, raw circles, error).

        The tray (order, titles, covers) is the first answer gallery-dl gets for
        this page; it is kept on its way, so it costs no extra request."""
        extr = self._extractor(f"https://www.instagram.com/{profile}/highlights/")
        trays, raw = [], []
        self._keep(extr, "highlights_tray", trays)
        self._keep(extr, "reels_media", raw)
        self._lenient_user_lookup(extr, profile)
        reels, error = self._collect(extr)
        tray = trays[0] if trays and isinstance(trays[0], list) else None
        return tray, reels, _flatten(raw), error

    def highlight(self, circle_id: str):
        """One circle again, without the others: (circles, raw circles, error)."""
        extr = self._extractor(f"https://www.instagram.com/stories/highlights/{circle_id}/")
        raw = []
        self._keep(extr, "reels_media", raw)
        reels, error = self._collect(extr)
        return reels, _flatten(raw), error

    def _extractor(self, url: str):
        extr = self._find(url)
        if extr is None:
            raise UserError(f"This gallery-dl does not know {url}. Run run_lont.bat, which updates it.")
        if self._session is not None:
            extr.session = self._session  # one session for the whole run
        extr.initialize()
        if self._session is None:
            self._session = session = extr.session
            if self._transport is not None:
                session.mount("https://", self._transport)
            send = session.send

            def send_or_stop(request, **kwargs):
                if self.rate_limited:  # nothing more after a 429
                    raise RateLimited(self.rate_limited)
                host = urllib.parse.urlsplit(request.url).hostname or ""
                if host == "instagram.com" or host.endswith(".instagram.com"):  # not its static files
                    self.requests += 1
                response = send(request, **kwargs)
                if response.status_code == 429:
                    self.rate_limited = response.url.partition("?")[0]
                    raise RateLimited(self.rate_limited)
                return response

            session.send = send_or_stop
        return extr

    @staticmethod
    def _keep(extr, name: str, store: list) -> None:
        """Keep a copy of what gallery-dl's API call `name` returns: a list, or answers
        that come one by one (newer gallery-dl versions page through Instagram's GraphQL)."""
        api = getattr(extr, "api", None)
        call = getattr(api, name, None)
        if not callable(call):
            return

        def keep(*args, **kwargs):
            result = call(*args, **kwargs)
            if isinstance(result, list):
                store.append(result)
            elif hasattr(result, "__next__"):
                return _passing(result, store)
            return result

        setattr(api, name, keep)

    def _lenient_user_lookup(self, extr, profile: str) -> None:
        """Newer gallery-dl versions look the account up twice: on its profile page, then
        with a GraphQL query that Instagram keeps changing. The highlights need only the
        account's number, which the profile page gives, so the second lookup may fail."""
        api = getattr(extr, "api", None)
        by_id, by_page = getattr(api, "user_by_id", None), getattr(api, "user_by_web", None)
        if not (callable(by_id) and callable(by_page)):
            return
        control = getattr(getattr(extr, "exc", None), "ControlException", ())

        def user_by_id(user_id, *args, **kwargs):
            try:
                return by_id(user_id, *args, **kwargs)
            except control:  # a login page, a stop: not this lookup's problem
                raise
            except Exception:
                if self.rate_limited:
                    raise
                user = by_page(profile)  # kept by gallery-dl from the profile page: no new request
                if not isinstance(user, dict) or str(user.get("id")) != str(user_id):
                    raise
                say("  (no problem: the account number from its profile page is used instead)")
                return {**user, "pk": str(user_id), "username": user.get("username") or profile,
                        "is_private": False}

        api.user_by_id = user_by_id

    def _collect(self, extr):
        """gallery-dl's DataJob: the data only, nothing is downloaded."""
        ANSWER.text = ""
        collector = self._job.DataJob(extr, file=None)
        stopped = []
        dispatch = collector.dispatch

        def dispatch_and_watch(messages):
            try:
                return dispatch(messages)
            except KeyboardInterrupt:  # DataJob would swallow it
                stopped.append(True)
                raise

        collector.dispatch = dispatch_and_watch
        collector.run()
        error = getattr(collector, "exception", None)
        for entry in collector.data:
            if error is None and entry[0] == -1 and isinstance(entry[-1], dict):  # how DataJob notes an error
                error = RuntimeError(f"{entry[-1].get('error')}: {entry[-1].get('message')}")
        if stopped:
            error = KeyboardInterrupt()  # raised once the data received so far is saved
        return reels_from_messages(collector.data, self._message), error


def _passing(entries, store: list):
    for entry in entries:
        store.append([entry])
        yield entry


def _flatten(answers: list) -> list[dict]:
    return [entry for answer in answers if isinstance(answer, list) for entry in answer if isinstance(entry, dict)]


def circle_id_of(entry: dict) -> str:
    """Instagram's number for a circle: "highlight:17912345" -> "17912345"."""
    return str(entry.get("id") or "").rpartition(":")[2] if isinstance(entry, dict) else ""


def reels_from_messages(data: list, message) -> list[dict]:
    """gallery-dl's results as circles {id, title, items}, in the order they came."""
    reels: dict[str, dict] = {}
    for entry in data:
        kind, kwdict = entry[0], entry[-1]
        if kind not in (message.Directory, message.Url) or not isinstance(kwdict, dict):
            continue
        circle_id = str(kwdict.get("post_id") or "")
        reel = reels.setdefault(circle_id, {"id": circle_id, "title": "", "items": []})
        reel["title"] = reel["title"] or str(kwdict.get("highlight_title") or "")
        if kind == message.Url and not str(kwdict.get("media_id")).endswith("_cover"):  # gallery-dl's own cover
            video = kwdict.get("video_url")
            reel["items"].append({"media_id": str(kwdict.get("media_id") or ""),
                                  "type": "video" if video else "image",
                                  "url": video or entry[1],
                                  "width": _int(kwdict.get("width")),
                                  "height": _int(kwdict.get("height"))})
    return [reel for reel in reels.values() if reel["id"]]


def explain(exc: BaseException) -> str:
    """What stopped gallery-dl and what to do, like explain_error() in insta_videos.py."""
    text = describe(exc)
    low = text.lower()
    if "challenge" in low or "checkpoint" in low:
        return (f"Instagram wants to check the account ({text}).\n"
                "  Open https://www.instagram.com in the browser, confirm it is you,\n"
                "  then export a new cookies.txt.\n" + COOKIE_HELP)
    if any(s in low for s in ("redirect to login", "redirect to home", "login page", "authrequired",
                              "authorizationerror", "authenticated cookies", "login required", "'401",
                              "'403", "forbidden")):
        return (f"Instagram did not accept the cookies ({text}).\n"
                "  The login in cookies.txt has probably run out or was logged out.\n" + COOKIE_HELP)
    if any(s in low for s in _NETWORK):
        return (f"Could not reach Instagram ({text}).\n"
                "  Check the internet connection and that instagram.com opens in the browser, then run again.")
    if "could not be found" in low:
        return (f"Instagram did not return the account {PROFILE} ({text}).\n"
                "  Check the internet connection. If it keeps happening, the cookies may have run out.\n"
                + COOKIE_HELP)
    return (f"gallery-dl stopped: {text}\n"
            "  Everything received so far is saved; run run_lont.bat again later (it also updates gallery-dl).\n"
            "  If it happens again, export a new cookies.txt into the lont-highlights folder.")


def check(instagram: Instagram, error: BaseException | None) -> None:
    if isinstance(error, KeyboardInterrupt):
        raise error
    if instagram.rate_limited:
        raise RateLimitStop()
    if error is not None:
        lines = explain(error).split("\n")
        if ANSWER.text:
            lines.insert(1, f"  Instagram's answer: {answer_line(ANSWER.text)}")
        raise UserError("\n".join(lines))


# ---------------------------------------------------------------------------
# raw_list.json: everything Instagram said, so a re-run does not ask again
# ---------------------------------------------------------------------------

def new_raw() -> dict:
    # "files": which story each downloaded file holds, to notice a circle that changed on Instagram
    return {"version": 1, "profile": PROFILE, "complete_list": False, "covers_from_tray": False,
            "order": [], "circles": {}, "files": {}}


def load_raw(path: Path) -> dict:
    if not path.is_file():
        return new_raw()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw.get("circles"), dict):
            raise ValueError("no circles")
    except (OSError, ValueError, AttributeError) as exc:
        raise UserError(f"{RAW_FILE} is damaged ({type(exc).__name__}). Delete it and run again: "
                        "it is made again from Instagram.") from None
    for key, value in new_raw().items():
        raw.setdefault(key, value)
    return raw


def save_raw(path: Path, raw: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _circle(raw: dict, circle_id: str) -> dict:
    return raw["circles"].setdefault(circle_id, {"title": "", "media_count": None, "cover": None,
                                                 "fetched_at_utc": None, "items": None})


def cover_of(entry: dict) -> dict | None:
    """The circle's round picture on the profile, from Instagram's cover_media."""
    media = entry.get("cover_media")
    if not isinstance(media, dict):
        return None
    for key in ("cropped_image_version", "full_image_version"):
        version = media.get(key)
        if isinstance(version, dict) and version.get("url"):
            url = version["url"]
            return {"url": url, "width": _int(version.get("width")), "height": _int(version.get("height")),
                    "media": str(media.get("media_id") or url.partition("?")[0])}
    return None


def apply_tray(raw: dict, tray: list) -> None:
    order = []
    for entry in tray:
        circle_id = circle_id_of(entry)
        if not circle_id or circle_id in order:
            continue
        order.append(circle_id)
        circle = _circle(raw, circle_id)
        circle["title"] = str(entry.get("title") or circle["title"])
        circle["media_count"] = _int(entry.get("media_count")) or circle["media_count"]
        circle["cover"] = cover_of(entry) or circle["cover"]
    raw["order"], raw["complete_list"], raw["covers_from_tray"] = order, True, True


def apply_reels(raw: dict, reels: list[dict], raw_reels: list[dict]) -> None:
    now = utc_now()
    for reel in reels:
        circle = _circle(raw, reel["id"])
        circle["title"] = circle["title"] or reel["title"]
        circle["items"], circle["fetched_at_utc"] = reel["items"], now
    for entry in raw_reels:  # a circle's answer carries its cover too, with a fresh link
        circle = raw["circles"].get(circle_id_of(entry))
        if circle is not None:
            circle["cover"] = cover_of(entry) or circle["cover"]
            circle["media_count"] = _int(entry.get("media_count")) or circle["media_count"]


def pending(raw: dict) -> list[str]:
    return [cid for cid in raw["order"] if raw["circles"].get(cid, {}).get("items") is None]


def gather(raw: dict, raw_path: Path, connect) -> None:
    """Complete raw_list.json, asking Instagram only for what it does not have yet."""
    if raw["complete_list"] and len(pending(raw)) > 2 + (len(raw["order"]) + 4) // 5:
        raw["complete_list"] = False  # asking for the whole page again takes fewer requests
    if not raw["complete_list"]:
        instagram = connect()
        say("- Asking Instagram for the highlights (data only, nothing is downloaded) ...")
        tray, reels, raw_reels, error = instagram.highlights(PROFILE)
        if tray is not None and reels and not {reel["id"] for reel in reels} & {circle_id_of(e) for e in tray}:
            tray = None  # the two answers cannot be lined up, so gallery-dl's results decide
        if tray is not None:
            apply_tray(raw, tray)
        apply_reels(raw, reels, raw_reels)
        if tray is None:  # gallery-dl changed: the order comes from its results, without covers
            raw["order"] = list(dict.fromkeys(raw["order"] + [reel["id"] for reel in reels]))
            raw["complete_list"] = error is None and not instagram.rate_limited
        if not raw["order"]:
            raw["complete_list"] = False  # nothing to keep: ask again next time
        save_raw(raw_path, raw)
        check(instagram, error)
    todo = pending(raw)
    for number, circle_id in enumerate(todo, 1):
        instagram = connect()
        say(f"- Asking Instagram for a missing circle ({number} of {len(todo)}) ...")
        reels, raw_reels, error = instagram.highlight(circle_id)
        apply_reels(raw, reels, raw_reels)
        if error is None and not instagram.rate_limited and raw["circles"][circle_id]["items"] is None:
            raw["circles"][circle_id].update(items=[], fetched_at_utc=utc_now())  # Instagram has nothing in it
        save_raw(raw_path, raw)
        check(instagram, error)


def refresh(raw: dict, raw_path: Path, circle_id: str, connect, why: str) -> None:
    instagram = connect()
    say(f"    ~ {why}: asking Instagram again for this circle only ...")
    reels, raw_reels, error = instagram.highlight(circle_id)
    apply_reels(raw, reels, raw_reels)
    save_raw(raw_path, raw)
    check(instagram, error)


# ---------------------------------------------------------------------------
# Order: the profile's, checked against the list of circles above
# ---------------------------------------------------------------------------

@dataclass
class Arrangement:
    order: list[str]          # circle ids, in the order of the files
    matched: dict[str, int]   # circle id -> its place in EXPECTED
    reordered: bool           # Instagram's order was different, so EXPECTED's order is used
    missing: list[int]        # places in EXPECTED with no circle on Instagram


def title_key(title: str) -> str:
    return " ".join(iv.tokenize(title))


def _words(key: str) -> set[str]:
    return {word[2:] if word.startswith("ال") and len(word) > 4 else word for word in key.split()}


def match_titles(titles: dict[str, str]) -> dict[str, int]:
    """Which circle is which entry of EXPECTED, by title: another spelling, an emoji, a shorter or
    a longer title still match ("الاسئلة الشائعة ❓", "توصيل الطلبات")."""
    wanted = [title_key(arabic) for arabic, _ in EXPECTED]
    pairs = []
    for circle_id, title in titles.items():
        key = title_key(title)
        if not key:
            continue
        for index, want in enumerate(wanted):
            score = 2.0 if key == want else difflib.SequenceMatcher(None, key, want).ratio()
            fewer, more = sorted((_words(key), _words(want)), key=len)
            if fewer <= more and len("".join(fewer)) >= 3:
                score = max(score, 0.85)
            if min(len(key), len(want)) >= 3 and (key.startswith(want) or want.startswith(key)):
                score = max(score, 0.9)
            if score >= 0.75:
                pairs.append((score, circle_id, index))
    matched: dict[str, int] = {}
    for _, circle_id, index in sorted(pairs, key=lambda pair: -pair[0]):
        if circle_id not in matched and index not in matched.values():
            matched[circle_id] = index
    return matched


def arrange(raw: dict) -> Arrangement:
    order = [cid for cid in raw["order"] if cid in raw["circles"]]
    matched = match_titles({cid: raw["circles"][cid]["title"] for cid in order})
    known = [matched[cid] for cid in order if cid in matched]
    reordered = known != sorted(known)
    if reordered:  # the list wins; circles that are not in it go after it, in Instagram's order
        order = sorted((cid for cid in order if cid in matched), key=matched.__getitem__) + \
                [cid for cid in order if cid not in matched]
    missing = [index for index in range(len(EXPECTED)) if index not in matched.values()]
    return Arrangement(order, matched, reordered, missing)


def label(arrangement: Arrangement, circle_id: str) -> str:
    index = arrangement.matched.get(circle_id)
    return f"{index + 1} {EXPECTED[index][1]}" if index is not None else "extra (not in your list)"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@dataclass
class Planned:
    stem: str   # lont-hl-01-cover, lont-hl-01-001, ...
    media: str  # the Instagram media it holds
    url: str
    kind: str   # cover, image or video


def planned_files(circle: dict, position: int) -> list[Planned]:
    prefix = f"{PREFIX}-{position:02d}"
    files = []
    cover = circle.get("cover")
    if cover and cover.get("url"):
        files.append(Planned(f"{prefix}-cover", cover.get("media") or "", cover["url"], "cover"))
    for number, item in enumerate(circle.get("items") or [], 1):
        files.append(Planned(f"{prefix}-{number:03d}", item.get("media_id") or "", item["url"], item["type"]))
    return files


def _versions(out_dir: Path, stem: str) -> list[Path]:
    return [p for p in sorted(out_dir.glob(stem + ".*")) if p.suffix != ".part" and p.is_file()]


def saved_file(out_dir: Path, records: dict, plan: Planned) -> Path | None:
    """The file downloaded earlier for `plan`, unless it holds another story."""
    record = records.get(plan.stem)
    if record is not None and record.get("media") != plan.media:
        return None
    if record is not None and (out_dir / record["file"]).is_file():
        return out_dir / record["file"]
    found = _versions(out_dir, plan.stem)
    return found[0] if found else None


def set_aside(out_dir: Path, stem: str) -> None:
    """Move older files with this name (another story, or another type) to output/old."""
    for path in _versions(out_dir, stem):
        old = out_dir / OLD_DIR
        old.mkdir(exist_ok=True)
        dest, n = old / path.name, 1
        while dest.exists():
            dest, n = old / f"{path.stem} ({n}){path.suffix}", n + 1
        os.replace(path, dest)


def link_expires(url: str) -> float | None:
    """When an Instagram CDN link stops working (its "oe" parameter), if it says."""
    value = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("oe")
    try:
        return float(int(value[0], 16))
    except (TypeError, ValueError, IndexError):
        return None


def link_expired(url: str, margin: float = 600) -> bool:
    expires = link_expires(url)
    return expires is not None and expires < time.time() + margin


CONTENT_TYPES = {"video/mp4": ".mp4", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
                 "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic",
                 "image/heif": ".heic", "image/avif": ".avif", "video/quicktime": ".mov", "video/webm": ".webm"}
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm")
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def file_kind(path: Path | None, kind: str) -> str:
    """A story's type as it is in output/: a video turned into a picture (videos_to_photos.py) is a picture."""
    if path is None or kind == "cover":
        return kind
    return "video" if path.suffix.lower() in VIDEO_SUFFIXES else "image"


def sniff(head: bytes) -> str | None:
    """The type of a file from its first bytes."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return ".heic" if brand in _HEIF_BRANDS else ".avif" if brand in (b"avif", b"avis") else ".mp4"
    return None


def extension(content_type: str, head: bytes, kind: str) -> str:
    """From the Content-Type, or from the first bytes when it is missing or vague."""
    found = sniff(head)
    if found == ".heic":  # whatever the server called it
        return ".heic"
    return CONTENT_TYPES.get(content_type) or found or (".mp4" if kind == "video" else ".jpg")


def heic_to_jpg(source: Path, dest: Path) -> bool:
    """Convert a HEIC picture to JPG. False when the converter (pillow-heif) is not installed."""
    try:
        import pillow_heif
        from PIL import Image
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    with Image.open(source) as image:
        icc = image.info.get("icc_profile")
        image.convert("RGB").save(dest, "JPEG", quality=95, **({"icc_profile": icc} if icc else {}))
    return True


class Fetcher:
    """Downloads from Instagram's CDN like the project's Downloader: no API, no cookies."""

    def __init__(self, transport=None):
        import requests
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": iv.VIDEO_USER_AGENT, "Referer": "https://www.instagram.com/"})
        if transport is not None:
            self.session.mount("https://", transport)

    def fetch(self, url: str, part: Path) -> str:
        """Write the file at `url` to `part`; returns its Content-Type."""
        for attempt in range(1, 4):
            try:
                return self._fetch_once(url, part)
            except (RateLimited, LinkGone):
                part.unlink(missing_ok=True)
                raise
            except Exception:
                part.unlink(missing_ok=True)
                if attempt == 3:
                    raise
                time.sleep(3 * attempt)

    def _fetch_once(self, url: str, part: Path) -> str:
        with self.session.get(url, stream=True, timeout=(20, 120)) as response:
            if response.status_code == 429:
                raise RateLimited(url.partition("?")[0])
            if response.status_code in (403, 404, 410):  # an expired or removed link won't recover
                raise LinkGone(f"HTTP {response.status_code}")
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type.startswith("text/"):
                raise RuntimeError("the link gave a web page instead of a picture or video")
            with open(part, "wb") as fh:
                for chunk in response.iter_content(1 << 18):
                    fh.write(chunk)
        if part.stat().st_size == 0:
            raise RuntimeError("the file came back empty")
        return content_type


def save_one(fetcher: Fetcher, out_dir: Path, plan: Planned) -> Path:
    part = out_dir / (plan.stem + ".part")
    content_type = fetcher.fetch(plan.url, part)
    with open(part, "rb") as fh:
        head = fh.read(32)
    ext, source = extension(content_type, head, plan.kind), part
    if ext == ".heic":
        converted = out_dir / (plan.stem + ".jpg.part")
        if heic_to_jpg(part, converted):
            part.unlink()
            ext, source = ".jpg", converted
    final = out_dir / (plan.stem + ext)
    set_aside(out_dir, plan.stem)  # a file with this name from before holds another story
    os.replace(source, final)
    return final


@dataclass
class CircleResult:
    planned: int = 0
    saved: int = 0  # in output/, from this run or an earlier one
    new: int = 0
    errors: list[str] = field(default_factory=list)


def download(raw: dict, raw_path: Path, arrangement: Arrangement, out_dir: Path, fetcher: Fetcher,
             connect, results: dict[str, CircleResult]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = raw["files"]
    heic_kept = False
    for position, circle_id in enumerate(arrangement.order, 1):
        result = results[circle_id] = CircleResult()
        say(f"- Circle {position:02d}: {label(arrangement, circle_id)}")
        refreshed = False
        while True:
            plans = planned_files(raw["circles"][circle_id], position)
            todo = [plan for plan in plans if saved_file(out_dir, records, plan) is None]
            if todo and not refreshed and any(link_expired(plan.url) for plan in todo):
                refresh(raw, raw_path, circle_id, connect, "its links have expired")
                refreshed = True
                continue
            result.errors, gone = [], []
            for plan in todo:
                try:
                    path = save_one(fetcher, out_dir, plan)
                except LinkGone as exc:
                    gone.append((plan, str(exc)))
                    continue
                except RateLimited:
                    raise RateLimitStop() from None
                except Exception as exc:  # one broken file should not stop the others
                    result.errors.append(f"{plan.stem}: {describe(exc)}")
                    say(f"    x {plan.stem}: {describe(exc)}")
                    continue
                records[plan.stem] = {"file": path.name, "media": plan.media}
                save_raw(raw_path, raw)
                result.new += 1
                say(f"    + {path.name} ({size_text(path.stat().st_size)})")
                if path.suffix == ".heic" and not heic_kept:
                    heic_kept = True
                    say("    ! HEIC picture kept as .heic: the converter (pillow-heif) is not installed.")
            if gone and not refreshed:
                refresh(raw, raw_path, circle_id, connect, "Instagram refused some links")
                refreshed = True
                continue
            for plan, reason in gone:
                result.errors.append(f"{plan.stem}: the link no longer works ({reason})")
                say(f"    x {plan.stem}: the link no longer works ({reason})")
            break
        plans = planned_files(raw["circles"][circle_id], position)
        stems = {plan.stem for plan in plans}
        extra = [s for s in records if s.startswith(f"{PREFIX}-{position:02d}-") and s not in stems]
        for stem in extra:
            set_aside(out_dir, stem)  # the circle has fewer stories on Instagram now
            del records[stem]
        if extra:
            save_raw(raw_path, raw)
        result.planned = len(plans)
        result.saved = sum(1 for plan in plans if saved_file(out_dir, records, plan))
        if result.saved - result.new:
            say(f"    = {result.saved - result.new} file(s) were already downloaded")


# ---------------------------------------------------------------------------
# Table, report.csv and manifest.json
# ---------------------------------------------------------------------------

def counts(circle: dict) -> tuple[int, int]:
    items = circle.get("items") or []
    videos = sum(1 for item in items if item["type"] == "video")
    return len(items) - videos, videos


def show_table(raw: dict, arrangement: Arrangement, base: Path) -> None:
    say()
    say("  #   Circle (your list)        Stories  Photos  Videos  Cover")
    say("  --  ------------------------  -------  ------  ------  -----")
    total = [0, 0, 0]
    for position, circle_id in enumerate(arrangement.order, 1):
        circle = raw["circles"][circle_id]
        photos, videos = counts(circle)
        total = [total[0] + photos + videos, total[1] + photos, total[2] + videos]
        say(f"  {position:02d}  {label(arrangement, circle_id):<24}  {photos + videos:>7}  {photos:>6}  "
            f"{videos:>6}  {'yes' if circle.get('cover') else 'no'}")
    say(f"  Total: {len(arrangement.order)} circles, {total[0]} stories ({total[1]} photos, {total[2]} videos)")
    say()
    report = shown(base / OUTPUT_DIR / REPORT_FILE, base)
    if arrangement.reordered:
        say("! Instagram gave the circles in another order than your list. Your list's order is used,")
        say("  and circles that are not in your list come after it.")
    else:
        say("- Order: the same as your list.")
    extras = [f"{p:02d}" for p, cid in enumerate(arrangement.order, 1) if cid not in arrangement.matched]
    if extras:
        say(f"- Circles that are not in your list: {', '.join(extras)} (their Arabic titles are in {report}).")
    if arrangement.missing:
        names = ", ".join(f"{i + 1} {EXPECTED[i][1]}" for i in arrangement.missing)
        say(f"! Not found on Instagram: {names}. Maybe the title changed: compare with {report}.")
    no_cover = [f"{p:02d}" for p, cid in enumerate(arrangement.order, 1) if not raw["circles"][cid].get("cover")]
    if not raw.get("covers_from_tray") and len(no_cover) == len(arrangement.order):
        say("! Instagram gave no covers this time; continuing without them.")
    elif no_cover:
        say(f"! No cover for circle(s): {', '.join(no_cover)}; continuing without them.")
    for position, circle_id in enumerate(arrangement.order, 1):
        circle = raw["circles"][circle_id]
        listed, got = circle.get("media_count"), len(circle.get("items") or [])
        if listed and listed != got:
            say(f"! Circle {position:02d}: Instagram lists {listed} stories but gave {got}.")


def report_rows(raw: dict, arrangement: Arrangement, out_dir: Path, results: dict[str, CircleResult] | None,
                dry_run: bool) -> list[list]:
    rows = []
    for position, circle_id in enumerate(arrangement.order, 1):
        circle = raw["circles"][circle_id]
        plans = planned_files(circle, position)
        found = {plan.stem: saved_file(out_dir, raw["files"], plan) for plan in plans}
        kinds = [file_kind(found[plan.stem], plan.kind) for plan in plans if plan.kind != "cover"]
        photos, videos = kinds.count("image"), kinds.count("video")
        saved = sum(1 for path in found.values() if path)
        result = (results or {}).get(circle_id)
        if plans and saved == len(plans):
            status = "اتحمّلت كاملة"
        elif not plans:
            status = "مافيهاش حاجة تتحمّل"
        elif dry_run:
            status = "لسه ماتحمّلتش (تجربة: بيانات بس)"
        elif result is None:
            status = "لسه ماتحمّلتش: شغّل run_lont.bat تاني يكمّل"
        else:
            status = f"ناقص {len(plans) - saved} من {len(plans)}"
            if result.errors:
                status += f" ({result.errors[0]})"
        notes = []
        if circle_id not in arrangement.matched:
            notes.append("مش في قايمتك")
        if not circle.get("cover"):
            notes.append("من غير غلاف")
        listed, got = circle.get("media_count"), photos + videos
        if listed and listed != got:
            notes.append(f"إنستجرام بيقول {listed} قصة ورجّع {got}")
        if notes:
            status += " - " + "، ".join(notes)
        rows.append([position, circle["title"], photos, videos, status])
    for index in arrangement.missing:
        rows.append(["-", EXPECTED[index][0], "", "", "مش لاقيها على إنستجرام: يمكن اسمها اتغيّر"])
    return rows


def write_report(path: Path, rows: list[list], base: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # the BOM lets Excel show Arabic
            writer = csv.writer(fh)
            writer.writerow(REPORT_COLUMNS)
            writer.writerows(rows)
        return True
    except OSError:
        say(f"! Could not write {shown(path, base)}. Is it open in Excel? Close it and run again.")
        return False


def write_manifest(path: Path, raw: dict, arrangement: Arrangement, out_dir: Path,
                   downloaded_at: str | None = None) -> None:
    highlights = []
    for position, circle_id in enumerate(arrangement.order, 1):
        circle = raw["circles"][circle_id]
        files = {plan.stem: saved_file(out_dir, raw["files"], plan) for plan in planned_files(circle, position)}

        def name(stem: str) -> str | None:
            found = files.get(stem)
            return found.name if found else None

        prefix = f"{PREFIX}-{position:02d}"
        items = [{"position": number, "file": name(f"{prefix}-{number:03d}"),
                  "type": file_kind(files.get(f"{prefix}-{number:03d}"), item["type"]),
                  "width": item["width"], "height": item["height"]}
                 for number, item in enumerate(circle.get("items") or [], 1)]
        highlights.append({"position": position, "title": circle["title"], "cover_file": name(f"{prefix}-cover"),
                           "items": items})
    manifest = {"profile": PROFILE, "downloaded_at_utc": downloaded_at or utc_now(), "highlights": highlights}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Download the story highlights of instagram.com/{PROFILE} in the profile's order.")
    parser.add_argument("--dry-run", action="store_true",
                        help="data only: show the circles and write output/report.csv, download nothing")
    parser.add_argument("--folder", type=Path, default=LONT_DIR, help=argparse.SUPPRESS)  # for the tests
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    base = args.folder.resolve()
    raw_path, out_dir = base / RAW_FILE, base / OUTPUT_DIR
    say(f"Lont highlights: instagram.com/{PROFILE}" + (" (dry run: data only)" if args.dry_run else ""))
    raw = load_raw(raw_path)
    instagram = None

    def connect() -> Instagram:
        nonlocal instagram
        if instagram is None:
            cookies = load_cookies(base)
            instagram = Instagram(cookies)
            say("- gallery-dl waits 6-12 seconds between requests to Instagram; that is normal.")
        return instagram

    if raw["complete_list"] and not pending(raw):
        say(f"- Data: {RAW_FILE} from an earlier run is complete, so Instagram is not asked again.")
    try:
        gather(raw, raw_path, connect)
        if not raw["order"]:
            raise UserError(f"Instagram gave no highlights for {PROFILE}.")
        arrangement = arrange(raw)
        show_table(raw, arrangement, base)
        report_path = out_dir / REPORT_FILE
        if args.dry_run:
            if write_report(report_path, report_rows(raw, arrangement, out_dir, None, True), base):
                say(f"- The circles with their Arabic titles: {shown(report_path, base)}")
            say("Dry run: nothing was downloaded. To download, run run_lont.bat without --dry-run.")
            return 0

        results: dict[str, CircleResult] = {}
        say(f"- Downloading to {shown(out_dir, base)} (straight from Instagram's CDN, no API requests) ...")
        try:
            download(raw, raw_path, arrangement, out_dir, Fetcher(), connect, results)
        finally:
            write_manifest(out_dir / MANIFEST_FILE, raw, arrangement, out_dir)
            write_report(report_path, report_rows(raw, arrangement, out_dir, results, False), base)

        files = [saved_file(out_dir, raw["files"], plan)
                 for position, cid in enumerate(arrangement.order, 1)
                 for plan in planned_files(raw["circles"][cid], position)]
        done = [path for path in files if path]
        errors = sum(len(result.errors) for result in results.values())
        say()
        say(f"Done: {len(done)} of {len(files)} files in {shown(out_dir, base)} "
            f"({size_text(sum(path.stat().st_size for path in done))}), "
            f"{sum(r.new for r in results.values())} of them downloaded in this run.")
        say(f"- Order and sizes: {shown(out_dir / MANIFEST_FILE, base)}; "
            f"circles with Arabic titles: {shown(report_path, base)}")
        if errors:
            say(f"! {errors} file(s) could not be downloaded (see report.csv). Run run_lont.bat again later.")
        return 1 if errors else 0
    finally:
        if instagram is not None:
            say(f"- Instagram requests in this run: {instagram.requests}")


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
    except RateLimitStop as exc:
        say(f"\nSTOPPED: {exc}")
        return 3
    except UserError as exc:
        say(f"\nSTOPPED: {exc}")
        return 2
    except KeyboardInterrupt:
        say("\nStopped. Everything saved so far is kept; run run_lont.bat again to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
