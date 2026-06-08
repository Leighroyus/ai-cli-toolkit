"""Execute SQL and emit results as NDJSON to stdout."""

import json
import os
import typer
from collections import Counter
from .common import log, emit

app = typer.Typer()


# Connection configuration — extend as needed
CONNECTIONS = {
    "default": {
        "driver": "sqlite",
        "path": os.environ.get("AI_CLI_DB_PATH", ":memory:"),
    },
}


def get_connection(name: str):
    """Look up a connection by name and return an active connection."""
    if name not in CONNECTIONS:
        available = ", ".join(CONNECTIONS.keys())
        log(f"ERROR: unknown connection '{name}'. Available: {available}")
        raise typer.Exit(code=1)

    cfg = CONNECTIONS[name]
    driver = cfg.get("driver", "sqlite")

    if driver == "sqlite":
        import sqlite3
        path = cfg.get("path", ":memory:")
        log(f"opening sqlite: {path}")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA busy_timeout = 30000")  # 30s lock timeout
        return conn
    elif driver == "duckdb":
        try:
            import duckdb
        except ImportError:
            log("ERROR: duckdb driver requires 'pip install duckdb'")
            raise typer.Exit(code=1)
        path = cfg.get("path", ":memory:")
        log(f"opening duckdb: {path}")
        return duckdb.connect(path)
    else:
        log(f"ERROR: unsupported driver '{driver}'")
        raise typer.Exit(code=1)


def _safe_value(v):
    """Make a value JSON-serializable (handle bytes, custom types, etc.)."""
    if isinstance(v, bytes):
        return v.hex()
    return v


def _deduplicate_columns(columns: list[str]) -> list[str]:
    """Deduplicate column names: SELECT a, a FROM t → ['a', 'a_2']."""
    counts = Counter(columns)
    if all(c == 1 for c in counts.values()):
        return columns
    seen: dict[str, int] = {}
    result = []
    for col in columns:
        if counts[col] > 1:
            seen[col] = seen.get(col, 0) + 1
            result.append(f"{col}_{seen[col]}")
        else:
            result.append(col)
    return result


@app.command()
def main(
    query: str = typer.Option(..., "--query", "-q", help="SQL query to execute"),
    conn: str = typer.Option("default", "--conn", "-c", help="Connection name from config"),
):
    """Execute a SQL query and emit each row as an NDJSON record."""
    log(f"connecting to '{conn}'...")
    connection = get_connection(conn)

    try:
        log("query submitted")
        try:
            cursor = connection.execute(query)
        except Exception as e:
            log(f"ERROR: query failed: {e}")
            raise typer.Exit(code=1)

        # cursor.description is None for DDL, PRAGMA, or statements with no result set
        if cursor.description is None:
            log("query produced no result set (DDL/DML)")
            connection.commit()
            log("done: committed")
            return

        columns = _deduplicate_columns([desc[0] for desc in cursor.description])

        count = 0
        for row in cursor:
            record = {col: _safe_value(val) for col, val in zip(columns, row)}
            emit(record)
            count += 1

        # Commit in case the query was DML (INSERT/UPDATE/DELETE)
        connection.commit()
        log(f"done: {count} rows")
    finally:
        connection.close()
