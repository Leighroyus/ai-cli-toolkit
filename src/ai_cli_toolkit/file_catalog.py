"""File Context Awareness Tool — catalog, describe, and search your filesystem.

Version: 0.2.0
"""

import contextlib
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

__version__ = "0.2.0"

# ── Pipe safety ───────────────────────────────────────────────────────────────
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except AttributeError:
    pass  # Windows has no SIGPIPE

app = typer.Typer(help="Catalog, describe, and search your filesystem by purpose.", add_help_option=True)

def _show_version():
    print(f"file-catalog {__version__}")
    raise typer.Exit()

# ── Database ──────────────────────────────────────────────────────────────────

DEFAULT_DB = os.environ.get("FILE_CATALOG_DB", os.path.expanduser("~/.config/file-catalog/catalog.db"))
SCAN_BATCH = 500

# Safe git environment — only pass what git needs, not full env
_GIT_SAFE_VARS = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP"}
_GIT_ENV = {k: v for k, v in os.environ.items() if k in _GIT_SAFE_VARS}
_GIT_ENV["GIT_TERMINAL_PROMPT"] = "0"
_GIT_ENV["GIT_NOGLOB_PATHSPECS"] = "1"
_GIT_ENV["GIT_CONFIG_NOSYSTEM"] = "1"


