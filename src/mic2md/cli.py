"""Command line interface: record, transcribe live, save Markdown, polish with Ollama."""

from __future__ import annotations

import queue
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import numpy as np
import typer
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.rule import Rule
from rich.table import Table

from mic2md import __version__, index, keys, llm, models
from mic2md.audio import SAMPLE_RATE, MicStream, Segmenter, list_input_devices
from mic2md.transcriber import build_vocabulary, mark_uncertain, read_glossary
from mic2md.ui import LiveView, LlmProgress, highlight_uncertain
from mic2md.writer import (
    DEFAULT_TYPE,
    SessionType,
    SessionWriter,
    extract_notes,
    extract_summary,
    format_tags,
    import_document,
    insert_summary,
    parse_document,
    session_type,
    split_front_matter,
    update_front_matter,
    write_final,
)

# The UI goes to stderr so stdout can be piped (e.g. `mic2md | pbcopy`).
console = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Real-time voice-to-text. Speak, watch the text appear, get a polished Markdown file.",
    epilog="Without a command it records and summarizes (same as `summarize`). --backend, "
    "--llm, --lang, --ollama-url, "
    "--output-dir and --glossary also apply to the commands, before or after the command "
    "name, e.g. "
    "[bold]mic2md -b claude summarize FILE[/] or [bold]mic2md summarize FILE -b claude[/]. "
    "For lower GPU load: "
    "[bold]mic2md -m small.en --beam-size 1 --vad energy[/].",
)

DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "mic2md"
# Partials are re-run at most this often, and at least twice as far apart as the last one
# took, so they never hog the machine.
PARTIAL_INTERVAL_S = 0.3
MIN_PARTIAL_S = 0.8


class Backend(StrEnum):
    ollama = "ollama"
    claude = "claude"
    copilot = "copilot"


BackendOpt = Annotated[
    Backend,
    typer.Option(
        "--backend",
        "-b",
        envvar="MIC2MD_BACKEND",
        help="LLM backend: local Ollama, Claude via `claude -p` or GitHub Copilot via "
        "`copilot -p` (the last two send the text to the cloud).",
    ),
]
ModelOpt = Annotated[
    str | None,
    typer.Option(
        "--llm",
        envvar="MIC2MD_LLM",
        help=f"Model name. ollama: any pulled model (default {llm.DEFAULT_MODEL}). "
        f"claude: {llm.DEFAULT_CLAUDE_MODEL} (default), opus, fable, haiku or a full model ID. "
        "copilot: e.g. gpt-5.4 (default: Copilot's own).",
    ),
]
TypeOpt = Annotated[
    SessionType | None,
    typer.Option(
        "--type",
        "-T",
        envvar="MIC2MD_TYPE",
        help="Kind of recording: meeting (calendar lookup, meeting notes; the default) or "
        "thoughts (no calendar, ideas and next steps). Sets the folder under transcripts/.",
    ),
]
GLOSSARY_FILE = "glossary.txt"
GlossaryOpt = Annotated[
    Path | None,
    typer.Option(
        "--glossary",
        envvar="MIC2MD_GLOSSARY",
        exists=True,
        dir_okay=False,
        help="Names and terms, one per line, that Whisper and the LLM should spell correctly. "
        f"Default: {GLOSSARY_FILE} in the output folder, if it exists.",
    ),
]


class Lang(StrEnum):
    en = "en"
    sv = "sv"


class Vad(StrEnum):
    silero = "silero"
    energy = "energy"


def _version(value: bool) -> None:
    if value:
        console.print(f"mic2md {__version__}")
        raise typer.Exit()


def _parse_device(device: str | None) -> int | str | None:
    if device is None:
        return None
    return int(device) if device.isdigit() else device


def _print_devices() -> None:
    table = Table(title="Input devices", show_edge=False)
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("")
    for idx, name, is_default in list_input_devices():
        table.add_row(str(idx), name, "[green]default[/]" if is_default else "")
    console.print(table)


# Options that may also be given before a subcommand (`mic2md -b claude summarize F`).
SHARED_OPTIONS = (
    "lang",
    "backend",
    "llm_model",
    "ollama_url",
    "output_dir",
    "glossary",
    "session_type",
)


def _from_command_line(ctx: typer.Context, name: str) -> bool:
    # Compared by name: ParameterSource lives in click or in typer's private copy of it.
    return getattr(ctx.get_parameter_source(name), "name", None) == "COMMANDLINE"


def _inherit(ctx: typer.Context, name: str, value):
    """Use the top-level value of a shared option unless the subcommand got it on its own
    command line. A command-line value beats one from an environment variable."""
    parent = ctx.obj or {}
    if name in parent and not _from_command_line(ctx, name):
        return parent[name]
    return value


def _emit_stdout(text: str) -> None:
    """When stdout is redirected, hand the final document to it."""
    if not sys.stdout.isatty():
        sys.stdout.write(text.rstrip() + "\n")
        sys.stdout.flush()


def _ask(question: str, default: str = "") -> str:
    """Prompt on stderr (stdout is reserved for the document); Enter or EOF keeps the default."""
    hint = f"[{default}]" if default else "(Enter for none)"
    try:
        answer = console.input(f"{question} [dim]{escape(hint)}[/]: ")
    except EOFError:
        return default
    return " ".join(answer.split()) or default


def _parse_when(text: str) -> datetime | None:
    """Accept ISO 8601 or ``YYYY-MM-DD HH:MM[:SS]``; returns local, timezone-aware time."""
    try:
        return datetime.fromisoformat(text.strip()).astimezone()
    except ValueError:
        return None


def _normalize_participants(text: str) -> str:
    return ", ".join(p.strip() for p in text.split(",") if p.strip())


