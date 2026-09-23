import numpy as np

from mic2md import transcriber
from mic2md.transcriber import build_vocabulary, clean_text, read_glossary


def test_clean_text():
    assert clean_text(" [BLANK_AUDIO] ") == ""
    assert clean_text("(music) Hello there.") == "Hello there."
    assert clean_text("Thank you.") == ""
    assert clean_text("Tack för att ni tittade!") == ""
    assert clean_text("Thank you for the help.") == "Thank you for the help."


def test_build_vocabulary_orders_and_caps():
    text = build_vocabulary("Q4 budget", "Ada, Bob ,", ["KBLab", "mic2md"])
    assert text == "Meeting: Q4 budget. Participants: Ada, Bob. Terms: KBLab, mic2md."
    assert build_vocabulary() == ""
    long = build_vocabulary("M", "", [f"term{i:03d}" for i in range(200)])
    assert len(long) <= transcriber.MAX_VOCABULARY_CHARS
    assert long.startswith("Meeting: M. Terms: term000, term001") and long.endswith(".")


def test_read_glossary_skips_comments_blanks_and_duplicates(tmp_path):
    path = tmp_path / "g.txt"
    path.write_text("# names\nKBLab\n\n  Anna   Berg \nKBLab\n")
    assert read_glossary(path) == ["KBLab", "Anna Berg"]


class _StubModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **params):
        self.calls.append(params)
        return []


def _stub_transcriber():
    t = transcriber.Transcriber.__new__(transcriber.Transcriber)
    t.language, t.vocabulary, t._model = "en", "", _StubModel()
    t.beam_size = transcriber.DEFAULT_BEAM_SIZE
    t._strategies = {"greedy": "GREEDY", "beam": "BEAM"}
    t.last_uncertain = set()
    t._text_tokens = lambda: []
    return t


def test_prompt_is_vocabulary_then_previous_tail_and_always_set():
    t = _stub_transcriber()
    t.vocabulary = "Participants: Ada."
    t.transcribe(np.zeros(1600, np.float32), prompt="x" * 300)
    assert t._model.calls[-1]["initial_prompt"] == "Participants: Ada. " + "x" * 200
    t.vocabulary = ""
    t.transcribe(np.zeros(1600, np.float32))
    assert t._model.calls[-1]["initial_prompt"] == ""  # reset, pywhispercpp keeps old params


def test_audio_ctx_rounds_up_to_multiples_of_256_and_falls_back_to_full():
    assert transcriber.audio_ctx_for(0.5) == 256
    assert transcriber.audio_ctx_for(3.8) == 256  # 190 + 64 margin
    assert transcriber.audio_ctx_for(4.0) == 512
    assert transcriber.audio_ctx_for(25) == 0  # would exceed 1500: use the full context


def test_partial_and_final_decodes_set_all_their_params():
    t = _stub_transcriber()
    t.transcribe(np.zeros(16_000 * 2, np.float32), partial=True)
    fast = t._model.calls[-1]
    assert fast["audio_ctx"] == 256 and fast["greedy"] == {"best_of": 1}
    assert fast["temperature_inc"] == 0.0 and fast["single_segment"] is True
    t.transcribe(np.zeros(16_000 * 2, np.float32))
    final = t._model.calls[-1]
    assert final["audio_ctx"] == 0 and final["temperature_inc"] == 0.2
    assert final["strategy"] == "BEAM" and final["beam_search"]["beam_size"] == 5
    assert fast["strategy"] == "GREEDY"
    t.beam_size = 1
    t.transcribe(np.zeros(16_000 * 2, np.float32))
    greedy = t._model.calls[-1]
    assert greedy["strategy"] == "GREEDY" and greedy["greedy"] == {"best_of": 5}


def test_uncertain_words_groups_tokens_and_ignores_punctuation():
    from mic2md.transcriber import mark_uncertain, uncertain_words

    tokens = [
        (b" and", 0.29),
        (b" Ellie", 0.79),
        (b" sign", 0.99),
        (b"-", 0.10),  # low-probability punctuation doesn't make "sign-off" uncertain
        (b"off", 1.0),
        (b" Ok", 0.28),
        (b"ta", 0.71),
        (b".", 0.2),
        (b" gr\xc3", 0.9),  # "grön" with the ö split across two tokens
        (b"\xb6n", 0.3),
    ]
    assert uncertain_words(tokens) == {"and", "okta", "grön"}
    text = "and Ellie said sign-off for Okta. Grön."
    assert mark_uncertain(text, {"and", "okta", "grön"}) == (
        "and(?) Ellie said sign-off for Okta(?). Grön(?)."
    )
    assert mark_uncertain(text, set()) == text


def test_only_final_transcriptions_update_uncertain_words():
    t = _stub_transcriber()
    t._model.transcribe = lambda audio, **p: [type("S", (), {"text": "Okta works"})()]
    t._text_tokens = lambda: [(b" Okta", 0.2), (b" works", 0.9)]
    t.transcribe(np.zeros(16_000, np.float32))
    assert t.last_uncertain == {"okta"}
    t._text_tokens = lambda: [(b" x", 0.0)]
    t.transcribe(np.zeros(16_000, np.float32), partial=True)
    assert t.last_uncertain == {"okta"}  # partials leave it alone
