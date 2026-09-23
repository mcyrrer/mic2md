"""LLM passes: polish the transcript into Markdown, and summarize it as meeting notes.

Backends: a local Ollama server (default), Claude via `claude -p`, or GitHub Copilot via
`copilot -p`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable

import httpx

OLLAMA = "ollama"
CLAUDE = "claude"
COPILOT = "copilot"
BACKENDS = (OLLAMA, CLAUDE, COPILOT)
CLI_INSTALL = {
    CLAUDE: "Install Claude Code and log in (https://claude.com/claude-code).",
    COPILOT: "Install GitHub Copilot CLI (`npm install -g @github/copilot`) and run "
    "`copilot login`.",
}
DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_URL = "http://localhost:11434"

LANGUAGE_NAMES = {"en": "English", "sv": "Swedish"}

SYSTEM_PROMPT = """\
You are an editor that turns raw speech-to-text transcripts into clean Markdown documents.

Rules:
- Correct spelling, grammar and punctuation.
- Remove filler words, false starts and accidental repetitions.
- Fix words the speech recognizer obviously misheard when the intended word is clear from context.
- Keep the original language ({language}). Never translate.
- Preserve the speaker's meaning, tone and wording as closely as possible. Do not add \
information, opinions or a summary.
- Never drop content: every sentence the speaker said must still be present in the body \
text, even introductory ones. The title does not replace any sentence.
- Structure the text: start with a short `# ` title that fits the content, split it into \
paragraphs, use `## ` headings for clearly distinct topics, and bullet or numbered lists when \
the speaker enumerates items. Use **bold** sparingly.
- If the speaker dictates formatting (e.g. "new paragraph", "bullet point", "heading"), apply \
it instead of writing the words.
- `(?)` right after a word means the speech recognizer was unsure of it. Replace the word with \
what was most likely said, using the context (and the names and terms listed, if any), and \
always drop the `(?)` marker.
- Lines may start with a `[HH:MM:SS]` timestamp (time into the recording). Don't keep them on \
every sentence. Instead, end each `## ` heading with the time its section starts, as \
`(HH:MM:SS)`, e.g. `## Budget (00:12:31)`. Without headings, put it at the end of the first \
line of each paragraph that starts a new topic. The `# ` title gets no time. Never invent or \
change times.
- Output only the Markdown document: no preamble, no explanations, no code fences."""


SUMMARY_PROMPT = """\
You are an assistant that turns meeting transcripts into clear, actionable notes.

Read the transcript and produce the following sections, as Markdown with `## ` headings. \
Write the headings and all content in {language}.

1. Executive summary
   Max 3 bullet points or 60 words, written for a senior stakeholder who won't read the rest.
   Cover: what was decided, what it means for the business/project, and what (if anything) \
needs their attention or approval.
   No background, no meeting mechanics, no names unless essential.

2. Detailed summary
   3–6 sentences covering the purpose of the meeting, the main topics discussed, and the \
overall outcome.

3. Key decisions
   A list of decisions that were actually made. Only include things that were agreed on, not \
ideas that were only floated. If no decisions were made, write "None recorded."

4. Action items
   A Markdown table with these columns:
   | # | Action | Owner | Deadline | Context/notes |
   - Owner: the person responsible. If it's unclear, write "Unassigned".
   - Deadline: only if one was mentioned, otherwise "Not set".
   - Phrase each action as a concrete task that starts with a verb (e.g. "Send the updated \
budget to finance").
   - Include implicit commitments too ("I'll look into that" = action item).

5. Open questions / unresolved issues
   Topics that were raised but not resolved, or that need follow-up.

6. Risks or blockers
   Anything that was mentioned as a dependency, concern, or obstacle. Leave this section out \
if there are none.

Rules:
- Only use information from the transcript. Do not invent names, dates, or details.
- If the transcript has times like `[00:12:31]` or `(00:12:31)`, add the time where a decision \
or action item was discussed, e.g. "(00:12:31)" in the Context/notes column.
- If the transcript is unclear or the speaker can't be identified, flag it with "[unclear]".
- Ignore small talk, filler words, and off-topic tangents.
- Keep it concise. Someone who wasn't in the meeting should understand the result in under \
2 minutes.
- Output only these sections: no title, no preamble, no explanations, no code fences."""


TAGS_PROMPT = """\
You pick tags for meeting notes so that notes about the same topic can be found together later.

