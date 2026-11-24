"""Private files shared by the receiver, route tool, and inbox watcher."""
import datetime
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Optional

ROUTE_NAME = re.compile(r"[a-z0-9_-]{1,40}")
NOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,123}\.md")
MUSE_RECORD = "(sent to Muse; the text is not kept here)"
WEEK = 7 * 86400


@contextmanager
def note_lock(home):
    """Serialize corrections with other corrections and the watcher's move to done/."""
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, ".route-lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_text(path, text, *, times_ns=None):
    fd, temp = tempfile.mkstemp(prefix=".ring-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if times_ns is not None:
            os.utime(temp, ns=times_ns)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def parse_note(raw):
    if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
        return {}, raw.strip()
    head, text = raw[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        key, _, value = line.partition(": ")
        meta[key] = value
    return meta, text.strip()


def read_note(path):
    return parse_note(path.read_text(encoding="utf-8"))


def mark_shown(path, raw):
    meta, _ = parse_note(raw)
    if meta and meta.get("shown") != "true":
        stat = os.stat(path)
        # Preserve the arrival time of old bookmark notes, which use mtime.
        head, body = raw[4:].split("\n---\n", 1)
        lines = [line for line in head.splitlines() if not line.startswith("shown: ")]
        marked = "---\n" + "\n".join(["shown: true"] + lines) + "\n---\n" + body
        atomic_text(path, marked, times_ns=(stat.st_atime_ns, stat.st_mtime_ns))


def muse_move_path(home, name):
    return Path(home) / "muse-moving" / name


def parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def note_time(path, meta):
    """Use arrival time. Older bookmark notes have only a file timestamp."""
    received = parse_time(meta.get("received_at", ""))
    if received is not None:
        return received
    try:
        stamp = datetime.datetime.strptime(path.name.split("-", 1)[0], "%Y%m%dT%H%M%SZ")
        return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return path.stat().st_mtime


def note_route(meta):
    return meta.get("routed_to") or meta.get("route")


def note_folders(home):
    root = Path(home)
    # Include old receiver folders even if the config has changed since delivery.
    folders = [root / folder for folder in ("muse-sent", "muse-failed", "done", "inbox")]
    folders += sorted((root / "inbox").glob("*/"))
    return [folder for folder in folders if folder.is_dir() and not folder.is_symlink()]


def routed_notes(home, name=None):
    found = {}
    for folder in note_folders(home):
        for path in ([folder / name] if name else folder.glob("*.md")):
            if path.name in found or not NOTE_NAME.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                meta, text = read_note(path)
                if note_route(meta) and meta.get("routed_by"):
                    found[path.name] = (note_time(path, meta), path, meta, text)
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                continue
    return sorted(found.values())


BOOKMARK_NAME = re.compile(r"x-bookmark-([0-9]+)\.md")
BOOKMARK_HEADER = re.compile(
    r"^(?:The owner bookmarked this X post by @\w+:\s*|"
    r"X bookmark(?:\s+(https?://\S+))?\s+@\w+:\s*|X bookmark\s+(https?://\S+)(?:\s+|$)|"
    r"x-bookmark-([0-9]+)\.md(?:\s+|$))", re.I)
BOOKMARK_LINKS = re.compile(r"(?:^|\s)Links:(?=\s|$)", re.I)
BOOKMARK_STATUS = re.compile(r"/status/([0-9]+)(?=\W|$)")


def bookmark_parts(text):
    """Return the post, any id in the header, and whether it had a legacy wrapper."""
    header = BOOKMARK_HEADER.match(text)
    bookmark_id = None
    if header:
        url = header[1] or header[2] or ""
        match = BOOKMARK_STATUS.search(url)
        bookmark_id = header[3] or (match[1] if match else None)
        text = text[header.end():]
    # New notes use a separate Links: block. Old TSV rows folded it onto one line.
    index = text.rfind("\n\nLinks:")
    if index < 0:
        footer = None
        for marker in BOOKMARK_LINKS.finditer(text):
            footer = marker
        if footer:
            links = text[footer.end():].split()
            if links in (["none"], ["-"]) or all(link.startswith(("https://", "http://")) for link in links):
                index = footer.start()
    return text[:index].strip() if index >= 0 else text.strip(), bookmark_id, bool(header or index >= 0)


def bookmark_note_text(text):
    """New bookmark bodies have a Links block and may begin with any post text."""
    post, footer, _ = text.rpartition("\n\nLinks:")
    return post.strip() if footer else bookmark_parts(text)[0]


def normalized(text):
    return " ".join(text.split())


class Example(NamedTuple):
    route: str
    text: str
    kind: str
    bookmark_id: Optional[str] = None
    origin: str = "mac"

    @property
    def match_text(self):
        return bookmark_parts(self.text)[0] if self.kind == "bookmark" else self.text


class Examples:
    def __init__(self, repo, private):
        self.repo = repo
        self.private = private
        self.cache = {}

    def read(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                stat = os.fstat(f.fileno())
                version = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
                cached = self.cache.get(path)
                if cached and cached[0] == version:
                    return cached[1]
                rows = []
                for line in f:
                    route, tab, text = line.partition("\t")
                    if tab and not line.startswith("#") and text.strip():
                        columns = text.rstrip("\r\n").split("\t")
                        kind, bookmark_id = "", None
                        if len(columns) == 2 and columns[1] in ("ring", "bookmark"):
                            text, kind = columns
                        elif len(columns) == 3 and columns[1] in ("", "bookmark") and columns[2].isdigit():
                            text, _, bookmark_id = columns
                        elif len(columns) == 2 and columns[1].isdigit():
                            text, bookmark_id = columns
                        _, header_id, wrapped = bookmark_parts(text)
                        kind = kind or ("bookmark" if wrapped or bookmark_id else "")
                        rows.append(Example(route.strip(), text.strip(), kind, bookmark_id or header_id))
        except FileNotFoundError:
            self.cache.pop(path, None)
            return []
        self.cache[path] = version, rows
        return rows

    def all(self):
        seen = set()
        rows = []
        for path, source in ((self.repo, "repo"), (self.private, "mac")):
            for row in self.read(path):
                key = row.route, row.kind, normalized(row.match_text)
                if key not in seen:
                    seen.add(key)
                    rows.append(row._replace(origin=source))
        return rows

    def save(self, route, text, *, kind, bookmark_id=None, keep=True):
        """Set the private label for this text. The caller holds note_lock."""
        bare = normalized(bookmark_note_text(text) if kind == "bookmark" else text)
        if not bare and not bookmark_id:
            return False
        example = Example(route, bare, kind, bookmark_id)
        old = self.read(self.private)
        exact = normalized(text)
        def matches(row):
            if kind == "bookmark":
                return (bool(bookmark_id and bookmark_id == row.bookmark_id) or
                        normalized(row.text) in (bare, exact) or normalized(row.match_text) == bare)
            return normalized(row.text) == exact
        rows = [row for row in old if not matches(row) or (keep and row == example)]
        if keep and bare and example not in rows:
            rows.append(example)
        if rows == old:
            return False
        atomic_text(self.private, "# Private route examples. Receiver, note, type, optional bookmark id.\n" +
                    "".join("\t".join((row.route, normalized(row.text)) + ((row.kind,) if row.kind else ()) +
                                      ((row.bookmark_id,) if row.bookmark_id else ())) + "\n" for row in rows))
        return True