def _meeting_meta() -> dict[str, str]:
    """Front matter for the current meeting, from the calendar or else asked interactively."""
    from mic2md import meetings

    found: list[meetings.Meeting] = []
    try:
        with console.status("Checking calendar…"):
            found = meetings.current_meetings()
    except Exception as e:  # never block a recording on the calendar
        console.print(f"[yellow]⚠ Calendar lookup skipped:[/] {e}")
    meeting = _choose_meeting(found)
    if meeting:
        console.print(f"Meeting: {meeting.title}", style="dim", markup=False, highlight=False)
        return {"meeting": meeting.title, "participants": ", ".join(meeting.participants)}
    if not sys.stdin.isatty():
        return {"meeting": "", "participants": ""}
    if not found:
        console.print("[dim]No meeting in the calendar right now.[/]")
    return {
        "meeting": _ask("Meeting name"),
        "participants": _normalize_participants(_ask("Participants, comma-separated")),
    }


def _session_meta(opts: RecordOptions) -> dict[str, str]:
    """Meeting front matter for a new recording; thoughts never look at the calendar."""
    if opts.session_type is not SessionType.meeting or opts.no_calendar:
        return {}
    return _meeting_meta()


def _meeting_line(meeting) -> str:
    """``10:00–10:30  Standup  (3 people)`` for the meeting picker."""
    when = ""
    if meeting.start and meeting.end:
        when = f"{meeting.start:%H:%M}–{meeting.end:%H:%M}  "
    people = len(meeting.participants)
    count = f"  ({people} {'person' if people == 1 else 'people'})" if people else ""
    return f"{when}{meeting.title or '(no title)'}{count}"


def _choose_meeting(found: list):
    """The meeting to record; asks which one when several are scheduled right now.

    The first one (the one starting last) is the default, also without a terminal.
    0 means none of them: the caller then asks for a name like when there is no meeting.
    """
    if len(found) < 2:
        return found[0] if found else None
    if not sys.stdin.isatty():
        console.print(f"[dim]{len(found)} meetings right now; using the one that started last.[/]")
        return found[0]
    console.print(f"[bold]{len(found)} meetings right now:[/]")
    for i, meeting in enumerate(found, 1):
        console.print(f"  [cyan]{i}[/]  {escape(_meeting_line(meeting))}", highlight=False)
    console.print("  [cyan]0[/]  [dim]none of these[/]")
    while True:
        answer = _ask("Which meeting?", "1")
        if answer.isdigit() and int(answer) <= len(found):
            return found[int(answer) - 1] if int(answer) else None
        console.print(f"[yellow]Choose a number from 0 to {len(found)}.[/]")


def _update_index(output_dir: Path) -> None:
    try:
        path = index.update(output_dir)
    except OSError as e:
        _say(f"[yellow]⚠ Could not update the index:[/] {e}")
        return
    _say(f"[dim]Index updated: {path}[/]", highlight=False, soft_wrap=True)


class Screen:
    """Keeps the fullscreen view on the alternate screen from recording through the LLM passes.

    While it is up nothing may print: messages (``_say``) go to the view's log, LLM output to
    its transcript panel, and both are replayed into the scrollback when it closes.
    """

    def __init__(self, view: LiveView) -> None:
        self.view = view
        # (renderable, console.print kwargs) to print once the alternate screen is gone.
        self.replay: list[tuple[object, dict]] = []
        self._stack = ExitStack()
        self.keys: keys.KeyReader | None = None

    def __enter__(self) -> Screen:
        global _screen
        self.keys = self._stack.enter_context(keys.KeyReader())
        self._stack.enter_context(
            Live(self.view, console=console, refresh_per_second=10, screen=True)
        )
        _screen = self
        return self

    def __exit__(self, *exc: object) -> None:
        global _screen
        _screen = None
        self._stack.close()
        for obj, kwargs in self.replay:
            console.print(obj, **kwargs)

    def handle_keys(self) -> None:
        """`m` toggles the matrix rain; everything else is ignored after recording."""
        for key in self.keys.read() if self.keys else []:
            if key in ("m", "M"):
                self.view.toggle_matrix()

    def run_llm(self, progress: LlmProgress, call, fallback: str, quiet: bool):
        """``_stream_llm`` inside the view: the step in the sidebar, the text in the panel."""
        view = self.view
        view.tasks.append(progress)
        title = f"{progress.task.capitalize()} with {progress.label}"
        if not quiet:
            view.output_title, view.output = title, ""

        def on_token(token: str) -> None:
            progress.chars += len(token)
            if not quiet:
                view.output += token
            self.handle_keys()

        try:
            result = call(on_token)
        except llm.LLMError as e:
            progress.end("failed")
            _say(f"[yellow]⚠ {progress.task.capitalize()} failed, {fallback}:[/] {e}")
            return None
        except KeyboardInterrupt:
            progress.end("cancelled")
            _say(f"[yellow]⚠ {progress.task.capitalize()} cancelled, {fallback}.[/]")
            return None
        progress.end("done")
        if not quiet:
            self.replay.append((Rule(f"[bold]{escape(title)}[/]", style="magenta"), {}))
            self.replay.append((view.output.strip(), {"markup": False, "highlight": False}))
            done = f"[dim]Done in {progress.elapsed():.0f}s ({progress.chars:,} characters).[/]"
            self.replay.append((done, {}))
        return result


# The fullscreen view while it is up (from recording until the last LLM pass is done).
_screen: Screen | None = None


def _say(obj, **kwargs) -> None:
    """Print a message, or while the fullscreen view is up, log it there and print it later."""
    if _screen is None:
        console.print(obj, **kwargs)
        return
    if isinstance(obj, str):
        _screen.view.log.append(console.render_str(obj, markup=kwargs.get("markup", True)))
    _screen.replay.append((obj, kwargs))


def _later(obj, **kwargs) -> None:
    """Print ``obj`` now, or once the fullscreen view has closed (without logging it)."""
    if _screen is None:
        console.print(obj, **kwargs)
    else:
        _screen.replay.append((obj, kwargs))