def get_db(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (and auto-create) the catalog database."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _try_add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    """Add a column to a table if it doesn't already exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # column already exists


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,
            parent      TEXT NOT NULL,
            name        TEXT NOT NULL,
            type        TEXT NOT NULL,
            size        INTEGER,
            extension   TEXT,
            purpose     TEXT,
            description TEXT,
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
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            root            TEXT NOT NULL,
            started         REAL NOT NULL,
            finished        REAL,
            file_count      INTEGER DEFAULT 0,
            dir_count       INTEGER DEFAULT 0,
            exclude_patterns TEXT,
            max_depth       INTEGER
        );
    """)
    # Migrate scan_log if old schema (missing new columns)
    _try_add_column(conn, "scan_log", "exclude_patterns", "TEXT")
    _try_add_column(conn, "scan_log", "max_depth", "INTEGER")
    # FTS5 virtual table for fast search
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
            USING fts5(name, purpose, description, tags, content='files', content_rowid='rowid')
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 not available — fall back to LIKE
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
    "manage.py": "Django", "app.py": "Flask", "wsgi.py": "WSGI", "asgi.py": "ASGI",
    "next.config.js": "Next.js", "nuxt.config.js": "Nuxt.js", "angular.json": "Angular",
    "vue.config.js": "Vue.js", "svelte.config.js": "Svelte",
    "gatsby-config.js": "Gatsby", "gatsby-config.ts": "Gatsby",
    "webpack.config.js": "Webpack", "vite.config.js": "Vite", "vite.config.ts": "Vite",
    "tailwind.config.js": "Tailwind CSS", "jest.config.js": "Jest",
    "pytest.ini": "pytest", "conftest.py": "pytest",
}

# Hidden directories that should be recursed into (not just cataloged as single entry)
_RECURSE_HIDDEN = {".github", ".vscode"}


def _safe_git(cmd: list[str], cwd: str, timeout: int = 5) -> Optional[str]:
    """Run a git command safely and return stdout or None."""
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
        # Check markers individually (no iterdir on huge dirs)
        marker_purpose = None
        for marker, desc in PROJECT_MARKERS.items():
            if (path / marker).exists():
                marker_purpose = desc
                break

        # Check framework markers
        for marker, fw in FRAMEWORK_MARKERS.items():
            if (path / marker).exists():
                result["framework"] = fw
                break

        # Check for git remote — read .git/config directly
        git_config = path / ".git" / "config"
        if git_config.exists():
            try:
                config_text = git_config.read_text(encoding="utf-8", errors="replace")
                for line in config_text.split("\n"):
                    line = line.strip()
                    if line.startswith("url = "):
                        result["git_remote"] = line[6:].strip()
                        break
            except (OSError, PermissionError):
                pass

        # README as fallback only (marker takes priority)
        if not marker_purpose:
            for readme in ("README.md", "README.rst", "README.txt", "README"):
                readme_path = path / readme
                if readme_path.exists():
                    try:
                        content = readme_path.read_text(encoding="utf-8", errors="replace")[:500]
                        for line in content.split("\n"):
                            line = line.strip()
                            if line and not line.startswith("#") and not line.startswith("==="):
                                marker_purpose = line[:200]
                                break
                    except (OSError, PermissionError):
                        pass
                    break

        result["purpose"] = marker_purpose

        # Special directory names
        name = path.name.lower()
        defaults = {
            ("src", "lib", "source"): "Source code directory",
            ("tests", "test", "__tests__"): "Test directory",
            ("docs", "doc", "documentation"): "Documentation",
            ("scripts", "bin", "tools"): "Scripts/tools directory",
            ("config", "conf", "settings"): "Configuration directory",
            (".git", ".hg", ".svn"): "Version control",
            ("node_modules", ".venv", "venv", "env", "__pycache__", ".tox"): "Generated/dependency directory",
            (".github",): "GitHub Actions & config",
            (".vscode",): "VS Code workspace settings",
            (".idea",): "JetBrains IDE settings",
        }
        for names, desc in defaults.items():
            if name in names:
                result["purpose"] = result["purpose"] or desc
                break

    else:
        ext = path.suffix.lower()
        result["language"] = LANG_MAP.get(ext)

        name = path.name.lower()
        if name in FRAMEWORK_MARKERS:
            result["framework"] = FRAMEWORK_MARKERS[name]

        if name.startswith("readme"):
            result["purpose"] = "Project documentation"
        elif name in (".gitignore", ".dockerignore", ".editorconfig", ".eslintrc", ".prettierrc"):
            result["purpose"] = "Configuration"
        elif ext in (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env"):
            result["purpose"] = "Configuration"
        elif ext == ".json" and "config" in name:
            result["purpose"] = "Configuration"
        elif ext in (".md", ".rst", ".txt"):
            result["purpose"] = "Documentation"
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
    """Scan a single DirEntry and return its record."""
    try:
        stat = entry.stat(follow_symlinks=False)
    except (OSError, PermissionError):
        return None

    is_dir = entry.is_dir(follow_symlinks=False)
    # Store the symlink path itself (not resolved) to avoid duplicates
    path_str = entry.path
    info = infer_purpose(Path(path_str), is_dir)

    return {
        "path": path_str,
        "parent": parent,
        "name": entry.name,
        "type": "dir" if is_dir else "file",
        "size": stat.st_size if not is_dir else None,
        "extension": Path(path_str).suffix.lower() if not is_dir else None,
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
    exclude_set = set(exclude)

    try:
        now = time.time()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scan_log (root, started, exclude_patterns, max_depth) VALUES (?, ?, ?, ?)",
            (str(root_path), now, json.dumps(exclude), max_depth)
        )
        scan_id = cur.lastrowid

        file_count = 0
        dir_count = 0
        batch = []
        visited: set[tuple[int, int]] = set()

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

                # Skip excluded directories
                if name in exclude_set and entry.is_dir(follow_symlinks=False):
                    record = _scan_entry(entry, str(directory))
                    if record:
                        record["purpose"] = f"Excluded directory ({name})"
                        batch.append(record)
                        dir_count += 1
                    continue

                # Hidden directories: catalog but only recurse into whitelisted ones
                if name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    record = _scan_entry(entry, str(directory))
                    if record:
                        batch.append(record)
                        dir_count += 1
                    if name in _RECURSE_HIDDEN:
                        _walk(Path(entry.path), depth + 1, str(Path(entry.path)))
                    continue

                # Hidden files: catalog them (.gitignore, .env, etc.)
                record = _scan_entry(entry, str(directory))
                if record is None:
                    continue

                batch.append(record)
                if entry.is_dir(follow_symlinks=False):
                    dir_count += 1
                    _walk(Path(entry.path), depth + 1, str(Path(entry.path)))
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
    finally:
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
            git_remote=excluded.git_remote,
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
    try:
        resolved = str(Path(path).resolve())

        row = conn.execute("SELECT * FROM files WHERE path=?", (resolved,)).fetchone()
        if not row:
            # Also try the non-resolved path (for symlinks stored as-is)
            row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            log(f"ERROR: '{path}' not in catalog. Run 'file-catalog scan' first.")
            raise typer.Exit(code=1)

        tags = [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE path=?", (row["path"],)).fetchall()]

        record = dict(row)
        if tags:
            record["tags"] = tags

        # Live git info
        if record["type"] == "dir":
            p = Path(record["path"])
            if (p / ".git").exists():
                commits = _safe_git(
                    ["git", "log", "--oneline", "-5", "--format=%h %s (%ar)"],
                    cwd=str(p),
                )
                if commits:
                    record["recent_commits"] = commits.split("\n")

                status = _safe_git(["git", "status", "--porcelain"], cwd=str(p))
                if status is not None:
                    record["uncommitted_changes"] = len(status.split("\n")) if status else 0
    finally:
        conn.close()

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

    try:
        escaped = _escape_like(query)
        pattern = f"%{escaped}%"

        # Try FTS first, fall back to LIKE
        try:
            fts_query = query.replace('"', '""')
            sql = """
                SELECT DISTINCT f.path, f.name, f.type, f.purpose, f.description, f.language, f.framework
                FROM files f
                LEFT JOIN files_fts ON f.rowid = files_fts.rowid
                WHERE files_fts MATCH ?
            """
            params = [fts_query]
            if type_filter:
                sql += " AND f.type = ?"
                params.append(type_filter)
            sql += " ORDER BY f.type ASC, f.name LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            # FTS not available or query error — fall back to LIKE
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
            sql += " ORDER BY f.type ASC, f.name LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
    finally:
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

    Output is plain text (not NDJSON) — designed for piping to call-llm.
    Use 'describe' for structured JSON output.
    """
    conn = get_db(db)
    try:
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
                COALESCE(SUM(CASE WHEN type='file' THEN 1 ELSE 0 END), 0) as files,
                COALESCE(SUM(CASE WHEN type='dir' THEN 1 ELSE 0 END), 0) as dirs
            FROM files WHERE parent = ?
        """, (resolved,)).fetchone()
    finally:
        conn.close()

    lines = [f"## {Path(path).name}"]
    lines.append(f"Path: {resolved}")
    lines.append(f"Contents: {stats['files']} files, {stats['dirs']} directories")
    lines.append("")

    for row in rows:
        icon = "📁" if row["type"] == "dir" else "📄"
        parts = [f"{icon} {row['name']}"]
        desc = row["description"] or row["purpose"]
        if desc:
            parts.append(f"— {desc}")
        if row["language"]:
            parts.append(f"[{row['language']}]")
        if row["framework"]:
            parts.append(f"({row['framework']})")
        lines.append(" ".join(parts))

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
    try:
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
    finally:
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
    try:
        if type_filter not in ("file", "dir", "all"):
            log(f"ERROR: --type must be 'file', 'dir', or 'all', got '{type_filter}'")
            raise typer.Exit(code=1)

        sql = "SELECT path, name, type, extension, language FROM files WHERE purpose IS NULL"
        params = []

        if type_filter != "all":
            sql += " AND type = ?"
            params.append(type_filter)

        sql += " ORDER BY path LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
    finally:
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
    try:
        totals = conn.execute("""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN type='file' THEN 1 ELSE 0 END), 0) as files,
                COALESCE(SUM(CASE WHEN type='dir' THEN 1 ELSE 0 END), 0) as dirs,
                COALESCE(SUM(CASE WHEN purpose IS NOT NULL THEN 1 ELSE 0 END), 0) as described,
                COALESCE(SUM(CASE WHEN purpose IS NULL THEN 1 ELSE 0 END), 0) as undescribed,
                COALESCE(SUM(size), 0) as total_size
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
    finally:
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
    try:
        if since is None:
            row = conn.execute("SELECT MAX(finished) as ts FROM scan_log WHERE finished IS NOT NULL").fetchone()
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

        # Load exclude patterns from most recent scan
        last_scan = conn.execute(
            "SELECT exclude_patterns FROM scan_log WHERE finished IS NOT NULL ORDER BY finished DESC LIMIT 1"
        ).fetchone()
        exclude_set = set()
        if last_scan and last_scan["exclude_patterns"]:
            try:
                exclude_set = set(json.loads(last_scan["exclude_patterns"]))
            except json.JSONDecodeError:
                pass

        # Batch-load known paths for each root (avoids N+1 queries)
        new_files = []
        deleted_files = []

        # Deduplicate roots (avoid overlapping scans)
        roots = conn.execute("SELECT DISTINCT root FROM scan_log WHERE finished IS NOT NULL").fetchall()
        root_paths = sorted([Path(r["root"]) for r in roots], key=lambda p: len(str(p)))

        # Remove nested roots (keep only top-level)
        filtered_roots = []
        for rp in root_paths:
            if not any(rp.is_relative_to(parent) for parent in filtered_roots if parent != rp):
                filtered_roots.append(rp)

        for root_path in filtered_roots:
            if not root_path.exists():
                continue

            escaped_root = _escape_like(str(root_path))
            known_paths: set[str] = set()
            for row in conn.execute(
                "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'",
                (f"{escaped_root}%",)
            ):
                known_paths.add(row["path"])

            # Walk with depth limit and symlink cycle detection
            visited: set[tuple[int, int]] = set()

            def _check_new(directory: Path, depth: int):
                if depth > max_depth or len(new_files) >= limit:
                    return
                try:
                    st = directory.stat()
                    key = (st.st_dev, st.st_ino)
                    if key in visited:
                        return
                    visited.add(key)
                except (OSError, PermissionError):
                    return

                try:
                    for entry in os.scandir(directory):
                        if len(new_files) >= limit:
                            break
                        name = entry.name
                        # Respect exclude patterns
                        if name in exclude_set:
                            continue
                        path_str = entry.path
                        if path_str not in known_paths:
                            try:
                                est = entry.stat(follow_symlinks=False)
                                new_files.append({
                                    "path": path_str,
                                    "name": name,
                                    "type": "dir" if entry.is_dir(follow_symlinks=False) else "file",
                                    "last_modified": datetime.fromtimestamp(est.st_mtime, tz=timezone.utc).isoformat(),
                                })
                            except (OSError, PermissionError):
                                pass
                            if entry.is_dir(follow_symlinks=False) and not name.startswith("."):
                                _check_new(Path(entry.path), depth + 1)
                except PermissionError:
                    pass

            _check_new(root_path, 0)

        # Deleted files — filter by scan roots, stream with LIMIT
        for root_path in filtered_roots:
            if len(deleted_files) >= limit:
                break
            escaped_root = _escape_like(str(root_path))
            for row in conn.execute(
                "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
                (f"{escaped_root}%", limit - len(deleted_files))
            ):
                if not Path(row["path"]).exists():
                    deleted_files.append({"path": row["path"]})
    finally:
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
    try:
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
    finally:
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
    """Read NDJSON descriptions from stdin and apply them.

    Expected format: {"path": "/full/path", "description": "what it is"}
    One per line. Preserves auto-inferred purpose; description is separate.
    """
    from .common import read_stdin_ndjson

    conn = get_db(db)
    try:
        count = 0
        skipped = 0

        for record in read_stdin_ndjson():
            path = record.get("path")
            desc = record.get("description")

            # Type validation
            if not isinstance(path, str) or not isinstance(desc, str):
                log(f"WARNING: invalid record (path and description must be strings): {record}")
                skipped += 1
                continue

            if not path or not desc:
                skipped += 1
                continue

            exists = conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone()
            if not exists:
                log(f"WARNING: '{path}' not in catalog, skipping")
                skipped += 1
                continue

            if dry_run:
                log(f"  would set description: {path} → {desc}")
            else:
                conn.execute("UPDATE files SET description=? WHERE path=?", (desc, path))
            count += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    action = "would apply" if dry_run else "applied"
    log(f"{action} {count} description(s), {skipped} skipped")
