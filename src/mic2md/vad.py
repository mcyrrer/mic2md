"""Silero voice activity detection (via pysilero-vad) as a speech/no-speech frame classifier."""

from __future__ import annotations

import numpy as np

# Silero's recommended hysteresis: speech starts at THRESHOLD and continues until the
# probability drops below THRESHOLD - HYSTERESIS.
THRESHOLD = 0.5
HYSTERESIS = 0.15


class SileroDetector:
    """Callable ``detector(frame) -> bool`` for :class:`mic2md.audio.Segmenter`.

    Frames of any length are buffered into the 512-sample chunks Silero needs; the model
    keeps its state between calls, so it must see the audio in order and only once.
    """

    def __init__(self, threshold: float = THRESHOLD, vad=None) -> None:
        if vad is None:
            from pysilero_vad import SileroVoiceActivityDetector

            vad = SileroVoiceActivityDetector()
        self._vad = vad
        self._chunk = vad.chunk_samples()
        self._pending = np.zeros(0, np.float32)
        self.threshold = threshold
        self.probability = 0.0
        self.speaking = False

    def __call__(self, frame: np.ndarray) -> bool:
        self._pending = np.concatenate([self._pending, frame.astype(np.float32, copy=False)])
        while self._pending.size >= self._chunk:
            chunk, self._pending = self._pending[: self._chunk], self._pending[self._chunk :]
            pcm = (np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2").tobytes()
            self.probability = float(self._vad(pcm))
        cutoff = self.threshold - HYSTERESIS if self.speaking else self.threshold
        self.speaking = self.probability >= cutoff
        return self.speaking