class LineStreamer:
    """Prints streamed tokens a full line at a time so they don't fight the spinner."""

    def __init__(self, progress: LlmProgress, echo: bool = True) -> None:
        self.progress = progress
        self.echo = echo
        self.buf = ""

    def __call__(self, token: str) -> None:
        self.progress.chars += len(token)
        if not self.echo:
            return
        self.buf += token
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            console.print(line, markup=False, highlight=False)

    def flush(self) -> None:
        if self.buf:
            console.print(self.buf, markup=False, highlight=False)
            self.buf = ""


def _stream_llm(
    task: str, backend: Backend, model: str, url: str, call, fallback: str, quiet: bool = False
):
    """Run one LLM pass with a spinner, streaming the output unless ``quiet``.

    Returns whatever ``call`` returns, or None (after a warning) on failure.
    """
    try:
        llm.check(url, model, backend=backend.value)
    except llm.LLMError as e:
        _say(f"[yellow]⚠ Skipping {task}:[/] {e}")
        return None
    label = llm.describe(backend.value, model)
    progress = LlmProgress(task, label)
    if _screen is not None:
        return _screen.run_llm(progress, call, fallback, quiet)
    if not quiet:
        console.rule(f"[bold]{task.capitalize()} with {escape(label)}[/]", style="magenta")
    stream = LineStreamer(progress, echo=not quiet)
    try:
        with console.status(progress, spinner="dots"):
            result = call(stream)
            stream.flush()
    except llm.LLMError as e:
        stream.flush()
        console.print(f"[yellow]⚠ {task.capitalize()} failed, {fallback}:[/] {e}")
        return None
    except KeyboardInterrupt:
        stream.flush()
        console.print(f"[yellow]⚠ {task.capitalize()} cancelled, {fallback}.[/]")
        return None
    if not quiet:
        elapsed = time.monotonic() - progress.started
        console.print(f"[dim]Done in {elapsed:.0f}s ({progress.chars:,} characters).[/]")
    return result


def _run_tags(
    transcript: str, language: str, backend: Backend, model: str, url: str, output_dir: Path
) -> list[str] | None:
    """Topic tags for the front matter, reusing tags from the other notes where they fit."""
    try:
        known = index.known_tags(output_dir)
    except OSError:
        known = []
    tags = _stream_llm(
        "tagging",
        backend,
        model,
        url,
        lambda on_token: llm.extract_tags(
            transcript, language, known, model, url, on_token=on_token, backend=backend.value
        ),
        "no tags added",
        quiet=True,
    )
    if tags:
        _say(f"[dim]Tags:[/] {escape(', '.join(tags))}", highlight=False)
    return tags


def _glossary_terms(glossary: Path | None, output_dir: Path) -> list[str]:
    """Terms from ``--glossary``, else from glossary.txt in the output folder (if any)."""
    path = glossary.expanduser() if glossary else output_dir / GLOSSARY_FILE
    if not path.is_file():
        return []
    try:
        return read_glossary(path)
    except (OSError, UnicodeDecodeError) as e:
        console.print(f"[yellow]⚠ Could not read the glossary:[/] {e}")
        return []


def _llm_terms(meta: dict[str, str], terms: list[str] | None) -> list[str]:
    """Glossary terms plus the participants' names, for the LLM prompts."""
    names = [n.strip() for n in meta.get("participants", "").split(",") if n.strip()]
    return list(dict.fromkeys([*names, *(terms or [])]))


def _run_polish(
    transcript: str,
    language: str,
    backend: Backend,
    model: str,
    url: str,
    terms: list[str] | None = None,
) -> str | None:
    return _stream_llm(
        "polishing",
        backend,
        model,
        url,
        lambda on_token: llm.polish(
            transcript,
            language,
            model,
            url,
            on_token=on_token,
            backend=backend.value,
            terms=terms,
        ),
        "keeping raw transcript",
    )


