# Security

## Overview / threat model

`mic2md` is a single-user local CLI, not a service: no network listener, no
authentication/authorization surface, no multi-tenant data. It runs on a developer's own
Mac, reads the mic and (optionally) the local calendar, writes Markdown files to the local
filesystem, and calls out to one LLM backend chosen by the user. The relevant threats are
therefore not "attacker reaches this process over the network" but (1) sensitive audio/text
leaving the machine when the user opts into a cloud backend, (2) other local users/processes
observing that data in transit on the same machine, and (3) capturing other people's speech
without their knowledge.

## Data sensitivity

- **Audio**: raw mic input, only ever held in memory (`audio.py`), never written to disk.
- **Transcripts**: full text of what was said, written to disk immediately
  (`writer.SessionWriter.append`) and kept until a polish pass succeeds, per the
  "never lose transcript text" invariant in `CLAUDE.md`.
- **Calendar metadata** (`meetings.py`): meeting title and attendee names/emails, read via
  EventKit and folded into front matter and into every LLM prompt (`llm.build_summary_messages`,
  `terms_rule`). This is personal data about people who are not the CLI's user and who have
  not consented to any of this.
- **Glossary/vocabulary**: user-supplied terms, low sensitivity, but also flows into every
  prompt sent to whichever backend is active.

## Backend trust boundary — the central design decision

This is the one security-relevant choice `mic2md` exposes to the user, via `--backend`:

- **Ollama (default, local)**: `llm._ollama_chat` posts to `http://localhost:11434` only.
  Nothing leaves the machine. This is the safe default.
- **`claude -p` / `copilot -p` (opt-in, cloud)**: `llm._claude_chat` / `llm._copilot_chat`
  hand the *entire* transcript — raw meeting speech, attendee names, glossary terms — to a
  third-party CLI that talks to a cloud API over the network. Choosing `--backend claude` or
  `--backend copilot` is an explicit, informed trade of privacy for quality, but it is a real
  data-exfiltration-shaped action: once invoked, the recording's content and the meeting's
  attendee list leave the local machine and become subject to that provider's own data
  handling/retention policies, which mic2md has no control over.

Mitigations already in place for the cloud path:
- Both CLIs are invoked from an **empty temporary directory** (`llm._run_cli`), so no
  `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, or other project files are
  picked up and leaked into the prompt or executed.
- Both are run **tool-less**: `claude_command` passes `--tools ""`, `--strict-mcp-config`,
  `--setting-sources ""`, `--disable-slash-commands`, `--no-session-persistence`; and
  `copilot_command` passes `--no-ask-user --no-custom-instructions
  --disable-builtin-mcps` and never `--allow-all-tools`, plus an explicit "do not use tools,
  run commands, or read/write files" instruction is prepended to the Copilot prompt
  (`copilot_prompt`). This limits (but does not eliminate — it's still a text instruction to
  an LLM) the blast radius if the transcript ever contained an injected instruction.
- No session persistence, no state kept between calls.

This is a design boundary the codebase makes clearly visible (an explicit CLI flag with
documented behavior in `CLAUDE.md`), not something hidden — but it is worth flagging
explicitly to anyone deciding which backend to default their team to.

## Subprocess invocation security (argv vs stdin)

Checked directly in `src/mic2md/llm.py`:

- **`claude -p`**: the transcript is sent over **stdin** (`_claude_chat` passes `prompt` as
  the `stdin` argument to `_run_cli`, which writes it via `proc.stdin.write(stdin)`). The
  system prompt (fixed text, no user data) is passed via the `--system-prompt` argv flag.
  This is the safer pattern: transcript content is not visible in `ps`/`/proc/<pid>/cmdline`
  to other local users.
- **`copilot -p`**: the transcript **is** passed via **argv** — `copilot_prompt()` builds one
  combined string (system instructions + transcript) and `copilot_command` puts it directly
  in the `-p PROMPT` argument, because "Copilot ignores stdin with `-p`" (comment in the code,
  confirmed against Copilot CLI 1.0.88 behavior). This means the full transcript text —
  including meeting title, attendee names, and everything said in the meeting — is visible
  for the process's lifetime to any other local user or process able to read `ps aux` /
  `/proc/<pid>/cmdline` on the same machine. On a genuinely single-user personal laptop this
  is low risk; on a shared or multi-user Mac (e.g. a shared build machine, or under EDR/process
  monitoring that logs command lines), this is a real local information-disclosure risk. This
  is a constraint of the Copilot CLI's own interface, not a bug in mic2md, but it is the most
  concrete finding in this review and worth documenting so users on shared machines can weigh
  it before choosing `--backend copilot`.

## Supply chain

- **Model downloads** (`models.py`): `ensure_model`/`download` fetch `.bin` ggml model files
  over HTTPS from Hugging Face (`https://huggingface.co/...`) with `follow_redirects=True`,
  streamed to a `.part` file and atomically renamed into place on success. **There is no
  integrity verification** — no checksum, no signature check against the downloaded bytes.
  Anyone able to MITM the connection (TLS aside) or compromise the upstream HF repo could
  substitute a malicious model file; whisper.cpp then loads it locally. This is a real, if
  currently unexploited, supply-chain gap; adding a pinned SHA256 per `ModelSpec` and
  verifying it after download would close it cheaply.
