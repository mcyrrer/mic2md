"""Whisper model registry, cache and downloader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

HF_BASE = "https://huggingface.co"


@dataclass(frozen=True)
class ModelSpec:
    language: str
    size: str
    repo: str
    remote_file: str
    local_file: str

    @property
    def url(self) -> str:
        return f"{HF_BASE}/{self.repo}/resolve/main/{self.remote_file}"


def _openai(size: str, remote_file: str) -> ModelSpec:
    return ModelSpec("en", size, "ggerganov/whisper.cpp", remote_file, remote_file)


def _kb(size: str) -> ModelSpec:
    # KBLab publishes every size under the same file name, so give each a unique local name.
    return ModelSpec(
        "sv",
        size,
        f"KBLab/kb-whisper-{size}",
        "ggml-model-q5_0.bin",
        f"ggml-kb-whisper-{size}-q5_0.bin",
    )


REGISTRY: dict[str, dict[str, ModelSpec]] = {
    "en": {
        "base.en": _openai("base.en", "ggml-base.en-q5_1.bin"),
        "small.en": _openai("small.en", "ggml-small.en-q5_1.bin"),
        "medium.en": _openai("medium.en", "ggml-medium.en-q5_0.bin"),
        "large-v3-turbo": _openai("large-v3-turbo", "ggml-large-v3-turbo-q5_0.bin"),
    },
    "sv": {
        "small": _kb("small"),
        "medium": _kb("medium"),
        "large": _kb("large"),
    },
}

DEFAULT_SIZE = {"en": "large-v3-turbo", "sv": "large"}


def cache_dir() -> Path:
    env = os.environ.get("MIC2MD_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "mic2md" / "models"


def get_spec(language: str, size: str | None = None) -> ModelSpec:
    if language not in REGISTRY:
        raise ValueError(f"Unsupported language {language!r}. Choose from: {', '.join(REGISTRY)}")
    size = size or DEFAULT_SIZE[language]
    sizes = REGISTRY[language]
    if size not in sizes:
        raise ValueError(
            f"Unknown model size {size!r} for language {language!r}. "
            f"Choose from: {', '.join(sizes)}"
        )
    return sizes[size]


def ensure_model(spec: ModelSpec, console: Console | None = None) -> Path:
    """Return the local path for ``spec``, downloading it first if it is not cached."""
    target = cache_dir() / spec.local_file
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    download(spec.url, target, console or Console(stderr=True))
    return target


def download(url: str, target: Path, console: Console) -> None:
    partial = target.with_name(target.name + ".part")
    console.print(f"[bold]Downloading Whisper model[/] {target.name}\n[dim]{url}[/]")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0)) or None
            with (
                Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    TimeRemainingColumn(),
                    console=console,
                ) as progress,
                partial.open("wb") as fh,
            ):
                task = progress.add_task(target.name, total=total)
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
                    progress.update(task, advance=len(chunk))
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
