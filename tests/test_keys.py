from mic2md import keys
from mic2md.keys import BACKSPACE, CLEAR, ENTER, ESC, KeyReader, parse_keys


def test_parse_keys_printable_and_named():
    assert parse_keys("nå\x7f\r") == ["n", "å", BACKSPACE, ENTER]
    assert parse_keys("\x15\x08\n") == [CLEAR, BACKSPACE, ENTER]


def test_parse_keys_lone_esc_vs_sequences():
    assert parse_keys("\x1b") == [ESC]
    assert parse_keys("\x1b[A\x1b[1;5Cx\x1bOP") == ["x"]  # arrows, Ctrl+Right, F1
    assert parse_keys("\x1bb") == []  # Alt+b
    assert parse_keys("\x1b\x1b") == [ESC, ESC]


def test_parse_keys_drops_control_chars_and_tab_is_space():
    assert parse_keys("a\x01\tb") == ["a", " ", "b"]


def test_key_reader_without_terminal_reads_nothing(monkeypatch):
    monkeypatch.setattr(keys.sys.stdin, "isatty", lambda: False, raising=False)
    with KeyReader() as reader:
        assert reader.read() == []
