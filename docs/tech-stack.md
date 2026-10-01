# Tech Stack

`mic2md` is a Python CLI for real-time local dictation on macOS Apple Silicon: mic → live
whisper.cpp transcription → Markdown file → one LLM polish pass (Ollama, Claude Code, or
GitHub Copilot CLI).

## Languages

- **Python** 3.11–3.13 (`requires-python = ">=3.11,<3.14"` in `pyproject.toml`; capped below
  3.14 because `pywhispercpp` has no 3.14 wheels yet).
- `from __future__ import annotations` used throughout; type hints required by style
  convention.
- Small amount of **shell** (`lauche.sh`, the legacy predecessor script; `Makefile` for
  developer shortcuts).

## Core Libraries (from `pyproject.toml`)

| Package | Version constraint | Role |
|---|---|---|
| `pywhispercpp` | `>=1.3` | Python bindings for whisper.cpp; loads ggml models, runs transcription |
| `sounddevice` | `>=0.5` | PortAudio bindings; microphone capture (`MicStream`) |
| `numpy` | `>=1.26` | Audio frame buffers, float32 sample arrays |
| `rich` | `>=13.7` | Live TTY UI (`LiveView`), console, progress bars, tables |
| `httpx` | `>=0.27` | HTTP client — Ollama REST calls, model file downloads |
| `typer` | `>=0.12` | CLI framework (the `mic2md` Typer `app` in `cli.py`) |
| `pyobjc-framework-eventkit` | `>=10` (darwin only) | macOS Calendar access (EventKit) for meeting lookup |
| `pysilero-vad` | `>=3.4` | Silero voice-activity-detection model wrapper |

Dev dependencies (`[dependency-groups].dev`): `pytest>=8`, `ruff>=0.6`.
Build backend: `hatchling`.

## Speech/ML Backends

- **whisper.cpp** via `pywhispercpp` (`src/mic2md/transcriber.py`) — C++ inference engine for
  OpenAI Whisper and KB-Whisper models, using Apple **Metal** for GPU acceleration on Apple
  Silicon.
- **Model format**: `ggml` binary files (e.g. `ggml-base.en-q5_1.bin`,
  `ggml-kb-whisper-large-q5_0.bin`), quantized (q5_0/q5_1), downloaded on demand via `httpx`
  and cached locally (`models.py` `REGISTRY`, `cache_dir()`).
- **Languages supported**: English (OpenAI Whisper models, default) and Swedish (KB-Whisper,
  KBLab). Adding a language means a `Lang` enum value, a `REGISTRY`/`DEFAULT_SIZE` entry, an
  `llm.LANGUAGE_NAMES` entry, and hallucination phrases in `transcriber.py`.
- **Voice activity detection**: Silero VAD via `pysilero-vad`, 30 ms frames chunked to 512
  samples with hysteresis (`vad.py` `SileroDetector`); falls back to an energy-threshold
  detector if Silero can't load. (`pywhispercpp`'s own bundled `whisper_vad_*` bindings are
  broken in 1.5.1, hence the separate Silero dependency.)
- Partial (in-progress) transcription uses a constrained `audio_ctx` (multiples of 256 only)
  with greedy decoding; finished utterances use full-context beam search
  (`--beam-size`, default 5).
- Per-token uncertainty: reads token log-probabilities off whisper.cpp's internal context
  (`model._ctx`) to flag low-confidence words.

## LLM Backends & Integrations

- **Ollama** (default) — local LLM server, called over its **REST API**:
  - `GET /api/tags` to verify the server is reachable and the model is installed
    (default URL `http://localhost:11434`).
  - `POST /api/chat` (streamed, `think: false` to suppress reasoning-model chain-of-thought)
    for polish, summarize, and tag-extraction passes.
- **`claude -p`** (`--backend claude`) — shells out to the Claude Code CLI with no tools, MCP
  servers, settings, skills or session persistence, run from an empty temp directory (so no
  `CLAUDE.md` loads). System prompt passed via `--system-prompt`; transcript piped on stdin;
  streamed via `--output-format stream-json --include-partial-messages`.
- **`copilot -p`** (`--backend copilot`) — shells out to the GitHub Copilot CLI:
  `copilot -p PROMPT -s --no-ask-user --no-custom-instructions --disable-builtin-mcps
  --no-color`. No system-prompt flag, so instructions + transcript are concatenated into the
  `-p` argument; never passes `--allow-all-tools`.
- All subprocess CLI backends invoked via a shared `_run_cli` helper (`llm.py`), from an
  empty temp directory, with output parsed as JSON-lines (`stream-json`) or plain text
  depending on backend.
- Shared post-processing: `strip_wrapping()` removes `<think>...</think>` blocks and Markdown
  code fences regardless of backend.

## macOS Integrations

- **EventKit** (`src/mic2md/meetings.py`), via `pyobjc-framework-eventkit`: looks up the
  in-progress Calendar event (`EKEventStore`, `EKEntityTypeEvent`) to populate meeting title
  and attendee names in the transcript front matter and as Whisper/LLM vocabulary hints.
  Requires Calendar access authorization (`authorizationStatusForEntityType_`,
  `requestAccessToEntityType_completion_`); macOS-only, gated behind `sys_platform == 'darwin'`
  and a runtime `import EventKit` guarded with a fallback error.
- **Metal** — whisper.cpp's GPU backend, used implicitly through `pywhispercpp` on Apple
  Silicon.

## Dev Tooling / Testing

- **uv** — dependency management and virtualenv creation (`uv sync`, `uv run`,
  `uv tool install`); lockfile `uv.lock`.
- **pytest** (`>=8`) — unit tests in `tests/`, run without mic/model/Ollama by using synthetic
  audio arrays and `httpx.MockTransport`; fake `claude`/`copilot` shell scripts on `PATH`
  stand in for the real CLIs.
- **ruff** (`>=0.6`) — lint (`E`, `F`, `I`, `B`, `UP`, `SIM` rule sets) and formatting;
  line length 100, target `py311`.
- **Makefile** — `make check`, `make install`, and other shortcuts wrapping the `uv`/`ruff`/
  `pytest` commands above.
- **hatchling** — PEP 517 build backend producing the `mic2md` console-script package.

## Protocols & Standards

- **Ollama `/api/chat`** — JSON-over-HTTP streaming chat completion protocol (NDJSON chunks).
- **ggml** — whisper.cpp's quantized model binary format (q5_0/q5_1 variants used here).
- **ISO 8601** — session file names (`%Y-%m-%dT%H-%M-%S.md`, `:` replaced with `-`) and
  in-file timestamps (`[HH:MM:SS]`).
- **YAML front matter** (flat `key: value` lines, plus a one-line YAML flow list for
  `tags: [a, b-c]`) — Obsidian-compatible Markdown metadata at the top of each transcript file.
- **Markdown** — the sole output document format (`## Heading (HH:MM:SS)` sections,
  `<!-- mic2md:summary -->` / `<!-- /mic2md:summary -->` comment-delimited summary blocks,
  legacy `voice2text:summary` markers, and `<summary>Raw transcript</summary>` blocks from
  older polished files).
- **CLI subprocess protocols** — `claude -p --output-format stream-json
  --include-partial-messages` (structured JSON-lines streaming) and `copilot -p -s`
  (plain text) as alternate "backends" to the Ollama HTTP protocol.
