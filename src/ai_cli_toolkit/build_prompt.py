"""Assemble a prompt from a template and stdin context."""

import typer
from pathlib import Path
from .common import log, read_stdin_text

app = typer.Typer()


@app.command()
def main(
    template: Path = typer.Option(..., "--template", "-t", help="Path to prompt template file"),
    var_name: str = typer.Option("context", "--var", "-v", help="Placeholder name in template"),
):
    """Read stdin, substitute into a template, and print the assembled prompt."""
    context = read_stdin_text()
    log(f"read {len(context)} chars from stdin")

    if not template.exists():
        log(f"ERROR: template not found: {template}")
        raise typer.Exit(code=1)

    template_text = template.read_text(encoding="utf-8")
    placeholder = f"{{{{{var_name}}}}}"

    if placeholder not in template_text:
        log(f"WARNING: placeholder '{placeholder}' not found in template")

    prompt = template_text.replace(placeholder, context)

    print(prompt)  # stdout — plain text, not NDJSON (this is a prompt string)
    log("prompt assembled")
