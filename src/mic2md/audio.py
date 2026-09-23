"""Microphone capture and energy-based voice activity segmentation."""

from __future__ import annotations

import queue
from collections import deque

import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))) if frame.size else 0.0


class Segmenter:
    """Splits a stream of audio frames into utterances separated by silence.

    The speech threshold is calibrated from the first ``calibration_ms`` of audio
    (assumed to be background noise) and then tracks the noise floor slowly.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = FRAME_MS,
        silence_ms: int = 700,
        max_utterance_s: float = 25.0,
        min_speech_ms: int = 250,
        preroll_ms: int = 300,
        calibration_ms: int = 500,
        threshold_mult: float = 3.0,
        min_threshold: float = 0.004,
        threshold: float | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_frames = max(1, silence_ms // frame_ms)
        self.max_samples = int(max_utterance_s * sample_rate)
        self.min_speech_frames = max(1, min_speech_ms // frame_ms)
        self.threshold_mult = threshold_mult
        self.min_threshold = min_threshold
        self.fixed_threshold = threshold

        self._calib_frames = max(1, calibration_ms // frame_ms)
        self._calib: list[float] = []
        self._noise: float | None = None
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_ms // frame_ms))
        self._buf: list[np.ndarray] = []
        self._buf_samples = 0
        self._speech_frames = 0
        self._silent_run = 0
        self.level = 0.0

    @property
    def threshold(self) -> float:
        if self.fixed_threshold is not None:
            return self.fixed_threshold
        if self._noise is None:
            return float("inf")
        return max(self.min_threshold, self._noise * self.threshold_mult)

    @property
    def calibrated(self) -> bool:
        return self.fixed_threshold is not None or self._noise is not None

    @property
    def in_speech(self) -> bool:
        return bool(self._buf)

    def current(self) -> np.ndarray | None:
        """The in-progress utterance, or None when not in speech."""
        return np.concatenate(self._buf) if self._buf else None

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        """Consume one frame; return a finished utterance when one ends."""
        level = rms(frame)
        self.level = level

        if not self.calibrated:
            self._calib.append(level)
            self._preroll.append(frame)
            if len(self._calib) >= self._calib_frames:
                self._noise = float(np.median(self._calib))
            return None

        loud = level >= self.threshold

        if not self._buf:
            if loud:
                self._buf = [*self._preroll, frame]
                self._buf_samples = sum(f.size for f in self._buf)
                self._speech_frames = 1
                self._silent_run = 0
                self._preroll.clear()
            else:
                self._preroll.append(frame)
                if self.fixed_threshold is None and self._noise is not None:
                    self._noise = 0.98 * self._noise + 0.02 * level
            return None

        self._buf.append(frame)
        self._buf_samples += frame.size
        if loud:
            self._speech_frames += 1
            self._silent_run = 0
        else:
            self._silent_run += 1

        if self._silent_run >= self.silence_frames or self._buf_samples >= self.max_samples:
            return self._finish()
        return None

    def flush(self) -> np.ndarray | None:
        """Return whatever is buffered (used when recording stops)."""
        return self._finish() if self._buf else None

    def _finish(self) -> np.ndarray | None:
        audio = np.concatenate(self._buf)
        enough_speech = self._speech_frames >= self.min_speech_frames
        self._buf = []
        self._buf_samples = 0
        self._speech_frames = 0
        self._silent_run = 0
        return audio if enough_speech else None


class MicStream:
    """Context manager that pushes fixed-size float32 mono frames into a queue."""

    def __init__(self, device: int | str | None = None, sample_rate: int = SAMPLE_RATE) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.frames: queue.Queue[np.ndarray] = queue.Queue()
        self.overflows = 0
        self._stream = None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status and status.input_overflow:
            self.overflows += 1
        self.frames.put(indata[:, 0].copy())

    def __enter__(self) -> MicStream:
        import sounddevice as sd

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.sample_rate * FRAME_MS // 1000,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def list_input_devices() -> list[tuple[int, str, bool]]:
    """Return (index, name, is_default) for every device with input channels."""
    import sounddevice as sd

    default_in = sd.default.device[0]
    return [
        (i, d["name"], i == default_in)
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]