class Recorder:
    """Pulls mic frames, segments them, and transcribes partial + final utterances."""

    def __init__(
        self,
        transcriber,
        segmenter: Segmenter,
        writer: SessionWriter,
        view: LiveView,
        show_transcript: bool = False,
    ):
        self.transcriber = transcriber
        self.seg = segmenter
        self.writer = writer
        self.view = view
        self.show_transcript = show_transcript
        self.context = ""
        # (audio, start) of an utterance whose transcription Ctrl+C interrupted.
        self.pending: tuple[np.ndarray, float | None] | None = None
        self._last_partial = 0.0
        self._partial_gap = PARTIAL_INTERVAL_S
        # True while the fullscreen view owns the terminal; nothing else may print then.
        self._screen = False
        # Session clock for typed notes (monotonic time when recording started).
        self._t0 = time.monotonic()

    def commit(self, audio: np.ndarray, start: float | None = None) -> None:
        """Transcribe a finished utterance and save it; ``start`` is seconds into the session."""
        self.pending = (audio, start)
        self.view.busy = True
        text = self.transcriber.transcribe(audio, prompt=self.context)
        self.view.busy = False
        self.view.partial = ""
        if text:
            uncertain = getattr(self.transcriber, "last_uncertain", set())
            line = highlight_uncertain(text, uncertain)
            self.view.add_line(line, start, unsure=len(uncertain))
            if self.show_transcript and not self._screen:
                console.print(line, highlight=False)
            self.writer.append(mark_uncertain(text, uncertain), at=start)
            self.context = text
        self.pending = None

    def handle_keys(self, pressed: list[str]) -> None:
        """`n` opens a note; then Enter saves it, Esc cancels, Backspace/Ctrl+U edit.

        `m` (outside the note editor) toggles the matrix rain behind the transcript.
        """
        view = self.view
        for key in pressed:
            if view.note is None:
                if key in ("n", "N"):
                    view.note, view.note_at = "", time.monotonic() - self._t0
                elif key in ("m", "M"):
                    view.toggle_matrix()
            elif key == keys.ENTER:
                self.save_note()
            elif key == keys.ESC:
                view.note = None
            elif key == keys.BACKSPACE:
                view.note = view.note[:-1]
            elif key == keys.CLEAR:
                view.note = ""
            elif len(key) == 1:
                view.note += key

    def save_note(self) -> None:
        """Save the note being typed (if it has text) and close the editor."""
        text, at = (self.view.note or "").strip(), self.view.note_at
        self.view.note = None
        if not text:
            return
        self.writer.append_note(text, at)
        line = self.view.add_note(text, at)
        if self.show_transcript and not self._screen:
            console.print(line, highlight=False)

    def process(self, frames: list[np.ndarray]) -> None:
        for frame in frames:
            done = self.seg.feed(frame)
            if done is not None:
                self.commit(done, self.seg.last_start_s)
            elif not self.seg.in_speech:
                self.view.partial = ""
        self.view.update_level(self.seg.level, self.seg.speaking)
        self.view.calibrated = self.seg.calibrated

    def maybe_partial(self) -> None:
        now = time.monotonic()
        if not self.seg.in_speech or now - self._last_partial < self._partial_gap:
            return
        current = self.seg.current()
        if current is None or current.size < MIN_PARTIAL_S * SAMPLE_RATE:
            return
        self.view.busy = True
        self.view.partial = self.transcriber.transcribe(current, prompt=self.context, partial=True)
        self.view.busy = False
        self._last_partial = time.monotonic()
        self._partial_gap = max(PARTIAL_INTERVAL_S, 2 * (self._last_partial - now))

    def run(self, mic: MicStream) -> None:
        """Record until Ctrl+C. A fullscreen view stays up afterwards if a ``Screen`` is open
        (it then shows the LLM passes); otherwise this opens and closes one itself."""
        fullscreen = self.view.fullscreen
        self._t0 = time.monotonic()
        with ExitStack() as stack:
            if fullscreen and _screen is None:
                stack.enter_context(Screen(self.view))
            self._screen = fullscreen
            try:
                self._loop(mic)
            finally:
                self._screen = False
                self.view.stop()
            if fullscreen and self.show_transcript:
                # Into the scrollback once the alternate screen is gone.
                for _, line, _ in self.view.lines:
                    _later(line, highlight=False)

    def _loop(self, mic: MicStream) -> None:
        with ExitStack() as stack:
            if _screen is not None:
                reader = _screen.keys
            else:
                reader = stack.enter_context(keys.KeyReader())
                stack.enter_context(
                    Live(self.view, console=console, refresh_per_second=10, transient=True)
                )
            try:
                while True:
                    self.handle_keys(reader.read())
                    try:
                        frames = [mic.frames.get(timeout=0.1)]
                    except queue.Empty:
                        continue
                    while not mic.frames.empty():
                        frames.append(mic.frames.get_nowait())
                    self.process(frames)
                    self.maybe_partial()
            except KeyboardInterrupt:
                pass

    def finish(self, mic: MicStream) -> None:
        """Transcribe whatever was still in flight when recording stopped."""
        self.save_note()  # a note still being typed at Ctrl+C is kept
        leftover = []
        while not mic.frames.empty():
            leftover.append(mic.frames.get_nowait())
        if _screen is not None:
            _screen.view.stage = "Transcribing the last words…"
            status = ExitStack()
        else:
            status = console.status("Transcribing the last words…")
        with status:
            if self.pending is not None:
                self.commit(*self.pending)
            for frame in leftover:
                if (done := self.seg.feed(frame)) is not None:
                    self.commit(done, self.seg.last_start_s)
            if (tail := self.seg.flush()) is not None:
                self.commit(tail, self.seg.last_start_s)
        self.view.stage = ""


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    lang: Annotated[
        Lang, typer.Option("--lang", "-l", envvar="MIC2MD_LANG", help="Spoken language.")
    ] = Lang.en,
    model_size: Annotated[
        str | None,
        typer.Option(
            "--model-size",
            "-m",
            envvar="MIC2MD_MODEL_SIZE",
            help="Whisper model size (see `mic2md models`). "
            "Default: large-v3-turbo for en, large for sv.",
        ),
    ] = None,
    model_path: Annotated[
        Path | None,
        typer.Option(
            "--model-path",
            envvar="MIC2MD_MODEL_PATH",
            exists=True,
            dir_okay=False,
            help="Use a local ggml model file instead of downloading one.",
        ),
    ] = None,
    backend: BackendOpt = Backend.ollama,
    llm_model: ModelOpt = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skip the Ollama pass; save the raw transcript.")
    ] = False,
    ollama_url: Annotated[
        str, typer.Option("--ollama-url", envvar="OLLAMA_HOST", help="Ollama server URL.")
    ] = llm.DEFAULT_URL,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            envvar="MIC2MD_OUTPUT_DIR",
            file_okay=False,
            help="Where index.md and transcripts/YYYY-MM/ session files are saved.",
        ),
    ] = DEFAULT_OUTPUT_DIR,
    device: Annotated[
        str | None,
        typer.Option(
            "--device",
            "-d",
            envvar="MIC2MD_DEVICE",
            help="Input device ID or name (see --list-devices).",
        ),
    ] = None,
    vad: Annotated[
        Vad,
        typer.Option(
            "--vad",
            envvar="MIC2MD_VAD",
            help="Speech detection: silero (neural, robust to noise) or energy (loudness "
            "threshold). --threshold implies energy.",
        ),
    ] = Vad.silero,
    beam_size: Annotated[
        int,
        typer.Option(
            "--beam-size",
            envvar="MIC2MD_BEAM_SIZE",
            min=1,
            help="Beam search width for finished sentences (1 = greedy, faster). "
            "The live partial line is always greedy.",
        ),
    ] = 5,
    silence_ms: Annotated[
        int, typer.Option("--silence-ms", help="Pause length (ms) that ends a sentence.")
    ] = 700,
    threshold: Annotated[
        float | None,
        typer.Option("--threshold", help="Fixed speech RMS threshold (default: auto-calibrate)."),
    ] = None,
    no_calendar: Annotated[
        bool,
        typer.Option(
            "--no-calendar",
            envvar="MIC2MD_NO_CALENDAR",
            help="Don't look up or ask for the current meeting.",
        ),
    ] = False,
    glossary: GlossaryOpt = None,
    show_transcript: Annotated[
        bool,
        typer.Option(
            "--transcript",
            "-t",
            help="Print the live transcript to the terminal as sentences finish "
            "(in fullscreen: when recording stops, so it stays in the scrollback).",
        ),
    ] = False,
    fullscreen: Annotated[
        bool,
        typer.Option(
            "--fullscreen/--no-fullscreen",
            envvar="MIC2MD_FULLSCREEN",
            help="Use the whole terminal while recording (transcript, session info, mic "
            "history). --no-fullscreen keeps the compact inline status line.",
        ),
    ] = True,
    list_devices: Annotated[
        bool, typer.Option("--list-devices", help="List microphones and exit.")
    ] = False,
    session_type: TypeOpt = None,
    version: Annotated[
        bool | None, typer.Option("--version", callback=_version, is_eager=True)
    ] = None,
) -> None:
    """Record from the microphone until Ctrl+C, then polish and summarize (like `summarize`)."""
    opts = RecordOptions(
        session_type=session_type or DEFAULT_TYPE,
        lang=lang,
        model_size=model_size,
        model_path=model_path,
        no_llm=no_llm,
        device=device,
        silence_ms=silence_ms,
        threshold=threshold,
        no_calendar=no_calendar,
        vad=vad,
        beam_size=beam_size,
        show_transcript=show_transcript,
        fullscreen=fullscreen,
    )
    if ctx.invoked_subcommand is not None:
        # The converted values (Path, enums), not the raw strings in ctx.params.
        values = {
            "lang": lang,
            "backend": backend,
            "llm_model": llm_model,
            "ollama_url": ollama_url,
            "output_dir": output_dir,
            "glossary": glossary,
            "session_type": session_type,
        }
        ctx.obj = {n: values[n] for n in SHARED_OPTIONS if _from_command_line(ctx, n)}
        # For `summarize` without a file, which records first.
        ctx.obj[RECORD_KEY] = opts
        return
    if list_devices:
        _print_devices()
        raise typer.Exit()

    ctx.obj = {RECORD_KEY: opts}
    ctx.invoke(
        summarize,
        ctx,
        lang=lang,
        output_dir=output_dir,
        backend=backend,
        llm_model=llm_model,
        ollama_url=ollama_url,
        glossary=glossary,
        session_type=None,
    )


