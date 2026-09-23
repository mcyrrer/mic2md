import numpy as np

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
