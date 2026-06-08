"""Send a prompt to an LLM and emit the response."""

import subprocess
import typer
from .common import log, read_stdin_text

app = typer.Typer()

DEFAULT_TIMEOUT = 300  # 5 minutes
CHARS_PER_SECOND = 200  # ~200 chars/sec throughput (conservative)
BASE_OVERHEAD = 15  # seconds for API handshake + first token


def estimate_timeout(char_count: int) -> int:
    """Estimate a reasonable timeout based on input size.

    Heuristic derived from benchmarks:
    - ~28 chars  → 3.8s  (MiMo)   → 1.8s after overhead
    - ~1950 chars → 8.8s  (MiMo)  → 6.8s after overhead
    - ~6800 chars → 8.8s+ (MiMo)  → scales ~200 chars/sec
    """
    return max(30, BASE_OVERHEAD + char_count // CHARS_PER_SECOND)


@app.command()
def main(
    model: str = typer.Option("openrouter/xiaomi/mimo-v2.5-pro", "--model", "-m", help="Model to use"),
    system: str = typer.Option(None, "--system", "-s", help="System prompt"),
    timeout: int = typer.Option(0, "--timeout", "-T", help="Subprocess timeout in seconds (0 = auto based on input size)"),
):
    """Read a prompt from stdin, send it to an LLM, and print the response."""
    prompt = read_stdin_text()
    if not prompt.strip():
        log("ERROR: empty prompt on stdin")
        raise typer.Exit(code=1)

    log(f"sending {len(prompt)} chars to {model}")

    if timeout <= 0:
        timeout = estimate_timeout(len(prompt))
        log(f"auto-timeout: {timeout}s (based on {len(prompt)} chars)")

    cmd = ["llm", "-m", model]
    if system:
        cmd.extend(["-s", system])

    try:
        result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"ERROR: timed out after {timeout}s")
        raise typer.Exit(code=1)

    if result.returncode != 0:
        log(f"ERROR: {result.stderr.strip()}")
        raise typer.Exit(code=1)

    print(result.stdout)  # stdout — the LLM response as plain text
    log("response received")