def _detector(opts: RecordOptions):
    """Silero detector for the Segmenter, or None for the energy threshold (the fallback)."""
    if opts.vad is Vad.energy or opts.threshold is not None:
        return None
    try:
        from mic2md.vad import SileroDetector

        return SileroDetector()
    except Exception as e:  # missing wheel, broken model: energy still works
        console.print(f"[yellow]⚠ Silero VAD unavailable, using the energy threshold:[/] {e}")
        return None


# ctx.obj key for the recording options given before a subcommand.
RECORD_KEY = "_record"


@dataclass
class RecordOptions:
    lang: Lang = Lang.en
    model_size: str | None = None
    model_path: Path | None = None
    no_llm: bool = False
    device: str | None = None
    silence_ms: int = 700
    threshold: float | None = None
    no_calendar: bool = False
    vad: Vad = Vad.silero
    beam_size: int = 5
    show_transcript: bool = False
    fullscreen: bool = True
    session_type: SessionType = DEFAULT_TYPE


def _done() -> None:
    """Show the finished state in the fullscreen view (if it is up) before it closes."""
    if _screen is not None:
        _screen.view.done = True
        _screen.view.stage = ""


def _record(
    opts: RecordOptions,
    backend: Backend,
    llm_model: str,
    ollama_url: str,
    output_dir: Path,
    terms: list[str] | None = None,
    tag: bool = True,
    *,
    stack: ExitStack,
) -> tuple[SessionWriter, str | None]:
    """Record until Ctrl+C, then polish, tag, save and update the index.

    In fullscreen mode the view goes onto ``stack``, so it stays up for the LLM passes and
    for whatever the caller does next, until the caller closes the stack.
    ``terms`` (the glossary) prime Whisper and the polish prompt with the right spelling.

    Returns the writer (its ``path`` is the session file) and the polished text, or None when
    the raw transcript was kept. Exits when nothing was said.
    """
    language = opts.lang.value
    model_size, model_path, no_llm = opts.model_size, opts.model_path, opts.no_llm
    try:
        if model_path:
            path, model_name = model_path.expanduser(), model_path.stem
        else:
            spec = models.get_spec(language, model_size)
            path, model_name = models.ensure_model(spec, console), spec.size
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--model-size") from e

    if not no_llm:
        try:
            llm.check(ollama_url, llm_model, backend=backend.value)
        except llm.LLMError as e:
            console.print(
                f"[yellow]⚠ {e}[/]\n[dim]Recording anyway; the raw transcript will be "
                "saved and you can run `mic2md polish FILE` later.[/]"
            )

    from mic2md.transcriber import Transcriber

    with console.status(f"Loading Whisper model [cyan]{model_name}[/]…"):
        transcriber = Transcriber(path, language, beam_size=opts.beam_size)

    meeting_meta = _session_meta(opts)
    transcriber.vocabulary = build_vocabulary(
        meeting_meta.get("meeting", ""), meeting_meta.get("participants", ""), terms or []
    )
    started = datetime.now()
    writer = SessionWriter(
        output_dir,
        started,
        language,
        model_name,
        extra_meta=meeting_meta,
        session_type=opts.session_type,
    )
    view = LiveView(
        language,
        model_name,
        fullscreen=opts.fullscreen and console.is_terminal,
        meeting=meeting_meta.get("meeting", ""),
        participants=meeting_meta.get("participants", ""),
        path=str(writer.path),
        session_type=opts.session_type.value,
    )
    recorder = Recorder(
        transcriber,
        Segmenter(silence_ms=opts.silence_ms, threshold=opts.threshold, detector=_detector(opts)),
        writer,
        view,
        show_transcript=opts.show_transcript,
    )

    console.print(f"[dim]Saving to {writer.path}[/]")
    if view.fullscreen:
        stack.enter_context(Screen(view))
    else:
        console.rule("[bold red]● Listening[/]", style="red")
    try:
        with MicStream(_parse_device(opts.device)) as mic:
            recorder.run(mic)
    except Exception as e:  # PortAudio raises plain Exceptions
        writer.discard_if_empty()
        transcriber.close()
        _say(f"[red]Microphone error:[/] {e}")
        raise typer.Exit(1) from e
    recorder.finish(mic)
    transcriber.close()
    ended = datetime.now()

    if writer.discard_if_empty():
        _say("[yellow]No speech detected — nothing saved.[/]")
        raise typer.Exit()
    # Only typed notes: nothing for the LLM, the notes are saved as they are.
    no_llm = no_llm or not writer.lines

    polished = (
        None
        if no_llm
        else _run_polish(
            writer.raw_text,
            language,
            backend,
            llm_model,
            ollama_url,
            _llm_terms(writer.meta, terms),
        )
    )
    if (
        tag
        and not no_llm
        and (
            tags := _run_tags(writer.raw_text, language, backend, llm_model, ollama_url, output_dir)
        )
    ):
        writer.meta["tags"] = format_tags(tags)
    writer.finalize(polished, llm.describe(backend.value, llm_model), ended)
    _update_index(output_dir)
    _later(Rule(style="green"))
    _say(f"[green]✔ Saved[/] [link=file://{writer.path}]{writer.path}[/link]")
    return writer, polished


