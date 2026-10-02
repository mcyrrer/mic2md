import pytest

from mic2md.cli import _normalize_participants


def test_normalize_participants():
    assert _normalize_participants(" Ada ,Bob,, Carl ") == "Ada, Bob, Carl"
    assert _normalize_participants("") == ""


def test_line_streamer_prints_whole_lines_and_counts_characters(monkeypatch):
    from mic2md import cli

    printed = []
    monkeypatch.setattr(cli.console, "print", lambda line, **kw: printed.append(line))
    progress = cli.LlmProgress("summarizing", "m")
    stream = cli.LineStreamer(progress)
    for token in ["## Ti", "tle\n", "\nBo", "dy"]:
        stream(token)
    assert printed == ["## Title", ""]
    stream.flush()
    assert printed == ["## Title", "", "Body"]
    assert progress.chars == len("## Title\n\nBody")
    assert "14 characters" in str(progress.__rich__())


def _fake_llm(summary: str, tags=("shipping", "release-planning")):
    """Stand-in for cli._stream_llm: a summary for summaries, a tag list for tagging."""

    def fake(task, *args, quiet=False, **kwargs):
        return list(tags) if task == "tagging" else summary

    return fake


def test_summarize_imports_external_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    ext = tmp_path / "elsewhere" / "notes.md"
    ext.parent.mkdir()
    ext.write_text("---\nmeeting: From file\n---\n\nWe agreed to ship on Friday.\n")
    out = tmp_path / "out"
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("## Executive summary\n\nShip Friday."))

    result = CliRunner().invoke(cli.app, ["summarize", str(ext), "-o", str(out)], input="")

    assert result.exit_code == 0, result.output
    (imported,) = (out / "transcripts").rglob("*.md")
    text = imported.read_text()
    assert "meeting: From file" in text
    assert f"source: {ext.resolve()}" in text
    assert text.index("Ship Friday.") < text.index("We agreed to ship on Friday.")
    assert ext.read_text().startswith("---\nmeeting: From file")  # original untouched
    assert "tags: [shipping, release-planning]" in text
    index_text = (out / "index.md").read_text()
    assert "From file" in index_text and "`release-planning`" in index_text


def test_summarize_does_not_import_files_already_in_output_dir(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    f = tmp_path / "transcripts" / "2026-09" / "2026-09-23T10-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nlanguage: en\n---\n\n# T\n\nBody.\n")
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("## S"))

    result = CliRunner().invoke(cli.app, ["summarize", str(f), "-o", str(tmp_path)], input="")

    assert result.exit_code == 0, result.output
    assert list((tmp_path / "transcripts").rglob("*.md")) == [f]
    assert "## S" in f.read_text()


def test_polish_imports_external_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    ext = tmp_path / "elsewhere" / "notes.md"
    ext.parent.mkdir()
    ext.write_text("---\nmeeting: From file\n---\n\num we ship friday\n")
    out = tmp_path / "out"
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("# Shipping\n\nWe ship on Friday."))

    result = CliRunner().invoke(cli.app, ["polish", str(ext), "-o", str(out)], input="")

    assert result.exit_code == 0, result.output
    (imported,) = (out / "transcripts").rglob("*.md")
    text = imported.read_text()
    assert "meeting: From file" in text
    assert f"source: {ext.resolve()}" in text
    assert "We ship on Friday." in text and "um we ship friday" not in text
    assert "tags: [shipping, release-planning]" in text
    assert ext.read_text() == "---\nmeeting: From file\n---\n\num we ship friday\n"
    assert "From file" in (out / "index.md").read_text()


def test_polish_does_not_import_files_already_in_output_dir(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    f = tmp_path / "transcripts" / "2026-09" / "2026-09-23T10-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nlanguage: en\n---\n\n# Transcript 2026-09-23 10:00\n\nbody\n")
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("# T\n\nBody."))

    result = CliRunner().invoke(cli.app, ["polish", str(f), "-o", str(tmp_path)], input="")

    assert result.exit_code == 0, result.output
    assert list((tmp_path / "transcripts").rglob("*.md")) == [f]
    assert "Body." in f.read_text()


