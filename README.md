# AI CLI Toolkit

Composable command-line tools following Unix philosophy for building AI-assisted workflows.

Each tool does one thing, reads from stdin, writes to stdout, and logs to stderr. Tools are connected via pipes.

## Core Principles

- **Stdout is data, stderr is logging.** Machine-readable output (NDJSON) goes to stdout. Human-readable status goes to stderr.
- **NDJSON as interchange format.** One JSON object per line. Works with `jq`, `head`, `tail`, `wc -l`.
- **Each tool is independently useful.** Standalone or composed in a pipeline.
- **Fail loudly with exit codes.** Exit 0 on success, non-zero on failure.
- **Respect TTY vs pipe.** Interactive stdin may prompt; piped stdin reads silently.

## Installation

```bash
# Install dependencies
pip install llm
llm install llm-openrouter
llm install llm-ollama
llm keys set openrouter  # paste OpenRouter API key

# Install toolkit in dev mode
cd ai-cli
pip install -e .
```

## Tools

### `read-sql`

Execute SQL and emit rows as NDJSON.

```bash
read-sql --query "SELECT * FROM episodes LIMIT 10" --conn default
read-sql -q "SELECT offence_code, description FROM offences" | jq '.offence_code'
```

**Options:**
- `--query, -q` — SQL query to execute (required)
- `--conn, -c` — Connection name from config (default: "default")

**Supported drivers:** sqlite, duckdb (via `--conn` config)

**Robustness:** try/finally connection cleanup, PRAGMA busy_timeout, DML commit support, BLOB-safe JSON serialization, duplicate column deduplication, clean error messages (no raw tracebacks).

### `build-prompt`

Combine stdin data with a prompt template.

```bash
read-sql -q "SELECT * FROM episodes LIMIT 5" | build-prompt --template summarise.txt
```

Template format — use `{{context}}` (or custom `--var`) as placeholder:

```
Summarise the following data records. Identify key patterns.

Data:
{{context}}
```

**Options:**
- `--template, -t` — Path to template file (required)
- `--var, -v` — Placeholder name (default: "context")

Warns if the placeholder isn't found in the template.

### `call-llm`

Send a prompt to an LLM via the `llm` CLI ecosystem.

```bash
echo "Explain dbt snapshots" | call-llm
cat script.py | call-llm -s "Review this code for bugs"
```

**Options:**
- `--model, -m` — Model identifier (default: `openrouter/xiaomi/mimo-v2.5-pro`)
- `--system, -s` — System prompt
- `--timeout, -T` — Subprocess timeout in seconds (0 = auto, default)

**Auto-timeout** scales based on input size: `max(30, 15 + chars/200)`. Override with `--timeout 120` for a hard cap.

### `review-code`

Code review convenience wrapper. Reads code from stdin, outputs review to stdout.

```bash
cat etl_pipeline.py | review-code --focus security
echo "def add(a,b): return a+b" | review-code
```

**Options:**
- `--model, -m` — Model to use (default: `openrouter/xiaomi/mimo-v2.5-pro`)
- `--focus, -f` — Specific focus area (e.g. "security", "performance")
- `--timeout, -T` — Subprocess timeout in seconds (0 = auto, default)

### `extract-field`

Pull a specific field from NDJSON input. Supports dot notation.

```bash
read-sql -q "SELECT * FROM episodes" | extract-field episode_id --raw
echo '{"name": "Alice", "age": 30}' | extract-field name --raw
```

**Options:**
- `field` — Field name (supports `nested.key` notation)
- `--raw, -r` — Output raw values instead of JSON wrapper

Reports missing field counts to stderr. Prints empty string (not "None") for missing values in raw mode.

### `file-catalog`

Filesystem context awareness tool. Catalog, describe, and search your files by purpose.

#### Scanning

```bash
# Basic scan
file-catalog scan ~/projects --depth 4

# Auto-detect profile (code vs personal)
file-catalog scan ~ --profile auto

# Deep scan: content sampling, metadata extraction, fingerprinting
file-catalog scan ~ --deep

# Exclude directories
file-catalog scan ~ -x node_modules -x .cache -x snap
```

**Scan profiles:**
- `code` — optimised for software projects (markers, frameworks, git)
- `personal` — optimised for user files (media, documents, year archives)
- `auto` — detects based on directory contents (default)

**`--deep` flag enables:**
- Content sampling (shebangs, docstrings, imports, API patterns)
- Media metadata extraction (EXIF from photos, ID3 from audio, PDF metadata)
- Duplicate fingerprinting (size + blake2b hash of first 4KB)

#### Querying

```bash
# Describe a specific path
file-catalog describe ~/projects/my-app

# Search by name, purpose, or tag
file-catalog search "portfolio tracker"

# Generate a text summary for LLM context
file-catalog summary ~/projects | call-llm -s "What projects do I have?"

# Find duplicate files (requires --deep scan)
file-catalog duplicates

# Check for new/modified/deleted files
file-catalog changes

# Show stats
file-catalog stats

# Disk usage breakdown by directory
file-catalog du ~/projects --depth 2

# Only show files untouched for 90+ days
file-catalog du ~ --stale 90 --depth 1

# Sort by file count instead of size
file-catalog du ~ --sort files --limit 15

# Cleanup suggestions (dry run — nothing deleted by default)
file-catalog clean --stale 60 --min-size 50

# Actually delete to trash (safe — recoverable)
file-catalog clean --stale 90 --min-size 100 --delete

# Permanent delete (use with caution)
file-catalog clean --stale 365 --min-size 200 --delete --permanent
```

