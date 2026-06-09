"""File Context Awareness Tool — catalog, describe, and search your filesystem.

Version: 0.3.0
"""

import contextlib
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from .common import emit, log

__version__ = "0.3.0"

# ── Pipe safety ───────────────────────────────────────────────────────────────
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except AttributeError:
    pass

app = typer.Typer(help="Catalog, describe, and search your filesystem by purpose.", add_help_option=True)

def _show_version():
    print(f"file-catalog {__version__}")
    raise typer.Exit()

# ── Database ──────────────────────────────────────────────────────────────────

DEFAULT_DB = os.environ.get("FILE_CATALOG_DB", os.path.expanduser("~/.config/file-catalog/catalog.db"))
SCAN_BATCH = 500

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
        pass


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
            max_depth       INTEGER,
            profile         TEXT
        );
    """)
    _try_add_column(conn, "scan_log", "exclude_patterns", "TEXT")
    _try_add_column(conn, "scan_log", "max_depth", "INTEGER")
    _try_add_column(conn, "scan_log", "profile", "TEXT")
    _try_add_column(conn, "files", "media_type", "TEXT")
    _try_add_column(conn, "files", "metadata", "TEXT")
    _try_add_column(conn, "files", "fingerprint", "TEXT")
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
            USING fts5(name, purpose, description, tags, content='files', content_rowid='rowid')
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()


# ── Step 3: Well-known paths ─────────────────────────────────────────────────

WELL_KNOWN_PATHS = {
    "Downloads": "Browser downloads",
    "Documents": "User documents",
    "Desktop": "Desktop files",
    "Pictures": "Photos and images",
    "Music": "Music library",
    "Movies": "Video library",
    "Videos": "Video library",
    "Public": "Shared public files",
    "Templates": "Document templates",
    ".ssh": "SSH keys and config",
    ".config": "Application configuration (XDG)",
    ".local/share": "Application data (XDG)",
    ".local/bin": "User binaries",
    ".Trash": "Deleted files",
    ".cache": "Application cache",
    "Library/Application Support": "macOS application data",
    "Library/Preferences": "macOS preferences",
    "iCloudDrive": "iCloud Drive",
    "Dropbox": "Dropbox sync folder",
    "OneDrive": "OneDrive sync folder",
    "Google Drive": "Google Drive sync folder",
}

# ── Step 5: Personal directory patterns ───────────────────────────────────────

PERSONAL_DIR_PATTERNS = [
    (re.compile(r"^(19|20)\d{2}$"), "Year archive"),
    (re.compile(r"^(19|20)\d{2}[-_](0[1-9]|1[0-2])"), "Month archive"),
    (re.compile(r"(?i)^(tax|ato|bas|myob|xero)"), "Tax/financial records"),
    (re.compile(r"(?i)^(resume|cv)s?$"), "Career documents"),
    (re.compile(r"(?i)(wedding|birthday|christmas|holiday|vacation|trip)"), "Event folder"),
    (re.compile(r"(?i)^(backup|old|archive|legacy)"), "Archive/backup"),
    (re.compile(r"(?i)^receipts?$"), "Receipts"),
    (re.compile(r"(?i)^(invoice|statement|bill)s?$"), "Financial documents"),
    (re.compile(r"(?i)^scan(ned|s)?$"), "Scanned documents"),
    (re.compile(r"(?i)^screenshots?$"), "Screenshots"),
    (re.compile(r"(?i)^wallpapers?$"), "Wallpapers"),
    (re.compile(r"(?i)^(album|playlist)s?$"), "Music collection"),
    (re.compile(r"(?i)^(photo|picture|pic|camera)s?$"), "Photo collection"),
    (re.compile(r"(?i)^(video|movie|film|clip)s?$"), "Video collection"),
    (re.compile(r"(?i)^(game|steam|saves?)$"), "Game data"),
    (re.compile(r"(?i)^(work|client|project)s?$"), "Work documents"),
    (re.compile(r"(?i)^(font|typeface)s?$"), "Font collection"),
    (re.compile(r"(?i)^(icon|asset|resource)s?$"), "Design assets"),
    (re.compile(r"(?i)^(template|boilerplate)s?$"), "Templates"),
    (re.compile(r"(?i)^(meme|gif|sticker)s?$"), "Memes/stickers"),
    (re.compile(r"(?i)^(torrent|download)s?$"), "Torrents/downloads"),
    (re.compile(r"(?i)^(iso|vm|virtualbox|vmware)$"), "Disk images/VMs"),
    (re.compile(r"(?i)^(podcast|audiobook)s?$"), "Audio collection"),
    (re.compile(r"(?i)^(ebook|book|kindle)s?$"), "Book collection"),
    (re.compile(r"(?i)^(comic|manga)s?$"), "Comics/manga"),
]

# ── Step 6: Media/document type map ──────────────────────────────────────────

MEDIA_MAP = {
    # Photos
    ".jpg": "photo", ".jpeg": "photo", ".png": "image", ".gif": "image",
    ".heic": "photo", ".heif": "photo", ".webp": "image",
    ".raw": "photo", ".cr2": "photo", ".nef": "photo", ".arw": "photo",
    ".dng": "photo", ".orf": "photo", ".rw2": "photo",
    ".tiff": "image", ".tif": "image", ".bmp": "image", ".svg": "vector graphic",
    ".ico": "icon", ".avif": "image", ".jxl": "image",
    # Video
    ".mp4": "video", ".mkv": "video", ".avi": "video", ".mov": "video",
    ".wmv": "video", ".flv": "video", ".webm": "video", ".m4v": "video",
    ".mpg": "video", ".mpeg": "video", ".3gp": "video", ".ts": "video",
    # Audio
    ".mp3": "audio", ".flac": "audio", ".ogg": "audio", ".wav": "audio",
    ".aac": "audio", ".m4a": "audio", ".wma": "audio", ".opus": "audio",
    ".aiff": "audio", ".ape": "audio", ".mid": "audio", ".midi": "audio",
    # Documents
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".csv": "spreadsheet",
    ".ppt": "presentation", ".pptx": "presentation",
    ".odt": "document", ".ods": "spreadsheet", ".odp": "presentation",
    ".rtf": "document", ".pages": "document", ".numbers": "spreadsheet",
    ".key": "presentation", ".tex": "LaTeX document",
    # Ebooks
    ".epub": "ebook", ".mobi": "ebook", ".azw3": "ebook", ".fb2": "ebook",
    # Archives
    ".zip": "archive", ".tar": "archive", ".gz": "archive",
    ".bz2": "archive", ".xz": "archive", ".7z": "archive",
    ".rar": "archive", ".dmg": "disk image", ".iso": "disk image",
    ".img": "disk image", ".cab": "archive",
    # Fonts
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
    ".eot": "font",
    # Design
    ".psd": "Photoshop file", ".ai": "Illustrator file",
    ".sketch": "Sketch file", ".fig": "Figma file", ".xd": "Adobe XD file",
    ".indd": "InDesign file", ".blend": "Blender file",
    ".obj": "3D model", ".fbx": "3D model", ".stl": "3D model",
    ".dwg": "CAD drawing", ".dxf": "CAD drawing",
}

# ── Step 4: Filename patterns ────────────────────────────────────────────────

PURPOSE_PATTERNS = [
    (re.compile(r"(test_.*|.*_test)\.py$"), "Test module"),
    (re.compile(r".*\.spec\.(ts|js|tsx|jsx)$"), "Test spec"),
    (re.compile(r".*\.test\.(ts|js|tsx|jsx)$"), "Test file"),
    (re.compile(r".*\.stories\.(tsx|jsx|ts|js)$"), "Storybook component"),
    (re.compile(r".*\.(migration|migrate)\..*$"), "Database migration"),
    (re.compile(r".*_pb2\.pyi?$"), "Generated protobuf"),
    (re.compile(r".*\.min\.(js|css)$"), "Minified asset"),
    (re.compile(r".*\.d\.ts$"), "TypeScript type declaration"),
    (re.compile(r".*\.generated\.\w+$"), "Generated file"),
    (re.compile(r"^Dockerfile\..*$"), "Docker variant"),
    (re.compile(r"^docker-compose\..*\.ya?ml$"), "Docker Compose variant"),
    (re.compile(r"^\.env\..+$"), "Environment config variant"),
    (re.compile(r".*\.bak$"), "Backup file"),
    (re.compile(r".*\.orig$"), "Original/pre-merge file"),
    (re.compile(r".*\.lock$"), "Lock file"),
    (re.compile(r".*\.log(\.\d+)?$"), "Log file"),
    (re.compile(r"^\.eslint.*$"), "ESLint config"),
    (re.compile(r"^\.prettier.*$"), "Prettier config"),
    (re.compile(r"^\.babel.*$"), "Babel config"),
    (re.compile(r"^jest\.config\.*$"), "Jest config"),
    (re.compile(r"^webpack\.config\.*$"), "Webpack config"),
    (re.compile(r"^vite\.config\.*$"), "Vite config"),
    (re.compile(r"^tsconfig.*\.json$"), "TypeScript config"),
    (re.compile(r"^\.github/workflows/.*\.ya?ml$"), "GitHub Actions workflow"),
    (re.compile(r"^\.gitlab-ci.*\.ya?ml$"), "GitLab CI config"),
    (re.compile(r".*\.service$"), "Systemd service"),
    (re.compile(r".*\.timer$"), "Systemd timer"),
    (re.compile(r".*\.socket$"), "Systemd socket"),
    (re.compile(r"^Makefile\..*$"), "Make variant"),
    (re.compile(r"^CMakeLists\..*$"), "CMake variant"),
]

# ── Step 2: Compound marker inference ────────────────────────────────────────

def _compose_project_purpose(markers: list[str], frameworks: list[str]) -> str:
    """Compose a rich purpose string from multiple detected markers."""
    if not markers and not frameworks:
        return None
    base = markers[0] if markers else "Project"
    extras = frameworks + markers[1:]
    if extras:
        return f"{base} with {', '.join(extras)}"
    return base

# ── Step 7: Parent context ───────────────────────────────────────────────────

@dataclass
class ScanContext:
    """Context propagated down the directory tree during scanning."""
    profile: str = "auto"
    project_type: Optional[str] = None
    layer: Optional[str] = None
    content_type: Optional[str] = None

# ── Step 8: Content sampling ─────────────────────────────────────────────────

def _sample_content(path: Path, max_bytes: int = 2048) -> Optional[str]:
    """Read the first N bytes of a text file for content analysis."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except (OSError, PermissionError, UnicodeDecodeError):
        return None