- **Dependency pinning** (`pyproject.toml`): dependencies use loose lower bounds only
  (`pywhispercpp>=1.3`, `sounddevice>=0.5`, `httpx>=0.27`, `typer>=0.12`, etc.), no upper
  bounds or hash pinning in the `pyproject.toml` itself; reproducibility for exact versions
  relies on `uv.lock` (not reviewed here) rather than the manifest.

## Secrets and credentials

Confirmed: mic2md itself stores no secrets or credentials.
- Ollama needs no API key (plain local HTTP).
- `claude -p` and `copilot -p` rely entirely on those CLIs' own separately-configured
  authentication (`claude` login / `copilot login`, per `CLI_INSTALL` messages in `llm.py`);
  mic2md never reads, stores, or passes credentials itself, and doesn't need any
  `--api-key`-style flag.
- No `.env`, keychain access, or credential files appear anywhere in the source.

## Compliance notes — recording other people without consent

This is a genuine, non-hypothetical concern for a meeting-dictation tool, not a hypothetical
one for a lawyer to wave at: `mic2md` records a live meeting's audio and, when EventKit
lookup succeeds, automatically attaches other attendees' names and email-derived labels
(`meetings.participant_label`) to the resulting Markdown notes and to every prompt sent to
the LLM backend, including cloud backends. Whether this requires the other participants'
consent depends on the user's jurisdiction (e.g. one-party vs. two-party/all-party consent
for recording conversations in the US; GDPR's lawful-basis and data-minimization
requirements in the EU/UK for recording and processing other identifiable people's speech
and calendar data) and their organization's own recording/AI-tooling policy. The tool itself
has no consent-capture, redaction, or attendee-opt-out mechanism — this is a user
responsibility, not something the codebase currently enforces, and should be flagged
particularly when `--backend claude`/`--backend copilot` sends that data to a third party.

## What's NOT a risk here, and why

- **No network listener**: mic2md is a CLI invoked interactively; it never binds a port or
  accepts inbound connections, so there is no remote attack surface into the process itself.
- **No auth/authz needed**: single local user, single local process, local filesystem
  permissions are the only relevant boundary (standard OS file permissions on
  `<output_dir>/transcripts/` apply, nothing app-specific).
- **No multi-tenant data**: one user's transcripts never mix with another's; there's no
  shared server-side state to leak across users.
- **Atomic writes** (`writer.write_final`, `insert_summary`, `update_front_matter`,
  `import_document`, and `models.download`'s `.part` file) all write to a `.tmp`/`.part`
  sibling and `Path.replace()` into place, so a crash mid-write can't corrupt or truncate an
  existing transcript — a reliability property that also has a security-adjacent benefit
  (no half-written file an attacker/race could exploit).
