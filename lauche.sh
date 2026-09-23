#!/usr/bin/env bash
# dictate — record until Enter, print transcription to stdout
MODEL="$HOME/models/ggml-kb-whisper-large-q5_0.bin"   # or point at VoiceInk's models folder
TMP=$(mktemp -t dictate).wav

sox -q -d -r 16000 -c 1 -b 16 "$TMP" & PID=$!
read -r -p "🎙  Recording… press Enter to stop" </dev/tty >&2
kill -INT $PID; wait $PID 2>/dev/null

TEXT=$(whisper-cli -m "$MODEL" -f "$TMP" -l sv -nt -np 2>/dev/null)
rm -f "$TMP"

if [[ "$1" == "--enhance" ]]; then
  ollama run qwen3.5-9b-64k:latest "Correct punctuation and remove filler words. Keep the original language. Output only the text:
$TEXT"
else
  echo "$TEXT"
fi
