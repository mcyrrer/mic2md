"""Live terminal view: finished lines scroll up, the current partial and a status bar stay below."""

from __future__ import annotations

import math
import re
import time

from rich.console import Group
from rich.text import Text

BARS = " ▁▂▃▄▅▆▇█"


def level_meter(level: float, speaking: bool, width: int = 8) -> Text:
    """Log-scaled input meter; green while speech is detected."""
    # -60 dBFS .. 0 dBFS mapped onto the meter width.
    db = 20 * math.log10(level) if level > 0 else -60.0
    filled = max(0.0, min(1.0, (db + 60) / 60)) * width
    chars = "".join(
        BARS[round(max(0.0, min(1.0, filled - i)) * (len(BARS) - 1))] for i in range(width)
    )
    return Text(chars, style="green" if speaking else "grey50")


UNCERTAIN_STYLE = "underline yellow"


def highlight_uncertain(text: str, uncertain: set[str]) -> Text:
    """``text`` with the words Whisper was unsure of underlined (keys as in ``word_key``)."""
    out = Text(text)
    for m in re.finditer(r"[\w'’-]+", text):
        if re.sub(r"\W", "", m[0].lower()) in uncertain:
            out.stylize(UNCERTAIN_STYLE, m.start(), m.end())
    return out


class LiveView:
    def __init__(self, language: str, model_name: str) -> None:
        self.language = language
        self.model_name = model_name
        self.started = time.monotonic()
        self.partial = ""
        self.level = 0.0
        self.speaking = False
        self.calibrated = False
        self.busy = False

    def elapsed(self) -> str:
        s = int(time.monotonic() - self.started)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    def __rich__(self) -> Group:
        parts = []
        if self.partial:
            parts.append(Text(self.partial, style="italic grey62"))
        status = Text()
        status.append(" ● REC ", style="bold white on red")
        status.append(f" {self.elapsed()}  ", style="bold")
        status.append(f"{self.language} · {self.model_name}  ", style="cyan")
        if self.calibrated:
            status.append("mic ", style="dim")
            status.append_text(level_meter(self.level, self.speaking))
        else:
            status.append("calibrating — stay quiet…", style="yellow")
        if self.busy:
            status.append("  ✎", style="magenta")
        status.append("   Ctrl+C to stop", style="dim")
        parts.append(status)
        return Group(*parts)
