from rich.console import Console
from rich.text import Text

from mic2md.ui import MATRIX_GLYPHS, BottomAligned, LiveView, MatrixRain, level_history


def render(obj, width=120, height=20) -> str:
    console = Console(width=width, height=height, color_system=None, force_terminal=False)
    with console.capture() as cap:
        console.print(obj, height=height)
    return cap.get()


def test_bottom_aligned_shows_newest_lines_that_fit():
    items = [Text(f"line {i}") for i in range(50)]
    out = render(BottomAligned(items), height=5).splitlines()
    assert [s.strip() for s in out[:5]] == [f"line {i}" for i in range(45, 50)]


def test_bottom_aligned_pads_short_content_at_the_top():
    out = render(BottomAligned([Text("only")]), height=4).splitlines()
    assert [s.strip() for s in out[:4]] == ["", "", "", "only"]


def test_level_history_is_right_aligned_and_clipped():
    assert level_history([(1.0, True)], 4).plain == "   █"
    assert len(level_history([(1.0, False)] * 10, 4).plain) == 4


def test_fullscreen_layout_has_transcript_sidebar_and_mic():
    view = LiveView("en", "base.en", fullscreen=True, meeting="Weekly sync", path="/x/a.md")
    view.calibrated = True
    view.add_line(Text("We ship on Friday."), at=75.0, unsure=1)
    view.partial = "and then"
    out = render(view)
    assert out.count("\n") == 20  # fills the terminal height
    for part in ("Transcript", "Session", "Mic", "Weekly sync", "00:01:15", "We ship on Friday."):
        assert part in out
    assert "and then" in out and "Ctrl+C to stop" in out
    assert view.words == 4 and view.unsure == 1


def test_fullscreen_drops_sidebar_on_narrow_terminals():
    view = LiveView("en", "base.en", fullscreen=True)
    assert "Session" not in render(view, width=80)


def test_inline_view_is_a_single_status_line():
    view = LiveView("en", "base.en")
    assert render(view).strip().startswith("● REC")
    assert render(view).count("\n") == 1


def test_notes_are_shown_in_time_order_and_editor_in_footer():
    view = LiveView("en", "base.en", fullscreen=True)
    view.add_line(Text("First."), at=5.0)
    view.add_line(Text("Third."), at=20.0)
    view.add_note("second", at=12.0)
    assert [t.plain for _, t, _ in view.lines] == ["First.", "✎ second", "Third."]
    view.note, view.note_at = "typing " * 40, 31.0
    out = render(view)
    assert "Note 00:00:31" in out and "Enter save · Esc cancel" in out
    assert "n note" not in out
    assert view.sentences == 2 and view.notes == 1


def test_matrix_rain_fills_blank_cells_and_keeps_text():
    rain = MatrixRain(seed=1)
    rain.frame(40, 10, now=0.0)
    rain._last = 0.0
    items = [Text("We ship on Friday.")]
    out = render(BottomAligned(items, background=rain), width=40, height=10).splitlines()[:10]
    assert "We ship on Friday." in out[-1]  # word gaps stay blank
    assert any(ch in MATRIX_GLYPHS for line in out for ch in line)
    assert all(len(line) == 40 for line in out)


def test_matrix_rain_drops_fall_over_time():
    rain = MatrixRain(seed=2)
    rain.frame(5, 20, now=0.0)
    before = [d[0] for d in rain.drops]
    rain.frame(5, 20, now=0.1)
    assert all(
        after > b or after < 0 for after, b in zip([d[0] for d in rain.drops], before, strict=True)
    )


def test_m_toggles_matrix_in_fullscreen_transcript():
    view = LiveView("en", "base.en", fullscreen=True)
    assert "m matrix" in render(view)
    view.toggle_matrix()
    assert isinstance(view.matrix, MatrixRain)
    view.toggle_matrix()
    assert view.matrix is None


def test_after_recording_the_panel_streams_llm_output_and_sidebar_lists_steps():
    from mic2md.ui import LlmProgress

    view = LiveView("en", "base.en", fullscreen=True)
    view.add_line(Text("um we ship friday"), at=3.0)
    view.stop()
    tags = LlmProgress("tagging", "qwen")
    tags.end("done")
    polish = LlmProgress("polishing", "qwen")
    polish.chars = 30
    view.tasks += [tags, polish]
    view.output_title, view.output = "Polishing with qwen", "# Shipping\n\nWe ship on"
    view.log.append(Text("Index updated"))
    out = render(view)
    assert "POLISHING" in out and "REC" not in out
    assert "Polishing with qwen" in out and "# Shipping" in out and "We ship on" in out
    assert "um we ship friday" not in out  # the panel shows the LLM output now
    assert "Tagging" in out and "✔" in out and "30 ch" in out
    assert "Index updated" in out and "Ctrl+C to skip" in out

    polish.end("done")
    view.done = True
    out = render(view)
    assert "DONE" in out and "Ctrl+C" not in out


def test_stop_freezes_the_clock():
    view = LiveView("en", "base.en", fullscreen=True)
    view.started -= 65
    view.stop()
    view.stopped = view.started + 65
    assert view.elapsed() == "00:01:05"
