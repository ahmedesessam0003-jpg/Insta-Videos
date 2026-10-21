#!/usr/bin/env python3
"""
Download a hand-picked list of Instagram videos for the store's highlights.

The list is a WhatsApp chat, copied or exported to a text file, in which the
store owner sent the videos she chose: every Instagram link is one video, and
what she wrote after it (up to the next link) says which products and bundles
it is for. The videos are saved as HL-01.mp4, HL-02.mp4, ... in the order of
the chat, with a list to review in Excel and a ready-made prompt for the
Claude conversation that is connected to the Shopify store.

Usage:  python highlights.py chat.txt --dry-run   (read the chat only)
        python highlights.py chat.txt             (download)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import insta_videos as iv
from insta_videos import UserError, log

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = "Shopify highlights"  # created next to the products folder (output_dir)
UPLOAD_DIR = "upload"               # only the videos, so the whole folder can go to Shopify at once
REMOVED_DIR = "removed"             # videos whose link left the chat are moved here, never deleted
STATE_FILE = ".highlights-state.json"
LIST_FILE = "highlights_list.csv"
PROMPT_FILE = "shopify_prompt.txt"
PREFIX = "HL"


# ---------------------------------------------------------------------------
# Reading the chat
# ---------------------------------------------------------------------------

# Direction marks WhatsApp puts around dates and names, and the narrow spaces before am/pm.
_MARKS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SPACES = str.maketrans({"\u202f": " ", "\u00a0": " ", "\u2009": " "})
_DIGITS = str.maketrans({**{chr(0x0660 + d): str(d) for d in range(10)},
                         **{chr(0x06F0 + d): str(d) for d in range(10)}})
_DATE = r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}"
_TIME = r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?"
# "[11:40 am, 02/10/2026] Reem: ..." (copied from WhatsApp), "[2/10, 11:40 am] Reem: ..." (copied on a phone)
# or "[02/10/2026, 11:40:12 AM] Reem: ..." (iPhone export)
_BRACKETED = re.compile(rf"\[(?=[^\]]*{_TIME})[^\]]{{4,40}}\]\s*")
# "02/10/2026, 11:40 am - Reem: ..." (Android export, in any language)
_DASHED = re.compile(rf"{_DATE}[,،]?\s*{_TIME}[^-–\n]{{0,12}}?\s[-–]\s")
_SENDER = re.compile(r"([^:]{1,60}):\s?")
# What WhatsApp writes in place of media, deleted and edited messages.
_NOISE = re.compile(r"<[^<>\n]*(?:omitted|attached|edited|الوسائط|تعديل)[^<>\n]*>"
                    r"|\b(?:image|video|audio|sticker|GIF|document) omitted"
                    r"|This message was deleted|You deleted this message|تم حذف هذه الرسالة|لقد حذفت هذه الرسالة",
                    re.IGNORECASE)
_LINK = re.compile(r"(?:https?://)?(?:www\.|m\.)?instagram\.com/[^\s<>\"'«»()]+", re.IGNORECASE)
_SHARE = re.compile(r"instagram\.com/share/", re.IGNORECASE)


@dataclass
class Message:
    sender: str | None  # None: text without a WhatsApp header; "": a WhatsApp notice
    lines: list[str]


@dataclass(eq=False)
class Item:
    """One link of the chat, with what was written after it."""
    number: int
    link: str
    code: str  # the post's shortcode; "" for a share link until it is opened
    notes: list[str] = field(default_factory=list)
    repeated: int = 0

    @property
    def share(self) -> bool:
        return bool(_SHARE.search(self.link))

    @property
    def share_key(self) -> str:
        return self.link.split("?")[0].rstrip("/")


def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UserError(f"تعذر فتح ملف الشات {path}: {exc}") from None
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # Notepad's "Unicode"
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1256", errors="replace")  # Notepad's "ANSI" on an Arabic Windows


def split_header(line: str) -> tuple[str, str] | None:
    """(sender, text) when `line` starts a WhatsApp message; the sender is "" for a notice."""
    probe = line.translate(_DIGITS)  # one character for one, so positions still match `line`
    found = _BRACKETED.match(probe) or _DASHED.match(probe)
    if not found:
        return None
    rest = line[found.end():]
    sender = _SENDER.match(rest)
    if not sender:
        return "", rest
    return sender.group(1).strip(), rest[sender.end():]


def read_messages(text: str) -> list[Message]:
    messages: list[Message] = []
    for raw in text.splitlines():
        line = _MARKS.sub("", raw).translate(_SPACES)
        header = split_header(line)
        if header is not None:
            messages.append(Message(header[0], [header[1]]))
        elif messages:
            messages[-1].lines.append(line)  # the next line of a message with several lines
        else:
            messages.append(Message(None, [line]))
    return messages


def pick_messages(messages: list[Message], wanted: str | None) -> tuple[list[Message], list[str]]:
    """The messages of the person who sent the links, and notes about the messages left out.

    Without `wanted`, that person is whoever sent the most Instagram links.
    Text that is not a WhatsApp chat is taken whole.
    """
    links: Counter[str] = Counter()
    for message in messages:
        if message.sender:
            links[message.sender] += sum(len(_LINK.findall(line)) for line in message.lines)
    if not links:
        return [m for m in messages if m.sender is None], []
    if wanted:
        key = iv.name_key(wanted)
        chosen = {name for name in links if key and key in iv.name_key(name)}
        if not chosen:
            raise UserError(f"لا يوجد في الشات أحد باسم «{wanted}». الأسماء الموجودة: " + "، ".join(links))
    else:
        chosen = {max(links, key=links.__getitem__)}
    others = Counter(m.sender for m in messages if m.sender and m.sender not in chosen)
    notes = [f"رسائل «{name}» لم تُحسب ({count})" for name, count in others.items()]
    return [m for m in messages if m.sender in chosen], notes


def link_code(link: str) -> str | None:
    """The shortcode of a post or reel link, "" for a share link, None for any other link."""
    if _SHARE.search(link):
        return ""
    found = iv._SHORTCODE.search(re.sub("(?i)instagram\\.com", "instagram.com", link))
    return found.group(1) if found else None


def _clean(text: str) -> str:
    return " ".join(_NOISE.sub(" ", text).split()).strip("-–—•*·|:،, ")


def _short(text: str, size: int = 80) -> str:
    return text if len(text) <= size else text[:size - 1] + "…"


def collect_items(messages: list[Message]) -> tuple[list[Item], list[str]]:
    """One item per post link, in the order of the chat, with the text written after it; and warnings."""
    items: list[Item] = []
    seen: dict[str, Item] = {}
    current: Item | None = None
    before: list[str] = []
    warnings: list[str] = []

    def note(text: str) -> str:
        text = _clean(text)
        if text:
            (current.notes if current else before).append(text)
        return text

    for message in messages:
        for line in message.lines:
            start = 0
            for found in _LINK.finditer(line):
                previous = current
                text = note(line[start:found.start()])
                start = found.end()
                link = found.group(0).rstrip(".,،؛;:!?")
                code = link_code(link)
                if code is None:
                    warnings.append(f"رابط ليس لمنشور أو ريل، تم تجاهله: {link}")
                    continue
                key = code or link.split("?")[0].rstrip("/")
                if key in seen:
                    current = seen[key]
                    current.repeated += 1
                else:
                    current = seen[key] = Item(len(items) + 1, link, code)
                    items.append(current)
                if text and previous is not None and previous is not current:
                    warnings.append(f"«{_short(text, 40)}» مكتوب قبل الرابط رقم {current.number} في نفس السطر، "
                                    f"فحُسب للرابط رقم {previous.number}: تأكد أنه له")
            note(line[start:])
    if before:
        warnings.append("كلام قبل أول رابط لم يُحسب: " + _short(" / ".join(before)))
    for item in items:
        if item.repeated:
            warnings.append(f"الرابط رقم {item.number} مكرر في الشات: حُسب مرة واحدة وجُمع الكلام المكتوب بعده")
        if not item.notes:
            warnings.append(f"الرابط رقم {item.number} ليس بعده أي كلام: {item.link}")
    return items, warnings


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def load_cookies(path: Path | None) -> dict[str, str]:
    path = path or iv.find_cookie_file()
    if not path:
        raise UserError("لا يوجد ملف كوكيز: ضع cookies.txt بجانب السكربت (نفس ملف insta_videos.py) "
                        "أو استخدم --cookies.")
    cookies = iv.parse_cookie_file(path)
    if "sessionid" not in cookies:
        raise UserError("الكوكيز لا تحتوي على sessionid الخاص بإنستجرام. "
                        "سجّل الدخول لإنستجرام في المتصفح ثم صدّر الكوكيز من جديد.")
    log(f"• استخدام ملف الكوكيز: {path}")
    return cookies


class PostNotFound(Exception):
    pass


def open_link(backend, item: Item) -> tuple[iv.PostRecord | None, Exception | None]:
    """Read the post behind `item` with gallery-dl: (record, None) or (None, error)."""
    if item.share:  # gallery-dl follows the share link to the post
        url = re.sub(r"(?i)^(?:https?://)?(?:www\.|m\.)?instagram\.com", "https://www.instagram.com", item.link)
    else:
        url = f"https://www.instagram.com/p/{item.code}/"
    files, error = backend._extract(url)
    if error is not None:
        return None, error
    if not files:
        return None, PostNotFound("المنشور غير موجود أو محذوف")
    code = str(files[0].get("post_shortcode") or files[0].get("shortcode") or item.code)
    return backend._record(code, files), None


def stops_run(exc: Exception) -> bool:
    """Errors after which the next links would fail too: login, rate limit, checkpoint, no connection."""
    name = type(exc).__name__
    if name in ("AuthenticationError", "AuthRequired", "ChallengeError") or \
            getattr(exc, "status", None) in (401, 429):
        return True
    text = f"{name}: {exc}".lower()
    return any(s in text for s in ("redirect to", "checkpoint", "429", "too many", "wait a few minutes",
                                   "max retries exceeded", "name resolution", "getaddrinfo",
                                   "connection refused", "connection aborted", "timed out", "proxyerror"))


def post_is_gone(exc: Exception) -> bool:
    """A deleted or unavailable post: only that link fails, the next ones are fine."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return getattr(exc, "status", None) in (400, 404, 410) or \
        any(s in text for s in ("notfound", "not found", "could not be found", "unavailable"))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