def test_summarize_without_file_records_polishes_then_summarizes(tmp_path, monkeypatch):
    from datetime import datetime

    from typer.testing import CliRunner

    from mic2md import cli

    seen = {}

    def fake_record(
        opts, backend, llm_model, ollama_url, output_dir, terms=None, tag=True, stack=None
    ):
        seen.update(opts=opts, tag=tag, output_dir=output_dir, terms=terms)
        writer = cli.SessionWriter(output_dir, datetime(2026, 9, 23, 10), opts.lang.value, "m")
        writer.append("um we ship friday")
        writer.finalize("# Shipping\n\nWe ship on Friday.", "qwen", datetime(2026, 9, 23, 11))
        return writer, "# Shipping\n\nWe ship on Friday."

    monkeypatch.setattr(cli, "_record", fake_record)
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("## Executive summary\n\nShip Friday."))
    args = ["-m", "small", "--no-calendar", "summarize", "-l", "sv", "-o", str(tmp_path)]
    monkeypatch.delenv("MIC2MD_BEAM_SIZE", raising=False)

    result = CliRunner().invoke(cli.app, args, input="")

    assert result.exit_code == 0, result.output
    assert seen["tag"] is False  # tags come from the summarize pass
    assert seen["terms"] == []  # no glossary.txt in the output folder
    assert seen["opts"].model_size == "small" and seen["opts"].no_calendar
    assert seen["opts"].lang == cli.Lang.sv
    assert seen["opts"].beam_size == 5 and seen["opts"].vad == cli.Vad.silero
    (session,) = (tmp_path / "transcripts").rglob("*.md")
    text = session.read_text()
    assert text.index("Ship Friday.") < text.index("We ship on Friday.")
    assert "tags: [shipping, release-planning]" in text


def test_summarize_without_file_rejects_no_llm(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    monkeypatch.setattr(cli, "_record", lambda *a, **k: pytest.fail("should not record"))
    result = CliRunner().invoke(cli.app, ["--no-llm", "summarize", "-o", str(tmp_path)])
    assert result.exit_code == 2


def test_short_names_run_polish_and_summarize(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    f = tmp_path / "transcripts" / "2026-09" / "2026-09-23T10-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nlanguage: en\n---\n\n# Transcript 2026-09-23 10:00\n\nbody\n")
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("# T\n\nPolished."))
    assert CliRunner().invoke(cli.app, ["p", str(f), "-o", str(tmp_path)]).exit_code == 0
    assert "Polished." in f.read_text()

    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("## Summary here"))
    assert CliRunner().invoke(cli.app, ["s", str(f), "-o", str(tmp_path)]).exit_code == 0
    assert "## Summary here" in f.read_text()


def _capture_summarize(tmp_path, monkeypatch):
    """Session file in tmp_path plus a fake LLM pass that records its backend and model."""
    from mic2md import cli

    f = tmp_path / "transcripts" / "2026-09" / "2026-09-23T10-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nlanguage: en\n---\n\n# T\n\nBody.\n")
    seen = {}

    def fake(task, backend, model, url, call, fallback, quiet=False):
        if task == "tagging":
            return ["q4-budget"]
        seen.update(backend=backend, model=model)
        return "## S"

    monkeypatch.setattr(cli, "_stream_llm", fake)
    return cli, f, seen


