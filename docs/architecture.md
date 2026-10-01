# mic2md — Logical Architecture

## Overview

`mic2md` is a single-user, local-first CLI for real-time dictation on macOS Apple
Silicon. It has no server, no persisted process, and no concept of accounts: each
invocation is one operation (record, polish, summarize, tag, reindex) that reads and
writes plain Markdown files on disk. The three external systems it talks to are all
optional and independently substitutable: the microphone (via `sounddevice`), the
local speech recognizer (`whisper.cpp` via `pywhispercpp`), and a text-generation
backend (Ollama, or a `claude -p` / `copilot -p` subprocess). Everything else — voice
activity detection, segmentation, uncertainty scoring, front-matter parsing, index
generation — is pure, testable Python with no I/O side effects beyond explicit file
writes.

The system is built around one pipeline:

```
mic → frames → Segmenter (VAD) → utterance audio → Transcriber (whisper.cpp)
    → raw text appended to session .md → LLM polish pass → final .md
    → (optional) summarize / tag pass → index.md rebuild
```

Two threads only: the `sounddevice` callback thread (produces frames into a
`queue.Queue`, does no work) and the main thread (`Recorder.run`, consumes the queue,
drives VAD, transcription, and UI). `llm._run_cli` additionally spawns short-lived
reader/writer threads per external CLI call, joined before returning.

## Key Components

