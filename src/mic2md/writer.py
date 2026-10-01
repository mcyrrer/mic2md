"""Session Markdown file: incremental raw transcript, then polished rewrite."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

TRANSCRIPTS_DIR = "transcripts"
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# Files polished by older versions kept the raw transcript in a <details> section.
_RAW_SECTION = re.compile(
    r"<details>\s*<summary>Raw transcript</summary>\s*(.*?)\s*</details>", re.S
)
SUMMARY_OPEN = "<!-- mic2md:summary -->"
SUMMARY_CLOSE = "<!-- /mic2md:summary -->"
# Also matches the markers written before the rename from voice2text.
_SUMMARY_SECTION = re.compile(
    r"<!-- (?:mic2md|voice2text):summary -->\n?(.*?)\n?<!-- /(?:mic2md|voice2text):summary -->"
    r"\s*(?:---\s*)?",
    re.S,
)
_TRANSCRIPT_HEADING = re.compile(r"\A\s*# Transcript [^\n]*\n")
NOTES_OPEN = "<!-- mic2md:notes -->"
NOTES_CLOSE = "<!-- /mic2md:notes -->"
_NOTES_SECTION = re.compile(r"\s*<!-- mic2md:notes -->\n?(.*?)\n?<!-- /mic2md:notes -->\s*", re.S)
NOTES_HEADINGS = {"en": "Notes", "sv": "Anteckningar"}
# A typed note in the raw transcript, before the session was finalized (e.g. after a crash).
NOTE_MARK = "NOTE:"
_RAW_NOTE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] NOTE: (.*)$", re.M)
# One note in a finished notes block: "- **HH:MM:SS** text" (time optional).
_NOTE_ITEM = re.compile(r"^- (?:\*\*(\d\d:\d\d:\d\d)\*\* )?(.*)$", re.M)


_SLUG_CHARS = re.compile(r"[^A-Za-z0-9]+")
_TITLE_MAX_LEN = 20


def slugify_title(title: str) -> str:
    """Lowercase-hyphenated, max 20 chars; empty if ``title`` has no word characters."""
    slug = _SLUG_CHARS.sub("-", title.strip()).strip("-").lower()
    return slug[:_TITLE_MAX_LEN].rstrip("-")


def session_filename(started: datetime, title: str = "") -> str:
    """ISO 8601 timestamp (``-`` instead of ``:``) plus an optional ``-<slug>`` suffix."""
    slug = slugify_title(title)
    suffix = f"-{slug}" if slug else ""
    return started.strftime("%Y-%m-%dT%H-%M-%S") + suffix + ".md"


def session_path(output_dir: Path, started: datetime, title: str = "") -> Path:
    """``<output_dir>/transcripts/YYYY-MM/<ISO timestamp>[-<title slug>].md``."""
    return output_dir / TRANSCRIPTS_DIR / f"{started:%Y-%m}" / session_filename(started, title)


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def render_front_matter(meta: dict[str, str]) -> str:
    return "---\n" + "".join(f"{k}: {v}".rstrip() + "\n" for k, v in meta.items()) + "---\n"


def format_tags(tags: list[str]) -> str:
    """One-line YAML flow list (``[a, b]``): stays flat, and Obsidian reads it as tags."""
    return "[" + ", ".join(tags) + "]" if tags else ""


def parse_tags(value: str) -> list[str]:
    """Inverse of :func:`format_tags`; also accepts a bare comma-separated list."""
    return [t.strip().strip("\"'") for t in value.strip().strip("[]").split(",") if t.strip()]


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """(front matter, everything after it), for any Markdown file."""
    meta: dict[str, str] = {}
    m = _FRONT_MATTER.match(text)
    if not m:
        return meta, text
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, text[m.end() :]


def format_notes(notes: list[str], language: str) -> str:
    """The notes block for the end of a document; ``notes`` are ``[HH:MM:SS] text`` lines."""
    items = []
    for note in notes:
        m = re.match(r"\[(\d\d:\d\d:\d\d)\] (.*)", note, re.S)
        items.append(f"- **{m[1]}** {m[2]}" if m else f"- {note}")
    heading = NOTES_HEADINGS.get(language, NOTES_HEADINGS["en"])
    return f"{NOTES_OPEN}\n## {heading}\n\n" + "\n".join(items) + f"\n{NOTES_CLOSE}\n"


def extract_notes(text: str) -> list[str]:
    """Typed notes as ``[HH:MM:SS] text`` lines, from a notes block or raw ``NOTE:`` lines."""
    if m := _NOTES_SECTION.search(text):
        return [
            f"[{t}] {note}" if t else note
            for t, note in _NOTE_ITEM.findall(m.group(1))
            if note.strip()
        ]
    return [f"[{t}] {note}" for t, note in _RAW_NOTE.findall(text)]


def parse_document(text: str) -> tuple[dict[str, str], str]:
    """Split a session file into (front matter, transcript).

    The transcript is the raw text when the file still has it, otherwise the polished body.
    A summary block added by ``summarize`` and typed notes are never part of it.
    """
    meta, body = split_front_matter(text)
    body = _SUMMARY_SECTION.sub("", body, count=1)
    body = _NOTES_SECTION.sub("\n", body, count=1)
    body = re.sub(r"\n*" + _RAW_NOTE.pattern + r"\n?", "\n", body, flags=re.M)
    if m := _RAW_SECTION.search(body):
        return meta, m.group(1).strip()
    return meta, _TRANSCRIPT_HEADING.sub("", body).strip()


class SessionWriter:
    """Writes the raw transcript to disk as it is produced so nothing is lost on a crash."""

    def __init__(
        self,
        output_dir: Path,
        started: datetime,
        language: str,
        whisper_model: str,
        extra_meta: dict[str, str] | None = None,
    ) -> None:
        self.started = started.astimezone()
        self.path = session_path(output_dir, self.started, (extra_meta or {}).get("meeting", ""))
        self.meta = {
            "date": self.started.isoformat(timespec="seconds"),
            "language": language,
            "whisper_model": whisper_model,
            **(extra_meta or {}),
        }
        self.lines: list[str] = []
        # Typed notes, "[HH:MM:SS] text"; kept apart so raw_text is speech only.
        self.notes: list[str] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        heading = f"# Transcript {self.started:%Y-%m-%d %H:%M}\n\n"
        self.path.write_text(render_front_matter(self.meta) + "\n" + heading, encoding="utf-8")

    @property
    def raw_text(self) -> str:
        return "\n\n".join(self.lines)

    def append(self, text: str, at: float | None = None) -> None:
        """Add a finished line; ``at`` (seconds into the session) becomes a [HH:MM:SS] prefix."""
        if at is not None:
            text = f"[{format_duration(timedelta(seconds=at))}] {text}"
        self.lines.append(text)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n\n")

    def append_note(self, text: str, at: float | None = None) -> None:
        """Add a typed note; it goes to the file right away, marked so it isn't speech."""
        text = " ".join(text.split())
        stamp = format_duration(timedelta(seconds=at or 0))
        self.notes.append(f"[{stamp}] {text}")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {NOTE_MARK} {text}\n\n")

    def finalize(self, polished: str | None, llm_model: str | None, ended: datetime) -> None:
        self.meta["duration"] = format_duration(ended.astimezone() - self.started)
        if polished and llm_model:
            self.meta["llm_model"] = llm_model
        write_final(self.path, self.meta, self.raw_text, polished, notes=self.notes)

    def discard_if_empty(self) -> bool:
        if not self.lines and not self.notes:
            self.path.unlink(missing_ok=True)
            return True
        return False


