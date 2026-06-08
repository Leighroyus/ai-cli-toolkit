"""File Context Awareness Tool — catalog, describe, and search your filesystem."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from .common import emit, log

app = typer.Typer(help="Catalog, describe, and search your filesystem by purpose.")

# ── Database ──────────────────────────────────────────────────────────────────

DEFAULT_DB = os.environ.get("FILE_CATALOG_DB", os.path.expanduser("~/.config/file-catalog/catalog.db"))
SCAN_BATCH = 500  # commit every N files


def get_db(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (and auto-create) the catalog database."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,
            parent      TEXT NOT NULL,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL,  -- 'file' or 'dir'
            size        INTEGER,
            extension   TEXT,
            purpose     TEXT,          -- inferred purpose
            description TEXT,          -- manual description
            language    TEXT,
            framework   TEXT,
            git_remote  TEXT,
            last_modified REAL,
            last_scanned REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent);
        CREATE INDEX IF NOT EXISTS idx_files_type ON files(type);
        CREATE INDEX IF NOT EXISTS idx_files_ext ON files(extension);

        CREATE TABLE IF NOT EXISTS tags (
            path TEXT NOT NULL,
            tag  TEXT NOT NULL,
            PRIMARY KEY (path, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

        CREATE TABLE IF NOT EXISTS scan_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            root       TEXT NOT NULL,
            started    REAL NOT NULL,
            finished   REAL,
            file_count INTEGER DEFAULT 0,
            dir_count  INTEGER DEFAULT 0
        );
    """)
    conn.commit()


# ── Purpose inference ─────────────────────────────────────────────────────────

PROJECT_MARKERS = {
    "pyproject.toml": "Python project",
    "setup.py": "Python project",
    "setup.cfg": "Python project",
    "package.json": "Node.js project",
    "Cargo.toml": "Rust project",
    "go.mod": "Go project",
    "Gemfile": "Ruby project",
    "pom.xml": "Java/Maven project",
    "build.gradle": "Java/Gradle project",
    "Makefile": "C/C++ or build-managed project",
    "CMakeLists.txt": "C/C++ project",
    "docker-compose.yml": "Docker Compose project",
    "Dockerfile": "Dockerised project",
    ".gitlab-ci.yml": "GitLab CI project",
    ".github": "GitHub-hosted project",
    "tox.ini": "Python test-managed project",
    "requirements.txt": "Python project (pip)",
    "Pipfile": "Python project (pipenv)",
    "poetry.lock": "Python project (poetry)",
}

LANG_MAP = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".rs": "Rust", ".go": "Go", ".rb": "Ruby",
    ".java": "Java", ".kt": "Kotlin", ".scala": "Scala",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".cs": "C#", ".fs": "F#",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell",
    ".sql": "SQL", ".r": "R", ".R": "R",
    ".lua": "Lua", ".pl": "Perl", ".php": "PHP",
    ".swift": "Swift", ".dart": "Dart", ".ex": "Elixir", ".erl": "Erlang",
    ".hs": "Haskell", ".ml": "OCaml", ".clj": "Clojure",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".less": "LESS",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".xml": "XML", ".md": "Markdown", ".rst": "reStructuredText",
    ".txt": "Text", ".csv": "CSV", ".tsv": "TSV",
    ".ini": "INI", ".cfg": "Config", ".conf": "Config",
    ".env": "Environment", ".dockerignore": "Docker config",
    ".gitignore": "Git config", ".editorconfig": "Editor config",
}

FRAMEWORK_MARKERS = {
    "manage.py": "Django",
    "app.py": "Flask",
    "wsgi.py": "WSGI",
    "asgi.py": "ASGI",
    "next.config.js": "Next.js",
    "nuxt.config.js": "Nuxt.js",
    "angular.json": "Angular",
    "vue.config.js": "Vue.js",
    "svelte.config.js": "Svelte",
    "gatsby-config.js": "Gatsby",
    "gatsby-config.ts": "Gatsby",
    "webpack.config.js": "Webpack",
    "vite.config.js": "Vite",
    "vite.config.ts": "Vite",
    "tailwind.config.js": "Tailwind CSS",
    "jest.config.js": "Jest",
    "pytest.ini": "pytest",
    "conftest.py": "pytest",
}


