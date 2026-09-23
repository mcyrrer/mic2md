"""Command line interface: record, transcribe live, save Markdown, polish with Ollama."""

from __future__ import annotations

import queue
import sys
import time
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
from rich.table import Table
from rich.text import Text

from mic2md import __version__, index, llm, models
from mic2md.audio import SAMPLE_RATE, MicStream, Segmenter, list_input_devices
from mic2md.ui import LiveView
from mic2md.writer import (
    SessionWriter,
    extract_summary,
    format_tags,
    import_document,
    insert_summary,
    parse_document,
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
    epilog="Without a command it records. --backend, --llm, --lang, --ollama-url and "
    "--output-dir also apply to the commands, before or after the command name, e.g. "
    "[bold]mic2md -b claude summarize FILE[/] or [bold]mic2md summarize FILE -b claude[/].",
)

DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "mic2md"
PARTIAL_INTERVAL_S = 1.0
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


class Lang(StrEnum):
    en = "en"
    sv = "sv"


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
SHARED_OPTIONS = ("lang", "backend", "llm_model", "ollama_url", "output_dir")


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

    meeting = None
    try:
        with console.status("Checking calendar…"):
            meeting = meetings.current_meeting()
    except Exception as e:  # never block a recording on the calendar
        console.print(f"[yellow]⚠ Calendar lookup skipped:[/] {e}")
    if meeting:
        console.print(f"Meeting: {meeting.title}", style="dim", markup=False, highlight=False)
        return {"meeting": meeting.title, "participants": ", ".join(meeting.participants)}
    if not sys.stdin.isatty():
        return {"meeting": "", "participants": ""}
    console.print("[dim]No meeting in the calendar right now.[/]")
    return {
        "meeting": _ask("Meeting name"),
        "participants": _normalize_participants(_ask("Participants, comma-separated")),
    }


def _update_index(output_dir: Path) -> None:
    try:
        path = index.update(output_dir)
    except OSError as e:
        console.print(f"[yellow]⚠ Could not update the index:[/] {e}")
        return
    console.print(f"[dim]Index updated: {path}[/]", highlight=False, soft_wrap=True)


class LlmProgress:
    """Spinner text for an Ollama pass; re-rendered by the spinner, so elapsed time ticks."""

    def __init__(self, task: str, label: str) -> None:
        self.task, self.label = task, label
        self.chars = 0
        self.started = time.monotonic()

    def __rich__(self) -> Text:
        elapsed = int(time.monotonic() - self.started)
        if self.chars == 0:
            return Text(f"Waiting for {self.label} (loading, reading the text)… {elapsed}s")
        return Text(
            f"{self.task.capitalize()} with {self.label}… {self.chars:,} characters · {elapsed}s"
        )


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
        console.print(f"[yellow]⚠ Skipping {task}:[/] {e}")
        return None
    label = llm.describe(backend.value, model)
    if not quiet:
        console.rule(f"[bold]{task.capitalize()} with {escape(label)}[/]", style="magenta")
    progress = LlmProgress(task, label)
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
        console.print(f"[dim]Tags:[/] {escape(', '.join(tags))}", highlight=False)
    return tags


def _run_polish(
    transcript: str, language: str, backend: Backend, model: str, url: str
) -> str | None:
    return _stream_llm(
        "polishing",
        backend,
        model,
        url,
        lambda on_token: llm.polish(
            transcript, language, model, url, on_token=on_token, backend=backend.value
        ),
        "keeping raw transcript",
    )


