"""Extract a field from NDJSON input."""

import json
import typer
from .common import log, read_stdin_ndjson, emit

app = typer.Typer()


@app.command()
def main(
    field: str = typer.Argument(help="Field name to extract (supports dot notation e.g. 'response.text')"),
    raw: bool = typer.Option(False, "--raw", "-r", help="Output raw values, one per line (not JSON)"),
):
    """Pull a specific field from each NDJSON record on stdin."""
    count = 0
    missing = 0
    for record in read_stdin_ndjson():
        value = record
        for key in field.split("."):
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
                break

        if value is None:
            missing += 1

        if raw:
            # Print empty string for None to avoid polluting stdout with literal "None"
            print("" if value is None else value)
        else:
            emit({"value": value})
        count += 1

    log(f"extracted '{field}' from {count} records" + (f" ({missing} missing)" if missing else ""))
