"""Thin wrapper around whisper.cpp (via pywhispercpp)."""

from __future__ import annotations

import gc
import os
import re
from pathlib import Path

import numpy as np

# Phrases Whisper tends to hallucinate on silence or noise. Compared case-insensitively
# after stripping punctuation.
HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subtitles by the amaraorg community",
    "you",
    "tack för att ni tittade",
    "tack för att du tittade",
    "tack för tittandet",
    "textning stina hedin",
    "undertexter från amaraorg gemenskapen",
    "svensktextning",
}

_ANNOTATION = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")
_PUNCT = re.compile(r"[^\w\s]")


def clean_text(text: str) -> str:
    """Drop Whisper sound annotations like [BLANK_AUDIO] and known hallucinations."""
    text = _ANNOTATION.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    key = _PUNCT.sub("", text).lower().strip()
    if not key or key in HALLUCINATIONS:
        return ""
    return text


class Transcriber:
    def __init__(self, model_path: Path, language: str, threads: int | None = None) -> None:
        from pywhispercpp.model import Model

        self.language = language
        self._model = Model(
            str(model_path),
            redirect_whispercpp_logs_to=None,
            n_threads=threads or max(1, min(8, (os.cpu_count() or 4) - 1)),
            language=language,
            print_progress=False,
            print_realtime=False,
            print_timestamps=False,
            no_context=True,
            suppress_blank=True,
        )

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> str:
        params = {"single_segment": audio.size < 16_000 * 10}
        if prompt:
            params["initial_prompt"] = prompt[-200:]
        segments = self._model.transcribe(audio.astype(np.float32, copy=False), **params)
        return clean_text(" ".join(s.text for s in segments))

    def close(self) -> None:
        """Free the model with stderr muted; whisper.cpp logs noisily on Metal teardown."""
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 2)
            del self._model
            gc.collect()
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(devnull)