def extract_summary(text: str) -> str | None:
    """The summary block's contents, if the document has one."""
    m = _SUMMARY_SECTION.search(text)
    return m.group(1).strip() if m else None


def import_document(output_dir: Path, started: datetime, meta: dict[str, str], body: str) -> Path:
    """Copy an external text into ``transcripts/YYYY-MM/`` as a session file.

    If a session already uses that second, the next free second is taken so the file name
    keeps the session pattern (and shows up in the index).
    """
    title = meta.get("meeting", "")
    path = session_path(output_dir, started, title)
    while path.exists():
        started += timedelta(seconds=1)
        path = session_path(output_dir, started, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_front_matter(meta) + "\n" + body.strip() + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def insert_summary(path: Path, meta: dict[str, str], text: str, summary: str) -> None:
    """Put ``summary`` at the top of the body (replacing an old one); keep the rest verbatim."""
    body = text[m.end() :] if (m := _FRONT_MATTER.match(text)) else text
    body = _SUMMARY_SECTION.sub("", body.lstrip(), count=1)
    content = (
        render_front_matter(meta)
        + f"\n{SUMMARY_OPEN}\n{summary.strip()}\n{SUMMARY_CLOSE}\n\n---\n\n"
        + body.lstrip()
    )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def update_front_matter(path: Path, meta: dict[str, str]) -> None:
    """Replace only the front matter; the body stays byte-for-byte the same."""
    _, body = split_front_matter(path.read_text(encoding="utf-8"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_front_matter(meta) + "\n" + body.lstrip("\n"), encoding="utf-8")
    tmp.replace(path)


def write_final(
    path: Path,
    meta: dict[str, str],
    raw: str,
    polished: str | None,
    summary: str | None = None,
    notes: list[str] | None = None,
) -> None:
    content = render_front_matter(meta) + "\n"
    if summary:
        content += f"{SUMMARY_OPEN}\n{summary.strip()}\n{SUMMARY_CLOSE}\n\n---\n\n"
    if polished:
        content += polished.strip() + "\n"
    else:
        started = meta.get("date", "")[:16].replace("T", " ")
        content += f"# Transcript {started}\n\n" + raw + "\n"
    if notes:
        content += "\n" + format_notes(notes, meta.get("language", "en"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