def infer_purpose(path: Path, is_dir: bool) -> dict:
    """Infer the purpose, language, and framework of a file or directory."""
    result = {"purpose": None, "language": None, "framework": None}

    if is_dir:
        # Check for project markers in the directory
        try:
            children = {f.name for f in path.iterdir()}
        except PermissionError:
            return result

        for marker, desc in PROJECT_MARKERS.items():
            if marker in children:
                result["purpose"] = desc
                break

        # Check for framework markers
        for marker, fw in FRAMEWORK_MARKERS.items():
            if marker in children:
                result["framework"] = fw
                break

        # Check for git remote
        git_dir = path / ".git"
        if git_dir.exists():
            try:
                remote = subprocess.run(
                    ["git", "-C", str(path), "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=5
                )
                if remote.returncode == 0:
                    result["git_remote"] = remote.stdout.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        # Check for README
        for readme in ("README.md", "README.rst", "README.txt", "README"):
            readme_path = path / readme
            if readme_path.exists():
                try:
                    content = readme_path.read_text(encoding="utf-8", errors="replace")[:500]
                    # Extract first non-empty, non-heading line as description
                    for line in content.split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#") and not line.startswith("==="):
                            result["purpose"] = line[:200]
                            break
                except (OSError, PermissionError):
                    pass
                break

        # Special directories
        name = path.name.lower()
        if name in ("src", "lib", "source"):
            result["purpose"] = result["purpose"] or "Source code directory"
        elif name in ("tests", "test", "__tests__"):
            result["purpose"] = "Test directory"
        elif name in ("docs", "doc", "documentation"):
            result["purpose"] = "Documentation"
        elif name in ("scripts", "bin", "tools"):
            result["purpose"] = "Scripts/tools directory"
        elif name in ("config", "conf", "settings"):
            result["purpose"] = "Configuration directory"
        elif name in (".git", ".hg", ".svn"):
            result["purpose"] = "Version control"
        elif name in ("node_modules", ".venv", "venv", "env", "__pycache__", ".tox"):
            result["purpose"] = "Generated/dependency directory"
        elif name == ".github":
            result["purpose"] = "GitHub Actions & config"
        elif name == ".vscode":
            result["purpose"] = "VS Code workspace settings"
        elif name == ".idea":
            result["purpose"] = "JetBrains IDE settings"

    else:
        # File — infer from extension
        ext = path.suffix.lower()
        result["language"] = LANG_MAP.get(ext)

        # Check for special filenames
        name = path.name.lower()
        if name in FRAMEWORK_MARKERS:
            result["framework"] = FRAMEWORK_MARKERS[name]

        # README files
        if name.startswith("readme"):
            result["purpose"] = "Project documentation"

        # Config files
        elif name in (".gitignore", ".dockerignore", ".editorconfig", ".eslintrc", ".prettierrc"):
            result["purpose"] = "Configuration"
        elif ext in (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env"):
            result["purpose"] = "Configuration"
        elif ext == ".json" and "config" in name:
            result["purpose"] = "Configuration"

        # Documentation
        elif ext in (".md", ".rst", ".txt"):
            result["purpose"] = "Documentation"

        # Try reading first line for scripts
        elif ext in (".py", ".sh", ".bash", ".js", ".ts", ".rb", ".pl"):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    first_line = f.readline().strip()
                    if first_line.startswith("#!") or first_line.startswith('"""') or first_line.startswith("'''"):
                        result["purpose"] = "Script/module"
            except (OSError, PermissionError):
                pass

    return result


def _scan_single(path: Path, parent: str) -> dict:
    """Scan a single file/directory and return its record."""
    try:
        stat = path.stat()
    except (OSError, PermissionError):
        return None

    is_dir = path.is_dir()
    info = infer_purpose(path, is_dir)

    return {
        "path": str(path),
        "parent": parent,
        "name": path.name,
        "type": "dir" if is_dir else "file",
        "size": stat.st_size if not is_dir else None,
        "extension": path.suffix.lower() if not is_dir else None,
        "purpose": info.get("purpose"),
        "language": info.get("language"),
        "framework": info.get("framework"),
        "git_remote": info.get("git_remote"),
        "last_modified": stat.st_mtime,
        "last_scanned": time.time(),
    }


# ── Commands ──────────────────────────────────────────────────────────────────

@app.command()
def scan(
    root: str = typer.Argument(help="Directory to scan"),
    max_depth: int = typer.Option(5, "--depth", "-d", help="Max directory depth"),
    exclude: list[str] = typer.Option(
        ["node_modules", "__pycache__", ".git", ".venv", "venv", ".tox", ".eggs", "dist", "build"],
        "--exclude", "-x", help="Directory names to exclude (repeatable)"
    ),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
):
    """Scan a directory tree and catalog all files and directories."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        log(f"ERROR: '{root}' is not a directory")
        raise typer.Exit(code=1)

    conn = get_db(db)
    now = time.time()
    cur = conn.cursor()
    cur.execute("INSERT INTO scan_log (root, started) VALUES (?, ?)", (str(root_path), now))
    scan_id = cur.lastrowid

    file_count = 0
    dir_count = 0
    batch = []
    exclude_set = set(exclude)

    log(f"scanning {root_path} (depth={max_depth}, excluding {', '.join(exclude)})")

    def _walk(directory: Path, depth: int, parent: str):
        nonlocal file_count, dir_count, batch
        if depth > max_depth:
            return

        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            log(f"  permission denied: {directory}")
            return

        for entry in entries:
            if entry.name in exclude_set or entry.name.startswith("."):
                # Still record hidden dirs and excluded dirs as single entries
                if entry.is_dir() and entry.name in exclude_set:
                    record = _scan_single(entry, str(directory))
                    if record:
                        record["purpose"] = f"Excluded directory ({entry.name})"
                        batch.append(record)
                        dir_count += 1
                    continue
                if entry.is_dir() and entry.name.startswith("."):
                    record = _scan_single(entry, str(directory))
                    if record:
                        batch.append(record)
                        dir_count += 1
                    continue
                continue

            record = _scan_single(entry, str(directory))
            if record is None:
                continue

            batch.append(record)
            if entry.is_dir():
                dir_count += 1
                _walk(entry, depth + 1, str(entry))
            else:
                file_count += 1

            if len(batch) >= SCAN_BATCH:
                _flush(conn, batch)
                batch = []

    _walk(root_path, 0, str(root_path))

    if batch:
        _flush(conn, batch)

    conn.execute(
        "UPDATE scan_log SET finished=?, file_count=?, dir_count=? WHERE id=?",
        (time.time(), file_count, dir_count, scan_id)
    )
    conn.commit()
    conn.close()

    log(f"done: {file_count} files, {dir_count} directories cataloged")
    emit({"status": "ok", "files": file_count, "dirs": dir_count, "root": str(root_path)})


def _flush(conn: sqlite3.Connection, batch: list[dict]):
    """Upsert a batch of records into the files table."""
    conn.executemany("""
        INSERT INTO files (path, parent, name, type, size, extension, purpose, language, framework, git_remote, last_modified, last_scanned)
        VALUES (:path, :parent, :name, :type, :size, :extension, :purpose, :language, :framework, :git_remote, :last_modified, :last_scanned)
        ON CONFLICT(path) DO UPDATE SET
            parent=excluded.parent, name=excluded.name, type=excluded.type,
            size=excluded.size, extension=excluded.extension,
            purpose=COALESCE(excluded.purpose, files.purpose),
            language=excluded.language, framework=excluded.framework,
            git_remote=COALESCE(excluded.git_remote, files.git_remote),
            last_modified=excluded.last_modified, last_scanned=excluded.last_scanned
    """, batch)
    conn.commit()


@app.command()
def describe(
    path: str = typer.Argument(help="File or directory to describe"),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
):
    """Show everything the catalog knows about a path."""
    conn = get_db(db)
    resolved = str(Path(path).resolve())

    row = conn.execute("SELECT * FROM files WHERE path=?", (resolved,)).fetchone()
    if not row:
        log(f"ERROR: '{path}' not in catalog. Run 'file-catalog scan' first.")
        raise typer.Exit(code=1)

    tags = [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE path=?", (resolved,)).fetchall()]
    conn.close()

    record = dict(row)
    if tags:
        record["tags"] = tags

    # Also grab live git info if it's a directory
    if record["type"] == "dir":
        p = Path(resolved)
        if (p / ".git").exists():
            try:
                log_result = subprocess.run(
                    ["git", "-C", resolved, "log", "--oneline", "-5", "--format=%h %s (%ar)"],
                    capture_output=True, text=True, timeout=5
                )
                if log_result.returncode == 0 and log_result.stdout.strip():
                    record["recent_commits"] = log_result.stdout.strip().split("\n")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

            try:
                status = subprocess.run(
                    ["git", "-C", resolved, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5
                )
                if status.returncode == 0:
                    changes = len(status.stdout.strip().split("\n")) if status.stdout.strip() else 0
                    record["uncommitted_changes"] = changes
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

    # Clean up None values and float timestamps for display
    clean = {}
    for k, v in record.items():
        if v is None:
            continue
        if k in ("last_modified", "last_scanned"):
            clean[k] = datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        else:
            clean[k] = v

    emit(clean)


@app.command()
def search(
    query: str = typer.Argument(help="Search term (matches name, purpose, description, tags)"),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    type_filter: str = typer.Option(None, "--type", "-t", help="Filter by type: file or dir"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """Search the catalog by name, purpose, description, or tag."""
    conn = get_db(db)
    pattern = f"%{query}%"

    sql = """
        SELECT f.path, f.name, f.type, f.purpose, f.description, f.language, f.framework
        FROM files f
        WHERE (f.name LIKE ? OR f.purpose LIKE ? OR f.description LIKE ?)
    """
    params = [pattern, pattern, pattern]

    # Also search tags
    sql = """
        SELECT DISTINCT f.path, f.name, f.type, f.purpose, f.description, f.language, f.framework
        FROM files f
        LEFT JOIN tags t ON f.path = t.path
        WHERE (f.name LIKE ? OR f.purpose LIKE ? OR f.description LIKE ? OR t.tag LIKE ?)
    """
    params = [pattern, pattern, pattern, pattern]

    if type_filter:
        sql += " AND f.type = ?"
        params.append(type_filter)

    sql += " ORDER BY f.type DESC, f.name LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        log(f"no results for '{query}'")
        return

    for row in rows:
        emit(dict(row))

    log(f"{len(rows)} result(s) for '{query}'")


@app.command()
def summary(
    path: str = typer.Argument(help="Directory to summarise"),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    max_items: int = typer.Option(50, "--max", "-m", help="Max items to include"),
):
    """Generate a text summary of a directory for LLM context."""
    conn = get_db(db)
    resolved = str(Path(path).resolve())

    # Get direct children
    rows = conn.execute("""
        SELECT name, type, purpose, language, framework
        FROM files WHERE parent = ?
        ORDER BY type DESC, name
        LIMIT ?
    """, (resolved, max_items)).fetchall()

    if not rows:
        log(f"ERROR: '{path}' not in catalog or empty. Run 'file-catalog scan' first.")
        raise typer.Exit(code=1)

    # Get stats
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN type='file' THEN 1 ELSE 0 END) as files,
            SUM(CASE WHEN type='dir' THEN 1 ELSE 0 END) as dirs
        FROM files WHERE parent = ?
    """, (resolved,)).fetchone()

    conn.close()

    lines = [f"## {Path(path).name}"]
    lines.append(f"Path: {resolved}")
    lines.append(f"Contents: {stats['files']} files, {stats['dirs']} directories")
    lines.append("")

    for row in rows:
        icon = "📁" if row["type"] == "dir" else "📄"
        parts = [f"{icon} {row['name']}"]
        if row["purpose"]:
            parts.append(f"— {row['purpose']}")
        if row["language"]:
            parts.append(f"[{row['language']}]")
        if row["framework"]:
            parts.append(f"({row['framework']})")
        lines.append(" ".join(parts))

    print("\n".join(lines))
    log(f"summary: {len(rows)} items")


@app.command()
def tag(
    path: str = typer.Argument(help="File or directory to tag"),
    tags: list[str] = typer.Argument(help="Tags to add"),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    remove: bool = typer.Option(False, "--remove", "-r", help="Remove tags instead of adding"),
):
    """Add (or remove) tags from a cataloged path."""
    conn = get_db(db)
    resolved = str(Path(path).resolve())

    # Ensure path exists in catalog
    exists = conn.execute("SELECT 1 FROM files WHERE path=?", (resolved,)).fetchone()
    if not exists:
        log(f"ERROR: '{path}' not in catalog. Run 'file-catalog scan' first.")
        raise typer.Exit(code=1)

    for t in tags:
        if remove:
            conn.execute("DELETE FROM tags WHERE path=? AND tag=?", (resolved, t))
        else:
            conn.execute("INSERT OR IGNORE INTO tags (path, tag) VALUES (?, ?)", (resolved, t))

    conn.commit()
    conn.close()

    action = "removed" if remove else "added"
    log(f"{action} {len(tags)} tag(s) on {path}")


@app.command()
def untitled(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """List files/dirs with no inferred purpose (candidates for manual tagging)."""
    conn = get_db(db)

    rows = conn.execute("""
        SELECT path, name, type, extension, language
        FROM files
        WHERE purpose IS NULL AND type = 'dir'
        ORDER BY path
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    if not rows:
        log("everything has a purpose!")
        return

    for row in rows:
        emit(dict(row))

    log(f"{len(rows)} items without purpose")


@app.command()
def stats(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
):
    """Show catalog statistics."""
    conn = get_db(db)

    totals = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN type='file' THEN 1 ELSE 0 END) as files,
            SUM(CASE WHEN type='dir' THEN 1 ELSE 0 END) as dirs,
            SUM(CASE WHEN purpose IS NOT NULL THEN 1 ELSE 0 END) as described,
            SUM(CASE WHEN purpose IS NULL THEN 1 ELSE 0 END) as undescribed,
            SUM(size) as total_size
        FROM files
    """).fetchone()

    langs = conn.execute("""
        SELECT language, COUNT(*) as count
        FROM files
        WHERE language IS NOT NULL AND type='file'
        GROUP BY language
        ORDER BY count DESC
        LIMIT 10
    """).fetchall()

    tag_count = conn.execute("SELECT COUNT(*) as c FROM tags").fetchone()["c"]

    recent_scans = conn.execute("""
        SELECT root, started, finished, file_count, dir_count
        FROM scan_log
        ORDER BY started DESC
        LIMIT 5
    """).fetchall()

    conn.close()

    record = {
        "total_entries": totals["total"],
        "files": totals["files"],
        "directories": totals["dirs"],
        "with_purpose": totals["described"],
        "without_purpose": totals["undescribed"],
        "coverage_pct": round(totals["described"] / totals["total"] * 100, 1) if totals["total"] else 0,
        "total_size_mb": round(totals["total_size"] / 1024 / 1024, 2) if totals["total_size"] else 0,
        "tags": tag_count,
        "top_languages": {r["language"]: r["count"] for r in langs},
        "recent_scans": [
            {
                "root": r["root"],
                "files": r["file_count"],
                "dirs": r["dir_count"],
                "date": datetime.fromtimestamp(r["started"], tz=timezone.utc).isoformat(),
            }
            for r in recent_scans
        ],
    }
    emit(record)
