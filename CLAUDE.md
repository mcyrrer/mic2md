# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`mic2md` is a Python CLI for real-time local dictation. It records from the mic, shows
text live with whisper.cpp, appends each finished sentence to an ISO-datetime-named Markdown
file, and then runs one Ollama pass to fix spelling and grammar and format the text as
Markdown. It supports English (default) and Swedish (KB-Whisper). Primary platform: macOS
Apple Silicon (Metal).

## Commands

`make` lists shortcuts for all of these (`make check`, `make install`, …).

```bash
uv sync                               # create .venv with runtime + dev deps
uv run mic2md                         # run from source (Ctrl+C to stop)
uv run mic2md --no-llm -m base.en     # quick run: small model, no Ollama
uv run pytest                         # unit tests (no mic, model or Ollama needed)
uv run ruff check . && uv run ruff format .
uv tool install --reinstall .         # install/update the global `mic2md` command
```

## Layout

```
src/mic2md/
  cli.py          Typer app. `main` (record), `polish FILE`, `summarize [FILE]`, `tag`, `reindex`,
                  `models`. Recorder class = main loop.
  audio.py        MicStream (sounddevice → queue of 30 ms float32 frames) + Segmenter (energy VAD)
  transcriber.py  pywhispercpp wrapper; clean_text() strips [BLANK_AUDIO] & known hallucinations
  models.py       REGISTRY of ggml models per language, cache dir, httpx downloader
  llm.py          chat() → Ollama /api/chat, `claude -p` (stream-json) or `copilot -p`
                  (text; all CLIs via _run_cli from an empty temp dir), SYSTEM_PROMPT
                  (polish), SUMMARY_PROMPT (summarize), check(), strip_wrapping()
  meetings.py     EventKit lookup of the meeting in progress (title + attendees) for front matter
  index.py        index.md builder (scans transcripts/**/ front matter), migrate_flat()
  writer.py       SessionWriter (incremental append), write_final(), parse_document()
  ui.py           rich LiveView: partial line + status bar with mic level meter
tests/            pytest; pure-logic tests with synthetic audio and httpx.MockTransport
lauche.sh         Legacy record-then-transcribe script (predecessor, kept for reference)
```

## Data flow

1. `MicStream` callback puts frames on a `queue.Queue` (audio thread; do no work there).
2. `Recorder.run` (main thread) drains the queue and calls `Segmenter.feed`. A returned array
   is a finished utterance, which goes to `commit()`: transcribe, print, `writer.append`.
3. While in speech, `maybe_partial()` re-transcribes the in-progress buffer about every 1 s
   and shows it in `LiveView.partial`. Partials are never saved.
4. Ctrl+C raises `KeyboardInterrupt` in `run()`. `finish()` re-commits `pending` (an
   utterance interrupted mid-transcription), drains the leftover frames and flushes the
   segmenter.
5. `_run_polish` calls `llm.check`, then `llm.polish` (streamed to stderr). `writer.finalize`
   atomically rewrites the file with the polished text only; the raw transcript is dropped.

## Conventions and invariants

- **UI goes to stderr** (`Console(stderr=True)`). stdout only gets the final document, and
  only when it isn't a TTY (`_emit_stdout`). Don't print anything else to stdout.
- **Never lose transcript text.** Raw lines are appended to disk immediately. The final
  rewrite goes through a `.tmp` file and `replace`. If Ollama fails for any reason, keep
  the raw transcript and warn; don't crash. Raw text is only removed once a polish succeeded.
- **Output layout**: `<output_dir>/transcripts/YYYY-MM/<name>.md` (`writer.session_path`) and
  `<output_dir>/index.md`, which is always regenerated from all session files' front matter
  (never edited incrementally) after each recording and by `mic2md reindex`. It has a
  table per month (with tags), a `## Tags` section (tag → notes) and a footer linking
  `REPO_URL` (`mic2md/__init__.py`).
- **summarize without FILE** records first: `_record_for_summary` → `_record` (the same
  record/polish/save flow as `main`, with `tag=False` so tagging happens once, in summarize).
  Recording options come from the top-level callback via `ctx.obj[RECORD_KEY]`
  (`RecordOptions`). A failed polish skips the summary; `--no-llm` is rejected.
