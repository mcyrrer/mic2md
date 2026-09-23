# mic2md

Real-time dictation in your terminal. You speak and the text shows up as you talk. When you
stop, a local LLM (via [Ollama](https://ollama.com)) fixes spelling and grammar and formats
everything as a clean Markdown document, saved to a file named after the session's start time.

By default everything runs **locally**: [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
(GPU-accelerated with Metal on Apple Silicon) for speech recognition and Ollama for the editing.
No audio or text leaves your machine. If you choose `--backend claude` or `--backend copilot`
instead, the transcript text (never the audio) is sent to that cloud service.

```
$ mic2md
Meeting: Release planning
Saving to ~/Documents/mic2md/transcripts/2026-09/2026-09-23T20-15-30.md
───────────────────────────── ● Listening ─────────────────────────────
Um so today I want to talk about the the new release.
First we need to fix the login bug. Second, ah, we should update the documentation.
so the next thing is…                                     ← live partial, updates while you talk
 ● REC  00:00:21  en · large-v3-turbo  mic ▅▃▁     Ctrl+C to stop
^C
──────────────────────── Polishing with qwen3.5:9b ────────────────────
# New Release Update

Today, I want to talk about the new release. There are two main tasks:

- Fix the login bug.
- Update the documentation.
Done in 9s (142 characters).
Tags: release, login-bug, documentation
Index updated: ~/Documents/mic2md/index.md
───────────────────────────────────────────────────────────────────────
✔ Saved ~/Documents/mic2md/transcripts/2026-09/2026-09-23T20-15-30.md
```

## Features

- **Live transcription.** A grey partial line updates about once a second while you speak.
  Each sentence is committed as soon as you pause.
- **Crash-safe saving.** Every committed sentence is appended to the session file right
  away, so a crash or closed terminal doesn't lose what you said.
- **Meeting detection.** When recording starts, mic2md checks the macOS Calendar for a
  meeting that is in progress or starts within 5 minutes, and saves its title and attendees
  as `meeting:` and `participants:` in the front matter. If there is no meeting, you're asked
  for a name and participants (only in an interactive terminal; press Enter to skip). The first
  run asks for Calendar access. Turn this off with `--no-calendar`.
- **LLM polish at the end.** Fixes spelling, grammar and punctuation, removes filler words,
  adds a title, headings and lists, and keeps the original language. Once polishing succeeds,
  the file holds only the polished text. Runs on local Ollama by default, or on Claude
  (`claude -p`) or GitHub Copilot CLI (`copilot -p`) with `--backend`.
- **Meeting notes on demand.** `mic2md summarize` records a meeting, polishes it and then
  adds meeting notes in one go; `mic2md summarize FILE` does the same for an existing file.
  The LLM is asked for an executive summary, a detailed summary, key decisions, an
  action-item table (owner, deadline), open questions and risks, which go at the top of the
  file. The meeting name and participants are passed along to help assign owners. Running it
  again replaces the old summary, and `polish` keeps it.
- **Import from elsewhere.** `polish` and `summarize` also take a file from outside the
  output folder (e.g. notes or a transcript from another tool). It is imported first: you're
  asked for the date, language, meeting name and participants, it's copied into
  `transcripts/YYYY-MM/` with that front matter plus `source:` (the original path), and the
  original is left untouched. The LLM then works on the copy, which shows up in `index.md`.
- **Topic tags.** After polishing (and in `polish` / `summarize`) the LLM adds up to 8 topic
  tags to the front matter, e.g. `tags: [q4-budget, hiring]`. It is shown the tags your other
  notes already use and reuses them when they fit, so related meetings share tags. They also
  work as tags in Obsidian. `mic2md tag --all` tags older notes.
- **Organized by month with an index.** Sessions go into `transcripts/YYYY-MM/` inside the
  output folder. `index.md` in the output folder has one table per month (date, time,
  meeting, tags), newest first, and a Tags section listing every tag with links to its notes.
  It's rebuilt from the files' front matter after each recording, `polish`, `summarize` and
  `tag`, so don't edit it by hand. Run `mic2md reindex` to rebuild it yourself (this also moves
  sessions saved by older versions into the month folders).
- **ISO 8601 file names**, e.g. `2026-09-23T20-15-30.md`, so files sort chronologically.
  Colons are replaced with `-` because they aren't valid in filenames on all systems.
- **English and Swedish.** English uses OpenAI Whisper `large-v3-turbo`. Swedish uses
  [KB-Whisper](https://huggingface.co/KBLab) from the National Library of Sweden.
- **Models download automatically** on first use.
- **Pipe-friendly.** The UI is written to stderr and the final document to stdout, so
  `mic2md | pbcopy` works.

## Quick start

See **[INSTALL.md](INSTALL.md)** for full setup. Short version (macOS):

```bash
brew install uv ollama
ollama serve &                 # or start the Ollama app
ollama pull qwen3.5:9b
uv tool install /path/to/mic2md
mic2md                         # English
mic2md --lang sv               # Swedish
```

Press **Ctrl+C** to stop recording. Press Ctrl+C again during polishing to skip it and keep
the raw transcript.

## Usage

```bash
mic2md                                  # record in English, polish with qwen3.5:9b
mic2md -l sv                            # record in Swedish (KB-Whisper large)
mic2md -m small.en                      # faster, smaller English model
mic2md --no-llm                         # raw transcript only, no LLM pass
mic2md --no-calendar                    # don't look up or ask for the meeting
mic2md --backend claude                 # polish with Claude (`claude -p`) instead of Ollama
mic2md summarize                        # record a meeting, polish it, then add meeting notes
mic2md -m small.en --no-calendar summarize   # recording options go before the command
mic2md summarize FILE -b claude --llm opus   # meeting notes from Claude Opus
mic2md summarize FILE -b copilot        # meeting notes via GitHub Copilot CLI
mic2md --llm gemma4:latest              # use a different Ollama model
mic2md -o ~/notes/dictation             # save somewhere else
mic2md --list-devices                   # show microphones
mic2md -d 3                             # record from device 3 (or -d "USB audio")
mic2md | pbcopy                         # also copy the final text to the clipboard
mic2md --model-path ~/models/my.bin     # use your own ggml model file
mic2md --version

mic2md polish ~/Documents/mic2md/transcripts/2026-09/2026-09-23T20-15-30.md   # re-run the LLM pass
mic2md summarize ~/Documents/mic2md/transcripts/2026-09/2026-09-23T20-15-30.md  # add meeting notes on top
mic2md summarize ~/Downloads/teams-transcript.md   # import a file from elsewhere, then summarize
mic2md tag FILE...                      # (re-)tag specific notes
mic2md tag --all                        # add tags to every note that has none
mic2md reindex                          # rebuild index.md
mic2md models                           # list models and which are downloaded
```

`--backend`, `--llm`, `--lang`, `--ollama-url` and `--output-dir` also work with the
commands, before or after the command name: `mic2md -b claude summarize FILE` and
`mic2md summarize FILE -b claude` do the same thing. Recording-only options (`-m`,
`--model-path`, `-d`, `--silence-ms`, `--threshold`, `--no-calendar`) go before the command,
e.g. `mic2md -d 3 summarize`.

### Commands

Without a command, `mic2md` records.

| Command | Description |
|---|---|
| `polish FILE` | Re-run the LLM polish on a session file (keeps an existing summary); imports files from outside the output folder first |
| `summarize [FILE]` | Add meeting notes to the top of a session file; imports files from outside the output folder first. Without FILE: record a new meeting, polish it, then summarize it |
| `tag [FILE...] [--all]` | Add topic tags. Named files are always re-tagged; `--all` picks every session without tags |
| `reindex` | Rebuild `index.md` and move sessions saved by older versions into `transcripts/YYYY-MM/` |
| `models` | List Whisper models and which are downloaded |

### Options

| Option | Env var | Default | Description |
|---|---|---|---|
| `-l, --lang` | `MIC2MD_LANG` | `en` | Spoken language: `en` or `sv` |
| `-m, --model-size` | `MIC2MD_MODEL_SIZE` | `large-v3-turbo` (en), `large` (sv) | Whisper model size, see below |
| `--model-path` | `MIC2MD_MODEL_PATH` | | Use a local ggml model file instead of downloading one |
| `-b, --backend` | `MIC2MD_BACKEND` | `ollama` | `ollama` (local), `claude` (runs `claude -p`) or `copilot` (runs GitHub Copilot CLI `copilot -p`). The last two send the text to the cloud |
| `--llm` | `MIC2MD_LLM` | `qwen3.5:9b` / `sonnet` / Copilot's default | Model for polishing, summaries and tags. With `claude`: `sonnet`, `opus`, `fable`, `haiku` or a full model ID. With `copilot`: e.g. `gpt-5.4` |
| `--no-llm` | | off | Skip the LLM pass (polish and tags) and save the raw transcript |
| `--no-calendar` | `MIC2MD_NO_CALENDAR` | off | Skip the meeting lookup (Calendar, then a prompt if none is found) |
| `--ollama-url` | `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `-o, --output-dir` | `MIC2MD_OUTPUT_DIR` | `~/Documents/mic2md` | Holds `index.md` and `transcripts/YYYY-MM/` |
| `-d, --device` | `MIC2MD_DEVICE` | system default | Microphone ID or name |
| `--silence-ms` | | `700` | Pause length that ends a sentence |
| `--threshold` | | auto | Fixed speech RMS threshold (for noisy rooms) |
| `--list-devices` | | | List microphones and exit |
| `--version` | | | Show the version and exit |

Put your preferred defaults in your shell profile, for example
`export MIC2MD_LANG=sv`.

### Models

| Language | Size | Download | Notes |
|---|---|---|---|
| en | `base.en` | ~60 MB | Very fast, lower accuracy |
| en | `small.en` | ~190 MB | Good balance on older machines |
| en | `medium.en` | ~540 MB | |
| en | `large-v3-turbo` *(default)* | ~550 MB | Best accuracy, fast with Metal |
| sv | `small` | ~190 MB | KB-Whisper |
| sv | `medium` | ~540 MB | KB-Whisper |
| sv | `large` *(default)* | ~1.1 GB | KB-Whisper, best Swedish accuracy |

Models are cached in `~/.cache/mic2md/models/` (or `$XDG_CACHE_HOME/mic2md/models/` if
`XDG_CACHE_HOME` is set). You can change this with `MIC2MD_CACHE_DIR`.

## Output file

```markdown
---
date: 2026-09-23T20:15:30+02:00
language: en
whisper_model: large-v3-turbo
meeting: Release planning
participants: Anna Berg, Erik Lund
tags: [release, login-bug, documentation]
duration: 00:00:21
llm_model: qwen3.5:9b
---

# New Release Update

Today, I want to talk about the new release. There are two main tasks:

- Fix the login bug.
- Update the documentation.
```

If the LLM isn't reachable, or you pass `--no-llm`, the file contains the front matter, a
`# Transcript YYYY-MM-DD HH:MM` heading and the raw transcript. You can polish it later with
`mic2md polish FILE`.

`mic2md summarize` adds `summary_model:` to the front matter and puts the meeting notes
between `<!-- mic2md:summary -->` and `<!-- /mic2md:summary -->` right after it, followed by
a `---` line and the transcript. Imported files also get `source:` with the original path.

## How it works

```
Calendar ─▶ meeting + participants (front matter)
mic ─▶ 30 ms frames ─▶ energy VAD ─┬─▶ in speech: transcribe buffer every ~1 s ─▶ grey partial line
                                   └─▶ pause ≥ 700 ms ─▶ final transcription ─▶ printed + appended to .md
Ctrl+C ─▶ flush last words ─▶ LLM polish (streamed) ─▶ LLM tags ─▶ polished .md (raw replaced) ─▶ index.md
```

1. **Capture.** `sounddevice` records 16 kHz mono audio in 30 ms frames.
2. **Segmentation.** The first half second calibrates the background noise level. Frames
   louder than about 3× that level count as speech, and a pause ends an utterance.
   Utterances longer than 25 s are split.
3. **Transcription.** whisper.cpp (through `pywhispercpp`) transcribes each utterance. The
   previous sentence is passed as context to keep terminology and punctuation consistent.
   Known Whisper hallucinations on silence, like "Thanks for watching", are filtered out.
4. **Polish.** The full transcript goes to the LLM once (Ollama `/api/chat`, `claude -p` or
   `copilot -p`), with instructions to fix errors and structure the text without adding,
   removing or translating content.
5. **Tags and index.** A second, quiet LLM call picks up to 8 topic tags, reusing the tags
   already in your notes where they fit. Then `index.md` is regenerated from every session's
   front matter.

## Limitations

- Partial text can change as more audio comes in. Only committed lines are saved.
- Very long sessions (well over an hour) might exceed the Ollama context window
  (`num_ctx` 16k). The raw transcript is always saved regardless.
- The LLM can occasionally rephrase more than you want. The raw transcript isn't kept after a
  successful polish; use `--no-llm` if you want the verbatim text.
- The calendar lookup only works on macOS. Elsewhere it's skipped with a warning and you're
  asked for the meeting instead.

## Development

Source: <https://github.com/mcyrrer/McVoice2Text>. See [CLAUDE.md](CLAUDE.md) for architecture
and conventions. Run `make` to list shortcuts (`make check`, `make install`, …).

```bash
uv sync
uv run mic2md
uv run pytest
uv run ruff check . && uv run ruff format .
```