def test_options_before_subcommand_are_used(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    cli, f, seen = _capture_summarize(tmp_path, monkeypatch)
    args = ["--backend", "claude", "--llm", "opus", "-o", str(tmp_path), "summarize", str(f)]
    result = CliRunner().invoke(cli.app, args, input="")
    assert result.exit_code == 0, result.output
    assert seen == {"backend": cli.Backend.claude, "model": "opus"}
    assert "summary_model: opus (claude -p)" in f.read_text()


def test_option_after_subcommand_wins_and_env_loses_to_command_line(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    cli, f, seen = _capture_summarize(tmp_path, monkeypatch)
    monkeypatch.setenv("MIC2MD_BACKEND", "copilot")
    args = ["-b", "claude", "summarize", str(f), "-o", str(tmp_path)]
    assert CliRunner().invoke(cli.app, args, input="").exit_code == 0
    assert seen["backend"] == cli.Backend.claude

    args = ["-b", "claude", "summarize", str(f), "-o", str(tmp_path), "-b", "ollama"]
    assert CliRunner().invoke(cli.app, args, input="").exit_code == 0
    assert seen["backend"] == cli.Backend.ollama

    args = ["summarize", str(f), "-o", str(tmp_path)]
    assert CliRunner().invoke(cli.app, args, input="").exit_code == 0
    assert seen["backend"] == cli.Backend.copilot


def test_tag_command_tags_untagged_sessions_and_keeps_body(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    month = tmp_path / "transcripts" / "2026-09"
    month.mkdir(parents=True)
    untagged = month / "2026-09-23T10-00-00.md"
    body = "<!-- mic2md:summary -->\n## S\n<!-- /mic2md:summary -->\n\n---\n\n# T\n\nBody.\n"
    untagged.write_text("---\nlanguage: en\nmeeting: M\n---\n\n" + body)
    tagged = month / "2026-09-24T10-00-00.md"
    tagged.write_text("---\nlanguage: en\ntags: [old]\n---\n\n# X\n")
    calls = []

    def fake(task, backend, model, url, call, fallback, quiet=False):
        calls.append(task)
        return ["q4-budget", "hiring"]

    monkeypatch.setattr(cli, "_stream_llm", fake)
    monkeypatch.setattr(cli.llm, "check", lambda *a, **k: None)
    result = CliRunner().invoke(cli.app, ["tag", "--all", "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls == ["tagging"]  # the already-tagged file is skipped
    assert untagged.read_text() == (
        "---\nlanguage: en\nmeeting: M\ntags: [q4-budget, hiring]\n---\n\n" + body
    )
    assert "tags: [old]" in tagged.read_text()
    assert "`q4-budget` `hiring`" in (tmp_path / "index.md").read_text()


def test_glossary_file_in_output_dir_reaches_polish_prompt(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    (tmp_path / "glossary.txt").write_text("# products\nKBLab\n\nmic2md\nKBLab\n")
    f = tmp_path / "transcripts" / "2026-09" / "2026-09-23T10-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nlanguage: en\nparticipants: Ada Lovelace\n---\n\n# Transcript x\n\nbody\n")
    seen = {}

    def fake(task, backend, model, url, call, fallback, quiet=False):
        if task == "tagging":
            return ["x"]

        def fake_chat(messages, *args):
            seen["m"] = messages
            return "# T"

        monkeypatch.setattr(cli.llm, "chat", fake_chat)
        call(lambda t: None)
        return "# T\n\nBody."

    monkeypatch.setattr(cli, "_stream_llm", fake)
    result = CliRunner().invoke(cli.app, ["polish", str(f), "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    system = seen["m"][0]["content"]
    assert (
        "exactly like this when they occur (the speech recognizer may have misheard them): "
        "Ada Lovelace, KBLab, mic2md." in system
    )


def test_glossary_option_overrides_default_file(tmp_path):
    from mic2md import cli

    (tmp_path / "glossary.txt").write_text("Default\n")
    other = tmp_path / "other.txt"
    other.write_text("Other\n")
    assert cli._glossary_terms(None, tmp_path) == ["Default"]
    assert cli._glossary_terms(other, tmp_path) == ["Other"]
    assert cli._glossary_terms(None, tmp_path / "missing") == []


def test_detector_choice_and_fallback(monkeypatch):
    from mic2md import cli, vad

    assert cli._detector(cli.RecordOptions(vad=cli.Vad.energy)) is None
    assert cli._detector(cli.RecordOptions(threshold=0.01)) is None
    assert isinstance(cli._detector(cli.RecordOptions()), vad.SileroDetector)

    def broken(*a, **k):
        raise OSError("no model")

    monkeypatch.setattr(vad, "SileroDetector", broken)
    assert cli._detector(cli.RecordOptions()) is None


def test_partials_are_fast_mode_and_rate_limited_by_their_own_cost(monkeypatch):
    import numpy as np

    from mic2md import cli

    class FakeTranscriber:
        def __init__(self):
            self.calls = []

        def transcribe(self, audio, prompt=None, partial=False):
            self.calls.append(partial)
            clock[0] += 0.5  # each partial "takes" 0.5 s
            return "partial"

    class FakeSeg:
        in_speech = True

        def current(self):
            return np.zeros(cli.SAMPLE_RATE, np.float32)

    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    tr = FakeTranscriber()
    rec = cli.Recorder(tr, FakeSeg(), writer=None, view=cli.LiveView("en", "m"))
    rec.maybe_partial()
    assert tr.calls == [True] and rec.view.partial == "partial"
    clock[0] += 0.9  # < 2 × 0.5 s since the last one ended
    rec.maybe_partial()
    assert len(tr.calls) == 1
    clock[0] += 0.2
    rec.maybe_partial()
    assert len(tr.calls) == 2


def test_commit_writes_timestamped_line_and_keeps_context_plain(tmp_path, monkeypatch):
    from datetime import datetime

    import numpy as np

    from mic2md import cli

    class FakeTranscriber:
        def transcribe(self, audio, prompt=None, partial=False):
            self.prompt = prompt
            return "We ship on Friday."

    monkeypatch.setattr(cli.console, "print", lambda *a, **k: None)
    writer = cli.SessionWriter(tmp_path, datetime(2026, 9, 23, 10), "en", "m")
    tr = FakeTranscriber()
    rec = cli.Recorder(tr, segmenter=None, writer=writer, view=cli.LiveView("en", "m"))
    rec.commit(np.zeros(10, np.float32), start=75.0)
    rec.commit(np.zeros(10, np.float32), start=80.0)
    assert writer.lines[0] == "[00:01:15] We ship on Friday."
    assert tr.prompt == "We ship on Friday."  # no timestamp in Whisper's prompt
    assert rec.pending is None


def test_commit_marks_uncertain_words_in_file_but_not_in_context(tmp_path, monkeypatch):
    from datetime import datetime

    import numpy as np

    from mic2md import cli

    class FakeTranscriber:
        last_uncertain = {"okta"}

        def transcribe(self, audio, prompt=None, partial=False):
            return "We touch Okta."

    printed = []
    monkeypatch.setattr(cli.console, "print", lambda obj, **k: printed.append(obj))
    writer = cli.SessionWriter(tmp_path, datetime(2026, 9, 23, 10), "en", "m")
    rec = cli.Recorder(
        FakeTranscriber(), segmenter=None, writer=writer, view=None, show_transcript=True
    )
    rec.view = cli.LiveView("en", "m")
    rec.commit(np.zeros(10, np.float32), start=1.0)
    assert writer.lines == ["[00:00:01] We touch Okta(?)."]
    assert rec.context == "We touch Okta."
    assert printed[0].plain == "We touch Okta."
    assert any("underline" in str(span.style) for span in printed[0].spans)


def test_commit_does_not_print_transcript_by_default(tmp_path, monkeypatch):
    from datetime import datetime

    import numpy as np

    from mic2md import cli

    class FakeTranscriber:
        last_uncertain: set[str] = set()

        def transcribe(self, audio, prompt=None, partial=False):
            return "We touch Okta."

    printed = []
    monkeypatch.setattr(cli.console, "print", lambda obj, **k: printed.append(obj))
    writer = cli.SessionWriter(tmp_path, datetime(2026, 9, 23, 10), "en", "m")
    rec = cli.Recorder(FakeTranscriber(), segmenter=None, writer=writer, view=None)
    rec.view = cli.LiveView("en", "m")
    rec.commit(np.zeros(10, np.float32), start=1.0)
    assert writer.lines == ["[00:00:01] We touch Okta."]
    assert printed == []


def test_polish_prompt_explains_uncertain_marker():
    from mic2md import llm

    assert "`(?)` right after a word" in llm.build_messages("x", "en")[0]["content"]


def test_fullscreen_run_prints_transcript_afterwards_only_with_flag(tmp_path, monkeypatch):
    import queue
    from datetime import datetime

    import numpy as np

    from mic2md import cli

    class FakeTranscriber:
        last_uncertain: set[str] = set()

        def transcribe(self, audio, prompt=None, partial=False):
            return "We ship on Friday."

    class FakeSeg:
        in_speech = False
        level = 0.0
        speaking = False
        calibrated = True
        last_start_s = 1.0

        def feed(self, frame):
            return frame

    class FakeMic:
        frames = queue.Queue()

        def __init__(self):
            self.frames.put(np.zeros(10, np.float32))

    class StopLive:
        def __init__(self, *a, **k):
            self.kwargs = k
            seen.append(k)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    printed, seen = [], []
    monkeypatch.setattr(cli.console, "print", lambda obj, **k: printed.append(obj))
    monkeypatch.setattr(cli, "Live", StopLive)

    def stop_after_first(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.Recorder, "maybe_partial", stop_after_first)
    for flag in (False, True):
        printed.clear()
        writer = cli.SessionWriter(tmp_path, datetime(2026, 9, 23, 10), "en", "m")
        view = cli.LiveView("en", "m", fullscreen=True)
        rec = cli.Recorder(FakeTranscriber(), FakeSeg(), writer, view, show_transcript=flag)
        rec.run(FakeMic())
        assert seen[-1]["screen"] is True
        assert [str(p) for p in printed] == (["We ship on Friday."] if flag else [])
        assert view.lines[0][1].plain == "We ship on Friday."


def _note_recorder(tmp_path, monkeypatch, show_transcript=False):
    from datetime import datetime

    from mic2md import cli

    printed = []
    monkeypatch.setattr(cli.console, "print", lambda obj, **k: printed.append(obj))
    writer = cli.SessionWriter(tmp_path, datetime(2026, 10, 1, 10), "en", "m")
    rec = cli.Recorder(None, None, writer, cli.LiveView("en", "m"), show_transcript=show_transcript)
    return rec, printed


def test_typed_note_is_saved_on_enter(tmp_path, monkeypatch):
    from mic2md import cli, keys

    rec, printed = _note_recorder(tmp_path, monkeypatch, show_transcript=True)
    monkeypatch.setattr(cli.time, "monotonic", lambda: rec._t0 + 75.0)
    rec.handle_keys(["x", "n"])  # keys before `n` are ignored
    assert rec.view.note == "" and rec.view.note_at == 75.0
    rec.handle_keys(list("hej nn") + [keys.BACKSPACE, "å", keys.ENTER])
    assert rec.view.note is None
    assert rec.writer.notes == ["[00:01:15] hej nå"]
    assert rec.view.notes == 1 and printed[0].plain == "✎ hej nå"


def test_m_toggles_matrix_but_is_typed_inside_a_note(tmp_path, monkeypatch):
    from mic2md import keys

    rec, _ = _note_recorder(tmp_path, monkeypatch)
    rec.handle_keys(["m"])
    assert rec.view.matrix is not None
    rec.handle_keys(["n", "m"])
    assert rec.view.note == "m" and rec.view.matrix is not None
    rec.handle_keys([keys.ESC, "M"])
    assert rec.view.matrix is None


def test_esc_and_empty_notes_save_nothing(tmp_path, monkeypatch):
    from mic2md import keys

    rec, _ = _note_recorder(tmp_path, monkeypatch)
    rec.handle_keys(["n", "a", keys.ESC, "n", " ", keys.ENTER, "n", "b", keys.CLEAR, keys.ENTER])
    assert rec.writer.notes == [] and rec.view.note is None


def test_note_open_at_ctrl_c_is_kept(tmp_path, monkeypatch):
    import queue

    rec, _ = _note_recorder(tmp_path, monkeypatch)

    class Seg:
        def flush(self):
            return None

    class Mic:
        frames = queue.Queue()

    rec.seg = Seg()
    rec.handle_keys(list("nunfinished"))
    rec.finish(Mic())
    assert rec.writer.notes == ["[00:00:00] unfinished"]


def test_polish_command_carries_notes_over(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli
    from mic2md.writer import extract_notes

    f = tmp_path / "transcripts" / "2026-10" / "2026-10-01T10-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text(
        "---\ndate: 2026-10-01T10:00:00+02:00\nlanguage: en\n---\n\n# Transcript x\n\n"
        "[00:00:01] Hello.\n\n[00:00:05] NOTE: my note\n\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_polish(raw, *a, **k):
        seen["raw"] = raw
        return "# Hi\n\nHello."

    monkeypatch.setattr(cli, "_run_polish", fake_polish)
    monkeypatch.setattr(cli, "_run_tags", lambda *a, **k: [])
    result = CliRunner().invoke(cli.app, ["polish", str(f), "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen["raw"] == "[00:00:01] Hello."
    assert extract_notes(f.read_text(encoding="utf-8")) == ["[00:00:05] my note"]


def test_llm_pass_inside_fullscreen_streams_into_the_view_and_replays(monkeypatch):
    from mic2md import cli

    class FakeLive:
        def __init__(self, *a, **k):
            assert k["screen"] is True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    printed = []
    monkeypatch.setattr(cli, "Live", FakeLive)
    monkeypatch.setattr(cli.console, "print", lambda obj, **k: printed.append(obj))
    monkeypatch.setattr(cli.llm, "check", lambda *a, **k: None)
    view = cli.LiveView("en", "m", fullscreen=True)

    def call(on_token):
        for token in ["# Ti", "tle\n\nBody"]:
            on_token(token)
            seen.append(view.output)
        return "# Title\n\nBody"

    seen = []
    with cli.Screen(view):
        result = cli._stream_llm("polishing", cli.Backend.ollama, "qwen", "u", call, "kept")
        cli._say("[green]✔ Saved[/] x")
        assert printed == []  # nothing prints while the screen is up
        assert view.tasks[0].status == "done" and view.tasks[0].chars == 13
        assert view.log[-1].plain == "✔ Saved x"
    assert result == "# Title\n\nBody"
    assert seen == ["# Ti", "# Title\n\nBody"]
    assert cli._screen is None
    texts = [str(p) for p in printed]
    assert "# Title\n\nBody" in texts and texts[-1] == "[green]✔ Saved[/] x"


def test_failed_llm_pass_inside_fullscreen_is_logged(monkeypatch):
    from mic2md import cli

    monkeypatch.setattr(cli, "Live", lambda *a, **k: cli.ExitStack())
    monkeypatch.setattr(cli.console, "print", lambda obj, **k: None)
    monkeypatch.setattr(cli.llm, "check", lambda *a, **k: None)
    view = cli.LiveView("en", "m", fullscreen=True)

    def call(on_token):
        raise cli.llm.LLMError("boom")

    with cli.Screen(view):
        assert cli._stream_llm("polishing", cli.Backend.ollama, "q", "u", call, "kept") is None
    assert view.tasks[0].status == "failed"
    assert "Polishing failed, kept" in view.log[-1].plain


def _two_meetings():
    from datetime import datetime

    from mic2md.meetings import Meeting

    return [
        Meeting(
            "Standup", ["Ada", "Bob"], datetime(2026, 10, 1, 10), datetime(2026, 10, 1, 10, 30)
        ),
        Meeting("Workshop", [], datetime(2026, 10, 1, 9), datetime(2026, 10, 1, 12)),
    ]


@pytest.mark.parametrize(
    ("answers", "expected"),
    [([""], "Standup"), (["2"], "Workshop"), (["x", "9", "2"], "Workshop"), (["0"], None)],
)
def test_choose_meeting_asks_when_several_are_scheduled(monkeypatch, answers, expected):
    from mic2md import cli

    replies = iter(answers)
    printed = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt: next(replies))
    monkeypatch.setattr(cli.console, "print", lambda obj="", **k: printed.append(str(obj)))
    chosen = cli._choose_meeting(_two_meetings())
    assert (chosen.title if chosen else None) == expected
    assert any("10:00–10:30  Standup  (2 people)" in p for p in printed)


def test_choose_meeting_without_a_terminal_takes_the_latest(monkeypatch):
    from mic2md import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: None)
    assert cli._choose_meeting(_two_meetings()).title == "Standup"
    assert cli._choose_meeting([]) is None


def test_meeting_meta_none_of_these_asks_for_a_name(monkeypatch):
    from mic2md import cli, meetings

    replies = iter(["0", "Planning", "Ada, Bob"])
    monkeypatch.setattr(meetings, "current_meetings", _two_meetings)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.console, "input", lambda prompt: next(replies))
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: None)
    assert cli._meeting_meta() == {"meeting": "Planning", "participants": "Ada, Bob"}


def test_thoughts_skip_the_calendar(monkeypatch):
    from mic2md import cli
    from mic2md.writer import SessionType

    monkeypatch.setattr(cli, "_meeting_meta", lambda: {"meeting": "Standup", "participants": ""})
    assert cli._session_meta(cli.RecordOptions()) == {"meeting": "Standup", "participants": ""}
    assert cli._session_meta(cli.RecordOptions(no_calendar=True)) == {}
    monkeypatch.setattr(cli, "_meeting_meta", lambda: pytest.fail("calendar used for thoughts"))
    assert cli._session_meta(cli.RecordOptions(session_type=SessionType.thoughts)) == {}


def test_type_option_reaches_the_recording(tmp_path, monkeypatch):
    from datetime import datetime

    from typer.testing import CliRunner

    from mic2md import cli
    from mic2md.writer import SessionType

    seen = []

    def fake_record(
        opts, backend, llm_model, ollama_url, output_dir, terms=None, tag=True, stack=None
    ):
        seen.append(opts.session_type)
        writer = cli.SessionWriter(
            output_dir, datetime(2026, 10, 2, 9), "en", "m", session_type=opts.session_type
        )
        writer.append("an idea")
        writer.finalize("# Idea\n\nAn idea.", "qwen", datetime(2026, 10, 2, 9, 5))
        return writer, "# Idea\n\nAn idea."

    monkeypatch.setattr(cli, "_record", fake_record)
    monkeypatch.setattr(cli, "_stream_llm", _fake_llm("## Summary\n\nAn idea."))
    monkeypatch.delenv("MIC2MD_TYPE", raising=False)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["-o", str(tmp_path / "a")]).exit_code == 0
    assert runner.invoke(cli.app, ["-T", "thoughts", "-o", str(tmp_path / "b")]).exit_code == 0
    args = ["summarize", "--type", "thoughts", "-o", str(tmp_path / "c")]
    assert runner.invoke(cli.app, args).exit_code == 0
    assert seen == [SessionType.meeting, SessionType.thoughts, SessionType.thoughts]
    assert list((tmp_path / "b" / "transcripts" / "thoughts").rglob("*.md"))


def _capture_summary_type(monkeypatch):
    """Fake LLM pass that runs the real summarize call against a stub llm.summarize."""
    from mic2md import cli

    seen = {}

    def fake_summarize(transcript, language, **kwargs):
        seen.update(kwargs)
        return "## Summary"

    def fake(task, backend, model, url, call, fallback, quiet=False):
        return ["idea"] if task == "tagging" else call(None)

    monkeypatch.setattr(cli.llm, "summarize", fake_summarize)
    monkeypatch.setattr(cli, "_stream_llm", fake)
    return seen


def test_import_as_thoughts_lands_in_thoughts_folder(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    seen = _capture_summary_type(monkeypatch)
    ext = tmp_path / "elsewhere" / "idea.md"
    ext.parent.mkdir()
    ext.write_text("Maybe we should rewrite the parser.\n")
    out = tmp_path / "out"

    args = ["summarize", str(ext), "-o", str(out), "-T", "thoughts"]
    result = CliRunner().invoke(cli.app, args, input="")

    assert result.exit_code == 0, result.output
    (imported,) = (out / "transcripts" / "thoughts").rglob("*.md")
    assert "type: thoughts" in imported.read_text()
    assert seen["session_type"] == "thoughts"
    assert "| thoughts |" in (out / "index.md").read_text()


def test_summarize_file_uses_its_type(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mic2md import cli

    seen = _capture_summary_type(monkeypatch)
    f = tmp_path / "transcripts" / "thoughts" / "2026-10" / "2026-10-02T09-00-00.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nlanguage: en\ntype: thoughts\n---\n\n# Idea\n\nBody.\n")

    result = CliRunner().invoke(cli.app, ["summarize", str(f), "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert seen["session_type"] == "thoughts"
