# Testing

## Test types

All tests are pure-logic unit tests (`tests/`, 9 files, 88 tests total, ~5s runtime).
No microphone, downloaded Whisper model, or running Ollama instance is required.

- **`test_audio.py`** — `Segmenter` fed synthetic audio: numpy arrays of Gaussian noise
  generated with `np.random.default_rng` at chosen amplitudes (quiet frames vs. "speech"
  frames), used to test noise-floor calibration, utterance splitting on pauses, short-blip
  rejection, max-length forcing a split, and `flush()`.
- **`test_vad.py`** — same synthetic-frame approach against `SileroDetector`.
- **`test_transcriber.py`** — `clean_text()` hallucination stripping, vocabulary building,
  uncertain-word grouping from fake token/probability data; no real whisper.cpp model
  loaded.
- **`test_llm.py`** — Ollama calls (`polish`, `check`, `extract_tags`) are tested by
  monkeypatching `llm.httpx.stream`/`llm.httpx.get` to route through
  `httpx.MockTransport(handler)`, so responses (including streamed NDJSON chunks and HTTP
  error codes) are scripted in-process with no network or real Ollama server. The `claude`
  and `copilot` CLI backends are tested via a helper (`_fake_claude`) that writes a small
  Python script as an executable named `claude`/`copilot` into a `tmp_path` directory and
  prepends it to `PATH` via `monkeypatch.setenv`; the fake script echoes back
  `stream-json`/text output and logs what it was called with, so backend argument-building,
  streaming/parsing, and error handling are verified without invoking the real CLIs.
- **`test_cli.py`** — Typer app commands (`main`, `polish`, `summarize`, `tag`, `reindex`,
  `models`) exercised with fakes/monkeypatches for recording, transcription and LLM calls;
  largest file (429 lines), covers file naming, front matter, tagging, importing.
- **`test_writer.py`** — `SessionWriter` incremental append/finalize, `parse_document`,
  summary-block insertion/extraction, tag formatting/parsing — all against real temp files
  on disk (`tmp_path`), no external services.
- **`test_index.py`** — `index.md` generation from front matter across synthetic session
  files, `migrate_flat()` for legacy flat layouts.
- **`test_models.py`** — `models.REGISTRY` shape/consistency checks (no actual downloads).
- **`test_meetings.py`** — EventKit meeting lookup logic with fakes; no real Calendar access.

Style: plain `pytest` functions (no test classes), `monkeypatch` and `tmp_path` fixtures,
small local helper functions (`_mock`, `_fake_claude`, `frames`, `run`) rather than shared
fixture files — there is no `conftest.py`.

## Test runner

[pytest](https://docs.pytest.org/), configured in `pyproject.toml`:

```toml
[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Run with:

```bash
uv run pytest        # all tests
make test             # same, via Makefile
```

Not needed to run the suite: a microphone/audio device, a downloaded whisper.cpp/ggml
model, a running Ollama server, or the real `claude`/`copilot` CLIs — everything is faked
or synthesized in-process, which is why `pytest` runs in a few seconds and works in CI-less,
headless, or offline environments.

## Linting / static analysis

[Ruff](https://docs.ruff.rs/) does both linting and formatting; no separate formatter
(e.g. Black) is used. Config in `pyproject.toml`:

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM"]
```

Rule groups: `E` (pycodestyle errors), `F` (Pyflakes), `I` (isort import ordering), `B`
(flake8-bugbear), `UP` (pyupgrade), `SIM` (flake8-simplify).

Run with:

```bash
uv run ruff check .              # lint
uv run ruff format .             # auto-format
make lint                        # ruff check . && ruff format --check .
make format                      # ruff format . && ruff check --fix .
```

## Type checking

No type checker (mypy, pyright, pyrefly, etc.) is configured anywhere in the repo — not in
`pyproject.toml`, the `dev` dependency group, the Makefile, or as a pre-commit hook. The
codebase uses type hints throughout (per CLAUDE.md conventions, with
`from __future__ import annotations`), but nothing currently enforces them statically.

## CI/CD test automation

**No CI exists.** There is no `.github/workflows/` directory (or any other CI config —
no `.gitlab-ci.yml`, `.circleci/`, Travis, etc.) in this repository. `make check` (lint +
test) is the closest thing to a gate, and per CLAUDE.md's Commands section it's intended to
be run manually/locally before committing; nothing runs it automatically on push or PR.

## Manual / end-to-end testing notes

There's no automated e2e test harness (no audio hardware, model, or Ollama server in CI),
but CLAUDE.md's Gotchas section documents manual recipes for exercising the real pipeline:

- **Generate synthetic speech and feed it through the real pipeline**:
  ```bash
  say -o x.aiff "…"
  sox x.aiff -r 16000 -c 1 -b 16 x.wav
  ```
  then feed the resulting WAV's frames to `Recorder.process` through a fake mic queue —
  this exercises the real `Segmenter`/`Transcriber`/`SessionWriter` path without a live
  microphone or a human talking.
- **Quick manual smoke run** with a small model and no LLM pass, to sanity-check recording
  and transcription without waiting on Ollama:
  ```bash
  uv run mic2md --no-llm -m base.en
  ```
  (also available as `make run-quick`).
- **Ctrl+C handling**: when testing interrupt behavior from a non-interactive shell,
  background processes inherit `SIGINT` as ignored, so restore default handling first, e.g.:
  ```bash
  perl -e '$SIG{INT}="DEFAULT"; exec @ARGV' mic2md …
  ```
- Full manual runs (`uv run mic2md`, `make run`) exercise the real mic, whisper.cpp model,
  and Ollama end-to-end but are inherently interactive/non-reproducible, which is why they
  aren't part of the automated suite.