- **`cli.py`** — Typer app and orchestration layer. Defines all commands and shared
  options, and hosts the `Recorder` class, which is the main-loop state machine tying
  together audio, VAD, transcription and the writer. Also owns interactive prompts
  (`_ask`, `_meeting_meta`, `_import_external`) and the LLM-progress spinner
  (`LlmProgress`/`LineStreamer`/`_stream_llm`). No other module talks to Typer, rich
  `Console`, or `sys.stdin`/`stdout` directly — `cli.py` is the only place user I/O
  happens (aside from `models.py`'s download progress bar).
- **`audio.py`** — `MicStream` (context manager wrapping a `sounddevice.InputStream`,
  pushing fixed 30ms float32 frames to a queue) and `Segmenter` (a small state machine
  that buffers frames into an utterance while "speech" is detected, applies preroll and
  minimum-length/max-length rules, and emits a finished `np.ndarray` on trailing
  silence). Speech detection is pluggable: a `detector` callable (Silero) or, absent
  one, an energy threshold self-calibrated from the first 500ms of audio.
- **`vad.py`** — `SileroDetector`, a thin adapter from `pysilero-vad`'s raw callable to
  the `Segmenter`'s `detector` protocol. Buffers arbitrary-length frames into the
  512-sample chunks Silero requires and applies hysteresis (enter at 0.5 probability,
  exit at 0.35) so short dips in confidence don't fragment an utterance. Stateful and
  must see audio once, in order.
- **`transcriber.py`** — Wraps `pywhispercpp.Model`. `clean_text` strips bracketed
  annotations and a fixed hallucination list (English + Swedish). `decode_params`
  chooses greedy/`audio_ctx`-limited decoding for partials vs. beam-search/full-context
  for finished utterances. `uncertain_words`/`_text_tokens` read per-token log
  probabilities out of the whisper.cpp context to flag low-confidence words. Also owns
  `build_vocabulary` (meeting/participants/glossary → Whisper prompt prefix, capped at
  600 chars) and `read_glossary`.
- **`models.py`** — Static `REGISTRY` of `ModelSpec`s (English from ggerganov's repo,
  Swedish from KBLab), a `cache_dir()` resolver (`MIC2MD_CACHE_DIR` /
  `XDG_CACHE_HOME` / `~/.cache`), and `ensure_model`/`download` which fetch a `.bin`
  from Hugging Face with a resumable-looking `.part` file swapped in atomically.
- **`llm.py`** — Backend-agnostic chat interface (`chat()` dispatches to
  `_ollama_chat`/`_claude_chat`/`_copilot_chat`) plus the three prompt templates
  (`SYSTEM_PROMPT` for polish, `SUMMARY_PROMPT`, `TAGS_PROMPT`) and post-processing
  (`strip_wrapping`, `drop_title_time`, tag `normalize_tag`/`parse_tags`). `check()`
  validates a backend is reachable/installed before a recording starts, so failures
  surface early with an actionable message. CLI backends run via `_run_cli` from an
  empty temp directory specifically to avoid loading the *target* project's own
  CLAUDE.md/AGENTS.md into the polishing model's context.
- **`meetings.py`** — Isolated EventKit integration (macOS Calendar). Finds the
  meeting "in progress" (with a 5-minute grace window before start) and its
  attendees, exposed as a single `current_meeting()` call the CLI treats as
  best-effort (any exception is caught and just produces an interactive prompt or
  empty metadata instead of blocking a recording).
- **`index.py`** — Rebuilds `index.md` by scanning every session file's front matter
  under `transcripts/`; never edited incrementally, always fully regenerated
  (`update()`) via `.tmp` + `replace`. Also `migrate_flat()` for moving pre-existing
  flat-layout files into `transcripts/YYYY-MM/`.
- **`writer.py`** — All Markdown file-format logic: front-matter
  serialization/parsing, session filename/path derivation, the append-as-you-go
  `SessionWriter`, atomic final rewrite (`write_final`), summary-block
  insertion/extraction, tag formatting, and external-file import
  (`import_document`). This is the module that encodes the on-disk data format and is
  the single source of truth for it — `cli.py` and `index.py` both depend on its
  parsing functions rather than re-implementing front-matter handling.
- **`ui.py`** — Pure rendering: the rich `LiveView` (status bar, level meter,
  uncertain-word underlining) with no side effects beyond producing `rich` renderables;
  `cli.py` is responsible for actually printing it via `Live`.

## Key Use Cases

1. **Record** (`mic2md`, default command) — Loads/downloads the Whisper model,
   optionally looks up the calendar meeting (or asks interactively on a TTY),
   opens the mic, and loops: segment → transcribe finished utterances → append to
   the session file live, while also re-transcribing the in-progress utterance as a
   fast "partial" purely for on-screen preview (never persisted). Ctrl+C stops the
   loop; `Recorder.finish` re-commits any interrupted utterance and flushes the
   segmenter's tail buffer so nothing spoken is lost. Then runs one polish pass and
   (if not `--no-llm`) one tagging pass, writes the final file, and rebuilds the
   index. With no subcommand this is actually implemented as `summarize` under the
   hood (record → polish → summarize → tag in one pass).
2. **Polish** (`mic2md polish FILE` / `p`) — Re-runs just the LLM cleanup pass on an
   existing session file's raw transcript (or imports an external file first). Used to
   retry a failed/skipped polish, or to re-polish with a different backend/model.
   Carries over an existing summary block untouched.
3. **Summarize** (`mic2md summarize [FILE]` / `s`) — Adds an executive summary,
   decisions, and action items block to the top of a session file. Without a FILE
   argument it records a brand-new meeting first (record → polish → summarize,
   tagging deferred to just once at the end). Re-running replaces the previous
   summary block in place.
4. **Tag** (`mic2md tag [FILES...] [--all]`) — Backfills/refreshes the `tags:` front
   matter on existing session files (e.g. ones made before tagging existed, or
   `--all` for every currently-untagged session), reusing `index.known_tags` so the
   model prefers existing tags over near-duplicate new ones.
5. **Reindex** (`mic2md reindex`) — Migrates any session files sitting flat in the
   output directory (older layout) into `transcripts/YYYY-MM/`, then rebuilds
   `index.md` from scratch. Also the module invoked implicitly after every
   record/polish/summarize/tag operation.
6. **Import** (implicit, from `polish`/`summarize` on a file outside `--output-dir`)
   — `_import_external` asks for date/language/meeting/participants (defaulting
   silently on a non-TTY) and copies the file into `transcripts/YYYY-MM/` under a
   free session-name slot; the original file is never modified or deleted.
7. **Models** (`mic2md models`) — Lists the Whisper model registry and cache status;
   pure read, no recording/LLM involved.

## Domain Rules & Invariants

- **Never lose transcript text.** Raw lines are appended to disk immediately as
  they're transcribed (`SessionWriter.append`), before any LLM call. If polish fails
  or is skipped (`--no-llm`, Ollama unreachable, LLM error, Ctrl+C during polish), the
  raw transcript is kept as the file's body and a warning is printed — the code path
  never crashes or discards it (`_run_polish` returns `None` on any `LLMError` or
  `KeyboardInterrupt`).
- **Raw text is dropped only after a successful polish.** `write_final`/`finalize`
  only replace the raw body with the polished text when `polished` is truthy;
  otherwise the raw text (with a synthesized `# Transcript …` heading) becomes the
  final content.
- **Atomic rewrites everywhere a file is finalized or rebuilt.** Every place that
  replaces a whole file's content (`writer.write_final`, `insert_summary`,
  `update_front_matter`, `import_document`, `index.update`, `models.download`) writes
  to a `<name>.tmp` sibling and calls `Path.replace()`, so a crash mid-write can never
  leave a half-written file in place of a good one.
