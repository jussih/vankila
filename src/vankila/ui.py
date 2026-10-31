"""Terminal output: quiet by default, colour only on a TTY."""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time
import unicodedata


def clean(text: str) -> str:
    """Make untrusted text safe to print: control and invisible characters become visible.

    Covers terminal escapes (OSC 52 can write the clipboard) and bidi overrides,
    which can make a line display differently from what it really says.
    """
    out = []
    for char in text:
        if char != "\t" and unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp"):
            code = ord(char)
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def _color(code: str, text: str) -> str:
    if not sys.stderr.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _color("1", text)


def dim(text: str) -> str:
    return _color("2", text)


def green(text: str) -> str:
    return _color("32", text)


def yellow(text: str) -> str:
    return _color("33", text)


def red(text: str) -> str:
    return _color("31", text)


def say(text: str = "") -> None:
    print(text, file=sys.stderr)


def info(text: str) -> None:
    say(f"{dim('vk')} {text}")


def ok(text: str) -> None:
    say(f"{green('✓')} {text}")


def warn(text: str) -> None:
    say(f"{yellow('!')} {text}")


class Fail(Exception):
    """A user-facing error: printed without a traceback."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class Spinner:
    """A one-line status that can be updated while work runs."""

    def __init__(self, text: str):
        self.text = text
        self.start_time = time.monotonic()
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self) -> "Spinner":
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            info(self.text)
        return self

    def update(self, text: str) -> None:
        self.text = text
        if not self._thread:
            info(text)

    def _spin(self) -> None:
        for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.wait(0.08):
                return
            elapsed = int(time.monotonic() - self.start_time)
            suffix = dim(f" {elapsed}s") if elapsed >= 2 else ""
            sys.stderr.write(f"\r\033[K{frame} {self.text}{suffix}")
            sys.stderr.flush()

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def confirm(question: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say()
        return False
    return default if not answer else answer in ("y", "yes")
