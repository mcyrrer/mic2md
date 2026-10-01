# Deployment

`mic2md` is a single-user, local-first CLI with no server component. There is no
traditional multi-tier deployment: the only "deployment" that matters is getting the
right build of the tool onto the machine that will run it, and pointing it at the
right local dependencies (a Whisper ggml model, optionally Ollama). This doc maps the
conventional dev/test/stage/prod split onto what actually exists here.

## CI/CD

**None.** There is no `.github/workflows/` directory or any other CI/CD configuration
in this repository. `make check` (`ruff check`, `ruff format --check`, `pytest`) is run
manually/locally before committing; there is no automated pipeline that runs it on
push or PR, and no automated release/publish job.

## Deployable units

There is effectively **one** deployable unit, distributed two ways:

1. **`mic2md` Python package / CLI** — the `src/mic2md` package, built as a wheel/sdist
   via `hatchling` (`pyproject.toml`) and exposing the `mic2md` console-script entry
   point (`mic2md = "mic2md.cli:app"`). This is the thing that gets "deployed": either
   run directly from source (`uv run`) or installed as a standalone global command
   (`uv tool install`).
2. **`lauche.sh`** — a legacy standalone shell script (record-then-transcribe
   predecessor), kept only for reference. It is not packaged, versioned, or installed;
   it has no build/deploy story of its own and isn't touched by `uv build`/`uv tool
   install`. Not covered further below.

Whisper ggml models and the Ollama LLM are external runtime dependencies fetched/used
at run time (`models.py`'s `REGISTRY` + on-demand `httpx` download to a local cache,
and a separately-installed `ollama` daemon) — they are not built or versioned by this
repo, so they aren't separate deployable units, just prerequisites to document per
environment below.

---

## Unit: `mic2md` CLI package

### Build

```bash
uv sync                 # resolve + create .venv with runtime + dev deps
uv build                # produce dist/*.whl and dist/*.tar.gz (make build)
```

Build backend is `hatchling`; the wheel packages `src/mic2md`. Versioning is a single
`version = "0.1.0"` field in `pyproject.toml` — bumped manually, no changelog
automation or tag-triggered release process exists.

Style/correctness gate before any build/release (manual, not enforced by CI):

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
# or: make check
```

### Deploy / install per environment

There's no server to promote a build through; "environments" here map to *how the
tool is being run* on a given machine.

- **Dev** (working on mic2md itself): run straight from the working tree, no install.
  ```bash
  uv sync
  uv run mic2md                     # full run
  uv run mic2md --no-llm -m base.en # fast iteration: small model, skip Ollama pass
  ```
  Edits to `src/mic2md/**` take effect immediately on the next `uv run`. This is also
  the mode CI-equivalent checks (`make check`) run in.

- **Test** (running the unit test suite): same venv as dev, no mic/model/Ollama
  needed — tests are pure-logic with synthetic audio and `httpx.MockTransport`.
  ```bash
  uv run pytest        # or: make test
  ```
  There's no separate "test environment" beyond this — no staging server, no fixture
  environment to provision.

- **Stage** (closest analogue: trying a locally-built package before making it your
  daily driver, e.g. to verify a release candidate end-to-end with a real mic and a
  real model/Ollama call): build and install into an isolated tool environment without
  overwriting your normal install, or just install into the current one and verify
  before relying on it:
  ```bash
  uv build
  uv tool install --reinstall dist/mic2md-<version>-py3-none-any.whl
  mic2md --no-llm -m base.en   # smoke test with a small model
  mic2md                       # full smoke test with Ollama polish
  uv run pytest                 # confirm the source tree this was built from is green
  ```
  Since there's only one machine role (the user's own Mac), "stage" in practice is
  just "install the candidate build and manually exercise it before trusting it,"
  optionally followed by `make reindex` / `mic2md polish` on a scratch transcript to
  check the polish/index pipeline.

- **Prod** (day-to-day use): the global `mic2md` command installed via `uv tool`,
  used from any directory, independent of any repo checkout's `.venv`.
  ```bash
  uv tool install --reinstall .     # from a repo checkout (make install)
  # or, once published: uv tool install mic2md
  uv tool uninstall mic2md          # remove (make uninstall)
  ```
  This is what `make install`/`make uninstall` wrap. `uv tool install --reinstall .`
  is also how you roll out an update after pulling new commits — there is no
  auto-update mechanism.

### Runtime prerequisites (all environments)

- macOS Apple Silicon (Metal) is the primary target; the tool degrades but is only
  tested there.
- A Whisper ggml model, downloaded on first use or via `mic2md models` into the local
  cache dir (`models.py`); no model ships in the package/wheel itself.
- Ollama running locally (`ollama serve`, default backend) for the polish pass, or
  `--backend claude` / `--backend copilot` (external CLIs, invoked as subprocesses)
  as substitutes — these must be separately installed and authenticated by the user;
  mic2md does not install or manage them.
- `--no-llm` skips the LLM dependency entirely (raw transcript only), useful for
  dev/test smoke runs without Ollama installed.

### Versioning / release notes

- Single version field in `pyproject.toml` (`0.1.0` currently); bumped by hand.
- No git tags, GitHub Releases, or PyPI publish step currently exist in this repo —
  distribution is source-checkout + `uv tool install .`, not a published package
  index artifact.
- No changelog file; commit history (`git log`) is the de facto record of what changed
  between installs.