class Recorder:
    """Pulls mic frames, segments them, and transcribes partial + final utterances."""

    def __init__(self, transcriber, segmenter: Segmenter, writer: SessionWriter, view: LiveView):
        self.transcriber = transcriber
        self.seg = segmenter
        self.writer = writer
        self.view = view
        self.context = ""
        self.pending: np.ndarray | None = None
        self._last_partial = 0.0

    def commit(self, audio: np.ndarray) -> None:
        self.pending = audio
        self.view.busy = True
        text = self.transcriber.transcribe(audio, prompt=self.context)
        self.view.busy = False
        self.view.partial = ""
        if text:
            console.print(text, highlight=False, markup=False)
            self.writer.append(text)
            self.context = text
        self.pending = None

    def process(self, frames: list[np.ndarray]) -> None:
        for frame in frames:
            done = self.seg.feed(frame)
            if done is not None:
                self.commit(done)
            elif not self.seg.in_speech:
                self.view.partial = ""
        self.view.level = self.seg.level
        self.view.threshold = self.seg.threshold
        self.view.calibrated = self.seg.calibrated

    def maybe_partial(self) -> None:
        now = time.monotonic()
        if not self.seg.in_speech or now - self._last_partial < PARTIAL_INTERVAL_S:
            return
        current = self.seg.current()
        if current is None or current.size < MIN_PARTIAL_S * SAMPLE_RATE:
            return
        self.view.busy = True
        self.view.partial = self.transcriber.transcribe(current, prompt=self.context)
        self.view.busy = False
        self._last_partial = time.monotonic()

    def run(self, mic: MicStream) -> None:
        with Live(self.view, console=console, refresh_per_second=10, transient=True):
            try:
                while True:
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
        leftover = []
        while not mic.frames.empty():
            leftover.append(mic.frames.get_nowait())
        with console.status("Transcribing the last words…"):
            if self.pending is not None:
                self.commit(self.pending)
            for frame in leftover:
                if (done := self.seg.feed(frame)) is not None:
                    self.commit(done)
            if (tail := self.seg.flush()) is not None:
                self.commit(tail)


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
    list_devices: Annotated[
        bool, typer.Option("--list-devices", help="List microphones and exit.")
    ] = False,
    version: Annotated[
        bool | None, typer.Option("--version", callback=_version, is_eager=True)
    ] = None,
) -> None:
    """Record from the microphone until Ctrl+C. Text is shown live and saved to Markdown."""
    if ctx.invoked_subcommand is not None:
        # The converted values (Path, enums), not the raw strings in ctx.params.
        values = {
            "lang": lang,
            "backend": backend,
            "llm_model": llm_model,
            "ollama_url": ollama_url,
            "output_dir": output_dir,
        }
        ctx.obj = {n: values[n] for n in SHARED_OPTIONS if _from_command_line(ctx, n)}
        # For `summarize` without a file, which records first.
        ctx.obj[RECORD_KEY] = RecordOptions(
            lang, model_size, model_path, no_llm, device, silence_ms, threshold, no_calendar
        )
        return
    if list_devices:
        _print_devices()
        raise typer.Exit()

    llm_model = llm_model or llm.default_model(backend.value)
    opts = RecordOptions(
        lang, model_size, model_path, no_llm, device, silence_ms, threshold, no_calendar
    )
    writer, polished = _record(opts, backend, llm_model, ollama_url, output_dir.expanduser())
    _emit_stdout(polished or writer.raw_text)


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


