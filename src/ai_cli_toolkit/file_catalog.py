"""File Context Awareness Tool — catalog, describe, and search your filesystem."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from .common import emit, log

# ── Pipe safety ───────────────────────────────────────────────────────────────
# Prevent BrokenPipeError when downstream closes early (e.g. | head -1)
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except AttributeError:
    pass  # Windows has no SIGPIPE

app = typer.Typer(help="Catalog, describe, and search your filesystem by purpose.")

# ── Database ──────────────────────────────────────────────────────────────────

DEFAULT_DB = os.environ.get("FILE_CATALOG_DB", os.path.expanduser("~/.config/file-catalog/catalog.db"))
SCAN_BATCH = 500  # commit every N files

# Safe git environment — prevents hooks, prompts, and glob expansion
_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NOGLOB_PATHSPECS": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
}


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
            description TEXT,          -- manual description (user-provided)
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


def _safe_git(cmd: list[str], cwd: str, timeout: int = 5) -> Optional[str]:
    """Run a git command safely (no hooks, no prompts) and return stdout or None."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=cwd, env=_GIT_ENV,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def infer_purpose(path: Path, is_dir: bool) -> dict:
    """Infer the purpose, language, and framework of a file or directory."""
    result = {"purpose": None, "language": None, "framework": None}

    if is_dir:
        # Check for project markers in the directory
        try:
            children = {f.name for f in path.iterdir()}
        except (PermissionError, OSError):
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

        # Check for git remote — read .git/config directly instead of shelling out
        git_config = path / ".git" / "config"
        if git_config.exists():
            try:
                config_text = git_config.read_text(encoding="utf-8", errors="replace")
                for line in config_text.split("\n"):
                    line = line.strip()
                    if line.startswith("url = ") and "github.com" in line:
                        result["git_remote"] = line[6:].strip()
                        break
                    elif line.startswith("url = "):
                        result["git_remote"] = line[6:].strip()
            except (OSError, PermissionError):
                pass

        # Check for README — only use as fallback if no marker found
        if not result["purpose"]:
            for readme in ("README.md", "README.rst", "README.txt", "README"):
                readme_path = path / readme
                if readme_path.exists():
                    try:
                        content = readme_path.read_text(encoding="utf-8", errors="replace")[:500]
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
            result["purpose"] = result["purpose"] or "Test directory"
        elif name in ("docs", "doc", "documentation"):
            result["purpose"] = result["purpose"] or "Documentation"
        elif name in ("scripts", "bin", "tools"):
            result["purpose"] = result["purpose"] or "Scripts/tools directory"
        elif name in ("config", "conf", "settings"):
            result["purpose"] = result["purpose"] or "Configuration directory"
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