@app.command()
def polish(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="Session .md file. Files outside the output folder are imported into it first.",
        ),
    ],
    lang: Annotated[
        Lang | None, typer.Option("--lang", "-l", help="Override the language in the file.")
    ] = None,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            envvar="MIC2MD_OUTPUT_DIR",
            file_okay=False,
            help="Folder that holds index.md and transcripts/.",
        ),
    ] = DEFAULT_OUTPUT_DIR,
    backend: BackendOpt = Backend.ollama,
    llm_model: ModelOpt = None,
    ollama_url: Annotated[
        str, typer.Option("--ollama-url", envvar="OLLAMA_HOST", help="Ollama server URL.")
    ] = llm.DEFAULT_URL,
    glossary: GlossaryOpt = None,
    session_type: TypeOpt = None,
) -> None:
    """Re-run the LLM polish on a session file (on the raw transcript if present).

    A file from outside the output folder is copied into transcripts/<type>/YYYY-MM/ first,
    with front matter you are asked for (--type sets the default type); the original is left
    untouched.
    """
    lang = _inherit(ctx, "lang", lang)
    backend = _inherit(ctx, "backend", backend)
    llm_model = _inherit(ctx, "llm_model", llm_model)
    ollama_url = _inherit(ctx, "ollama_url", ollama_url)
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser().resolve()
    terms = _glossary_terms(_inherit(ctx, "glossary", glossary), output_dir)
    imported = not file.resolve().is_relative_to(output_dir)
    if imported:
        file = _import_external(file, output_dir, lang, _inherit(ctx, "session_type", session_type))
        _update_index(output_dir)
    text = file.read_text(encoding="utf-8")
    meta, raw = parse_document(text)
    if not raw:
        console.print("[red]No transcript found in file.[/]")
        raise typer.Exit(1)
    language = lang.value if lang else meta.get("language", "en")
    llm_model = llm_model or llm.default_model(backend.value)
    polished = _run_polish(raw, language, backend, llm_model, ollama_url, _llm_terms(meta, terms))
    if polished is None:
        if imported:
            console.print(f"[dim]The imported file is kept: {file}[/]", soft_wrap=True)
        raise typer.Exit(1)
    meta["llm_model"] = llm.describe(backend.value, llm_model)
    if tags := _run_tags(raw, language, backend, llm_model, ollama_url, output_dir):
        meta["tags"] = format_tags(tags)
    write_final(file, meta, raw, polished, summary=extract_summary(text), notes=extract_notes(text))
    _update_index(output_dir)
    console.print(f"[green]✔ Updated[/] {file}", soft_wrap=True)
    _emit_stdout(polished)


def _import_external(
    file: Path, output_dir: Path, lang: Lang | None, kind: SessionType | None = None
) -> Path:
    """Copy a file from outside the output folder into it, asking for the front matter.

    The type defaults to ``kind`` (--type), else the file's own ``type:``, else meeting.
    """
    meta, body = split_front_matter(file.read_text(encoding="utf-8"))
    if not body.strip():
        console.print("[red]The file is empty.[/]")
        raise typer.Exit(1)
    known = _parse_when(meta.get("date", ""))
    default_when = known or datetime.fromtimestamp(file.stat().st_mtime).astimezone()
    default_lang = lang.value if lang else meta.get("language", "en")
    if default_lang not in Lang.__members__:
        default_lang = "en"
    console.print(
        f"[bold]{escape(file.name)}[/] is outside {escape(str(output_dir))}; importing it.",
        soft_wrap=True,
    )
    when, front = _ask_front_matter(meta, default_when, default_lang, kind or session_type(meta))
    front["source"] = str(file.resolve())
    path = import_document(output_dir, when, front, body)
    console.print(f"[green]✔ Imported to[/] {path}", soft_wrap=True)
    return path


def _interactive() -> bool:
    return sys.stdin.isatty()