def _record(
    opts: RecordOptions,
    backend: Backend,
    llm_model: str,
    ollama_url: str,
    output_dir: Path,
    tag: bool = True,
) -> tuple[SessionWriter, str | None]:
    """Record until Ctrl+C, then polish, tag, save and update the index.

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
        transcriber = Transcriber(path, language)

    meeting_meta = {} if opts.no_calendar else _meeting_meta()
    started = datetime.now()
    writer = SessionWriter(output_dir, started, language, model_name, extra_meta=meeting_meta)
    view = LiveView(language, model_name)
    recorder = Recorder(
        transcriber, Segmenter(silence_ms=opts.silence_ms, threshold=opts.threshold), writer, view
    )

    console.print(f"[dim]Saving to {writer.path}[/]")
    console.rule("[bold red]● Listening[/]", style="red")
    try:
        with MicStream(_parse_device(opts.device)) as mic:
            recorder.run(mic)
    except Exception as e:  # PortAudio raises plain Exceptions
        writer.discard_if_empty()
        transcriber.close()
        console.print(f"[red]Microphone error:[/] {e}")
        raise typer.Exit(1) from e
    recorder.finish(mic)
    transcriber.close()
    ended = datetime.now()

    if writer.discard_if_empty():
        console.print("[yellow]No speech detected — nothing saved.[/]")
        raise typer.Exit()

    polished = (
        None if no_llm else _run_polish(writer.raw_text, language, backend, llm_model, ollama_url)
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
    console.rule(style="green")
    console.print(f"[green]✔ Saved[/] [link=file://{writer.path}]{writer.path}[/link]")
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
) -> None:
    """Re-run the LLM polish on a session file (on the raw transcript if present).

    A file from outside the output folder is copied into transcripts/YYYY-MM/ first, with
    front matter you are asked for; the original is left untouched.
    """
    lang = _inherit(ctx, "lang", lang)
    backend = _inherit(ctx, "backend", backend)
    llm_model = _inherit(ctx, "llm_model", llm_model)
    ollama_url = _inherit(ctx, "ollama_url", ollama_url)
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser().resolve()
    imported = not file.resolve().is_relative_to(output_dir)
    if imported:
        file = _import_external(file, output_dir, lang)
        _update_index(output_dir)
    text = file.read_text(encoding="utf-8")
    meta, raw = parse_document(text)
    if not raw:
        console.print("[red]No transcript found in file.[/]")
        raise typer.Exit(1)
    language = lang.value if lang else meta.get("language", "en")
    llm_model = llm_model or llm.default_model(backend.value)
    polished = _run_polish(raw, language, backend, llm_model, ollama_url)
    if polished is None:
        if imported:
            console.print(f"[dim]The imported file is kept: {file}[/]", soft_wrap=True)
        raise typer.Exit(1)
    meta["llm_model"] = llm.describe(backend.value, llm_model)
    if tags := _run_tags(raw, language, backend, llm_model, ollama_url, output_dir):
        meta["tags"] = format_tags(tags)
    write_final(file, meta, raw, polished, summary=extract_summary(text))
    _update_index(output_dir)
    console.print(f"[green]✔ Updated[/] {file}", soft_wrap=True)
    _emit_stdout(polished)


def _import_external(file: Path, output_dir: Path, lang: Lang | None) -> Path:
    """Copy a file from outside the output folder into it, asking for the front matter."""
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
    interactive = sys.stdin.isatty()
    if not interactive:
        console.print("[dim]Not a terminal; using defaults for the front matter.[/]")

    when = default_when
    language, meeting = default_lang, meta.get("meeting", "")
    participants = meta.get("participants", "")
    if interactive:
        while (
            answer := _parse_when(_ask("Date and time", f"{default_when:%Y-%m-%d %H:%M}"))
        ) is None:
            console.print("[yellow]Use YYYY-MM-DD HH:MM.[/]")
        when = answer
        while (language := _ask("Language (en/sv)", default_lang)) not in Lang.__members__:
            console.print("[yellow]Choose en or sv.[/]")
        meeting = _ask("Meeting name", meeting)
        participants = _normalize_participants(_ask("Participants, comma-separated", participants))

    front = {
        **meta,
        "date": when.isoformat(timespec="seconds"),
        "language": language,
        "meeting": meeting,
        "participants": participants,
        "source": str(file.resolve()),
    }
    path = import_document(output_dir, when, front, body)
    console.print(f"[green]✔ Imported to[/] {path}", soft_wrap=True)
    return path


def _record_for_summary(
    ctx: typer.Context,
    lang: Lang | None,
    backend: Backend,
    llm_model: str,
    ollama_url: str,
    output_dir: Path,
) -> Path:
    """Record and polish a new session for `summarize`; returns its file.

    Tags are left to the summarize pass, so they are only asked for once.
    """
    opts = (ctx.obj or {}).get(RECORD_KEY) or RecordOptions()
    if lang:
        opts.lang = lang
    if opts.no_llm:
        console.print("[red]--no-llm can't be combined with summarize.[/]")
        raise typer.Exit(2)
    writer, polished = _record(opts, backend, llm_model, ollama_url, output_dir, tag=False)
    if polished is None:
        console.print(
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
) -> None:
    """Add an executive summary, decisions and action items to the top of a session file.

    Without FILE it records a meeting first (like plain `mic2md`), polishes it and then
    summarizes it. Recording options such as -m, -d or --no-calendar go before the command:
    [bold]mic2md -m small.en summarize[/].

    Running it again replaces the previous summary. A file from outside the output folder is
    copied into transcripts/YYYY-MM/ first, with front matter you are asked for; the original
    is left untouched.
    """
    lang = _inherit(ctx, "lang", lang)
    backend = _inherit(ctx, "backend", backend)
    llm_model = _inherit(ctx, "llm_model", llm_model) or llm.default_model(backend.value)
    ollama_url = _inherit(ctx, "ollama_url", ollama_url)
    output_dir = _inherit(ctx, "output_dir", output_dir).expanduser().resolve()
    imported = False
    if file is None:
        file = _record_for_summary(ctx, lang, backend, llm_model, ollama_url, output_dir)
    elif imported := not file.resolve().is_relative_to(output_dir):
        file = _import_external(file, output_dir, lang)
        _update_index(output_dir)
    text = file.read_text(encoding="utf-8")
    meta, transcript = parse_document(text)
    if not transcript:
        console.print("[red]No transcript found in file.[/]")
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
        ),
        "file left unchanged",
    )
    if summary is None:
        if imported:
            console.print(f"[dim]The imported file is kept: {file}[/]", soft_wrap=True)
        raise typer.Exit(1)
    meta["summary_model"] = llm.describe(backend.value, llm_model)
    if tags := _run_tags(transcript, language, backend, llm_model, ollama_url, output_dir):
        meta["tags"] = format_tags(tags)
    insert_summary(file, meta, text, summary)
    _update_index(output_dir)
    console.print(f"[green]✔ Updated[/] {file}", soft_wrap=True)
    _emit_stdout(summary)


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
    """Rebuild index.md, moving sessions saved by older versions into transcripts/YYYY-MM/."""
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


if __name__ == "__main__":
    app()
