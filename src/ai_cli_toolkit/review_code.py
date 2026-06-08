"""Send code to an LLM for review."""

import subprocess
import typer
from .common import log, read_stdin_text

app = typer.Typer()

DEFAULT_REVIEW_PROMPT = (
    "Review the following code. Identify bugs, edge cases, security issues, "
    "and suggest improvements. Be specific and reference line numbers where relevant."
)

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
    model: str = typer.Option("openrouter/xiaomi/mimo-v2.5-pro", "--model", "-m", help="Model to use for review"),
    focus: str = typer.Option(None, "--focus", "-f", help="Specific review focus, e.g. 'security' or 'performance'"),
    timeout: int = typer.Option(0, "--timeout", "-T", help="Subprocess timeout in seconds (0 = auto based on input size)"),
):
    """Read code from stdin, send it to a review model, and print the review."""
    code = read_stdin_text()
    if not code.strip():
        log("ERROR: no code on stdin")
        raise typer.Exit(code=1)

    log(f"reviewing {len(code)} chars with {model}")

    if timeout <= 0:
        timeout = estimate_timeout(len(code))
        log(f"auto-timeout: {timeout}s (based on {len(code)} chars)")

    system_prompt = DEFAULT_REVIEW_PROMPT
    if focus:
        system_prompt += f"\n\nFocus especially on: {focus}"

    cmd = ["llm", "-m", model, "-s", system_prompt]

    try:
        result = subprocess.run(cmd, input=code, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"ERROR: timed out after {timeout}s")
        raise typer.Exit(code=1)

    if result.returncode != 0:
        log(f"ERROR: {result.stderr.strip()}")
        raise typer.Exit(code=1)

    print(result.stdout)
    log("review complete")
