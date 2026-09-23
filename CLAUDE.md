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
  cli.py          Typer app. `main` (record), `polish FILE` (alias `p`), `summarize [FILE]`
                  (alias `s`), `tag`, `reindex`, `models`. Recorder class = main loop.
  audio.py        MicStream (sounddevice → queue of 30 ms float32 frames) + Segmenter (utterances;
                  speech decided by a detector, or an energy threshold without one)
  vad.py          SileroDetector (pysilero-vad): 30 ms frames → 512-sample chunks, hysteresis
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
3. While in speech, `maybe_partial()` re-transcribes the in-progress buffer with
   `partial=True` (small `audio_ctx`, greedy best_of 1, no temperature fallback) and shows it
   in `LiveView.partial`. The next partial waits max(0.3 s, 2 × the last one's duration).
   Partials are never saved. Finished utterances use the full context and beam search
   (`--beam-size`, default 5; ~8% slower than greedy, measured). `decode_params` returns the
   strategy as "greedy"/"beam" and `Transcriber` maps it to the whisper.cpp enum, which is
   settable per call.
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
- **Glossary**: `--glossary` / `<output_dir>/glossary.txt` (`cli._glossary_terms`,
  `transcriber.read_glossary`). `transcriber.build_vocabulary` joins meeting, participants and
  terms (≤600 chars) into `Transcriber.vocabulary`, which leads every Whisper prompt. The LLM
  gets participants + terms via `llm.terms_rule` (polish and summarize).
- **pywhispercpp keeps params between calls** (`Model.transcribe(**params)` sets them on a
  shared struct). Set every param a call relies on each time, e.g. `initial_prompt=""`.
- **Timestamps**: `Segmenter.last_start_s` (sample count incl. preroll) → `Recorder.commit(audio,
  start)` → `SessionWriter.append(text, at=)` writes `[HH:MM:SS] text`. Only the file gets the
  prefix; the terminal line and Whisper's prompt stay plain. `SYSTEM_PROMPT` turns them into
  `## Heading (HH:MM:SS)`; `llm.drop_title_time` removes the one models put on the `# ` title.
- **Uncertain words**: after a non-partial decode, `Transcriber._text_tokens` reads token bytes +
  probabilities from `model._ctx` (ids ≥ `whisper_token_eot` are special, skipped);
  `uncertain_words` groups them into words (min p of word-bearing tokens, < `UNCERTAIN_P` 0.4)
  → `Transcriber.last_uncertain`. `Recorder.commit` underlines them (`ui.highlight_uncertain`)
  and writes `word(?)` (`mark_uncertain`); `SYSTEM_PROMPT` tells the LLM to fix and drop it.
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
- Without a `detector`, the Segmenter calibrates the noise floor from the first 500 ms. Tests
  must feed quiet frames first, or pass `threshold=` or `detector=`.
- Silero is stateful: each `SileroDetector` must see the audio once and in order. `--vad`
  defaults to silero; `cli._detector` falls back to the energy threshold (with a warning) if
  it can't load, and `--threshold` implies energy.
- pywhispercpp 1.5.1's own `whisper_vad_*` bindings are broken ("Unregistered type:
  whisper_vad_context_wrapper"), which is why Silero comes from pysilero-vad.
- whisper.cpp pads input to 30 s internally, so a full pass costs ~1 s for large models on an
  M-series chip regardless of length. Partials limit the encoder with `audio_ctx`
  (`transcriber.audio_ctx_for`), ~0.15 s for a few seconds of audio. Only multiples of 256
  work: other sizes (128, 192, 320, 384, 448, 640) give garbage like "of" or repeated words.
- When testing Ctrl+C from a non-interactive shell, background processes inherit SIGINT as
  ignored. Restore it first, e.g. `perl -e '$SIG{INT}="DEFAULT"; exec @ARGV' mic2md …`.
- Manual end-to-end test without talking: generate speech with
  `say -o x.aiff "…"; sox x.aiff -r 16000 -c 1 -b 16 x.wav` and feed the frames to
  `Recorder.process` through a fake mic queue.
