"""Non-blocking key presses from the terminal while recording (for typed notes)."""

from __future__ import annotations

import codecs
import os
import select
import sys

ENTER = "enter"
BACKSPACE = "backspace"
ESC = "esc"
CLEAR = "clear"


def parse_keys(text: str) -> list[str]:
    """Split terminal input into printable characters and named keys.

    Escape sequences (arrow keys, function keys) are dropped; a lone ESC is ``ESC``.
    """
    keys: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        i += 1
        if ch in "\r\n":
            keys.append(ENTER)
        elif ch in "\x7f\x08":
            keys.append(BACKSPACE)
        elif ch == "\x15":  # Ctrl+U
            keys.append(CLEAR)
        elif ch == "\x1b":
            if i < len(text) and text[i] in "[O":
                # CSI/SS3 sequence: parameters, then one final byte in @..~.
                i += 1
                while i < len(text) and not "@" <= text[i] <= "~":
                    i += 1
                i += 1
            elif i < len(text) and text[i] != "\x1b":
                i += 1  # Alt+key
            else:
                keys.append(ESC)
        elif ch == "\t":
            keys.append(" ")
        elif ch.isprintable():
            keys.append(ch)
    return keys


class KeyReader:
    """Puts a terminal stdin in cbreak mode (Ctrl+C still interrupts) and reads keys.

    Without a terminal it does nothing and :meth:`read` returns no keys.
    """

    def __init__(self) -> None:
        self._fd: int | None = None
        self._saved = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    def __enter__(self) -> KeyReader:
        try:
            if sys.stdin.isatty():
                import termios
                import tty

                fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                self._fd = fd
        except (OSError, ValueError, ImportError):
            self._fd = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None and self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        self._fd = None

    def read(self) -> list[str]:
        if self._fd is None:
            return []
        data = b""
        while select.select([self._fd], [], [], 0)[0]:
            chunk = os.read(self._fd, 1024)
            if not chunk:
                break
            data += chunk
        return parse_keys(self._decoder.decode(data)) if data else []