- **Session filenames are the identity key.** `writer.session_filename`:
  `%Y-%m-%dT%H-%M-%S.md`, ISO 8601 with `-` instead of `:`, plus an optional
  `-<slug>` suffix (`slugify_title`: lowercase, non-alphanumerics collapsed to `-`,
  max 20 chars). `index._SESSION_NAME` and `migrate_flat` must accept both the bare
  and suffixed forms — this regex is effectively a schema contract shared across
  `writer.py` and `index.py`.
- **Front matter is deliberately flat `key: value` lines**, not real YAML — chosen so
  `render_front_matter`/`split_front_matter` can be simple, and so Obsidian still
  reads it correctly. `tags:` is the one field that's an inline flow list
  (`[a, b-c]`) rather than a nested block, for the same reason. `parse_document`
  depends exactly on this format plus fixed markers for the (optional) raw-transcript
  `<details>` section and the `<!-- mic2md:summary -->`/`<!-- /mic2md:summary -->`
  block (also accepting the pre-rename `voice2text:summary` marker for backward
  compatibility) — the summary block is always stripped before transcript text is
  fed back into any LLM prompt.
- **`index.md` is a derived, disposable artifact.** It is fully regenerated from
  session front matter on every mutating command and by `reindex`; the code never
  edits it incrementally, and its own footer tells the reader edits will be
  overwritten.
- **Partial (in-progress) transcriptions are never persisted** — they exist only in
  `LiveView.partial` for on-screen preview and are recomputed with fast, low-context,
  greedy decoding; only `Recorder.commit` (finished utterances) writes to the
  `SessionWriter`.
- **pywhispercpp param statefulness.** `Model.transcribe(**params)` mutates a shared
  struct that persists between calls, so `Transcriber.transcribe` must set every
  param it depends on (including e.g. `initial_prompt=""`) on every call rather than
  relying on defaults from a previous call.
- **Uncertain-word marking is a one-way pipeline**: whisper.cpp token probabilities →
  `uncertain_words` (per-word min probability < 0.4) → `mark_uncertain` appends
  `word(?)` in the file the LLM will see → the polish `SYSTEM_PROMPT` is explicitly
  instructed to resolve `(?)` markers using context/glossary and always remove them;
  they must never survive into a polished document.
- **CLI LLM backends are sandboxed from project context.** `claude -p`/`copilot -p`
  always run from an empty temp directory with no tools, MCP servers, or settings, so
  that polishing a transcript can never pick up and be influenced by an unrelated
  project's CLAUDE.md/AGENTS.md sitting in the working directory.
- **A tagging failure only warns**, never blocks saving a file — tags are best-effort
  metadata, unlike the polish pass which the raw-text-preservation rule protects more
  strictly.
- **Model registry uniqueness constraint**: KBLab publishes every Swedish model size
  under the identical remote filename `ggml-model-q5_0.bin`, so `ModelSpec.local_file`
  must be given a unique name per size to avoid cache collisions — enforced only by
  convention in `models._kb`, not by a runtime check.

## User Roles

Single-user, local CLI — there is no authentication, multi-tenancy, or shared state.
The only "role" distinction in the code is environmental, not a user role: whether
stdout is a TTY (interactive prompts + rich UI) vs. piped (`_emit_stdout` writes only
the final document, so `mic2md | pbcopy` works), and whether a run is attended
(calendar/front-matter prompts) or unattended/non-interactive (silent defaults used
instead, e.g. in `_import_external` and `_meeting_meta`). All commands operate on the
invoking user's own filesystem and calendar; there's no concept of another user's
data.
