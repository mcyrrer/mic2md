# Session types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a session type (`meeting`, `thoughts`) that picks the storage folder, the summary prompt and whether the calendar is used.

**Architecture:** `writer.SessionType` is the single source of truth (folder name = front matter value). `session_path` adds the type folder; `llm` picks the summary prompt by type; `cli` threads `--type/-T` through recording and imports; `index` shows a Type column and `migrate_flat` moves old month folders under `meeting/`.

**Tech Stack:** Python 3.11+, Typer, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-02-session-types-design.md`

## Global Constraints

- Layout: `<output_dir>/transcripts/<type>/YYYY-MM/<name>.md`.
- Flag: `--type` / `-T`, env `MIC2MD_TYPE`, default `meeting`.
- Missing or unknown `type:` → `meeting`.
- Migration only moves files, never rewrites; skips existing targets.
- UI to stderr only; never lose transcript text; ruff line length 100.

## Review Focus

- Old files in `transcripts/YYYY-MM/` must still appear in the index before `reindex` runs.
- A file with `type: something-else` must not crash `summarize`, `index` or import.
- `reindex` run twice must be a no-op the second time.
- `summarize FILE` on a thoughts file must use the thoughts prompt.
- Importing with `--type thoughts` on a non-TTY must not prompt.

---

### Task 1: SessionType + typed paths (writer)

**Files:** Modify `src/mic2md/writer.py`; Test `tests/test_writer.py`

**Produces:** `SessionType(StrEnum)` {meeting, thoughts}; `DEFAULT_TYPE`; `session_type(meta) -> SessionType`;
`session_path(output_dir, started, title="", session_type=DEFAULT_TYPE)`;
`SessionWriter(..., session_type=DEFAULT_TYPE)` writes `type:`; `import_document` uses `session_type(meta)`.

- [ ] Tests: path per type, writer front matter has `type`, `session_type({})`/unknown → meeting, import of thoughts lands in `transcripts/thoughts/`.
- [ ] Implement, run `uv run pytest tests/test_writer.py`, commit.

### Task 2: Thoughts summary prompt (llm)

**Files:** Modify `src/mic2md/llm.py`; Test `tests/test_llm.py`

**Produces:** `THOUGHTS_SUMMARY_PROMPT`; `build_summary_messages(..., session_type="meeting")`; `summarize(..., session_type="meeting")`. Neutral wording in `TAGS_PROMPT`, `notes_block`.

- [ ] Tests: thoughts → thoughts prompt; default → meeting prompt; notes block has no "meeting".
- [ ] Implement, run tests, commit.

### Task 3: Index Type column + migration (index)

**Files:** Modify `src/mic2md/index.py`; Test `tests/test_index.py`

**Produces:** `Entry.session_type`; table `| Date | Time | Type | Title | Tags |`; `migrate_flat` moves root and `transcripts/YYYY-MM/` files to `transcripts/meeting/YYYY-MM/`, removes empty month dir.

- [ ] Tests: Type column, missing type → meeting, migration of both legacy layouts, skip existing target, second run no-op.
- [ ] Implement, run tests, commit.

### Task 4: CLI `--type` for recording, imports and summarize (cli, ui)

**Files:** Modify `src/mic2md/cli.py`, `src/mic2md/ui.py`; Test `tests/test_cli.py`

**Consumes:** Tasks 1–3.

- [ ] Tests: thoughts recording skips `_meeting_meta`; import with `--type thoughts` lands in thoughts folder without meeting prompts; `summarize FILE` on thoughts file calls `llm.summarize` with `session_type="thoughts"`.
- [ ] Implement `RecordOptions.session_type`, `--type/-T` on `main`, `polish`, `summarize`; `_import_external(..., session_type)`; `LiveView(session_type=)` label.
- [ ] Run full `uv run pytest`, `uv run ruff check . && uv run ruff format --check .`, commit.

### Task 5: Docs + PR

- [ ] Update `CLAUDE.md`, `README.md`, `docs/data.md`. Commit, push `session-types`, open PR.
