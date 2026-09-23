import json

import httpx
import pytest

from mic2md import llm


def test_strip_wrapping():
    assert llm.strip_wrapping("```markdown\n# T\n\nx\n```") == "# T\n\nx"
    assert llm.strip_wrapping("<think>hmm</think>\n# T") == "# T"
    assert llm.strip_wrapping("# plain") == "# plain"


def test_prompt_mentions_language():
    msgs = llm.build_messages("hej", "sv")
    assert "Swedish" in msgs[0]["content"]
    assert "<transcript>\nhej\n</transcript>" in msgs[1]["content"]


def _mock(monkeypatch, handler):
    real = httpx.Client

    def stream(method, url, **kw):
        return real(transport=httpx.MockTransport(handler)).stream(method, url, **kw)

    def get(url, **kw):
        return real(transport=httpx.MockTransport(handler)).get(url)

    monkeypatch.setattr(llm.httpx, "stream", stream)
    monkeypatch.setattr(llm.httpx, "get", get)


def test_polish_streams_tokens(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "m" and body["think"] is False
        lines = [{"message": {"content": t}, "done": False} for t in ("# Ti", "tle\n\nText.")]
        lines.append({"done": True})
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines))

    _mock(monkeypatch, handler)
    seen = []
    assert llm.polish("raw", "en", model="m", on_token=seen.append) == "# Title\n\nText."
    assert seen == ["# Ti", "tle\n\nText."]


def test_polish_http_error(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(llm.OllamaError, match="404"):
        llm.polish("raw", "en", model="m")


def test_check_missing_model(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"models": [{"name": "other:latest"}]}))
    with pytest.raises(llm.OllamaError, match="ollama pull m"):
        llm.check("localhost:11434", "m")


def test_summary_prompt_has_language_and_calendar_context():
    msgs = llm.build_summary_messages("hej", "sv", meeting="Planering", participants="Ada, Bo")
    assert "Swedish" in msgs[0]["content"] and "Action items" in msgs[0]["content"]
    assert "Meeting: Planering" in msgs[1]["content"]
    assert "Ada, Bo" in msgs[1]["content"]
    assert "<transcript>\nhej\n</transcript>" in msgs[1]["content"]
    assert "Meeting:" not in llm.build_summary_messages("x", "en")[1]["content"]


def _fake_claude(tmp_path, monkeypatch, script: str, name: str = "claude"):
    """Put a fake `claude` (or other CLI) executable first on PATH."""
    exe = tmp_path / name
    exe.write_text("#!/usr/bin/env python3\n" + script)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")


def test_claude_backend_streams_text_and_sends_prompts(tmp_path, monkeypatch):
    log = tmp_path / "args.json"
    _fake_claude(
        tmp_path,
        monkeypatch,
        f"""
import json, sys
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}, open({str(log)!r}, "w"))
def ev(d): print(json.dumps(d), flush=True)
ev({{"type": "system", "subtype": "init"}})
for t in ["# Ti", "tle\\n\\nText."]:
    ev({{"type": "stream_event", "event": {{"type": "content_block_delta",
        "delta": {{"type": "text_delta", "text": t}}}}}})
ev({{"type": "result", "subtype": "success", "is_error": False, "result": "# Title\\n\\nText."}})
""",
    )
    seen = []
    out = llm.polish("hej", "sv", model="sonnet", on_token=seen.append, backend=llm.CLAUDE)
    assert out == "# Title\n\nText."
    assert seen == ["# Ti", "tle\n\nText."]
    call = json.loads(log.read_text())
    argv = call["argv"]
    assert argv[:3] == ["-p", "--model", "sonnet"]
    assert "Swedish" in argv[argv.index("--system-prompt") + 1]
    assert argv[argv.index("--tools") + 1] == ""
    assert "<transcript>\nhej\n</transcript>" in call["stdin"]


def test_claude_backend_reports_errors(tmp_path, monkeypatch):
    _fake_claude(
        tmp_path,
        monkeypatch,
        """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
""",
    )
    with pytest.raises(llm.LLMError, match="Not logged in"):
        llm.polish("x", "en", model="sonnet", backend=llm.CLAUDE)


def test_claude_check_needs_cli(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(llm.LLMError, match="claude"):
        llm.check("", "sonnet", backend=llm.CLAUDE)


def test_describe():
    assert llm.describe(llm.CLAUDE, "sonnet") == "sonnet (claude -p)"
    assert llm.describe(llm.OLLAMA, "qwen3.5:9b") == "qwen3.5:9b"
    assert llm.describe(llm.COPILOT, "") == "copilot -p"
    assert llm.describe(llm.COPILOT, "gpt-5.4") == "gpt-5.4 (copilot -p)"


def test_copilot_backend_puts_everything_in_the_prompt(tmp_path, monkeypatch):
    log = tmp_path / "args.json"
    _fake_claude(
        tmp_path,
        monkeypatch,
        f"""
import json, sys
json.dump(sys.argv[1:], open({str(log)!r}, "w"))
print("```markdown")
print("# Title")
print("")
print("Text.")
print("```")
""",
        name="copilot",
    )
    seen = []
    out = llm.summarize("we ship friday", "en", model="", on_token=seen.append, backend=llm.COPILOT)
    assert out == "# Title\n\nText."
    assert "".join(seen).startswith("```markdown\n# Title")
    argv = json.loads(log.read_text())
    assert argv[0] == "-p" and "-s" in argv and "--no-ask-user" in argv
    assert "--no-custom-instructions" in argv and "--disable-builtin-mcps" in argv
    assert "--model" not in argv  # no model given: Copilot's default
    prompt = argv[1]
    assert prompt.index("Action items") < prompt.index("<transcript>\nwe ship friday")
    assert "--allow-all-tools" not in argv


def test_copilot_backend_passes_model_and_reports_failure(tmp_path, monkeypatch):
    log = tmp_path / "args.json"
    _fake_claude(
        tmp_path,
        monkeypatch,
        f"""
import json, sys
json.dump(sys.argv[1:], open({str(log)!r}, "w"))
print("Error: not authenticated", file=sys.stderr)
sys.exit(1)
""",
        name="copilot",
    )
    with pytest.raises(llm.LLMError, match="not authenticated"):
        llm.polish("x", "en", model="gpt-5.4", backend=llm.COPILOT)
    argv = json.loads(log.read_text())
    assert argv[argv.index("--model") + 1] == "gpt-5.4"


def test_parse_tags_json_and_fallbacks():
    assert llm.parse_tags('["Q4 Budget", "#hiring", "q4-budget", "Kund_Möte"]') == [
        "q4-budget",
        "hiring",
        "kund-möte",
    ]
    assert llm.parse_tags('```json\n["release"]\n```') == ["release"]
    assert llm.parse_tags("roadmap, ci/cd") == ["roadmap", "ci-cd"]
    assert llm.parse_tags("- roadmap\n- ci/cd") == ["roadmap", "ci-cd"]
    assert len(llm.parse_tags(json.dumps([f"t{i}" for i in range(20)]))) == llm.MAX_TAGS


def test_tags_prompt_passes_known_tags_and_language():
    msgs = llm.build_tags_messages("hej", "sv", ["q4-budget", "hiring"])
    assert "Swedish" in msgs[0]["content"]
    assert "Existing tags: q4-budget, hiring" in msgs[1]["content"]
    assert "(none yet)" in llm.build_tags_messages("x", "en")[1]["content"]


def test_extract_tags_rejects_empty(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "[]")
    with pytest.raises(llm.LLMError):
        llm.extract_tags("x", "en")