Read the transcript and return 3 to 8 tags for its most important topics: projects, products, \
customers, teams, technologies, decisions areas and recurring themes. Prefer specific topics \
over generic words ("q4-budget" rather than "meeting", "discussion", "update", "notes").

Rules:
- Lowercase, words joined with hyphens, no spaces and no "#" (e.g. "release-planning").
- Write tags in {language}, except names of products, companies and technologies.
- Only topics that are actually discussed in the transcript. No people's names.
- If one of the existing tags below fits a topic, use it exactly instead of inventing a variant.
- Output only a JSON array of strings, e.g. ["q4-budget", "hiring"]. No other text."""

MAX_TAGS = 8


class LLMError(RuntimeError):
    pass


OllamaError = LLMError  # older name


def default_model(backend: str) -> str:
    """Ollama and Claude get a fixed default; Copilot uses whatever its CLI defaults to."""
    return {OLLAMA: DEFAULT_MODEL, CLAUDE: DEFAULT_CLAUDE_MODEL}.get(backend, "")


def describe(backend: str, model: str) -> str:
    """How the model is named in the UI and in front matter."""
    if backend == OLLAMA:
        return model
    return f"{model} ({backend} -p)" if model else f"{backend} -p"


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if "://" in url else f"http://{url}"


def check(url: str, model: str, timeout: float = 3.0, backend: str = OLLAMA) -> None:
    """Raise LLMError with an actionable message if the backend or model is unavailable."""
    if backend in CLI_INSTALL:
        if shutil.which(backend) is None:
            raise LLMError(f"The `{backend}` CLI was not found on PATH. {CLI_INSTALL[backend]}")
        return
    url = normalize_url(url)
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise OllamaError(
            f"Ollama is not reachable at {url} ({e}). Start it with `ollama serve`."
        ) from e
    names = {m.get("name", "") for m in resp.json().get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        raise OllamaError(f"Ollama model {model!r} is not installed. Run `ollama pull {model}`.")


def strip_wrapping(text: str) -> str:
    """Remove <think> blocks and a surrounding ```markdown fence if the model added them."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if m := re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, flags=re.S):
        text = m.group(1).strip()
    return text


_TITLE_TIME = re.compile(r"^(# [^\n]*?)\s*\(\d{2}:\d{2}:\d{2}\)[ \t]*$", re.M)


def drop_title_time(text: str) -> str:
    """Models like to put the first section's time on the `# ` title as well; remove it there."""
    return _TITLE_TIME.sub(r"\1", text, count=1)


def terms_rule(terms: list[str] | None) -> str:
    """Extra prompt rule for glossary terms and names (empty when there are none)."""
    if not terms:
        return ""
    return (
        "\n- Spell these names and terms exactly like this when they occur (the speech "
        "recognizer may have misheard them): " + ", ".join(terms) + "."
    )


def build_messages(
    transcript: str, language: str, terms: list[str] | None = None
) -> list[dict[str, str]]:
    lang = LANGUAGE_NAMES.get(language, language)
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(language=lang) + terms_rule(terms)},
        {"role": "user", "content": f"Transcript:\n\n<transcript>\n{transcript}\n</transcript>"},
    ]


def build_summary_messages(
    transcript: str,
    language: str,
    meeting: str = "",
    participants: str = "",
    terms: list[str] | None = None,
) -> list[dict[str, str]]:
    lang = LANGUAGE_NAMES.get(language, language)
    context = ""
    if meeting:
        context += f"Meeting: {meeting}\n"
    if participants:
        context += f"Invited participants (from the calendar): {participants}\n"
    return [
        {"role": "system", "content": SUMMARY_PROMPT.format(language=lang) + terms_rule(terms)},
        {
            "role": "user",
            "content": f"{context}Transcript:\n\n<transcript>\n{transcript}\n</transcript>",
        },
    ]


def build_tags_messages(
    transcript: str, language: str, known_tags: list[str] | None = None
) -> list[dict[str, str]]:
    lang = LANGUAGE_NAMES.get(language, language)
    known = ", ".join(known_tags or []) or "(none yet)"
    return [
        {"role": "system", "content": TAGS_PROMPT.format(language=lang)},
        {
            "role": "user",
            "content": f"Existing tags: {known}\n\nTranscript:\n\n<transcript>\n{transcript}\n"
            "</transcript>",
        },
    ]


