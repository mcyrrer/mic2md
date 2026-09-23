"""Thin wrapper around whisper.cpp (via pywhispercpp)."""

from __future__ import annotations

import gc
import math
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

# Whisper's prompt holds about 224 tokens; leave room for the previous sentence.
MAX_VOCABULARY_CHARS = 600
PREVIOUS_CHARS = 200

# Whisper's encoder sees 30 s as 1500 frames. A partial only needs the frames for its audio
# plus a margin. Measured with large-v3-turbo on Metal: only multiples of 256 give sane text;
# 128, 192, 320, 384, 448 and 640 produce "", "of" or "It's It's It's…".
AUDIO_CTX_FULL = 1500
AUDIO_CTX_PER_S = AUDIO_CTX_FULL / 30
AUDIO_CTX_MARGIN = 64
AUDIO_CTX_STEP = 256

# Words whose least likely token is below this are marked as uncertain (measured: misheard
# names land around 0.3, ordinary words above 0.6).
UNCERTAIN_P = 0.4

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


def word_key(word: str) -> str:
    """Case- and punctuation-insensitive form used to match words to confidence scores."""
    return re.sub(r"\W", "", word.lower())


def uncertain_words(tokens: list[tuple[bytes, float]], threshold: float = UNCERTAIN_P) -> set[str]:
    """Word keys whose least likely token has probability below ``threshold``.

    ``tokens`` are (bytes, probability) of the text tokens in order. A token starting with a
    space starts a new word; bytes are joined before decoding, so characters split across
    tokens (å, ä, ö) come out right. Punctuation-only tokens don't count toward a word's score.
    """
    words: list[tuple[bytes, float]] = []
    for raw, p in tokens:
        if raw.startswith(b" ") or not words:
            words.append((raw, 1.0))
        else:
            words[-1] = (words[-1][0] + raw, words[-1][1])
        if re.search(rb"\w", raw):
            words[-1] = (words[-1][0], min(words[-1][1], p))
    return {
        key
        for raw, p in words
        if p < threshold and (key := word_key(raw.decode("utf-8", errors="replace")))
    }


_WORD = re.compile(r"[\w'’-]+")


def mark_uncertain(text: str, uncertain: set[str]) -> str:
    """Append ``(?)`` to every word of ``text`` that is in ``uncertain``."""
    if not uncertain:
        return text
    return _WORD.sub(lambda m: m[0] + "(?)" if word_key(m[0]) in uncertain else m[0], text)


def read_glossary(path: Path) -> list[str]:
    """One term per line; blank lines and ``#`` comments are skipped, duplicates dropped."""
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = " ".join(line.split())
        if term and not term.startswith("#") and term not in terms:
            terms.append(term)
    return terms


def build_vocabulary(meeting: str = "", participants: str = "", terms: list[str] = ()) -> str:
    """Prompt text that primes Whisper with the spelling of names and terms.

    Items are added in order (meeting, participants, glossary) until the length cap is hit.
    """
    parts = []
    if meeting:
        parts.append(f"Meeting: {meeting}.")
    names = [n.strip() for n in participants.split(",") if n.strip()]
    for label, items in (("Participants", names), ("Terms", list(terms))):
        if items:
            parts.append(f"{label}: " + ", ".join(items) + ".")
    text = ""
    for part in parts:
        candidate = f"{text} {part}".strip()
        if len(candidate) > MAX_VOCABULARY_CHARS:
            # Keep as many whole items of this part as fit.
            label, _, rest = part.partition(": ")
            kept = []
            for item in rest.rstrip(".").split(", "):
                trial = f"{text} {label}: {', '.join([*kept, item])}.".strip()
                if len(trial) > MAX_VOCABULARY_CHARS:
                    break
                kept.append(item)
            if kept:
                text = f"{text} {label}: {', '.join(kept)}.".strip()
            break
        text = candidate
    return text


