from datetime import datetime, timedelta

from mic2md.writer import (
    SessionWriter,
    format_duration,
    parse_document,
    session_filename,
    write_final,
)


def test_session_filename_is_iso_and_filesystem_safe():
    assert session_filename(datetime(2026, 9, 23, 20, 5, 7)) == "2026-09-23T20-05-07.md"


def test_format_duration():
    assert format_duration(timedelta(hours=1, minutes=2, seconds=3)) == "01:02:03"


def test_append_is_written_immediately(tmp_path):
    w = SessionWriter(tmp_path, datetime(2026, 9, 23, 20, 5, 7), "en", "base.en")
    w.append("Hello world.")
    text = w.path.read_text()
    assert w.path.name == "2026-09-23T20-05-07.md"
    assert text.startswith("---\ndate: 2026-09-23T20:05:07")
    assert "language: en" in text
    assert "# Transcript 2026-09-23 20:05" in text
    assert "Hello world." in text


def test_finalize_with_polish_drops_raw(tmp_path):
    start = datetime(2026, 9, 23, 20, 0, 0)
    w = SessionWriter(tmp_path, start, "sv", "large")
    w.append("hej hej")
    w.append("hur mår du")
    w.finalize("# Hälsning\n\nHej! Hur mår du?", "qwen3.5:9b", start + timedelta(seconds=75))
    text = w.path.read_text()
    assert "llm_model: qwen3.5:9b" in text
    assert "duration: 00:01:15" in text
    assert "<details>" not in text and "hej hej" not in text
    meta, body = parse_document(text)
    assert meta["language"] == "sv"
    assert body == "# Hälsning\n\nHej! Hur mår du?"


def test_finalize_without_polish(tmp_path):
    w = SessionWriter(tmp_path, datetime(2026, 9, 23, 20, 0, 0), "en", "base.en")
    w.append("just raw")
    w.finalize(None, "qwen3.5:9b", datetime(2026, 9, 23, 20, 0, 10))
    text = w.path.read_text()
    assert "llm_model" not in text
    assert "<details>" not in text
    assert parse_document(text)[1] == "just raw"


def test_discard_if_empty(tmp_path):
    w = SessionWriter(tmp_path, datetime.now(), "en", "base.en")
    assert w.discard_if_empty()
    assert not w.path.exists()


def test_write_final_is_repeatable(tmp_path):
    p = tmp_path / "x.md"
    write_final(p, {"language": "en"}, "raw text", "# One")
    meta, raw = parse_document(p.read_text())
    assert raw == "# One"
    write_final(p, meta, raw, "# Two")
    text = p.read_text()
    assert "# Two" in text and "# One" not in text


def test_parse_document_reads_raw_section_from_old_files():
    text = (
        "---\nlanguage: en\n---\n\n# Title\n\n---\n\n"
        "<details>\n<summary>Raw transcript</summary>\n\nraw words\n\n</details>\n"
    )
    assert parse_document(text) == ({"language": "en"}, "raw words")


def test_meeting_meta_round_trips_and_empty_values_have_no_trailing_space(tmp_path):
    extra = {"meeting": "Sprint planning: Q4", "participants": "Ada, Bob"}
    w = SessionWriter(tmp_path, datetime(2026, 9, 23, 20, 0, 0), "en", "base.en", extra)
    w.append("hi")
    w.finalize(None, None, datetime(2026, 9, 23, 20, 0, 10))
    meta, _ = parse_document(w.path.read_text())
    assert meta["meeting"] == "Sprint planning: Q4"
    assert meta["participants"] == "Ada, Bob"

    empty = {"meeting": "", "participants": ""}
    w = SessionWriter(tmp_path / "e", datetime(2026, 9, 23, 20, 0, 0), "en", "base.en", empty)
    text = w.path.read_text()
    assert "\nmeeting:\nparticipants:\n" in text
    assert parse_document(text)[0]["meeting"] == ""


def test_insert_summary_goes_on_top_and_replaces_previous(tmp_path):
    from mic2md.writer import extract_summary, insert_summary

    p = tmp_path / "x.md"
    write_final(p, {"language": "en"}, "raw", "# Title\n\nBody.")
    meta, _ = parse_document(p.read_text())
    insert_summary(p, {**meta, "summary_model": "m"}, p.read_text(), "## Executive summary\n\nA")
    text = p.read_text()
    assert text.startswith("---\nlanguage: en\nsummary_model: m\n---\n\n<!-- mic2md:summary")
    assert text.index("## Executive summary") < text.index("# Title")
    assert parse_document(text)[1] == "# Title\n\nBody."

    insert_summary(p, meta, text, "## Executive summary\n\nB")
    text = p.read_text()
    assert text.count("<!-- mic2md:summary -->") == 1 and "\n\nA\n" not in text
    assert extract_summary(text) == "## Executive summary\n\nB"
    assert text.count("---\n") == 3  # front matter (2) + separator (1)
    assert text.endswith("# Title\n\nBody.\n")


def test_summary_with_legacy_voice2text_markers_is_read_and_replaced(tmp_path):
    from mic2md.writer import extract_summary, insert_summary

    p = tmp_path / "x.md"
    text = (
        "---\nlanguage: en\n---\n\n<!-- voice2text:summary -->\n## Old\n"
        "<!-- /voice2text:summary -->\n\n---\n\n# Title\n\nBody.\n"
    )
    assert extract_summary(text) == "## Old"
    meta, body = parse_document(text)
    assert body == "# Title\n\nBody."

    insert_summary(p, meta, text, "## New")
    text = p.read_text()
    assert "voice2text" not in text and text.count("<!-- mic2md:summary -->") == 1
    assert text.endswith("# Title\n\nBody.\n")


def test_write_final_keeps_summary(tmp_path):
    p = tmp_path / "x.md"
    write_final(p, {"language": "en"}, "raw", "# Two", summary="## Summary\n\nS")
    text = p.read_text()
    assert text.index("## Summary") < text.index("# Two")
    assert parse_document(text)[1] == "# Two"


def test_import_document_uses_session_pattern_and_avoids_collisions(tmp_path):
    from mic2md.writer import import_document, split_front_matter

    when = datetime(2026, 9, 1, 14, 0, 0)
    meta = {"date": when.isoformat(), "meeting": "Ext", "source": "/x/notes.md"}
    first = import_document(tmp_path, when, meta, "Some notes.")
    second = import_document(tmp_path, when, meta, "Other notes.")
    assert first.relative_to(tmp_path).as_posix() == "transcripts/2026-09/2026-09-01T14-00-00.md"
    assert second.name == "2026-09-01T14-00-01.md"
    got_meta, body = split_front_matter(first.read_text())
    assert got_meta["meeting"] == "Ext" and body.strip() == "Some notes."