class HighlightState:
    """What earlier runs found and saved, so a run resumes without asking Instagram again."""

    def __init__(self, path: Path):
        self.path = path
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UserError(f"ملف الحالة {path} تالف ({exc}). احذفه واحذف ما في مجلد {UPLOAD_DIR} "
                                "ثم أعد التشغيل.") from None
        self.links: dict[str, str] = data.get("links", {})  # share link -> shortcode
        self.posts: dict[str, dict] = data.get("posts", {})  # shortcode -> kind, date, caption, videos
        self.files: dict[str, str] = data.get("files", {})  # "<shortcode>_<position>" -> file in upload/

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = {"version": 1, "links": self.links, "posts": self.posts, "files": self.files}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def file_names(number: int, width: int, positions: list[int]) -> dict[int, str]:
    """HL-07.mp4 for a post with one video; HL-07-1.mp4, HL-07-2.mp4, ... for an album."""
    base = f"{PREFIX}-{number:0{width}d}"
    if len(positions) == 1:
        return {positions[0]: f"{base}.mp4"}
    return {position: f"{base}-{i}.mp4" for i, position in enumerate(sorted(positions), 1)}


def _rename(folder: Path, old: str, new: str) -> None:
    try:
        os.rename(folder / old, folder / new)
    except OSError as exc:
        raise UserError(f"تعذرت إعادة تسمية {folder / old}: {exc}\n"
                        "  أغلق أي برنامج يفتح هذا الفيديو ثم أعد التشغيل (سيكمل من حيث توقف).") from None