#### Cleanup Categories

`file-catalog clean` checks five categories:

1. **Stale files** — large files not modified in N days
2. **Duplicates** — files with identical content fingerprints
3. **Old downloads** — files sitting in ~/Downloads
4. **Backup directories** — dirs with "backup" in the path
5. **Cache/temp** — `__pycache__`, `node_modules`, `.venv`, etc.

Options: `--downloads/--no-downloads`, `--backups/--no-backups`, `--cache/--no-cache`

#### Tagging & Enrichment

```bash
# Add manual tags
file-catalog tag ~/projects/my-app "production" "api"

# Get items without purpose
file-catalog prompt-missing --count 5

# Auto-enrich via LLM
file-catalog enrich --count 10

# Apply descriptions from NDJSON
echo '{"path":"/dir","description":"old backups"}' | file-catalog apply-descriptions

# Preview without applying
file-catalog prompt-missing | call-llm | file-catalog apply-descriptions --dry-run
```

**Purpose inference from:**
- README files (first non-heading line)
- Package manifests (pyproject.toml, package.json, Cargo.toml, go.mod, etc.)
- Git remotes (read from .git/config directly)
- File extensions → language/framework mapping
- Framework markers (Django, Flask, Next.js, Vue, Angular, etc.)
- Directory names (src, tests, docs, scripts, config, etc.)
- Well-known paths (~/.ssh, ~/Downloads, ~/Documents, etc.)
- Filename patterns (test_*.py, *.spec.ts, *.migration, *.lock, etc.)
- Personal directory patterns (year archives, tax, photos, events, etc.)
- Media types (photos, video, audio, ebooks, fonts, design files)
- Content sampling with `--deep` (shebangs, docstrings, imports, API patterns)
- Media metadata with `--deep` (EXIF, ID3, PDF metadata)

**Database:** `~/.config/file-catalog/catalog.db` (override with `FILE_CATALOG_DB` env var)

## Example Pipelines

### Summarise query results

```bash
read-sql -q "SELECT * FROM episodes LIMIT 20" \
  | build-prompt -t templates/summarise.txt \
  | call-llm
```

### Generate code then review with a different model

```bash
echo "Write a Python ETL script that loads CSV to Redshift" \
  | call-llm \
  | review-code -m ollama/qwen2.5-coder
```

### Review an existing file

```bash
cat src/etl_pipeline.py | review-code --focus "error handling"
```

### Chain local and remote models

```bash
echo "Explain the tradeoffs of SCD Type 2 vs Type 1" \
  | call-llm -m ollama/qwen2.5-coder \
  | call-llm -s "Critique and improve this explanation"
```

### File catalog + LLM

```bash
# Summarise projects
file-catalog summary ~/clawd/projects | call-llm -s "Summarise my active projects"

# Search and filter
file-catalog search "python" | jq '.purpose' | sort -u

# Auto-enrich missing descriptions
file-catalog enrich --count 10 --confirm

# Find duplicates
file-catalog duplicates | jq '.size_mb * .count' -s 'add'

# Disk usage — biggest dirs untouched for 90+ days
file-catalog du ~ --stale 90 --depth 1 --limit 10

# Cleanup suggestions — pipe to LLM for prioritisation
file-catalog clean --stale 60 --min-size 100 | call-llm -s "Prioritise these cleanup targets by safety and impact"
```

## Configuration

### Database Connections

Set via environment variables or edit `CONNECTIONS` in `read_sql.py`:

```bash
export AI_CLI_DB_PATH="/path/to/database.db"
```

### File Catalog Database

```bash
export FILE_CATALOG_DB="/path/to/catalog.db"  # default: ~/.config/file-catalog/catalog.db
```

### LLM Setup

```bash
# Set OpenRouter key
llm keys set openrouter

# List available models
llm models

# Test a model
echo "hello" | llm -m openrouter/xiaomi/mimo-v2.5-pro
```

## Conventions

- All tools use `typer` for CLI argument parsing.
- Data tools (`read-sql`, `extract-field`, `file-catalog`) emit NDJSON to stdout.
- Text tools (`build-prompt`, `call-llm`, `review-code`, `file-catalog summary`) emit plain text to stdout.
- All diagnostic output goes to stderr via `log()`.
- All tools flush stdout immediately for pipe compatibility.
- Exit code 0 = success, non-zero = failure.
- `call-llm` and `review-code` auto-scale timeout based on input size.
- `read-sql` handles DML commits, BLOBs, duplicate columns, and PRAGMA queries safely.

## Roadmap

- [ ] Connection config for `read-sql` (YAML/env-var based, Redshift, SQL Server RDS)
- [ ] `format-output` — convert NDJSON to CSV, markdown tables, etc.
- [ ] `cache-response` — hash prompts, cache LLM responses to SQLite
- [ ] `diff-reviews` — send code to two models, diff their feedback
- [ ] Thin orchestrator for multi-step pipelines with error handling
- [ ] `file-catalog watch` — filesystem watcher for real-time change detection
- [ ] `file-catalog changes` — detect deletions (currently only detects new/modified)
