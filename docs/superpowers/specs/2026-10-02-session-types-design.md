# Session types (meeting, thoughts) — design

Date: 2026-10-02

## Goal

Recordings get a **type**. Today every recording is a meeting. A second type, **thoughts**
(general thoughts, a solo brain-dump), is added. Each type is stored in its own subfolder of
the output dir and gets an LLM summary suited to it. Adding a further type later should be a
small, local change.

## Decisions (agreed)

| Topic | Decision |
|---|---|
| Behaviour | Thoughts get their own summary prompt and skip the calendar lookup. Polish is the same. |
| Layout | `<output_dir>/transcripts/<type>/YYYY-MM/<name>.md` |
| Selection | `--type` / `-T` (env `MIC2MD_TYPE`), default `meeting`. `-t` is taken by `--transcript`. |
| Existing files | `mic2md reindex` moves them under `transcripts/meeting/`. |

## Model

- `writer.SessionType(StrEnum)`: `meeting`, `thoughts`. `DEFAULT_TYPE = SessionType.meeting`.
  The enum value is both the folder name and the front matter value.
- `writer.session_type(meta) -> SessionType`: reads `type:` from front matter; missing or
  unknown → `meeting` (files from before this change are meetings).
- Front matter of every new session gets `type: <value>` (right after `whisper_model`, before
  meeting fields).

## Storage

- `session_path(output_dir, started, title="", session_type=DEFAULT_TYPE)` →
  `output_dir / "transcripts" / <type> / YYYY-MM / session_filename(started, title)`.
- `SessionWriter(..., session_type=)` writes `type:` and uses the typed path.
- `import_document` derives the folder from `meta["type"]` via `session_type(meta)`.
- File names are unchanged (`index._SESSION_NAME` unchanged).

## Recording (cli)

- `RecordOptions.session_type: SessionType = meeting`; `main` gets
  `--type/-T` (envvar `MIC2MD_TYPE`).
- `_record`: for `thoughts`, no `_meeting_meta()` (no calendar, no "Meeting name" /
  participants prompts), so the file name is the bare timestamp. The glossary still primes
  Whisper. `meeting` behaves exactly as today (`--no-calendar` still applies to it).
- `LiveView` gets the type; when there is no meeting title and the type is `thoughts`, the
  header and sidebar show "Thoughts" in place of the meeting title.

## LLM

- New `THOUGHTS_SUMMARY_PROMPT`, sections with `## ` headings in the session language:
  1. Summary (3–5 sentences)
  2. Key ideas (bullets)
  3. Open questions
  4. Next steps (verb-first bullets; no owner/deadline table)
  5. Connections / themes (omitted if none)
  Same rules as the meeting prompt where they apply: only the transcript's content, times
  where useful, `[unclear]`, `<notes>` as reliable context, output only these sections.
- `build_summary_messages(..., session_type=)` / `summarize(..., session_type=)` pick the
  prompt; default `meeting` keeps current behaviour.
- Neutral wording so both types fit: `TAGS_PROMPT` says "notes" instead of "meeting notes";
  `notes_block` says "while recording" instead of "during the meeting".
- `cli._summarize` passes `session_type(meta)` from the file.

## Imports

- `polish` and `summarize` get `--type/-T` (no default value: `None` = not given).
- `_import_external(file, output_dir, lang, session_type)`: default type = `--type`, else the
  file's `type:`, else `meeting`. On a TTY it asks "Type (meeting/thoughts)". For thoughts it
  does not ask for meeting name and participants (keeps any already in the file's front
  matter). Writes `type:` to the front matter.
- `summarize` without FILE uses `--type` given to `summarize` when present, else the
  top-level `RecordOptions.session_type`.

## Index

- `Entry.session_type` from front matter (`session_type(meta)`).
- Month tables: `| Date | Time | Type | Title | Tags |`; Title is the `meeting` field.
- `## Tags` section unchanged (shared across types).
- `collect` still scans `transcripts/**/*.md`.

## Migration

`index.migrate_flat(output_dir)` (run by `mic2md reindex`) moves:

- session files directly in `output_dir` → `transcripts/meeting/YYYY-MM/` (was
  `transcripts/YYYY-MM/`);
- session files in `transcripts/YYYY-MM/` (a month folder directly under `transcripts/`) →
  `transcripts/meeting/YYYY-MM/`, then removes the month folder if it is left empty.

Files are moved, never rewritten; a file whose target exists is skipped. Nothing migrates
automatically on recording; until `reindex`, old files stay put and still appear in the
index (as meetings).

## Error handling

Unchanged invariants: raw lines are appended immediately; a failed polish/summary keeps the
raw transcript; calendar errors never block. An unknown `type:` value in a file is treated
as `meeting` rather than an error.

## Tests

- `session_path` / `SessionWriter` per type, `type:` in front matter; `session_type()` default.
- `import_document` puts a thoughts file under `transcripts/thoughts/`.
- `build_summary_messages` picks the thoughts prompt for `thoughts`, meeting prompt by default.
- CLI: `--type thoughts` skips `_meeting_meta` (monkeypatched to fail if called); imports
  with `--type thoughts` land in the thoughts folder and don't ask meeting questions.
- Index: Type column; files without `type:` show as meeting.
- `migrate_flat`: root files and `transcripts/YYYY-MM/` files move to
  `transcripts/meeting/YYYY-MM/`; existing targets are skipped; empty month dir removed.

## Docs

`CLAUDE.md` (output layout, new "Session types" convention, how to add a type),
`README.md` (paths, `--type`), `docs/data.md` (paths, index columns, migration).

## Out of scope

Per-type polish prompts, per-type index files, auto-detecting the type, titles for
thoughts.
