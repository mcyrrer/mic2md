# voice2text

Real-time dictation in your terminal. You speak and the text shows up as you talk. When you
stop, a local LLM (via [Ollama](https://ollama.com)) fixes spelling and grammar and formats
everything as a clean Markdown document, saved to a file named after the session's start time.

Everything runs **locally**: [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
(GPU-accelerated with Metal on Apple Silicon) for speech recognition and Ollama for the editing.
No audio or text leaves your machine.

```
$ voice2text
Saving to ~/Documents/voice2text/2026-09-23T20-15-30.md
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
───────────────────────────────────────────────────────────────────────
✔ Saved ~/Documents/voice2text/2026-09-23T20-15-30.md
```

## Features

- **Live transcription.** A grey partial line updates about once a second while you speak.
  Each sentence is committed as soon as you pause.
- **Crash-safe saving.** Every committed sentence is appended to the session file right
  away, so a crash or closed terminal doesn't lose what you said.
- **ISO 8601 file names**, e.g. `2026-09-23T20-15-30.md`, so files sort chronologically.
  Colons are replaced with `-` because they aren't valid in filenames on all systems.
- **LLM polish at the end.** Fixes spelling, grammar and punctuation, removes filler words,
  adds a title, headings and lists, and keeps the original language. Once polishing succeeds, the file
  holds only the polished text.
- **English and Swedish.** English uses OpenAI Whisper `large-v3-turbo`. Swedish uses
  [KB-Whisper](https://huggingface.co/KBLab) from the National Library of Sweden.
- **Models download automatically** on first use.
- **Pipe-friendly.** The UI is written to stderr and the final document to stdout, so
  `voice2text | pbcopy` works.

## Quick start

See **[INSTALL.md](INSTALL.md)** for full setup. Short version (macOS):

```bash
brew install uv ollama
ollama serve &                 # or start the Ollama app
ollama pull qwen3.5:9b
uv tool install /path/to/voice2text
voice2text                     # English
voice2text --lang sv           # Swedish
```

Press **Ctrl+C** to stop recording. Press Ctrl+C again during polishing to skip it and keep
the raw transcript.

## Usage

```bash
voice2text                                  # record in English, polish with qwen3.5:9b
voice2text -l sv                            # record in Swedish (KB-Whisper large)
voice2text -m small.en                      # faster, smaller English model
voice2text --no-llm                         # raw transcript only, no Ollama
voice2text --llm gemma4:latest              # use a different Ollama model
voice2text -o ~/notes/dictation             # save somewhere else
voice2text --list-devices                   # show microphones
voice2text -d 3                             # record from device 3 (or -d "USB audio")
voice2text | pbcopy                         # also copy the final text to the clipboard
voice2text --model-path ~/models/my.bin     # use your own ggml model file

voice2text polish ~/Documents/voice2text/2026-09-23T20-15-30.md   # re-run the LLM pass
voice2text models                           # list models and which are downloaded
```

### Options

| Option | Env var | Default | Description |
|---|---|---|---|
| `-l, --lang` | `VOICE2TEXT_LANG` | `en` | Spoken language: `en` or `sv` |
| `-m, --model-size` | `VOICE2TEXT_MODEL_SIZE` | `large-v3-turbo` (en), `large` (sv) | Whisper model size, see below |
| `--model-path` | `VOICE2TEXT_MODEL_PATH` | | Use a local ggml model file instead of downloading one |
| `--llm` | `VOICE2TEXT_LLM` | `qwen3.5:9b` | Ollama model used for polishing |
| `--no-llm` | | off | Skip polishing and save the raw transcript |
| `--no-calendar` | `VOICE2TEXT_NO_CALENDAR` | off | Skip the meeting lookup (Calendar, then a prompt if none is found) |
| `--ollama-url` | `OLLAMA_HOST` | `http://localhost:11434` | Ollama server |
| `-o, --output-dir` | `VOICE2TEXT_OUTPUT_DIR` | `~/Documents/voice2text` | Where session files go |
| `-d, --device` | `VOICE2TEXT_DEVICE` | system default | Microphone ID or name |
| `--silence-ms` | | `700` | Pause length that ends a sentence |
| `--threshold` | | auto | Fixed speech RMS threshold (for noisy rooms) |
| `--list-devices` | | | List microphones and exit |

Put your preferred defaults in your shell profile, for example
`export VOICE2TEXT_LANG=sv`.

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

Models are cached in `~/.cache/voice2text/models/`. You can change this with
`VOICE2TEXT_CACHE_DIR`.

## Output file

```markdown
---
date: 2026-09-23T20:15:30+02:00
language: en
whisper_model: large-v3-turbo
duration: 00:00:21
llm_model: qwen3.5:9b
---

# New Release Update

Today, I want to talk about the new release. There are two main tasks:

- Fix the login bug.
- Update the documentation.
```

If Ollama isn't running, or you pass `--no-llm`, the file contains just the front matter and
the raw transcript. You can polish it later with `voice2text polish FILE`.

## How it works

```
mic ─▶ 30 ms frames ─▶ energy VAD ─┬─▶ in speech: transcribe buffer every ~1 s ─▶ grey partial line
                                   └─▶ pause ≥ 700 ms ─▶ final transcription ─▶ printed + appended to .md
Ctrl+C ─▶ flush last words ─▶ Ollama /api/chat (streamed) ─▶ polished .md (raw replaced)
```

1. **Capture.** `sounddevice` records 16 kHz mono audio in 30 ms frames.
2. **Segmentation.** The first half second calibrates the background noise level. Frames
   louder than about 3× that level count as speech, and a pause ends an utterance.
   Utterances longer than 25 s are split.
3. **Transcription.** whisper.cpp (through `pywhispercpp`) transcribes each utterance. The
   previous sentence is passed as context to keep terminology and punctuation consistent.
   Known Whisper hallucinations on silence, like "Thanks for watching", are filtered out.
4. **Polish.** The full transcript goes to Ollama once, with instructions to fix errors and
   structure the text without adding, removing or translating content.

## Limitations

- Partial text can change as more audio comes in. Only committed lines are saved.
- Very long sessions (well over an hour) might exceed the LLM context window
  (`num_ctx` 16k). The raw transcript is always saved regardless.
- The LLM can occasionally rephrase more than you want. The raw transcript isn't kept after a
  successful polish; use `--no-llm` if you want the verbatim text.

## Development

See [CLAUDE.md](CLAUDE.md) for architecture and conventions.

```bash
uv sync
uv run voice2text
uv run pytest
uv run ruff check . && uv run ruff format .
```
