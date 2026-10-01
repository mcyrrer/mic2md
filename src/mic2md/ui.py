"""Live terminal view while recording.

Inline mode: finished lines scroll up, the current partial and a status bar stay below.
Fullscreen mode (alternate screen): header, transcript panel, session sidebar and a mic level
history across the bottom, sized to the terminal. After recording it stays up for the LLM
passes: the transcript panel streams their output and the sidebar lists the steps.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections import deque
from pathlib import Path

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

BARS = " ▁▂▃▄▅▆▇█"
# Mic history: one column per sample, sampled every LEVEL_SAMPLE_S (the peak in between).
LEVEL_SAMPLE_S = 0.1
LEVEL_HISTORY = 600
# Below this width the sidebar is dropped and the transcript takes the whole row.
SIDEBAR_MIN_WIDTH = 100
SIDEBAR_WIDTH = 36


def _fill(level: float) -> float:
    """``level`` (RMS) as 0..1 on a log scale, -60 dBFS .. 0 dBFS."""
    db = 20 * math.log10(level) if level > 0 else -60.0
    return max(0.0, min(1.0, (db + 60) / 60))


def level_meter(level: float, speaking: bool, width: int = 8) -> Text:
    """Log-scaled input meter; green while speech is detected."""
    filled = _fill(level) * width
    chars = "".join(
        BARS[round(max(0.0, min(1.0, filled - i)) * (len(BARS) - 1))] for i in range(width)
    )
    return Text(chars, style="green" if speaking else "grey50")


def level_history(samples: list[tuple[float, bool]], width: int) -> Text:
    """One bar per sample, right-aligned to ``width``; green where speech was detected."""
    out = Text(" " * max(0, width - len(samples)))
    for level, speaking in samples[-width:] if width > 0 else []:
        out.append(
            BARS[round(_fill(level) * (len(BARS) - 1))], style="green" if speaking else "grey42"
        )
    return out


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


UNCERTAIN_STYLE = "underline yellow"
NOTE_STYLE = "yellow"
NOTE_ICON = "✎"


def highlight_uncertain(text: str, uncertain: set[str]) -> Text:
    """``text`` with the words Whisper was unsure of underlined (keys as in ``word_key``)."""
    out = Text(text)
    for m in re.finditer(r"[\w'’-]+", text):
        if re.sub(r"\W", "", m[0].lower()) in uncertain:
            out.stylize(UNCERTAIN_STYLE, m.start(), m.end())
    return out


def _hanging(gutter: Text, text: Text) -> Table:
    """``gutter`` (timestamp) beside ``text``, so wrapped lines stay indented."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=8, no_wrap=True)
    grid.add_column()
    grid.add_row(gutter, text)
    return grid


# Half-width katakana (one terminal cell each), digits and a few symbols.
MATRIX_GLYPHS = "".join(chr(c) for c in range(0xFF66, 0xFF9E)) + "0123456789Z:.=*+-<>|"
MATRIX_HEAD = Style(color="#e6ffe6", bold=True)
MATRIX_NEAR = Style(color="#00e040")
MATRIX_FAR = Style(color="#006b1e")