def _infer_from_content(sample: str, ext: str) -> dict:
    """Infer purpose from file content."""
    result = {}
    lines = sample.split("\n")
    first_line = lines[0].strip() if lines else ""

    # Shebang
    if first_line.startswith("#!"):
        result["purpose"] = "Executable script"

    # Python entry point
    if 'if __name__ == "__main__"' in sample:
        result["purpose"] = result.get("purpose") or "Entry point / CLI"

    # Docstrings
    if ext == ".py":
        for line in lines[:10]:
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                doc = stripped.strip("'\"").strip()
                if doc and len(doc) > 10:
                    result["content_desc"] = doc[:200]
                break

    # SQL DDL
    sample_upper = sample.upper()
    if any(kw in sample_upper for kw in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX")):
        result["purpose"] = "Database migration/schema"

    # Test patterns
    if any(kw in sample for kw in ("class.*TestCase", "describe(", "it(", "def test_", "unittest")):
        result["purpose"] = result.get("purpose") or "Test module"

    # API patterns
    if any(kw in sample for kw in ("@app.route", "@router", "@api_view", "APIView", "ViewSet")):
        result["purpose"] = result.get("purpose") or "API endpoint/view"

    # Framework detection from imports
    framework_imports = {
        "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
        "torch": "PyTorch", "tensorflow": "TensorFlow",
        "react": "React", "vue": "Vue", "angular": "Angular",
        "express": "Express", "hono": "Hono",
        "pytest": "pytest", "unittest": "unittest",
    }
    for kw, fw in framework_imports.items():
        if f"import {kw}" in sample or f"from {kw}" in sample:
            result["detected_framework"] = fw
            break

    return result

# ── Step 9: Document metadata extraction ─────────────────────────────────────

def _extract_image_meta(path: Path) -> Optional[dict]:
    """Extract EXIF metadata from images."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return None
        meta = {}
        for tag_id, value in exif.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "DateTimeOriginal":
                meta["date_taken"] = str(value)
            elif tag == "Model":
                meta["camera"] = str(value)
            elif tag == "GPSInfo":
                meta["has_gps"] = True
        return meta if meta else None
    except Exception:
        return None

def _extract_audio_meta(path: Path) -> Optional[dict]:
    """Extract ID3/Vorbis tags from audio files."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path, easy=True)
        if audio is None or not audio.tags:
            return None
        meta = {}
        if "artist" in audio.tags:
            meta["artist"] = audio.tags["artist"][0]
        if "album" in audio.tags:
            meta["album"] = audio.tags["album"][0]
        if "title" in audio.tags:
            meta["title"] = audio.tags["title"][0]
        if "genre" in audio.tags:
            meta["genre"] = audio.tags["genre"][0]
        if audio.info and hasattr(audio.info, "length"):
            meta["duration_sec"] = round(audio.info.length)
        return meta if meta else None
    except Exception:
        return None

def _extract_pdf_meta(path: Path) -> Optional[dict]:
    """Extract metadata from PDF files."""
    try:
        import subprocess
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        meta = {}
        for line in result.stdout.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().lower()
                val = val.strip()
                if key == "title" and val:
                    meta["title"] = val
                elif key == "author" and val:
                    meta["author"] = val
                elif key == "pages":
                    try:
                        meta["pages"] = int(val)
                    except ValueError:
                        pass
        return meta if meta else None
    except Exception:
        return None

def _compose_media_description(media_type: str, meta: dict) -> Optional[str]:
    """Compose a human-readable description from media metadata."""
    if not meta:
        return None
    if media_type == "photo":
        parts = []
        if "date_taken" in meta:
            parts.append(f"Taken {meta['date_taken'][:10]}")
        if "camera" in meta:
            parts.append(meta["camera"])
        if meta.get("has_gps"):
            parts.append("GPS tagged")
        return ", ".join(parts) if parts else None
    elif media_type == "audio":
        parts = []
        if "artist" in meta:
            parts.append(meta["artist"])
        if "title" in meta:
            parts.append(f'"{meta["title"]}"')
        if "genre" in meta:
            parts.append(f"[{meta['genre']}]")
        if "duration_sec" in meta:
            m, s = divmod(meta["duration_sec"], 60)
            parts.append(f"{m}:{s:02d}")
        return " ".join(parts) if parts else None
    elif media_type == "document":
        parts = []
        if "title" in meta:
            parts.append(f'"{meta["title"]}"')
        if "author" in meta:
            parts.append(f"by {meta['author']}")
        if "pages" in meta:
            parts.append(f"({meta['pages']} pages)")
        return " ".join(parts) if parts else None
    return None

# ── Step 10: Size-based heuristics ───────────────────────────────────────────

PDF_SIZE_HINTS = [
    (0, 100_000, "Short document or form"),
    (100_000, 2_000_000, "Document or report"),
    (2_000_000, 20_000_000, "Long document or manual"),
    (20_000_000, None, "Book or large scanned document"),
]

def _size_hint(ext: str, size: int) -> Optional[str]:
    """Return a rough purpose based on file size."""
    if ext == ".pdf":
        for lo, hi, desc in PDF_SIZE_HINTS:
            if hi is None and size >= lo:
                return desc
            elif lo <= size < hi:
                return desc
    return None

# ── Step 11: Fingerprinting ──────────────────────────────────────────────────

def _compute_fingerprint(path: Path) -> Optional[str]:
    """Compute a lightweight fingerprint: size:blake2b(first_4kb)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            chunk = f.read(4096)
        h = hashlib.blake2b(chunk, digest_size=16).hexdigest()
        return f"{size}:{h}"
    except (OSError, PermissionError):
        return None

# ── Core inference ────────────────────────────────────────────────────────────

def infer_purpose(path: Path, is_dir: bool, ctx: Optional[ScanContext] = None) -> dict:
    """Infer the purpose, language, and framework of a file or directory."""
    result = {"purpose": None, "language": None, "framework": None}
    if ctx is None:
        ctx = ScanContext()

    if is_dir:
        # Step 3: Well-known paths
        try:
            rel = path.resolve().relative_to(Path.home())
            rel_str = str(rel)
            for suffix, desc in WELL_KNOWN_PATHS.items():
                if rel_str == suffix or rel_str.startswith(suffix + os.sep):
                    result["purpose"] = desc
                    break
        except (ValueError, OSError):
            pass

        # Step 2: Compound marker inference — collect ALL matches
        matched_markers = []
        matched_frameworks = []
        for marker, desc in PROJECT_MARKERS.items():
            if (path / marker).exists():
                matched_markers.append(desc)
        for marker, fw in FRAMEWORK_MARKERS.items():
            if (path / marker).exists():
                matched_frameworks.append(fw)

        if matched_markers or matched_frameworks:
            composed = _compose_project_purpose(matched_markers, matched_frameworks)
            result["purpose"] = result["purpose"] or composed
            if matched_frameworks:
                result["framework"] = matched_frameworks[0]

        # Git remote — read .git/config directly
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

        # README as fallback
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

        # Step 5: Personal directory patterns
        if not result["purpose"]:
            name = path.name
            for pattern, desc in PERSONAL_DIR_PATTERNS:
                if pattern.search(name):
                    result["purpose"] = desc
                    break

        # Special directory names (fallback)
        if not result["purpose"]:
            name_lower = path.name.lower()
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
                if name_lower in names:
                    result["purpose"] = desc
                    break

        # Step 7: Update context for children
        if result["purpose"] and any(kw in (result["purpose"] or "").lower() for kw in ("test", "migration", "config", "doc")):
            for kw, layer in [("test", "tests"), ("migration", "migrations"), ("config", "config"), ("doc", "docs")]:
                if kw in result["purpose"].lower():
                    # Context is read-only for children; they get a copy
                    pass

    else:
        ext = path.suffix.lower()
        name = path.name
        name_lower = name.lower()

        # Language from extension
        result["language"] = LANG_MAP.get(ext)

        # Framework from filename
        if name_lower in FRAMEWORK_MARKERS:
            result["framework"] = FRAMEWORK_MARKERS[name_lower]

        # Step 6: Media type classification
        media_type = MEDIA_MAP.get(ext)
        if media_type:
            result["purpose"] = media_type
            result["media_type"] = media_type

        # Step 4: Filename pattern matching (only if no purpose yet)
        if not result["purpose"]:
            for pattern, desc in PURPOSE_PATTERNS:
                if pattern.search(name):
                    result["purpose"] = desc
                    break

        # Exact name checks
        if not result["purpose"]:
            if name_lower.startswith("readme"):
                result["purpose"] = "Project documentation"
            elif name_lower in (".gitignore", ".dockerignore", ".editorconfig", ".eslintrc", ".prettierrc"):
                result["purpose"] = "Configuration"
            elif ext in (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env"):
                result["purpose"] = "Configuration"
            elif ext == ".json" and "config" in name_lower:
                result["purpose"] = "Configuration"
            elif ext in (".md", ".rst", ".txt"):
                result["purpose"] = "Documentation"

        # Shebang check for scripts
        if not result["purpose"] and ext in (".py", ".sh", ".bash", ".js", ".ts", ".rb", ".pl"):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    first_line = f.readline().strip()
                    if first_line.startswith("#!") or first_line.startswith('"""') or first_line.startswith("'''"):
                        result["purpose"] = "Script/module"
            except (OSError, PermissionError):
                pass

        # Step 10: Size-based heuristics (fallback)
        if not result["purpose"] and path.exists():
            try:
                size = path.stat().st_size
                hint = _size_hint(ext, size)
                if hint:
                    result["purpose"] = hint
            except (OSError, PermissionError):
                pass

    return result

# ── Step 1: Profile detection ────────────────────────────────────────────────

def _detect_profile(root: Path) -> str:
    """Auto-detect scan profile based on directory contents."""
    # Check well-known personal paths
    try:
        rel = root.resolve().relative_to(Path.home())
        rel_str = str(rel)
        for suffix in WELL_KNOWN_PATHS:
            if rel_str == suffix or rel_str.startswith(suffix + os.sep):
                return "personal"
    except (ValueError, OSError):
        pass

    # Check for project markers
    for marker in PROJECT_MARKERS:
        if (root / marker).exists():
            return "code"

    # Sample first 50 entries
    media_exts = set(MEDIA_MAP.keys())
    try:
        entries = list(os.scandir(root))[:50]
        if not entries:
            return "code"
        media_count = sum(1 for e in entries if not e.is_dir() and Path(e.name).suffix.lower() in media_exts)
        if media_count / len(entries) > 0.6:
            return "personal"
    except (PermissionError, OSError):
        pass

    return "code"

# ── Scan helpers ──────────────────────────────────────────────────────────────

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


def _scan_entry(entry: os.DirEntry, parent: str, ctx: ScanContext, deep: bool = False) -> Optional[dict]:
    """Scan a single DirEntry and return its record."""
    try:
        stat = entry.stat(follow_symlinks=False)
    except (OSError, PermissionError):
        return None

    is_dir = entry.is_dir(follow_symlinks=False)
    path_str = entry.path
    path = Path(path_str)

    # Step 7: Infer context for directories
    child_ctx = ScanContext(
        profile=ctx.profile,
        project_type=ctx.project_type,
        layer=ctx.layer,
        content_type=ctx.content_type,
    )
    if is_dir:
        name_lower = entry.name.lower()
        # Update project type from markers
        for marker, fw in FRAMEWORK_MARKERS.items():
            if (path / marker).exists():
                child_ctx.project_type = fw
                break
        # Detect layer
        for kw, layer in [("test", "tests"), ("migration", "migrations"), ("config", "config"), ("doc", "docs"), ("src", "src"), ("lib", "lib")]:
            if name_lower == kw or name_lower == kw + "s":
                child_ctx.layer = layer
                break
        # Detect content type for personal
        for pattern, ctype in PERSONAL_DIR_PATTERNS:
            if pattern.search(entry.name):
                child_ctx.content_type = ctype
                break

    info = infer_purpose(path, is_dir, child_ctx)

    # Step 8: Deep content sampling
    deep_meta = {}
    if deep and not is_dir:
        ext = path.suffix.lower()
        text_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs", ".sh", ".sql", ".md", ".txt", ".cfg", ".ini", ".yaml", ".yml", ".toml", ".json"}
        if ext in text_exts and not info.get("purpose"):
            sample = _sample_content(path)
            if sample:
                deep_meta = _infer_from_content(sample, ext)
                if deep_meta.get("purpose") and not info.get("purpose"):
                    info["purpose"] = deep_meta["purpose"]
                if deep_meta.get("detected_framework") and not info.get("framework"):
                    info["framework"] = deep_meta["detected_framework"]
                if deep_meta.get("content_desc"):
                    info["content_desc"] = deep_meta["content_desc"]

    # Step 9: Document metadata extraction (deep + personal)
    media_desc = None
    if deep and not is_dir:
        media_type = info.get("media_type")
        if media_type == "photo":
            meta = _extract_image_meta(path)
            if meta:
                media_desc = _compose_media_description("photo", meta)
                info["media_meta"] = meta
        elif media_type == "audio":
            meta = _extract_audio_meta(path)
            if meta:
                media_desc = _compose_media_description("audio", meta)
                info["media_meta"] = meta
        elif media_type == "document" and path.suffix.lower() == ".pdf":
            meta = _extract_pdf_meta(path)
            if meta:
                media_desc = _compose_media_description("document", meta)
                info["media_meta"] = meta

    # Step 11: Compute fingerprint for files
    fingerprint = None
    if not is_dir and deep:
        fingerprint = _compute_fingerprint(path)

    # Build description from content/media metadata
    description = None
    if media_desc:
        description = media_desc
    elif info.get("content_desc"):
        description = info["content_desc"]

    return {
        "path": path_str,
        "parent": parent,
        "name": entry.name,
        "type": "dir" if is_dir else "file",
        "size": stat.st_size if not is_dir else None,
        "extension": path.suffix.lower() if not is_dir else None,
        "purpose": info.get("purpose"),
        "description": description,
        "language": info.get("language"),
        "framework": info.get("framework"),
        "git_remote": info.get("git_remote"),
        "media_type": info.get("media_type"),
        "metadata": json.dumps(info.get("media_meta")) if info.get("media_meta") else None,
        "fingerprint": fingerprint,
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
    profile: str = typer.Option("auto", "--profile", "-p", help="Scan profile: code, personal, or auto"),
    deep: bool = typer.Option(False, "--deep", help="Deep scan: content sampling, metadata extraction, fingerprinting"),
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
):
    """Scan a directory tree and catalog all files and directories."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        log(f"ERROR: '{root}' is not a directory")
        raise typer.Exit(code=1)

    # Step 1: Profile detection
    if profile == "auto":
        profile = _detect_profile(root_path)
        log(f"auto-detected profile: {profile}")
    elif profile not in ("code", "personal"):
        log(f"ERROR: --profile must be 'code', 'personal', or 'auto', got '{profile}'")
        raise typer.Exit(code=1)

    conn = get_db(db)
    exclude_set = set(exclude)
    ctx = ScanContext(profile=profile)

    try:
        now = time.time()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scan_log (root, started, exclude_patterns, max_depth, profile) VALUES (?, ?, ?, ?, ?)",
            (str(root_path), now, json.dumps(exclude), max_depth, profile)
        )
        scan_id = cur.lastrowid

        file_count = 0
        dir_count = 0
        batch = []
        visited: set[tuple[int, int]] = set()

        log(f"scanning {root_path} (depth={max_depth}, profile={profile}, deep={deep})")

        def _walk(directory: Path, depth: int, parent: str, walk_ctx: ScanContext):
            nonlocal file_count, dir_count, batch
            if depth > max_depth:
                return

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

                if name in exclude_set and entry.is_dir(follow_symlinks=False):
                    record = _scan_entry(entry, str(directory), walk_ctx, deep)
                    if record:
                        record["purpose"] = f"Excluded directory ({name})"
                        batch.append(record)
                        dir_count += 1
                    continue

                if name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    record = _scan_entry(entry, str(directory), walk_ctx, deep)
                    if record:
                        batch.append(record)
                        dir_count += 1
                    if name in _RECURSE_HIDDEN:
                        child_ctx = ScanContext(
                            profile=walk_ctx.profile,
                            project_type=walk_ctx.project_type,
                            layer=walk_ctx.layer,
                            content_type=walk_ctx.content_type,
                        )
                        _walk(Path(entry.path), depth + 1, str(Path(entry.path)), child_ctx)
                    continue

                record = _scan_entry(entry, str(directory), walk_ctx, deep)
                if record is None:
                    continue

                batch.append(record)
                if entry.is_dir(follow_symlinks=False):
                    dir_count += 1
                    # Build child context
                    child_ctx = ScanContext(
                        profile=walk_ctx.profile,
                        project_type=walk_ctx.project_type,
                        layer=walk_ctx.layer,
                        content_type=walk_ctx.content_type,
                    )
                    name_lower = entry.name.lower()
                    for marker, fw in FRAMEWORK_MARKERS.items():
                        if (Path(entry.path) / marker).exists():
                            child_ctx.project_type = fw
                            break
                    for kw, layer in [("test", "tests"), ("migration", "migrations"), ("config", "config"), ("doc", "docs")]:
                        if name_lower == kw or name_lower == kw + "s":
                            child_ctx.layer = layer
                            break
                    for pattern, ctype in PERSONAL_DIR_PATTERNS:
                        if pattern.search(entry.name):
                            child_ctx.content_type = ctype
                            break
                    _walk(Path(entry.path), depth + 1, str(Path(entry.path)), child_ctx)
                else:
                    file_count += 1

                if len(batch) >= SCAN_BATCH:
                    _flush(conn, batch)
                    batch = []

        _walk(root_path, 0, str(root_path), ctx)

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
    emit({"status": "ok", "files": file_count, "dirs": dir_count, "root": str(root_path), "profile": profile, "deep": deep})


def _flush(conn: sqlite3.Connection, batch: list[dict]):
    """Upsert a batch of records into the files table."""
    conn.executemany("""
        INSERT INTO files (path, parent, name, type, size, extension, purpose, description, language, framework, git_remote, media_type, metadata, fingerprint, last_modified, last_scanned)
        VALUES (:path, :parent, :name, :type, :size, :extension, :purpose, :description, :language, :framework, :git_remote, :media_type, :metadata, :fingerprint, :last_modified, :last_scanned)
        ON CONFLICT(path) DO UPDATE SET
            parent=excluded.parent, name=excluded.name, type=excluded.type,
            size=excluded.size, extension=excluded.extension,
            purpose=COALESCE(excluded.purpose, files.purpose),
            description=COALESCE(excluded.description, files.description),
            language=excluded.language, framework=excluded.framework,
            git_remote=excluded.git_remote,
            media_type=COALESCE(excluded.media_type, files.media_type),
            metadata=COALESCE(excluded.metadata, files.metadata),
            fingerprint=COALESCE(excluded.fingerprint, files.fingerprint),
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
            row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            log(f"ERROR: '{path}' not in catalog. Run 'file-catalog scan' first.")
            raise typer.Exit(code=1)

        tags = [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE path=?", (row["path"],)).fetchall()]

        record = dict(row)
        if tags:
            record["tags"] = tags

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

    # Parse metadata JSON if present
    if record.get("metadata"):
        try:
            record["metadata"] = json.loads(record["metadata"])
        except json.JSONDecodeError:
            pass

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

        try:
            fts_query = query.replace('"', '""')
            sql = """
                SELECT DISTINCT f.path, f.name, f.type, f.purpose, f.description, f.language, f.framework, f.media_type
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
            sql = """
                SELECT DISTINCT f.path, f.name, f.type, f.purpose, f.description, f.language, f.framework, f.media_type
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
    """Generate a plain-text summary of a directory for LLM context."""
    conn = get_db(db)
    try:
        resolved = str(Path(path).resolve())

        rows = conn.execute("""
            SELECT name, type, purpose, language, framework, description, media_type
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
        if row["media_type"] and row["media_type"] not in (row["description"] or ""):
            parts.append(f"{{{row['media_type']}}}")
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

        media = conn.execute("""
            SELECT media_type, COUNT(*) as count
            FROM files
            WHERE media_type IS NOT NULL AND type='file'
            GROUP BY media_type
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()

        tag_count = conn.execute("SELECT COUNT(*) as c FROM tags").fetchone()["c"]

        recent_scans = conn.execute("""
            SELECT root, started, finished, file_count, dir_count, profile
            FROM scan_log
            WHERE finished IS NOT NULL
            ORDER BY started DESC
            LIMIT 5
        """).fetchall()

        dupes = conn.execute("""
            SELECT COUNT(*) as groups, SUM(cnt - 1) as extra_files
            FROM (SELECT fingerprint, COUNT(*) as cnt FROM files WHERE fingerprint IS NOT NULL GROUP BY fingerprint HAVING cnt > 1)
        """).fetchone()
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
        "media_types": {r["media_type"]: r["count"] for r in media},
        "duplicate_groups": dupes["groups"] if dupes else 0,
        "duplicate_files": dupes["extra_files"] if dupes else 0,
        "recent_scans": [
            {
                "root": r["root"],
                "files": r["file_count"],
                "dirs": r["dir_count"],
                "profile": r["profile"],
                "date": datetime.fromtimestamp(r["started"], tz=timezone.utc).isoformat(),
            }
            for r in recent_scans
        ],
    }
    emit(record)


@app.command()
def duplicates(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    min_group: int = typer.Option(2, "--min", help="Minimum files per group"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max groups to show"),
):
    """Find duplicate files based on size + content fingerprint."""
    conn = get_db(db)
    try:
        rows = conn.execute("""
            SELECT fingerprint, GROUP_CONCAT(path) as paths, COUNT(*) as cnt,
                   MAX(size) as size
            FROM files
            WHERE fingerprint IS NOT NULL
            GROUP BY fingerprint
            HAVING cnt >= ?
            ORDER BY cnt DESC, size DESC
            LIMIT ?
        """, (min_group, limit)).fetchall()
    finally:
        conn.close()

    if not rows:
        log("no duplicates found (run 'scan --deep' first to enable fingerprinting)")
        return

    for row in rows:
        paths = row["paths"].split(",")
        emit({
            "fingerprint": row["fingerprint"],
            "count": row["cnt"],
            "size_bytes": row["size"],
            "size_mb": round(row["size"] / 1024 / 1024, 2) if row["size"] else 0,
            "paths": paths,
        })

    log(f"{len(rows)} duplicate group(s) found")


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

        last_scan = conn.execute(
            "SELECT exclude_patterns FROM scan_log WHERE finished IS NOT NULL ORDER BY finished DESC LIMIT 1"
        ).fetchone()
        exclude_set = set()
        if last_scan and last_scan["exclude_patterns"]:
            try:
                exclude_set = set(json.loads(last_scan["exclude_patterns"]))
            except json.JSONDecodeError:
                pass

        new_files = []
        deleted_files = []

        roots = conn.execute("SELECT DISTINCT root FROM scan_log WHERE finished IS NOT NULL").fetchall()
        root_paths = sorted([Path(r["root"]) for r in roots], key=lambda p: len(str(p)))

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
def enrich(
    db: str = typer.Option(DEFAULT_DB, "--db", help="Catalog database path"),
    count: int = typer.Option(10, "--count", "-n", help="Items per batch"),
    type_filter: str = typer.Option("dir", "--type", "-t", help="Filter: file, dir, or all"),
    focus: str = typer.Option(None, "--focus", "-f", help="Filter by parent path prefix"),
    llm_cmd: str = typer.Option("call-llm", "--llm-cmd", help="LLM command to use"),
    batch_size: int = typer.Option(10, "--batch-size", help="Items per LLM call"),
    confirm: bool = typer.Option(False, "--confirm", "-c", help="Prompt for confirmation before applying"),
):
    """Auto-enrich missing descriptions using an LLM."""
    from .common import read_stdin_ndjson

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
        params.append(min(count, batch_size))

        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        log("everything has a purpose!")
        return

    # Build prompt
    items = []
    for r in rows:
        items.append({"path": r["path"], "name": r["name"], "type": r["type"]})

    prompt_lines = [
        f"Describe what each of these {len(items)} items is for. Reply as NDJSON, one per line:",
        f'{{"path": "/path", "description": "what it is"}}',
        "",
    ]
    for item in items:
        icon = "📁" if item["type"] == "dir" else "📄"
        prompt_lines.append(f"{icon} {item['path']}")

    prompt = "\n".join(prompt_lines)
    log(f"sending {len(items)} items to {llm_cmd}")

    # Call LLM
    try:
        result = subprocess.run(
            [llm_cmd], input=prompt, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            log(f"ERROR: {llm_cmd} failed: {result.stderr.strip()}")
            raise typer.Exit(code=1)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"ERROR: failed to run {llm_cmd}: {e}")
        raise typer.Exit(code=1)

    # Parse response as NDJSON
    descriptions = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if isinstance(record.get("path"), str) and isinstance(record.get("description"), str):
                descriptions.append(record)
        except json.JSONDecodeError:
            continue

    if not descriptions:
        log("WARNING: no valid descriptions parsed from LLM response")
        return

    if confirm:
        for desc in descriptions:
            log(f"  {desc['path']} → {desc['description']}")
        answer = input("Apply these descriptions? [y/N] ").strip().lower()
        if answer != "y":
            log("cancelled")
            return

    # Apply
    conn = get_db(db)
    try:
        count_applied = 0
        for desc in descriptions:
            conn.execute("UPDATE files SET description=? WHERE path=?", (desc["description"], desc["path"]))
            count_applied += 1
        conn.commit()
    finally:
        conn.close()

    log(f"applied {count_applied} description(s) via {llm_cmd}")


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

            if not isinstance(path, str) or not isinstance(desc, str):
                log(f"WARNING: invalid record: {record}")
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
