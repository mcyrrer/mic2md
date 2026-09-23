import numpy as np

from mic2md.vad import SileroDetector


class FakeVad:
    """Returns the queued probabilities, one per 512-sample chunk."""

    def __init__(self, probs):
        self.probs = list(probs)
        self.chunks = []

    def chunk_samples(self):
        return 512

    def __call__(self, pcm: bytes) -> float:
        self.chunks.append(pcm)
        return self.probs.pop(0)


def test_buffers_frames_into_512_sample_int16_chunks():
    vad = FakeVad([0.9, 0.9])
    det = SileroDetector(vad=vad)
    assert det(np.full(480, 0.5, np.float32)) is False  # no full chunk yet
    assert det(np.full(480, 0.5, np.float32)) is True  # 960 samples: one chunk
    assert len(vad.chunks) == 1 and len(vad.chunks[0]) == 1024
    assert np.frombuffer(vad.chunks[0], "<i2")[0] == 16383


def test_hysteresis_keeps_speech_until_probability_drops_well_below():
    det = SileroDetector(vad=FakeVad([0.6, 0.4, 0.3, 0.45]))
    frame = np.zeros(512, np.float32)
    assert [det(frame) for _ in range(4)] == [True, True, False, False]


def test_real_silero_hears_no_speech_in_silence():
    det = SileroDetector()
    assert not any(det(np.zeros(480, np.float32)) for _ in range(50))