def normalize_tag(tag: str) -> str:
    """``"#Q4 Budget"`` → ``"q4-budget"``; keeps letters (incl. å/ä/ö), digits and hyphens."""
    tag = re.sub(r"[\s_/]+", "-", tag.strip().lstrip("#").lower())
    tag = re.sub(r"[^\w-]", "", tag).replace("_", "-")
    return re.sub(r"-{2,}", "-", tag).strip("-")


def parse_tags(text: str) -> list[str]:
    """Tags from the model's answer: a JSON array, or failing that a comma/line list."""
    text = strip_wrapping(text)
    if m := re.search(r"\[.*\]", text, re.S):
        try:
            items = json.loads(m.group(0))
        except json.JSONDecodeError:
            items = re.split(r"[,\n]", m.group(0).strip("[]"))
    else:
        items = re.split(r"[,\n]", text)
    tags: list[str] = []
    for item in items:
        tag = normalize_tag(str(item).strip().strip("\"'`-* "))
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:MAX_TAGS]


def extract_tags(
    transcript: str,
    language: str,
    known_tags: list[str] | None = None,
    model: str = DEFAULT_MODEL,
    url: str = DEFAULT_URL,
    on_token: Callable[[str], None] | None = None,
    timeout: float = 300.0,
    backend: str = OLLAMA,
) -> list[str]:
    """Ask the model for topic tags; reuses ``known_tags`` from other notes when they fit."""
    messages = build_tags_messages(transcript, language, known_tags)
    tags = parse_tags(chat(messages, model, url, on_token, timeout, backend))
    if not tags:
        raise LLMError("The model returned no usable tags.")
    return tags


def polish(
    transcript: str,
    language: str,
    model: str = DEFAULT_MODEL,
    url: str = DEFAULT_URL,
    on_token: Callable[[str], None] | None = None,
    timeout: float = 600.0,
    backend: str = OLLAMA,
    terms: list[str] | None = None,
) -> str:
    """Stream a polished Markdown version of ``transcript``."""
    messages = build_messages(transcript, language, terms)
    return drop_title_time(chat(messages, model, url, on_token, timeout, backend))


def summarize(
    transcript: str,
    language: str,
    meeting: str = "",
    participants: str = "",
    model: str = DEFAULT_MODEL,
    url: str = DEFAULT_URL,
    on_token: Callable[[str], None] | None = None,
    timeout: float = 600.0,
    backend: str = OLLAMA,
    terms: list[str] | None = None,
) -> str:
    """Stream meeting notes (summary, decisions, action items…) for ``transcript``."""
    messages = build_summary_messages(transcript, language, meeting, participants, terms)
    return chat(messages, model, url, on_token, timeout, backend)


def chat(
    messages: list[dict[str, str]],
    model: str,
    url: str,
    on_token: Callable[[str], None] | None = None,
    timeout: float = 600.0,
    backend: str = OLLAMA,
) -> str:
    """Stream one completion from the chosen backend; return the cleaned-up full response."""
    if backend == CLAUDE:
        return _claude_chat(messages, model, on_token, timeout)
    if backend == COPILOT:
        return _copilot_chat(messages, model, on_token, timeout)
    return _ollama_chat(messages, model, url, on_token, timeout)


def claude_command(system: str, model: str) -> list[str]:
    """A bare, tool-less `claude -p` call: no tools, MCP servers, settings, skills or session."""
    return [
        "claude",
        "-p",
        "--model",
        model,
        "--system-prompt",
        system,
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]