def _ask_front_matter(
    meta: dict[str, str], default_when: datetime, default_lang: str, kind: SessionType
) -> tuple[datetime, dict[str, str]]:
    """Ask for date, language, type and (meetings) name and participants of an imported text.

    Without a terminal the defaults are used. Returns the start time and the front matter
    (``meta`` with those fields set).
    """
    when = default_when
    language, meeting = default_lang, meta.get("meeting", "")
    participants = meta.get("participants", "")
    if not _interactive():
        console.print("[dim]Not a terminal; using defaults for the front matter.[/]")
    else:
        while (
            answer := _parse_when(_ask("Date and time", f"{default_when:%Y-%m-%d %H:%M}"))
        ) is None:
            console.print("[yellow]Use YYYY-MM-DD HH:MM.[/]")
        when = answer
        while (language := _ask("Language (en/sv)", default_lang)) not in Lang.__members__:
            console.print("[yellow]Choose en or sv.[/]")
        types = "/".join(t.value for t in SessionType)
        while (answer := _ask(f"Type ({types})", kind.value)) not in SessionType.__members__:
            console.print(f"[yellow]Choose {types.replace('/', ' or ')}.[/]")
        kind = SessionType(answer)
        if kind is SessionType.meeting:
            meeting = _ask("Meeting name", meeting)
            participants = _normalize_participants(
                _ask("Participants, comma-separated", participants)
            )
    return when, {
        **meta,
        "date": when.isoformat(timespec="seconds"),
        "language": language,
        "type": kind.value,
        "meeting": meeting,
        "participants": participants,
    }


def _record_for_summary(
    ctx: typer.Context,
    lang: Lang | None,
    backend: Backend,
    llm_model: str,
    ollama_url: str,
    output_dir: Path,
    terms: list[str],
    stack: ExitStack,
    kind: SessionType | None = None,
) -> Path:
    """Record and polish a new session for `summarize`; returns its file.

    Tags are left to the summarize pass, so they are only asked for once. A fullscreen view
    stays on ``stack`` so the summary streams into it too.
    """
    opts = (ctx.obj or {}).get(RECORD_KEY) or RecordOptions()
    if lang:
        opts.lang = lang
    if kind:
        opts.session_type = kind
    if opts.no_llm:
        console.print("[red]--no-llm can't be combined with summarize.[/]")
        raise typer.Exit(2)
    writer, polished = _record(
        opts, backend, llm_model, ollama_url, output_dir, terms, tag=False, stack=stack
    )
    if polished is None:
        _say(
            "[yellow]⚠ Skipping the summary because polishing didn't finish. Run "
            f"`mic2md summarize {writer.path}` later.[/]",
            soft_wrap=True,
        )
        raise typer.Exit(1)
    return writer.path


@app.command()
def summarize(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="Session .md file. Files outside the output folder are imported into it first. "
            "Without a file, a new meeting is recorded, polished and then summarized.",
        ),
    ] = None,
    lang: Annotated[
        Lang | None,
        typer.Option(
            "--lang", "-l", help="Override the language in the file (or the spoken language)."
        ),
    ] = None,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            envvar="MIC2MD_OUTPUT_DIR",
            file_okay=False,
            help="Folder that holds index.md and transcripts/.",
        ),
    ] = DEFAULT_OUTPUT_DIR,
    backend: BackendOpt = Backend.ollama,
    llm_model: ModelOpt = None,
    ollama_url: Annotated[
        str, typer.Option("--ollama-url", envvar="OLLAMA_HOST", help="Ollama server URL.")
    ] = llm.DEFAULT_URL,
    glossary: GlossaryOpt = None,
    session_type: TypeOpt = None,
) -> None:
    """Add a summary (for meetings: decisions and action items) to the top of a session file.

    Without FILE it records a meeting first (like plain `mic2md`), polishes it and then
    summarizes it. Recording options such as -m, -d or --no-calendar go before the command:
    [bold]mic2md -m small.en summarize[/].

    The summary follows the session's type (front matter `type:`): meeting notes for meetings,
    key ideas and next steps for thoughts. --type picks the type of a new recording or import.

    Running it again replaces the previous summary. A file from outside the output folder is
    copied into transcripts/<type>/YYYY-MM/ first, with front matter you are asked for; the
    original is left untouched.
    """
    lang = _inherit(ctx, "lang", lang)
    backend = _inherit(ctx, "backend", backend)
    llm_model = _inherit(ctx, "llm_model", llm_model) or llm.default_model(backend.value)
    ollama_url = _inherit(ctx, "ollama_url", ollama_url)
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser().resolve()
    terms = _glossary_terms(_inherit(ctx, "glossary", glossary), output_dir)
    kind = _inherit(ctx, "session_type", session_type)
    with ExitStack() as stack:
        summary = _summarize(
            ctx, file, lang, backend, llm_model, ollama_url, output_dir, terms, stack, kind
        )
        _done()
    _emit_stdout(summary)


def _summarize(
    ctx: typer.Context,
    file: Path | None,
    lang: Lang | None,
    backend: Backend,
    llm_model: str,
    ollama_url: str,
    output_dir: Path,
    terms: list[str],
    stack: ExitStack,
    kind: SessionType | None = None,
) -> str:
    """The body of `summarize`; returns the summary that was inserted."""
    imported = False
    if file is None:
        file = _record_for_summary(
            ctx, lang, backend, llm_model, ollama_url, output_dir, terms, stack, kind
        )
    elif imported := not file.resolve().is_relative_to(output_dir):
        file = _import_external(file, output_dir, lang, kind)
        _update_index(output_dir)
    text = file.read_text(encoding="utf-8")
    meta, transcript = parse_document(text)
    if not transcript:
        _say("[red]No transcript found in file.[/]")
        raise typer.Exit(1)
    language = lang.value if lang else meta.get("language", "en")
    summary = _stream_llm(
        "summarizing",
        backend,
        llm_model,
        ollama_url,
        lambda on_token: llm.summarize(
            transcript,
            language,
            meeting=meta.get("meeting", ""),
            participants=meta.get("participants", ""),
            model=llm_model,
            url=ollama_url,
            on_token=on_token,
            backend=backend.value,
            terms=terms,
            notes=extract_notes(text),
            session_type=session_type(meta).value,
        ),
        "file left unchanged",
    )
    if summary is None:
        if imported:
            _say(f"[dim]The imported file is kept: {file}[/]", soft_wrap=True)
        raise typer.Exit(1)
    meta["summary_model"] = llm.describe(backend.value, llm_model)
    if tags := _run_tags(transcript, language, backend, llm_model, ollama_url, output_dir):
        meta["tags"] = format_tags(tags)
    insert_summary(file, meta, text, summary)
    _update_index(output_dir)
    _say(f"[green]✔ Updated[/] {file}", soft_wrap=True)
    return summary