def _set_aside(upload: Path, removed: Path, name: str) -> None:
    removed.mkdir(parents=True, exist_ok=True)
    dest = removed / name
    if dest.exists():
        dest = removed / f"{uuid.uuid4().hex[:6]} {name}"
    os.replace(upload / name, dest)


LIST_COLUMNS = ["رقم", "اسم الملف", "المطلوب (من الشات)", "الحالة", "الرابط", "تاريخ المنشور",
                "وصف المنشور على إنستجرام"]


def write_list(path: Path, rows: list[dict]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:  # BOM so Excel shows Arabic
            writer = csv.DictWriter(fh, fieldnames=LIST_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in LIST_COLUMNS})
    except OSError as exc:  # e.g. the list is open in Excel
        log(f"! تعذر كتابة القائمة {path}: {exc}")
        return False
    return True


def build_prompt(entries: list[tuple[list[str], list[str]]], per_circle: int) -> str:
    """The message for the Claude conversation connected to the store.

    `entries`: for each link, its files and what was written after it.
    """
    entries = [([Path(name).stem for name in files], notes) for files, notes in entries]
    names = [name for files, _ in entries for name in files]
    lines = [
        "إنت متوصل بمتجري على Shopify من خلال الـ connector. عايزك تعمل الخطوات دي كلها بنفسك على المتجر.",
        "",
        f"رفعت في صفحة Files على Shopify (من قائمة Content) {len(names)} فيديو جديد للهايلايتس، "
        f"أساميها بتبدأ بـ {PREFIX}-. ممكن Shopify يكون زوّد حروف في آخر الاسم، ودا عادي.",
        "صاحبة المتجر اختارت الفيديوهات دي بالظبط، وكتبت مع كل فيديو هو لأنهي منتجات وباقات. "
        "في آخر الرسالة دي كل فيديو وكلامها زي ما كتبته بالظبط، وممكن يكون فيه أخطاء كتابة، فافهم المقصود.",
        "",
        "المطلوب:",
        "1. شوف الهايلايتس معمولة إزاي على المتجر.",
        "2. لكل فيديو، حدد من كلامها المنتجات والباقات المقصودة بأساميها الحقيقية في المتجر. "
        "الفيديو الواحد ممكن يتحط في أكتر من منتج أو باقة. ولو مكتوب «كل الباقات» أو «الباقات كلها» "
        "يبقى الفيديو لكل الباقات والبرامج اللي في المتجر، ولو مكتوب «المنتجات كلها» يبقى لكل المنتجات.",
        "3. كل منتج أو باقة ياخد بس الفيديوهات اللي كلامها بيقول إنها ليه. يعني الباقة متاخدش فيديوهات "
        "المنتجات اللي جواها، إلا لو اسم الباقة نفسه مكتوب مع الفيديو.",
        "4. امسح كل فيديوهات الهايلايتس الحالية من كل المنتجات والباقات، وحط الجديدة مكانها.",
        f"5. رتب فيديوهات كل منتج أو باقة بالأرقام ({names[0]} الأول). أقصى حاجة {per_circle} فيديوهات "
        f"في الدايرة الواحدة، ولو أكتر من {per_circle} اعمل دواير زيادة.",
        "6. قبل ما تمسح أو تغيّر أي حاجة، اعرض عليا الخطة في جدولين واستنى موافقتي:",
        "   أ) كل فيديو، والمنتجات والباقات اللي فهمتها من كلامها. وقولي على أي كلمة مش فاهمها "
        "أو ملقتلهاش منتج أو باقة.",
        "   ب) كل منتج أو باقة، وفيديوهاته بالترتيب، وعدد الدواير. وكمان المنتجات والباقات اللي "
        "هتفضل من غير فيديوهات.",
        "7. بعد التنفيذ ابعتلي تقرير باللي اتعمل، وبأي فيديو ملقتهوش في Files، وبأي حاجة معرفتش "
        "توصلها أو تعدلها.",
        "8. متمسحش أي ملف من Files (لا القديم ولا الجديد) غير لما أقولك.",
        "",
        "الفيديوهات وكلامها:",
    ]
    for files, notes in entries:
        lines.append(f"{' و '.join(files)}: " + (" / ".join(notes) or "(مكتوبش بعده حاجة: اسألني عليه)"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="تحميل فيديوهات محددة بالروابط من شات واتساب لهايلايتس المتجر، بأسماء HL-01 وHL-02 ... "
                    "بترتيب الشات، مع قائمة للمراجعة وأمر جاهز لمحادثة Claude المتصلة بالمتجر.")
    parser.add_argument("chat", type=Path,
                        help="ملف نصي فيه رسائل الشات (منسوخة من واتساب أو من Export chat). "
                             "كل رابط = فيديو، والكلام المكتوب بعده = المنتجات والباقات الخاصة به")
    parser.add_argument("--sender", help="اسم صاحبة الرسائل في الشات (الافتراضي: من أرسل أكثر روابط)")
    parser.add_argument("--out", type=Path,
                        help=f'مجلد الناتج (الافتراضي: "{DEFAULT_OUT}" بجانب مجلد المنتجات output_dir)')
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "products.json",
                        help="ملف الإعدادات، لمعرفة مجلد المنتجات ونسخ الفيديوهات الموجودة فيه بدل تحميلها")
    parser.add_argument("--cookies", type=Path, help="ملف الكوكيز (الافتراضي: cookies.txt بجانب السكربت)")
    parser.add_argument("--per-circle", type=int, default=4, metavar="N",
                        help="أقصى عدد فيديوهات في دائرة الهايلايت الواحدة، للأمر الخاص بشوبيفاي (الافتراضي 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="تجربة: قراءة الشات وكتابة القائمة فقط، بدون فتح إنستجرام أو تحميل أي شيء")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.per_circle < 1:
        raise UserError("--per-circle يجب أن يكون رقماً أكبر من صفر.")
    messages, skipped = pick_messages(read_messages(read_text(args.chat)), args.sender)
    items, warnings = collect_items(messages)
    if not items:
        raise UserError(f"لا يوجد أي رابط منشور أو ريل من إنستجرام في {args.chat.name}.")
    width = max(2, len(str(len(items))))
    log(f"• الروابط في الشات: {len(items)}")
    for item in items:
        log(f"  {item.number:0{width}d}  {item.code or item.link}  ←  {' / '.join(item.notes) or '(ليس بعده كلام)'}")
    for line in skipped + warnings:
        log(f"  ! {line}")

    config = iv.load_config(args.config) if args.config.is_file() else None
    library = config.output_dir.expanduser().resolve() if config else None
    root = (args.out or (library.parent if library else SCRIPT_DIR) / DEFAULT_OUT).expanduser().resolve()
    upload = root / UPLOAD_DIR
    list_path, prompt_path = root / LIST_FILE, root / PROMPT_FILE
    state = HighlightState(root / STATE_FILE)

    def base_row(item: Item) -> dict:
        post = state.posts.get(item.code, {})
        return {"رقم": item.number, "المطلوب (من الشات)": " / ".join(item.notes), "الرابط": item.link,
                "تاريخ المنشور": post.get("date", ""), "وصف المنشور على إنستجرام": post.get("caption", "")}

    if args.dry_run:
        if write_list(list_path, [{**base_row(item), "الحالة": "تجربة: لم يُحمَّل"} for item in items]):
            log(f"\n• القائمة للمراجعة: {list_path}")
        log("  (تجربة: لم يُفتح إنستجرام ولم يُحمَّل أي فيديو)")
        return 0

    # Forget files the user deleted; share links opened in an earlier run are known already.
    state.files = {key: name for key, name in state.files.items() if (upload / name).is_file()}
    for item in items:
        if item.share and item.share_key in state.links:
            item.code = state.links[item.share_key]

    def complete(item: Item) -> bool:
        post = state.posts.get(item.code) if item.code else None
        return post is not None and all(f"{item.code}_{n}" in state.files for n in post["videos"])

    errors: dict[int, str] = {}  # item number -> why it has no file
    urls: dict[str, tuple[str, iv.PostRecord]] = {}  # video key -> (download link, post)
    todo = [item for item in items if not complete(item)]
    if todo:
        backend = iv.GalleryDlBackend("", load_cookies(args.cookies))
        log(f"\n• فتح {len(todo)} رابط على إنستجرام (حوالي 10 ثوانٍ لكل رابط) ...")
        failures = 0
        for position, item in enumerate(todo):
            record, error = open_link(backend, item)
            if record is not None:
                failures = 0
                item.code = record.shortcode
                if item.share:
                    state.links[item.share_key] = item.code
                videos = record.get_videos()
                state.posts[item.code] = {"kind": record.kind, "date": f"{record.date:%Y-%m-%d %H:%M}",
                                          "caption": " ".join(record.caption.split()),
                                          "videos": [n for n, _ in videos]}
                for n, url in videos:
                    urls[f"{item.code}_{n}"] = (url, record)
                state.save()
                log(f"  ✓ {item.number:0{width}d} {item.code} (فيديوهات: {len(videos)})")
                continue
            # Three unexplained failures in a row stop the run too, but a deleted post is not counted,
            # otherwise a few deleted posts would block the links after them on every run.
            failures += 0 if post_is_gone(error) else 1
            errors[item.number] = f"تعذر فتح الرابط: {type(error).__name__}: {error}"
            log(f"  ✗ {item.number:0{width}d} {item.link}: {error}")
            if stops_run(error) or failures == 3:
                for hint in iv.explain_error(error, "gallery-dl", suggest_other=False).splitlines()[1:]:
                    log(hint)
                for rest in todo[position + 1:]:
                    errors[rest.number] = "لم يُفتح (توقف التشغيل قبله)"
                log("  توقف فتح الروابط هنا. بعد حل المشكلة أعد التشغيل وسيكمل الباقي فقط.")
                break

    # A share link can turn out to be the same post as another link.
    first: dict[str, Item] = {}
    for item in items:
        if item.code in first:
            twin = first[item.code]
            twin.notes += item.notes
            errors[item.number] = f"نفس فيديو الرابط رقم {twin.number}"
        elif item.code:
            first[item.code] = item

    wanted: dict[str, tuple[Item, str]] = {}  # video key -> (item, file name)
    for item in first.values():
        videos = state.posts.get(item.code, {}).get("videos")
        if videos:
            for n, name in file_names(item.number, width, videos).items():
                wanted[f"{item.code}_{n}"] = (item, name)

    own = {name.lower() for name in state.files.values()}
    taken = {p.name.lower() for p in upload.iterdir()} - own if upload.is_dir() else set()
    clash = sorted(name for _, name in wanted.values() if name.lower() in taken)
    if clash:
        raise UserError(f"في مجلد {upload} ملفات لم ينشئها السكربت بنفس أسماء الفيديوهات: "
                        + "، ".join(clash) + "\n  انقلها لمكان آخر ثم أعد التشغيل.")

    # Videos whose link left the chat are set aside, then the others take their new numbers.
    for key, name in list(state.files.items()):
        if key not in wanted:
            _set_aside(upload, root / REMOVED_DIR, name)
            del state.files[key]
            state.save()
            log(f"  ← نُقل إلى مجلد {REMOVED_DIR} (رابطه لم يعد في الشات): {name}")
    moves = [key for key, name in state.files.items() if wanted[key][1] != name]
    for key in moves:  # through temporary names, so numbers can swap
        temp = f".{uuid.uuid4().hex}.renaming"
        _rename(upload, state.files[key], temp)
        state.files[key] = temp
        state.save()
    for key in moves:
        _rename(upload, state.files[key], wanted[key][1])
        state.files[key] = wanted[key][1]
        state.save()
    if moves:
        log(f"! تغيّرت أرقام {len(moves)} فيديو لأن ترتيب الروابط في الشات تغيّر. إذا كنت رفعتها على "
            "شوبيفاي من قبل، فارفع مجلد upload من جديد.")

    library_state = None  # videos already in the products folder are copied instead of downloaded
    if library and (library / iv.STATE_FILE).is_file():
        try:
            library_state = iv.State(library / iv.STATE_FILE)
        except UserError as exc:
            log(f"! لن تُنسخ فيديوهات من مجلد المنتجات: {exc}")
    downloader = None
    statuses: dict[str, str] = {}
    counts = Counter()
    for key, (item, name) in wanted.items():
        if key in state.files:
            statuses[key] = "موجود من قبل"
            counts["kept"] += 1
            continue
        dest = upload / name
        upload.mkdir(parents=True, exist_ok=True)
        copy = iv._existing_copy(library, library_state, key) if library_state else None
        try:
            if copy is not None:
                shutil.copy2(copy, dest)
                status = "نُسخ من مجلد المنتجات"
            elif key in urls:
                url, record = urls[key]
                downloader = downloader or iv.Downloader()
                downloader.fetch(url, dest)
                iv._set_mtime(dest, record.date)
                status = "تم التحميل"
            else:
                raise RuntimeError(errors.get(item.number) or "لم يُفتح المنشور في هذا التشغيل")
        except Exception as exc:
            statuses[key] = f"فشل التحميل: {exc}"
            counts["failed"] += 1
            log(f"  ✗ {name}: {exc}")
            continue
        state.files[key] = name
        state.save()
        statuses[key] = status
        counts["copied" if copy is not None else "downloaded"] += 1
        source = "  (من مجلد المنتجات)" if copy is not None else ""
        log(f"  ↓ {name}  ({dest.stat().st_size / (1024 * 1024):.1f} MB){source}")

    rows, entries = [], []
    for item in items:
        keys = [key for key, (owner, _) in wanted.items() if owner is item]
        if not keys:
            post = state.posts.get(item.code)
            reason = errors.get(item.number) or ("المنشور ليس فيه فيديو" if post else "")
            rows.append({**base_row(item), "الحالة": reason})
            if item.number in errors and not reason.startswith("نفس فيديو"):
                counts["unopened"] += 1
            continue
        for key in keys:
            rows.append({**base_row(item), "اسم الملف": wanted[key][1], "الحالة": statuses[key]})
        ready = [wanted[key][1] for key in keys if key in state.files]
        if ready:
            entries.append((ready, item.notes))

    listed = write_list(list_path, rows)
    if entries:
        try:
            prompt_path.write_text(build_prompt(entries, args.per_circle), encoding="utf-8-sig")
        except OSError as exc:
            log(f"! تعذر كتابة الأمر {prompt_path}: {exc}")
            entries = []
    failed = counts["failed"] + counts["unopened"]

    log("\nالملخص:")
    log(f"  فيديوهات جاهزة للرفع: {sum(len(names) for names, _ in entries)} (تحميل جديد {counts['downloaded']}، "
        f"منسوخ من مجلد المنتجات {counts['copied']}، موجود من قبل {counts['kept']})")
    if failed:
        log(f"  ✗ لم يكتمل: {failed} (السبب في القائمة؛ أعد التشغيل لاحقاً وسيكمل الناقص فقط)")
    log(f"  الفيديوهات: {upload}")
    if listed:
        log(f"  القائمة للمراجعة: {list_path}")
    if entries:
        log(f"  الأمر لمحادثة شوبيفاي: {prompt_path}")
        log("\nالخطوات التالية:\n"
            f"  1. ارفع كل الفيديوهات التي في {upload} على شوبيفاي (صفحة Files من قائمة Content).\n"
            f"  2. افتح {prompt_path.name} وانسخ كل ما فيه في محادثة Claude المتصلة بالمتجر.")
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
        log(f"✗ {exc}")
        return 2
    except KeyboardInterrupt:
        log("\n✗ تم الإيقاف. ما تم تحميله محفوظ، وعند إعادة التشغيل سيكمل من حيث توقف.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