def _scan_entry(entry: os.DirEntry, parent: str) -> Optional[dict]:
    """Scan a single DirEntry and return its record (uses cached stat)."""
    try:
        stat = entry.stat(follow_symlinks=False)
    except (OSError, PermissionError):
        return None

    is_dir = entry.is_dir(follow_symlinks=False)
    path = Path(entry.path)
    info = infer_purpose(path, is_dir)

    return {
        "path": str(path.resolve()),
        "parent": parent,
        "name": entry.name,
        "type": "dir" if is_dir else "file",
        "size": stat.st_size if not is_dir else None,
        "extension": path.suffix.lower() if not is_dir else None,
        "purpose": info.get("purpose"),
        "description": None,
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
    visited: set[tuple[int, int]] = set()  # (dev, ino) for symlink cycle detection

    log(f"scanning {root_path} (depth={max_depth}, excluding {', '.join(exclude)})")

    def _walk(directory: Path, depth: int, parent: str):
        nonlocal file_count, dir_count, batch
        if depth > max_depth:
            return

        # Symlink cycle detection
        try:
            st = directory.stat()
            key = (st.st_dev, st.st_ino)
            if key in visited:
                log(f"  symlink cycle detected: {directory}")
                return
            visited.add(key)
        except (OSError, PermissionError):
            return

        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except PermissionError:
            log(f"  permission denied: {directory}")
            return

        for entry in entries:
            name = entry.name

            # Skip excluded directories (but catalog them as excluded)
            if name in exclude_set and entry.is_dir(follow_symlinks=False):
                record = _scan_entry(entry, str(directory))
                if record:
                    record["purpose"] = f"Excluded directory ({name})"
                    batch.append(record)
                    dir_count += 1
                continue

            # Hidden directories: catalog but don't recurse (except well-known ones)
            if name.startswith(".") and entry.is_dir(follow_symlinks=False):
                record = _scan_entry(entry, str(directory))
                if record:
                    batch.append(record)
                    dir_count += 1
                # Don't recurse into hidden dirs (except .github, .vscode which are cataloged)
                continue

            # Hidden files: catalog them (they have purpose — .gitignore, .env, etc.)
            record = _scan_entry(entry, str(directory))
            if record is None:
                continue

            batch.append(record)
            if entry.is_dir(follow_symlinks=False):
                dir_count += 1
                _walk(Path(entry.path), depth + 1, str(Path(entry.path).resolve()))
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
        INSERT INTO files (path, parent, name, type, size, extension, purpose, description, language, framework, git_remote, last_modified, last_scanned)
        VALUES (:path, :parent, :name, :type, :size, :extension, :purpose, :description, :language, :framework, :git_remote, :last_modified, :last_scanned)
        ON CONFLICT(path) DO UPDATE SET
            parent=excluded.parent, name=excluded.name, type=excluded.type,
            size=excluded.size, extension=excluded.extension,
            purpose=COALESCE(excluded.purpose, files.purpose),
            description=COALESCE(files.description, excluded.description),
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
            commits = _safe_git(
                ["git", "log", "--oneline", "-5", "--format=%h %s (%ar)"],
                cwd=resolved,
            )
            if commits:
                record["recent_commits"] = commits.split("\n")

            status = _safe_git(["git", "status", "--porcelain"], cwd=resolved)
            if status is not None:
                record["uncommitted_changes"] = len(status.split("\n")) if status else 0

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


def _escape_like(s: str) -> str:
    """Escape SQL LIKE special characters."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.command()
def search(
    query: str = typer.Argument(help="Search term (matches name, purpose, description, tags)"),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    type_filter: str = typer.Option(None, "--type", "-t", help="Filter by type: file or dir"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """Search the catalog by name, purpose, description, or tag."""
    conn = get_db(db)

    if type_filter and type_filter not in ("file", "dir"):
        log(f"ERROR: --type must be 'file' or 'dir', got '{type_filter}'")
        raise typer.Exit(code=1)

    escaped = _escape_like(query)
    pattern = f"%{escaped}%"

    sql = """
        SELECT DISTINCT f.path, f.name, f.type, f.purpose, f.description, f.language, f.framework
        FROM files f
        LEFT JOIN tags t ON f.path = t.path
        WHERE (f.name LIKE ? ESCAPE '\\' OR f.purpose LIKE ? ESCAPE '\\'
               OR f.description LIKE ? ESCAPE '\\' OR t.tag LIKE ? ESCAPE '\\')
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
    """Generate a plain-text summary of a directory for LLM context.

    Note: output is plain text (not NDJSON) — intentionally designed for
    piping to call-llm. Use 'describe' for structured JSON output.
    """
    conn = get_db(db)
    resolved = str(Path(path).resolve())

    rows = conn.execute("""
        SELECT name, type, purpose, language, framework, description
        FROM files WHERE parent = ?
        ORDER BY type DESC, name
        LIMIT ?
    """, (resolved, max_items)).fetchall()

    if not rows:
        log(f"ERROR: '{path}' not in catalog or empty. Run 'file-catalog scan' first.")
        raise typer.Exit(code=1)

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
        # Prefer manual description over inferred purpose
        desc = row["description"] or row["purpose"]
        if desc:
            parts.append(f"— {desc}")
        if row["language"]:
            parts.append(f"[{row['language']}]")
        if row["framework"]:
            parts.append(f"({row['framework']})")
        lines.append(" ".join(parts))

    # Plain text to stdout — intentional for LLM pipeline (documented in docstring)
    print("\n".join(lines), flush=True)
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
    type_filter: str = typer.Option("all", "--type", "-t", help="Filter: file, dir, or all"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """List files and/or dirs with no inferred purpose (candidates for manual tagging)."""
    conn = get_db(db)

    sql = "SELECT path, name, type, extension, language FROM files WHERE purpose IS NULL"
    params = []

    if type_filter and type_filter != "all":
        if type_filter not in ("file", "dir"):
            log(f"ERROR: --type must be 'file', 'dir', or 'all', got '{type_filter}'")
            raise typer.Exit(code=1)
        sql += " AND type = ?"
        params.append(type_filter)

    sql += " ORDER BY path LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
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
        WHERE finished IS NOT NULL
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


@app.command()
def changes(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max results"),
    max_depth: int = typer.Option(3, "--depth", "-d", help="Max depth for detecting new files"),
    since: float = typer.Option(None, "--since", "-s", help="Unix timestamp to compare against (default: last scan)"),
):
    """Detect new, modified, or deleted files since the last scan."""
    conn = get_db(db)

    if since is None:
        row = conn.execute("SELECT MAX(started) as ts FROM scan_log WHERE finished IS NOT NULL").fetchone()
        if not row or not row["ts"]:
            log("ERROR: no completed scans recorded. Run 'file-catalog scan' first.")
            raise typer.Exit(code=1)
        since = row["ts"]

    since_dt = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
    log(f"checking changes since {since_dt}")

    # Modified files (mtime > last_scanned)
    modified = []
    for r in conn.execute("""
        SELECT path, name, type, last_modified
        FROM files
        WHERE last_modified > ? AND type = 'file'
        ORDER BY last_modified DESC
        LIMIT ?
    """, (since, limit)).fetchall():
        modified.append({
            "path": r["path"], "name": r["name"], "type": r["type"],
            "last_modified": datetime.fromtimestamp(r["last_modified"], tz=timezone.utc).isoformat(),
        })

    # Batch-load known paths for each root into a set (avoids N+1 queries)
    new_files = []
    deleted_files = []

    roots = conn.execute("SELECT DISTINCT root FROM scan_log WHERE finished IS NOT NULL").fetchall()
    for root_row in roots:
        root_path = Path(root_row["root"])
        if not root_path.exists():
            continue

        # Load known paths for this root
        known_paths: set[str] = set()
        for row in conn.execute("SELECT path FROM files WHERE path LIKE ?", (f"{root_path}%",)):
            known_paths.add(row["path"])

        # Walk with depth limit and find new files
        def _check_new(directory: Path, depth: int):
            if depth > max_depth or len(new_files) >= limit:
                return
            try:
                for entry in os.scandir(directory):
                    if len(new_files) >= limit:
                        break
                    resolved = str(Path(entry.path).resolve())
                    if resolved not in known_paths:
                        try:
                            st = entry.stat(follow_symlinks=False)
                            new_files.append({
                                "path": resolved,
                                "name": entry.name,
                                "type": "dir" if entry.is_dir(follow_symlinks=False) else "file",
                                "last_modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                            })
                        except (OSError, PermissionError):
                            pass
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            _check_new(Path(entry.path), depth + 1)
            except PermissionError:
                pass

        _check_new(root_path, 0)

    # Check for deleted files — stream, don't load all at once
    for row in conn.execute("SELECT path FROM files ORDER BY path"):
        if len(deleted_files) >= limit:
            break
        if not Path(row["path"]).exists():
            deleted_files.append({"path": row["path"]})

    conn.close()

    result = {
        "since": since_dt,
        "modified_count": len(modified),
        "new_count": len(new_files),
        "deleted_count": len(deleted_files),
    }
    if modified:
        result["modified"] = modified[:limit]
    if new_files:
        result["new"] = new_files[:limit]
    if deleted_files:
        result["deleted"] = deleted_files[:limit]

    emit(result)
    log(f"changes: {len(modified)} modified, {len(new_files)} new, {len(deleted_files)} deleted")


@app.command()
def prompt_missing(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    count: int = typer.Option(10, "--count", "-n", help="Number of items to include"),
    type_filter: str = typer.Option("dir", "--type", "-t", help="Filter: file, dir, or all"),
    focus: str = typer.Option(None, "--focus", "-f", help="Filter by parent path prefix"),
):
    """Generate a prompt about items without purpose — for cron or manual tagging."""
    conn = get_db(db)

    if type_filter not in ("file", "dir", "all"):
        log(f"ERROR: --type must be 'file', 'dir', or 'all', got '{type_filter}'")
        raise typer.Exit(code=1)

    sql = "SELECT path, name, type, extension, language, parent FROM files WHERE purpose IS NULL"
    params = []

    if type_filter != "all":
        sql += " AND type = ?"
        params.append(type_filter)

    if focus:
        sql += " AND path LIKE ? ESCAPE '\\'"
        params.append(f"{_escape_like(focus)}%")

    sql += " ORDER BY RANDOM() LIMIT ?"
    params.append(count)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        emit({"status": "all_described", "message": "Everything has a purpose!"})
        return

    items = []
    for r in rows:
        item = {"path": r["path"], "name": r["name"], "type": r["type"]}
        if r["extension"]:
            item["extension"] = r["extension"]
        if r["language"]:
            item["language"] = r["language"]
        items.append(item)

    prompt_lines = [
        f"I found {len(items)} items without a description in the file catalog.",
        "Can you tell me what each one is for?",
        "",
    ]
    for i, item in enumerate(items, 1):
        icon = "📁" if item["type"] == "dir" else "📄"
        line = f"{i}. {icon} {item['path']}"
        if item.get("language"):
            line += f" [{item['language']}]"
        prompt_lines.append(line)

    prompt_lines.append("")
    prompt_lines.append("Reply with descriptions like:")
    prompt_lines.append("  1. ASX stock portfolio tracker")
    prompt_lines.append("  2. Old backup directory — can delete")
    prompt_lines.append("")
    prompt_lines.append("Or say 'skip' to skip this batch.")

    emit({
        "status": "needs_input",
        "count": len(items),
        "items": items,
        "prompt": "\n".join(prompt_lines),
    })
    log(f"generated prompt for {len(items)} untagged items")


@app.command()
def apply_descriptions(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview changes without applying"),
):
    """Read NDJSON descriptions from stdin and apply them to the description column.

    Expected format: {"path": "/full/path", "description": "what it is"}
    One per line. Preserves auto-inferred purpose; description is separate.
    """
    from .common import read_stdin_ndjson

    conn = get_db(db)
    count = 0
    skipped = 0

    for record in read_stdin_ndjson():
        path = record.get("path")
        desc = record.get("description")
        if not path or not desc:
            skipped += 1
            continue

        # Verify path exists in catalog
        exists = conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone()
        if not exists:
            log(f"WARNING: '{path}' not in catalog, skipping")
            skipped += 1
            continue

        if dry_run:
            log(f"  would set description: {path} → {desc}")
        else:
            conn.execute(
                "UPDATE files SET description=? WHERE path=?",
                (desc, path)
            )
        count += 1

    if not dry_run:
        conn.commit()
    conn.close()

    action = "would apply" if dry_run else "applied"
    log(f"{action} {count} description(s), {skipped} skipped")
