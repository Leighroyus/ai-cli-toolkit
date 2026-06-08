"""Shared helpers for all CLI tools."""

import sys
import json

MAX_STDIN_BYTES = 10 * 1024 * 1024  # 10 MB safety cap


def log(msg: str) -> None:
    """Write a log message to stderr (never pollutes the data pipe)."""
    print(msg, file=sys.stderr)


def emit(record: dict) -> None:
    """Write a single NDJSON record to stdout and flush immediately."""
    print(json.dumps(record), flush=True)


def read_stdin_ndjson():
    """Yield parsed JSON objects from stdin, one per line."""
    for line in sys.stdin:
        line = line.strip()
        if line:
            yield json.loads(line)


def read_stdin_text() -> str:
    """Read all of stdin as a single text string (capped at 10 MB)."""
    data = sys.stdin.read()
    if len(data) > MAX_STDIN_BYTES:
        log(f"ERROR: stdin exceeds {MAX_STDIN_BYTES:,} byte limit ({len(data):,} bytes)")
        raise SystemExit(1)
    return data


def is_interactive() -> bool:
    """Check if stdin is a TTY (interactive) vs a pipe."""
    return sys.stdin.isatty()