def parse_claude_event(line: str) -> tuple[str | None, dict | None]:
    """(text delta, final result event) from one stream-json line; either may be None."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    if event.get("type") == "result":
        return None, event
    if event.get("type") == "stream_event":
        inner = event.get("event", {})
        delta = inner.get("delta", {})
        if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return delta.get("text", ""), None
    return None, None


def _run_cli(
    name: str,
    cmd: list[str],
    stdin: str | None,
    on_line: Callable[[str], None],
    timeout: float,
) -> tuple[int, str]:
    """Run an LLM CLI from an empty temp folder, feeding ``stdin`` and handing each stdout line
    to ``on_line``. Returns (exit code, stderr). The empty folder keeps project instruction
    files (CLAUDE.md, AGENTS.md, .github/copilot-instructions.md…) out of the context."""
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            raise LLMError(f"Could not start `{name}`: {e}") from e
        err: list[str] = []
        timer = threading.Timer(timeout, proc.kill)
        timer.start()

        # stdin and stderr run in threads so a long transcript or chatty stderr can't
        # deadlock against the stdout reader.
        def feed() -> None:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threads = [threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)]
        if stdin is not None:
            threads.append(threading.Thread(target=feed, daemon=True))
        for t in threads:
            t.start()
        try:
            for line in proc.stdout:
                on_line(line)
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            threads[0].join(timeout=1)
    return proc.returncode, "".join(err).strip()


def _claude_chat(
    messages: list[dict[str, str]],
    model: str,
    on_token: Callable[[str], None] | None,
    timeout: float,
) -> str:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    prompt = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
    parts: list[str] = []
    result: dict | None = None

    def on_line(line: str) -> None:
        nonlocal result
        text, final = parse_claude_event(line)
        if final is not None:
            result = final
        elif text:
            parts.append(text)
            if on_token:
                on_token(text)

    code, stderr = _run_cli("claude", claude_command(system, model), prompt, on_line, timeout)
    if result is None or result.get("is_error"):
        detail = (result or {}).get("result") or stderr or f"exit code {code}"
        raise LLMError(f"claude -p failed: {detail}")
    text = strip_wrapping("".join(parts) or result.get("result", ""))
    if not text:
        raise LLMError("claude -p returned an empty response.")
    return text


def copilot_prompt(messages: list[dict[str, str]]) -> str:
    """Copilot CLI has no system-prompt flag, so instructions and transcript go in one prompt."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
    return (
        f"{system}\n\nDo not use any tools, run commands or read or write files. "
        f"Reply with the requested Markdown only.\n\n{user}"
    )


def copilot_command(prompt: str, model: str) -> list[str]:
    """Non-interactive `copilot -p`: only the answer (-s), no questions, no AGENTS.md or
    built-in GitHub MCP server.

    No tools are pre-approved (never --allow-all-tools), so in -p mode any tool call is
    refused rather than run. Copilot ignores stdin with -p, so the transcript is in ``prompt``.
    """
    cmd = [
        "copilot",
        "-p",
        prompt,
        "-s",
        "--no-ask-user",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-color",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _copilot_chat(
    messages: list[dict[str, str]],
    model: str,
    on_token: Callable[[str], None] | None,
    timeout: float,
) -> str:
    # Plain text output (-s); the JSONL event format isn't documented, so lines stream as-is.
    parts: list[str] = []

    def on_line(line: str) -> None:
        parts.append(line)
        if on_token:
            on_token(line)

    code, stderr = _run_cli(
        "copilot", copilot_command(copilot_prompt(messages), model), None, on_line, timeout
    )
    if code != 0:
        raise LLMError(f"copilot -p failed: {stderr or ''.join(parts).strip() or f'exit {code}'}")
    text = strip_wrapping("".join(parts))
    if not text:
        raise LLMError("copilot -p returned an empty response.")
    return text


def _ollama_chat(
    messages: list[dict[str, str]],
    model: str,
    url: str,
    on_token: Callable[[str], None] | None,
    timeout: float,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": 16384},
    }
    parts: list[str] = []
    try:
        with httpx.stream(
            "POST", f"{normalize_url(url)}/api/chat", json=payload, timeout=timeout
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise OllamaError(f"Ollama returned {resp.status_code}: {resp.text.strip()}")
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if err := chunk.get("error"):
                    raise OllamaError(f"Ollama error: {err}")
                token = chunk.get("message", {}).get("content", "")
                if token:
                    parts.append(token)
                    if on_token:
                        on_token(token)
                if chunk.get("done"):
                    break
    except httpx.HTTPError as e:
        raise OllamaError(f"Ollama request failed: {e}") from e
    result = strip_wrapping("".join(parts))
    if not result:
        raise OllamaError("Ollama returned an empty response.")
    return result
