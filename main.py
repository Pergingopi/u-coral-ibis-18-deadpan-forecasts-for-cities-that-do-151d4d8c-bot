#!/usr/bin/env python3
"""Deadpan forecast for a city that does not exist — make.fun entry.

This program is a one-line weather forecast writer. It checks a single
forecast against the bounty gate (one line, 1-160 characters, no
invisible or forbidden characters) and then publishes it to
``$MAKEFUN_OUTPUT_DIR/entry.txt`` (default ``/output/entry.txt``).

Publishing is atomic on purpose. The bytes are staged in a temporary
file that lives inside the output directory itself — never /tmp, which
sits on a different filesystem from the grader's bind-mount and would
make os.replace fail with EXDEV. The staged file is fsynced, given
mode 0644, moved into place with os.replace, and the result is checked
by reading the file back and running the gate over it once more.
"""

import os
import sys

FORECAST = ("In Glimmerwick, expect 4 cm of sideways rain, a high of 11, "
            "and fog thick enough to lose the mayor in. Umbrellas "
            "optional; dignity not guaranteed.")

MAX_CHARS = 160
OUTPUT_NAME = "entry.txt"
STAGING_NAME = ".entry.txt.tmp"


# ---------------------------------------------------------------------------
# What a clean forecast line may contain
# ---------------------------------------------------------------------------

def _forbidden_codepoint(code: int):
    """Return a short reason the code point is banned, or None if allowed.

    The gate is deliberately strict: a forecast is one clean line of
    printable text, so anything that could hide, split, or re-order the
    line is refused.
    """
    if code < 0x20 or code == 0x7F:
        return "C0 control or DEL"
    if 0x80 <= code <= 0x9F:
        return "C1 control"
    if code in (0xFEFF, 0x00AD):
        return "BOM or soft hyphen"
    if code in (0x2028, 0x2029):
        return "line or paragraph separator"
    if code in (0x200B, 0x200C, 0x200D, 0x2060, 0x2061, 0x2062, 0x2063,
                0x2064, 0xFFFC):
        return "invisible format character"
    if 0x200E <= code <= 0x200F or 0x202A <= code <= 0x202E:
        return "bidi control"
    if 0x2066 <= code <= 0x2069:
        return "bidi isolate control"
    if 0xD800 <= code <= 0xDFFF:
        return "surrogate"
    return None


def _reject_bad_shape(text: str) -> None:
    """Refuse blank text and text outside the 1-160 character gate.

    Blank includes whitespace-only text, and a forecast with leading or
    trailing whitespace is refused as well: the gate counts characters,
    so padding must not silently eat the budget.
    """
    if text == "" or text.strip() == "":
        raise ValueError("forecast is empty or whitespace-only")
    if text != text.strip():
        raise ValueError("forecast has leading or trailing whitespace")
    if len(text) > MAX_CHARS:
        raise ValueError(
            "forecast is %d characters, over the %d-character limit"
            % (len(text), MAX_CHARS)
        )


def _reject_line_breaks(text: str) -> None:
    """Refuse anything that would make the forecast more than one line."""
    if "\n" in text or "\r" in text:
        raise ValueError("forecast must be a single line")


def _reject_bad_characters(text: str) -> None:
    """Refuse every forbidden character, naming its code point."""
    for ch in text:
        reason = _forbidden_codepoint(ord(ch))
        if reason is not None:
            raise ValueError(
                "forecast contains a forbidden character U+%04X (%s)"
                % (ord(ch), reason)
            )


def check_forecast(text: str) -> None:
    """Raise ValueError if the text fails the gate; otherwise return None."""
    if not isinstance(text, str):
        raise ValueError("forecast is not text")
    _reject_bad_shape(text)
    _reject_line_breaks(text)
    _reject_bad_characters(text)
    # Round-trip through UTF-8 to prove it is clean encodable text.
    text.encode("utf-8")


# ---------------------------------------------------------------------------
# Getting the forecast onto disk
# ---------------------------------------------------------------------------

def locate_output_directory() -> str:
    """Work out the output directory from the environment.

    Defaults to /output. A value that points at the entry file itself
    (ending in /entry.txt) is treated as its containing directory.
    """
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    if out_dir.endswith("/" + OUTPUT_NAME) or out_dir == OUTPUT_NAME:
        out_dir = os.path.dirname(out_dir) or "."
    if not out_dir:
        out_dir = "/output"
    return os.path.normpath(out_dir)


def empty_output_directory(out_dir: str) -> None:
    """Delete everything inside out_dir, never the directory itself.

    The grader mounts this directory, so deleting the mount point would
    fail. Stale files from an interrupted earlier run are swept here too.
    """
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path, topdown=False):
                for f in files:
                    os.unlink(os.path.join(root, f))
                for d in dirs:
                    os.rmdir(os.path.join(root, d))
            os.rmdir(path)
        else:
            os.unlink(path)


def _sync_directory(out_dir: str) -> None:
    """Fsync the directory so a rename inside it is durable."""
    dir_fd = os.open(out_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _discard_stale_staging(tmp_path: str) -> None:
    """Remove a leftover staging file from a crashed earlier run."""
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def publish_entry_text(out_dir: str, data: bytes) -> str:
    """Write data to entry.txt atomically and return its path."""
    target = os.path.join(out_dir, OUTPUT_NAME)
    # Stage inside the output dir (same filesystem, so os.replace is
    # atomic). A stale staging file is discarded first so the create
    # below never inherits a half-written file.
    tmp_path = os.path.join(out_dir, STAGING_NAME)
    _discard_stale_staging(tmp_path)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target)
        os.chmod(target, 0o644)
        _sync_directory(out_dir)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def confirm_written_entry(target: str, data: bytes) -> None:
    """Read entry.txt back: exact bytes, mode 0644, and the gate again."""
    with open(target, "rb") as fh:
        back = fh.read()
    if back != data:
        raise ValueError("read-back mismatch: entry.txt differs from input")
    if (os.stat(target).st_mode & 0o777) != 0o644:
        raise ValueError("entry.txt mode is not 0644")
    check_forecast(back.decode("utf-8"))


def run() -> int:
    # FORECAST is the judged text: keep it byte-identical. Only the
    # surrounding plumbing (checks, staging, publication) may change.
    check_forecast(FORECAST)
    data = FORECAST.encode("utf-8")

    out_dir = locate_output_directory()
    os.makedirs(out_dir, exist_ok=True)
    empty_output_directory(out_dir)
    target = publish_entry_text(out_dir, data)
    confirm_written_entry(target, data)

    print("wrote %s (%d characters)" % (target, len(FORECAST)))
    return 0


if __name__ == "__main__":
    sys.exit(run())