@app.command()
def notes(
    ctx: typer.Context,
    lang: Annotated[
        Lang | None, typer.Option("--lang", "-l", help="Language of the text (default: en).")
    ] = None,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            envvar="MIC2MD_OUTPUT_DIR",
            file_okay=False,
            help="Folder that holds index.md and transcripts/.",
        ),
    ] = DEFAULT_OUTPUT_DIR,
    session_type: TypeOpt = None,
) -> None:
    """Save a pasted transcript (e.g. from Teams) as a session, as is: no polish or summary.

    Paste the text and press Ctrl+D on an empty line, or pipe it in: [bold]pbpaste | mic2md
    n[/]. You're asked for the date, language, type and meeting like when importing a file.
    It lands in transcripts/<type>/YYYY-MM/ and in index.md; run `polish`, `summarize` or
    `tag` on it later if you want.
    """
    lang = _inherit(ctx, "lang", lang)
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser().resolve()
    kind = _inherit(ctx, "session_type", session_type) or DEFAULT_TYPE
    if _interactive():
        console.print("Paste the transcript, then press [bold]Ctrl+D[/] on an empty line.")
    body = sys.stdin.read()
    if not body.strip():
        console.print("[red]Nothing pasted; no file saved.[/]")
        raise typer.Exit(1)
    when, front = _ask_front_matter(
        {}, datetime.now().astimezone(), lang.value if lang else "en", kind
    )
    front["source"] = "pasted"
    path = import_document(output_dir, when, front, body)
    _update_index(output_dir)
    console.print(f"[green]✔ Saved[/] {path}", soft_wrap=True)


@app.command()
def tag(
    ctx: typer.Context,
    files: Annotated[
        list[Path] | None,
        typer.Argument(exists=True, dir_okay=False, help="Session .md files to (re-)tag."),
    ] = None,
    all_untagged: Annotated[
        bool, typer.Option("--all", help="Tag every session in the output folder without tags.")
    ] = False,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            envvar="MIC2MD_OUTPUT_DIR",
            file_okay=False,
            help="Folder that holds index.md and transcripts/.",
        ),
    ] = DEFAULT_OUTPUT_DIR,
    backend: BackendOpt = Backend.ollama,
    llm_model: ModelOpt = None,
    ollama_url: Annotated[
        str, typer.Option("--ollama-url", envvar="OLLAMA_HOST", help="Ollama server URL.")
    ] = llm.DEFAULT_URL,
) -> None:
    """Add topic tags to the front matter of session files (e.g. ones made before tags existed).

    Files given by name are always re-tagged; --all only picks sessions that have no tags yet.
    """
    backend = _inherit(ctx, "backend", backend)
    llm_model = _inherit(ctx, "llm_model", llm_model) or llm.default_model(backend.value)
    ollama_url = _inherit(ctx, "ollama_url", ollama_url)
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser()
    targets = list(files or [])
    if all_untagged:
        targets += [
            output_dir / e.path
            for e in index.collect(output_dir)
            if not e.tags and output_dir / e.path not in targets
        ]
    if not targets:
        console.print("Nothing to tag. Pass session files, or --all for every untagged session.")
        raise typer.Exit()
    try:
        llm.check(ollama_url, llm_model, backend=backend.value)
    except llm.LLMError as e:
        console.print(f"[red]Can't tag:[/] {e}")
        raise typer.Exit(1) from e
    failed = 0
    for path in targets:
        console.print(f"[bold]{escape(path.name)}[/]", highlight=False)
        meta, transcript = parse_document(path.read_text(encoding="utf-8"))
        if not transcript:
            console.print("[yellow]⚠ No transcript, skipped.[/]")
            continue
        language = meta.get("language", "en")
        tags = _run_tags(transcript, language, backend, llm_model, ollama_url, output_dir)
        if not tags:
            failed += 1
            continue
        update_front_matter(path, {**meta, "tags": format_tags(tags)})
    _update_index(output_dir)
    if failed:
        raise typer.Exit(1)


@app.command()
def reindex(
    ctx: typer.Context,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            envvar="MIC2MD_OUTPUT_DIR",
            file_okay=False,
            help="Folder that holds index.md and transcripts/.",
        ),
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Rebuild index.md, moving sessions saved by older versions into transcripts/meeting/."""
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser()
    if not output_dir.is_dir():
        console.print(f"[red]No such folder:[/] {output_dir}")
        raise typer.Exit(1)
    for path in index.migrate_flat(output_dir):
        console.print(f"[dim]Moved to {path}[/]", highlight=False, soft_wrap=True)
    console.print(f"[green]✔ Updated[/] {index.update(output_dir)}", soft_wrap=True)


@app.command("models")
def list_models() -> None:
    """List available Whisper models and whether they are downloaded."""
    table = Table(title=f"Whisper models  [dim]({models.cache_dir()})[/]", show_edge=False)
    for col in ("Lang", "Size", "Source", "Cached"):
        table.add_column(col)
    for language, sizes in models.REGISTRY.items():
        for size, spec in sizes.items():
            default = " [dim](default)[/]" if models.DEFAULT_SIZE[language] == size else ""
            cached = (models.cache_dir() / spec.local_file).exists()
            table.add_row(language, size + default, spec.repo, "[green]✔[/]" if cached else "")
    console.print(table)


# Short names. short_help is for the command list; `mic2md s --help` shows the full help.
app.command("p", short_help="Short for polish.")(polish)
app.command("s", short_help="Short for summarize.")(summarize)
app.command("n", short_help="Short for notes.")(notes)


if __name__ == "__main__":
    app()
