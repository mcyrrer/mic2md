import numpy as np
import pytest

from mic2md.audio import FRAME_SAMPLES, Segmenter


def frames(amplitude: float, n: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    for _ in range(n):
        yield (rng.standard_normal(FRAME_SAMPLES) * amplitude).astype(np.float32)


def run(seg, *chunks):
    out = []
    for chunk in chunks:
        for f in chunk:
            if (u := seg.feed(f)) is not None:
                out.append(u)
    return out


def test_calibrates_then_detects_utterance():
    seg = Segmenter(silence_ms=300)
    utterances = run(seg, frames(0.001, 20), frames(0.1, 30), frames(0.001, 20))
    assert seg.calibrated
    assert len(utterances) == 1
    # speech frames plus preroll and trailing silence
    assert utterances[0].size >= 30 * FRAME_SAMPLES


def test_two_utterances_split_by_pause():
    seg = Segmenter(silence_ms=300)
    utts = run(
        seg,
        frames(0.001, 20),
        frames(0.1, 20),
        frames(0.001, 20),
        frames(0.1, 20),
        frames(0.001, 20),
    )
    assert len(utts) == 2


def test_short_blip_is_ignored():
    seg = Segmenter(silence_ms=300, min_speech_ms=250)
    assert run(seg, frames(0.001, 20), frames(0.1, 2), frames(0.001, 20)) == []


def test_max_length_forces_split():
    seg = Segmenter(silence_ms=300, max_utterance_s=1.0)
    utts = run(seg, frames(0.001, 20), frames(0.1, 70))
    assert len(utts) == 2


def test_flush_returns_in_progress_speech():
    seg = Segmenter(threshold=0.01)
    run(seg, frames(0.1, 20))
    assert seg.in_speech
    assert seg.current() is not None
    assert seg.flush() is not None
    assert not seg.in_speech


def test_detector_decides_speech_without_calibration():
    # No quiet calibration frames first: the Segmenter trusts the detector from frame one.
    seg = Segmenter(silence_ms=300, detector=lambda f: float(np.abs(f).mean()) > 0.05)
    assert seg.calibrated
    utterances = run(seg, frames(0.1, 20), frames(0.001, 20), frames(0.1, 20), frames(0.001, 20))
    assert len(utterances) == 2
    assert not seg.speaking


def test_detector_overrides_loudness():
    seg = Segmenter(silence_ms=300, detector=lambda f: False)
    assert run(seg, frames(0.001, 20), frames(0.5, 40), frames(0.001, 20)) == []


def test_utterance_start_times_include_preroll():
    seg = Segmenter(silence_ms=300, preroll_ms=300)
    run(seg, frames(0.001, 20))  # 0.6 s: calibration + quiet
    run(seg, frames(0.1, 20), frames(0.001, 15))  # speech starts at 0.6 s
    first = seg.last_start_s
    assert first == pytest.approx(0.6 - 0.3)  # 300 ms preroll before the first loud frame
    run(seg, frames(0.1, 20))  # starts at 0.6 + 1.05 = 1.65 s
    assert seg.flush() is not None
    # The first utterance ended after 10 of the 15 quiet frames, so only 5 (150 ms) are preroll.
    assert seg.last_start_s == pytest.approx(1.65 - 0.15)