- **Summary block**: `summarize` writes its output between `<!-- mic2md:summary -->` and
  `<!-- /mic2md:summary -->` right after the front matter, followed by `---`. The parser also
  accepts the older `voice2text:summary` markers from before the rename.
  `parse_document` strips that block so it is never fed back to the LLM as transcript;
  `insert_summary` replaces it in place; `polish` carries it over via `extract_summary`.
- **Tags**: `tags: [a, b-c]` (one-line YAML flow list, so the front matter stays flat and
  Obsidian reads it). Written by `writer.format_tags`, read by `writer.parse_tags`. Produced by
  a separate quiet LLM call (`llm.extract_tags`, `TAGS_PROMPT`) after polish, in `polish`,
  `summarize` and `tag`; `index.known_tags` feeds existing tags back so the model reuses
  them. Tags are normalized to lowercase-hyphenated (`llm.normalize_tag`), max 8. A tagging
  failure only warns.
- **Importing**: `polish` or `summarize` on a file outside `--output-dir` calls `_import_external` (asks
  for date/language/meeting/participants on a TTY, defaults otherwise) and
  `writer.import_document`, which copies it into `transcripts/YYYY-MM/` under a free
  session name. The source file is never modified.
- **File names** come from `writer.session_filename`: `%Y-%m-%dT%H-%M-%S.md`, ISO 8601 with
  `-` instead of `:`. Front matter is flat `key: value` lines. `parse_document` depends on
  that format. It still reads the `<summary>Raw transcript</summary>` section that older
  polished files have; newer files contain only the polished body.
- **Models**: add new ones only through `models.REGISTRY`. KBLab repos all use the remote
  name `ggml-model-q5_0.bin`, so `local_file` must be unique per size.
- **Languages**: adding one means a `Lang` enum value (cli.py), a `REGISTRY` and
  `DEFAULT_SIZE` entry (models.py), a `LANGUAGE_NAMES` entry (llm.py), and ideally
  hallucination phrases (transcriber.py).
- **claude backend** (`--backend claude`) runs `claude -p` with no tools, MCP servers,
  settings, skills or session persistence, from an empty temp dir (so no CLAUDE.md is
  loaded). System prompt via `--system-prompt`, transcript on stdin, text deltas parsed
  from `--output-format stream-json --include-partial-messages`. Tests use a fake `claude`
  script on PATH; never call the real CLI from tests.
- **copilot backend** (`--backend copilot`) runs `copilot -p PROMPT -s --no-ask-user
  --no-custom-instructions --disable-builtin-mcps --no-color`. It has no system-prompt flag
  and ignores stdin with -p, so instructions + transcript go in the -p argument. Never pass
  `--allow-all-tools`: without it, tool calls are refused in -p mode (verified on 1.0.88).
  No `--model` unless `--llm` is given, so Copilot picks its default.
- **Ollama calls** set `think: false`, so reasoning models like qwen3.5 answer directly.
  `strip_wrapping` still removes `<think>` blocks and code fences as a fallback.
- **Heavy imports** (`pywhispercpp`, `sounddevice`) are imported lazily, so `--help`,
  `models` and the tests stay fast and work without audio hardware.
- Style: ruff (line length 100, rules E/F/I/B/UP/SIM), type hints, `from __future__ import
  annotations`. Python 3.11–3.13 (`pywhispercpp` has no 3.14 wheels yet).

## Gotchas

- `Transcriber.close()` frees the model with fd 2 muted. Without it, whisper.cpp prints
  `ggml_metal_free: deallocating` at exit.
- The Segmenter calibrates the noise floor from the first 500 ms. Tests must feed quiet
  frames first, or pass `threshold=`.
- whisper.cpp pads input to 30 s internally, so a partial pass costs about the same as a
  final one (~1 s for large models on an M-series chip). That's why partials are rate-limited.
- When testing Ctrl+C from a non-interactive shell, background processes inherit SIGINT as
  ignored. Restore it first, e.g. `perl -e '$SIG{INT}="DEFAULT"; exec @ARGV' mic2md …`.
- Manual end-to-end test without talking: generate speech with
  `say -o x.aiff "…"; sox x.aiff -r 16000 -c 1 -b 16 x.wav` and feed the frames to
  `Recorder.process` through a fake mic queue.
