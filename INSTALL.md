# Installing mic2md

This guide targets **macOS on Apple Silicon**, which is the main platform, with GPU
acceleration through Metal. Linux notes are at the end.

## 1. Prerequisites

| Tool | Why | Install |
|---|---|---|
| [Homebrew](https://brew.sh) | Package manager | see brew.sh |
| [uv](https://docs.astral.sh/uv/) | Installs Python and the tool in an isolated env | `brew install uv` |
| [Ollama](https://ollama.com) | Local LLM for spelling/grammar/Markdown | `brew install ollama` or the app from ollama.com |

You don't need to install Python yourself. uv downloads a compatible version (3.11–3.13)
automatically. Python 3.14 isn't supported yet because the whisper.cpp bindings don't ship
wheels for it.

You also don't need `ffmpeg`, `sox` or a separate whisper.cpp install. Audio is captured
with PortAudio, which ships inside the `sounddevice` wheel, and whisper.cpp is bundled in
`pywhispercpp`.

## 2. Set up Ollama

```bash
# Start the server (skip this if you use the Ollama menu-bar app)
brew services start ollama        # or: ollama serve

# Pull the default polishing model (~6.6 GB)
ollama pull qwen3.5:9b

# Check that it works
ollama run qwen3.5:9b "Say hi"
```

You can use any other chat model with `--llm` or `MIC2MD_LLM`, for example
`gemma4:latest`, which is also good at Swedish.

## 3. Install mic2md

From the project directory:

```bash
uv tool install .
```

This puts a `mic2md` command in `~/.local/bin`. If that directory isn't on your `PATH`,
run:

```bash
uv tool update-shell     # then open a new terminal
```

Check the install:

```bash
mic2md --version
mic2md --list-devices
```

**Updating** after pulling new code:

```bash
uv tool install --reinstall .
```

**Running without installing**, from a checkout:

```bash
uv sync
uv run mic2md
```

## 4. Allow microphone access (macOS)

The first time you record, macOS asks whether your terminal app (Terminal, iTerm2, Ghostty,
VS Code…) may use the microphone. Click **Allow**.

If you clicked "Don't Allow", or you never got the prompt and nothing is transcribed:

1. Open **System Settings → Privacy & Security → Microphone**.
2. Turn on the switch for your terminal app.
3. Quit and reopen the terminal.

## 5. First run: model download

The first run for each language downloads its Whisper model from Hugging Face:

| Command | Model | Size |
|---|---|---|
| `mic2md` | `ggml-large-v3-turbo-q5_0.bin` | ~550 MB |
| `mic2md --lang sv` | `KBLab/kb-whisper-large` `ggml-model-q5_0.bin` | ~1.1 GB |

Models are cached in `~/.cache/mic2md/models/`. You can change this with
`MIC2MD_CACHE_DIR`. Run `mic2md models` to see what's downloaded.

**Already have a ggml model?** Point to it instead of downloading:

```bash
mic2md --lang sv --model-path ~/models/ggml-kb-whisper-large-q5_0.bin
# or permanently:
export MIC2MD_MODEL_PATH=~/models/ggml-kb-whisper-large-q5_0.bin
```

## 6. Optional configuration

Add any of these to `~/.zshrc`:

```bash
export MIC2MD_LANG=sv                          # default language
export MIC2MD_OUTPUT_DIR=~/notes/dictation     # where .md files go
export MIC2MD_LLM=gemma4:latest                # polishing model
export MIC2MD_DEVICE="USB audio CODEC"         # preferred microphone
export OLLAMA_HOST=http://192.168.1.10:11434       # remote Ollama server
```

## Troubleshooting

**Nothing appears while I talk**
- Check microphone permission (step 4).
- Run `mic2md --list-devices` and pick the right mic with `-d ID`.
- Stay quiet for the first half second. That's when the noise level is calibrated. If you
  start talking right away, the threshold ends up too high.
- Watch the `mic` meter in the status bar. It turns green when your voice counts as speech.
  If it never turns green, set a lower fixed threshold, e.g. `--threshold 0.005`. If it stays
  green in a noisy room, set a higher one, e.g. `--threshold 0.03`.

**Sentences get cut in the middle**: raise `--silence-ms` to something like 1000.

**Transcription is slow or lags behind**: use a smaller model: `-m small.en`
(English) or `-m medium` (Swedish).

**`⚠ Ollama is not reachable`**: start it with `brew services start ollama` or
`ollama serve`. Your raw transcript is still saved, and you can polish it later with
`mic2md polish FILE.md`.

**`⚠ Ollama model 'qwen3.5:9b' is not installed`**: run `ollama pull qwen3.5:9b`, or pick an
installed model with `--llm` (see `ollama list`).

**`uv tool install` fails building `pywhispercpp`**: this usually means uv picked a Python
version without prebuilt wheels. Force a supported one:

```bash
uv tool install --python 3.13 .
```

**Model download interrupted**: just run again. Partial downloads (`*.part`) are
discarded and restarted.

**`PortAudioError` / `Error opening InputStream`**: another app may have exclusive access
to the device, or the device ID changed. Run `mic2md --list-devices` again.

## Linux

This should work but isn't the primary target:

```bash
sudo apt install libportaudio2          # PortAudio runtime for sounddevice
curl -fsSL https://ollama.com/install.sh | sh
uv tool install .
```

Transcription runs on the CPU unless you build `pywhispercpp` with CUDA yourself. On a CPU,
`small.en` or `medium` is recommended.

## Uninstall

```bash
uv tool uninstall mic2md
rm -rf ~/.cache/mic2md          # downloaded Whisper models
# Your transcripts in ~/Documents/mic2md are left untouched.
```