def audio_ctx_for(seconds: float) -> int:
    """Encoder context just big enough for ``seconds`` of audio (0 = whisper's full 30 s)."""
    needed = math.ceil(seconds * AUDIO_CTX_PER_S) + AUDIO_CTX_MARGIN
    ctx = math.ceil(needed / AUDIO_CTX_STEP) * AUDIO_CTX_STEP
    return 0 if ctx >= AUDIO_CTX_FULL else ctx


DEFAULT_BEAM_SIZE = 5


def decode_params(seconds: float, partial: bool, beam_size: int = DEFAULT_BEAM_SIZE) -> dict:
    """Fast settings for live partials, careful ones for the committed transcript.

    ``strategy`` is "greedy" or "beam"; :class:`Transcriber` maps it to whisper.cpp's enum.
    """
    if partial:
        return {
            "strategy": "greedy",
            "greedy": {"best_of": 1},
            "audio_ctx": audio_ctx_for(seconds),
            "temperature_inc": 0.0,  # no fallback re-decodes for a throwaway preview
        }
    params = {"audio_ctx": 0, "temperature_inc": 0.2}
    if beam_size > 1:
        return {
            **params,
            "strategy": "beam",
            "beam_search": {"beam_size": beam_size, "patience": -1.0},
        }
    return {**params, "strategy": "greedy", "greedy": {"best_of": 5}}


class Transcriber:
    def __init__(
        self,
        model_path: Path,
        language: str,
        threads: int | None = None,
        beam_size: int = DEFAULT_BEAM_SIZE,
    ) -> None:
        import _pywhispercpp
        from pywhispercpp.model import Model

        self._strategies = {
            "greedy": _pywhispercpp.whisper_sampling_strategy.WHISPER_SAMPLING_GREEDY,
            "beam": _pywhispercpp.whisper_sampling_strategy.WHISPER_SAMPLING_BEAM_SEARCH,
        }
        self._pw = _pywhispercpp
        self.language = language
        self.beam_size = beam_size
        # Word keys (see word_key) Whisper was unsure of in the last non-partial transcription.
        self.last_uncertain: set[str] = set()
        # Names and terms (see build_vocabulary) put in front of every prompt.
        self.vocabulary = ""
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

    def build_prompt(self, previous: str | None) -> str:
        """Vocabulary first, then the tail of the previous sentence for continuity."""
        return " ".join(p for p in (self.vocabulary, (previous or "")[-PREVIOUS_CHARS:]) if p)

    def transcribe(
        self, audio: np.ndarray, prompt: str | None = None, partial: bool = False
    ) -> str:
        """Transcribe one utterance. ``partial`` trades accuracy for speed (live preview)."""
        # Every param is set on every call: pywhispercpp keeps them from the previous call.
        params = {
            "single_segment": partial or audio.size < 16_000 * 10,
            "initial_prompt": self.build_prompt(prompt),
            **decode_params(audio.size / 16_000, partial, self.beam_size),
        }
        params["strategy"] = self._strategies[params["strategy"]]
        segments = self._model.transcribe(audio.astype(np.float32, copy=False), **params)
        text = clean_text(" ".join(s.text for s in segments))
        if not partial:
            self.last_uncertain = uncertain_words(self._text_tokens()) if text else set()
        return text

    def _text_tokens(self) -> list[tuple[bytes, float]]:
        """(bytes, probability) of every text token of the last transcription."""
        pw, ctx = self._pw, self._model._ctx
        eot = pw.whisper_token_eot(ctx)  # special tokens ([_BEG_], timestamps…) have ids ≥ this
        tokens = []
        for i in range(pw.whisper_full_n_segments(ctx)):
            for j in range(pw.whisper_full_n_tokens(ctx, i)):
                if (tid := pw.whisper_full_get_token_id(ctx, i, j)) < eot:
                    tokens.append(
                        (
                            pw.whisper_token_to_bytes(ctx, tid),
                            pw.whisper_full_get_token_p(ctx, i, j),
                        )
                    )
        return tokens

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