class MatrixRain:
    """Falling green glyphs, one drop per column, advanced by wall-clock time."""

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.size = (0, 0)
        self.glyphs: list[list[str]] = []
        # Per column: [head row (float), speed in rows/s, trail length].
        self.drops: list[list[float]] = []
        self._last = time.monotonic()

    def _drop(self, height: int, start: float) -> list[float]:
        return [start, self.rng.uniform(6.0, 20.0), self.rng.randint(4, max(5, height))]

    def _resize(self, width: int, height: int) -> None:
        self.size = (width, height)
        pick = self.rng.choice
        self.glyphs = [[pick(MATRIX_GLYPHS) for _ in range(width)] for _ in range(height)]
        self.drops = [self._drop(height, self.rng.uniform(-height, height)) for _ in range(width)]

    def frame(self, width: int, height: int, now: float | None = None) -> list[list[tuple]]:
        """``height`` rows of ``width`` cells, each ``(glyph, style)`` or None (empty)."""
        now = time.monotonic() if now is None else now
        if (width, height) != self.size:
            self._resize(width, height)
        dt = min(max(0.0, now - self._last), 0.5)
        self._last = now
        for col, drop in enumerate(self.drops):
            drop[0] += drop[1] * dt
            if drop[0] - drop[2] > height:
                self.drops[col] = self._drop(height, -self.rng.uniform(0, height))
        for _ in range(max(1, width * height // 50)):  # a few glyphs flicker each frame
            if width and height:
                row = self.rng.randrange(height)
                self.glyphs[row][self.rng.randrange(width)] = self.rng.choice(MATRIX_GLYPHS)
        grid: list[list[tuple]] = [[None] * width for _ in range(height)]
        for col, (head, _, length) in enumerate(self.drops):
            for row in range(max(0, math.ceil(head - length)), min(height, math.floor(head) + 1)):
                dist = head - row
                style = (
                    MATRIX_HEAD if dist < 1 else MATRIX_NEAR if dist < length / 2 else MATRIX_FAR
                )
                grid[row][col] = (self.glyphs[row][col], style)
        return grid


def _overlay(line: list[Segment], background: list[tuple]) -> list[Segment]:
    """``line`` with ``background`` glyphs in its blank cells, keeping a blank next to text."""
    cells: list[tuple[str, Style | None]] = []
    for seg in line:
        if seg.control:
            continue
        for ch in seg.text:
            cells.append((ch, seg.style))
            cells.extend(("", seg.style) for _ in range(Segment(ch).cell_length - 1))
    text = [ch not in (" ", "") for ch, _ in cells]
    out: list[Segment] = []
    for i, (ch, style) in enumerate(cells):
        if not ch:
            continue
        bg = background[i] if i < len(background) else None
        blank = ch == " " and (style is None or style.bgcolor is None)
        near_text = (i > 0 and text[i - 1]) or (i + 1 < len(cells) and text[i + 1])
        if bg is not None and blank and not near_text:
            out.append(Segment(*bg))
        else:
            out.append(Segment(ch, style))
    return Segment.simplify(out)


class BottomAligned:
    """Renders the tail of ``items`` that fits the available height, newest at the bottom.

    With a ``background`` (``MatrixRain``), its glyphs fill the cells the text leaves empty.
    """

    def __init__(
        self,
        items: list,
        placeholder: Text | None = None,
        background: MatrixRain | None = None,
    ) -> None:
        self.items = items
        self.placeholder = placeholder
        self.background = background

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        height = options.height or options.max_height
        width = options.max_width
        if self.background is None and not self.items and self.placeholder is not None:
            yield self.placeholder
            return
        free = options.reset_height()
        pad = self.background is not None
        lines: list[list[Segment]] = []
        if not self.items and self.placeholder is not None:
            lines = console.render_lines(self.placeholder, free, pad=pad)[:height]
            lines += [[Segment(" " * width)] for _ in range(height - len(lines))]
        else:
            for item in reversed(self.items):
                lines[:0] = console.render_lines(item, free, pad=pad)
                if len(lines) >= height:
                    break
            lines = lines[-height:]
            blank = [Segment(" " * width)] if pad else []
            lines = [blank for _ in range(height - len(lines))] + lines
        if self.background is not None:
            grid = self.background.frame(width, height)
            lines = [_overlay(line, row) for line, row in zip(lines, grid, strict=True)]
        for line in lines:
            yield from line
            yield Segment.line()


class LlmProgress:
    """One LLM pass; as a renderable it is the spinner text, so the elapsed time ticks."""

    def __init__(self, task: str, label: str) -> None:
        self.task, self.label = task, label
        self.chars = 0
        self.started = time.monotonic()
        self.ended: float | None = None
        # running, done, failed or cancelled
        self.status = "running"

    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def end(self, status: str) -> None:
        self.status, self.ended = status, time.monotonic()

    def __rich__(self) -> Text:
        elapsed = int(self.elapsed())
        if self.chars == 0:
            return Text(f"Waiting for {self.label} (loading, reading the text)… {elapsed}s")
        return Text(
            f"{self.task.capitalize()} with {self.label}… {self.chars:,} characters · {elapsed}s"
        )


STEP_ICONS = {
    "running": ("⋯", "magenta"),
    "done": ("✔", "green"),
    "failed": ("✗", "red"),
    "cancelled": ("✗", "yellow"),
}
# How many log messages the sidebar keeps visible.
LOG_LINES = 6


class LiveView:
    def __init__(
        self,
        language: str,
        model_name: str,
        *,
        fullscreen: bool = False,
        meeting: str = "",
        participants: str = "",
        path: str = "",
    ) -> None:
        self.language = language
        self.model_name = model_name
        self.fullscreen = fullscreen
        self.meeting = meeting
        self.participants = participants
        self.path = path
        self.started = time.monotonic()
        self.partial = ""
        self.level = 0.0
        self.speaking = False
        self.calibrated = False
        self.busy = False
        # Finished utterances and typed notes in time order:
        # (seconds into the session or None, text, is_note).
        self.lines: list[tuple[float | None, Text, bool]] = []
        self.sentences = 0
        self.words = 0
        self.unsure = 0
        self.notes = 0
        # The note being typed (None when the editor is closed) and when it was opened.
        self.note: str | None = None
        self.note_at = 0.0
        # Matrix rain behind the fullscreen transcript (toggled with `m`).
        self.matrix: MatrixRain | None = None
        self.levels: deque[tuple[float, bool]] = deque(maxlen=LEVEL_HISTORY)
        self._peak = 0.0
        self._peak_speaking = False
        self._last_sample = self.started
        # After recording (fullscreen): when it stopped, what is happening now, the LLM
        # passes, the output of the latest one that streams text, and short messages.
        self.stopped: float | None = None
        self.stage = ""
        self.tasks: list[LlmProgress] = []
        self.output_title = ""
        self.output: str | None = None
        self.log: list[Text] = []
        self.done = False

    def elapsed(self) -> str:
        return hms((self.stopped or time.monotonic()) - self.started)

    def stop(self) -> None:
        """Recording ended: freeze the clock and the mic, keep the view for the LLM passes."""
        if self.stopped is None:
            self.stopped = time.monotonic()
        self.partial, self.note, self.speaking, self.busy = "", None, False, False

    def current_task(self) -> LlmProgress | None:
        return next((t for t in reversed(self.tasks) if t.status == "running"), None)

    def add_line(self, text: Text, at: float | None = None, unsure: int = 0) -> None:
        self._insert(at, text, False)
        self.sentences += 1
        self.words += len(text.plain.split())
        self.unsure += unsure

    def add_note(self, text: str, at: float) -> Text:
        """Show a saved note; returns it styled, for printing outside the live view."""
        line = Text(f"{NOTE_ICON} {text}", style=NOTE_STYLE)
        self._insert(at, line, True)
        self.notes += 1
        return line

    def _insert(self, at: float | None, text: Text, is_note: bool) -> None:
        # A sentence can finish transcribing after a note typed while it was spoken.
        i = len(self.lines)
        if at is not None:
            while i and (prev := self.lines[i - 1][0]) is not None and prev > at:
                i -= 1
        self.lines.insert(i, (at, text, is_note))

    def toggle_matrix(self) -> None:
        self.matrix = None if self.matrix is not None else MatrixRain()

    def update_level(self, level: float, speaking: bool) -> None:
        """Set the current level and feed the history (peak per LEVEL_SAMPLE_S)."""
        self.level, self.speaking = level, speaking
        self._peak = max(self._peak, level)
        self._peak_speaking |= speaking
        now = time.monotonic()
        if now - self._last_sample >= LEVEL_SAMPLE_S:
            self.levels.append((self._peak, self._peak_speaking))
            self._peak, self._peak_speaking = 0.0, False
            self._last_sample = now

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.fullscreen:
            yield self._layout(options.max_width)
        else:
            yield self._inline()

    # Inline mode

    def _inline(self) -> Group:
        parts = []
        if self.partial:
            parts.append(Text(self.partial, style="italic grey62"))
        status = Text()
        status.append(" ● REC ", style="bold white on red")
        status.append(f" {self.elapsed()}  ", style="bold")
        status.append(f"{self.language} · {self.model_name}  ", style="cyan")
        if self.note is not None:
            status.append_text(self._note_editor())
            status.append("   Enter save · Esc cancel", style="dim")
            parts.append(status)
            return Group(*parts)
        if self.calibrated:
            status.append("mic ", style="dim")
            status.append_text(level_meter(self.level, self.speaking))
        else:
            status.append("calibrating — stay quiet…", style="yellow")
        if self.busy:
            status.append("  ⋯", style="magenta")
        status.append("   n note · Ctrl+C to stop", style="dim")
        parts.append(status)
        return Group(*parts)

    # Fullscreen mode

    def _layout(self, width: int) -> Layout:
        root = Layout()
        root.split_column(
            Layout(self._header(), name="header", size=1),
            Layout(name="body"),
            Layout(self._footer(width), name="footer", size=4),
        )
        transcript = Layout(self._body(), name="transcript")
        if width >= SIDEBAR_MIN_WIDTH:
            root["body"].split_row(
                transcript, Layout(self._sidebar(), name="sidebar", size=SIDEBAR_WIDTH)
            )
        else:
            root["body"].update(transcript)
        return root

    def _header(self) -> Table:
        left = Text()
        left.append_text(self._badge())
        left.append(f"  {self.elapsed()}", style="bold")
        if self.meeting:
            left.append("   ")
            left.append(self.meeting, style="bold cyan")
        right = Text(f"{self.language} · {self.model_name} ", style="cyan")
        grid = Table.grid(expand=True)
        grid.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        grid.add_column(no_wrap=True, justify="right")
        grid.add_row(left, right)
        return grid

    def _badge(self) -> Text:
        if self.stopped is None:
            return Text(" ● REC ", style="bold white on red")
        if task := self.current_task():
            return Text(f" ⋯ {task.task.upper()} ", style="bold white on magenta")
        if self.done:
            return Text(" ✔ DONE ", style="bold white on green")
        return Text(" ■ STOPPED ", style="bold white on grey37")

    def _body(self) -> Panel:
        return self._output() if self.output is not None else self._transcript()

    def _output(self) -> Panel:
        """The streamed text of the latest LLM pass, Markdown headings highlighted."""
        items: list = []
        for line in (self.output or "").split("\n"):
            style = "bold cyan" if line.startswith("#") else ""
            items.append(Text(line, style=style))
        running = self.current_task() is not None
        placeholder = Text("Waiting for the model…", style="dim italic")
        return Panel(
            BottomAligned(items if self.output else [], placeholder, background=self.matrix),
            title=f"[bold]{escape(self.output_title)}[/]",
            title_align="left",
            border_style="magenta" if running else "green",
            padding=(0, 1),
        )

    def _transcript(self) -> Panel:
        items: list = []
        for at, text, is_note in self.lines:
            stamp = hms(at) if at is not None else ""
            items.append(_hanging(Text(stamp, style=NOTE_STYLE if is_note else "dim"), text))
        if self.partial:
            items.append(
                _hanging(Text("›", style="grey62"), Text(self.partial, style="italic grey62"))
            )
        placeholder = Text(
            "Listening… start speaking." if self.calibrated else "Calibrating — stay quiet…",
            style="dim italic",
        )
        return Panel(
            BottomAligned(items, placeholder, background=self.matrix),
            title="[bold]Transcript[/]",
            title_align="left",
            border_style="red" if self.speaking else "grey37",
            padding=(0, 1),
        )

    def _sidebar(self) -> Panel:
        info = Table.grid(padding=(0, 1))
        info.add_column(style="dim", no_wrap=True)
        info.add_column(overflow="fold")
        if self.meeting:
            info.add_row("Meeting", Text(self.meeting))
        if self.participants:
            info.add_row("People", Text(self.participants))
        info.add_row("Language", Text(self.language))
        info.add_row("Model", Text(self.model_name))
        info.add_row("", "")
        info.add_row("Sentences", Text(f"{self.sentences:,}"))
        info.add_row("Words", Text(f"{self.words:,}"))
        if self.unsure:
            info.add_row("Unsure", Text(f"{self.unsure:,}", style=UNCERTAIN_STYLE))
        info.add_row("Notes", Text(f"{self.notes:,}", style=NOTE_STYLE if self.notes else ""))
        if self.path:
            info.add_row("", "")
            home = str(Path.home())
            path = "~" + self.path[len(home) :] if self.path.startswith(home) else self.path
            info.add_row("File", Text(path, style="dim"))
        if self.tasks:
            info.add_row("", "")
            for task in self.tasks:
                icon, style = STEP_ICONS.get(task.status, ("?", ""))
                detail = Text(f"{icon} ", style=style)
                detail.append(f"{task.elapsed():.0f}s")
                if task.chars:
                    detail.append(f" · {task.chars:,} ch", style="dim")
                info.add_row(task.task.capitalize(), detail)
        body = Group(info, Text(""), *self.log[-LOG_LINES:]) if self.log else info
        return Panel(body, title="[bold]Session[/]", title_align="left", border_style="grey37")

    def _state(self):
        if self.stopped is not None:
            if task := self.current_task():
                return Text.assemble(("⋯ ", "magenta"), task.__rich__())
            if self.stage:
                return Text(f"⋯ {self.stage}", style="magenta")
            if self.log:
                return self.log[-1]
            return Text("■ stopped", style="grey62")
        if not self.calibrated:
            return Text("◌ calibrating — stay quiet", style="yellow")
        if self.busy:
            return Text("⋯ transcribing", style="magenta")
        if self.speaking:
            return Text("● speaking", style="green")
        return Text("○ listening", style="grey62")

    def _note_editor(self, width: int | None = None) -> Text:
        """``✎ Note 00:12:31 › text▌``; with ``width``, only the end of the text that fits."""
        out = Text(f"{NOTE_ICON} Note {hms(self.note_at)} › ", style=f"bold {NOTE_STYLE}")
        text = self.note or ""
        if width is not None:
            room = max(1, width - len(out) - 1)
            if len(text) > room:
                text = "…" + text[-(room - 1) :] if room > 1 else ""
        out.append(text)
        out.append("▌", style="blink")
        return out

    def _footer(self, width: int) -> Panel:
        status = Table.grid(expand=True)
        status.add_column(no_wrap=True, ratio=1)
        status.add_column(no_wrap=True, justify="right")
        editing = self.note is not None
        if editing:
            hint = Text("Enter save · Esc cancel", style="dim")
            status.add_row(self._note_editor(width - 4 - len(hint) - 1), hint)
        elif self.stopped is not None:
            skip = " · Ctrl+C to skip" if self.current_task() else ""
            status.add_row(self._state(), Text(f"m matrix{skip}", style="dim"))
        else:
            hint = Text("n note · m matrix · Ctrl+C to stop", style="dim")
            status.add_row(self._state(), hint)
        history = level_history(list(self.levels), max(0, width - 4))
        return Panel(
            Group(history, status),
            title="[bold]Mic[/]",
            title_align="left",
            border_style=NOTE_STYLE if editing else "grey37",
            padding=(0, 1),
        )
