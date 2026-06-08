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

### `call-llm`

Send a prompt to an LLM via the `llm` CLI ecosystem.

```bash
echo "Explain dbt snapshots" | call-llm --model openrouter/minimax/minimax-m2.7
cat script.py | call-llm -s "Review this code for bugs"
```

**Options:**
- `--model, -m` — Model identifier (default: openrouter/minimax/minimax-m2.7)
- `--system, -s` — System prompt

### `review-code`

Code review convenience wrapper. Reads code from stdin, outputs review to stdout.

```bash
cat etl_pipeline.py | review-code --focus security
openclaw generate --task "build ETL" | review-code
```

**Options:**
- `--model, -m` — Model to use (default: openrouter/minimax/minimax-m2.7)
- `--focus, -f` — Specific focus area (e.g. "security", "performance")

### `extract-field`

Pull a specific field from NDJSON input. Supports dot notation.

```bash
read-sql -q "SELECT * FROM episodes" | extract-field episode_id --raw
echo '{"name": "Alice", "age": 30}' | extract-field name --raw
```

**Options:**
- `field` — Field name (supports `nested.key` notation)
- `--raw, -r` — Output raw values instead of JSON wrapper

## Example Pipelines

### Summarise query results

```bash
read-sql -q "SELECT * FROM episodes LIMIT 20" \
  | build-prompt -t templates/summarise.txt \
  | call-llm -m openrouter/minimax/minimax-m2.7
```

### Generate code then review with a different model

```bash
echo "Write a Python ETL script that loads CSV to Redshift" \
  | call-llm -m openrouter/minimax/minimax-m2.7 \
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
  | call-llm -m openrouter/minimax/minimax-m2.7 -s "Critique and improve this explanation"
```

## Configuration

### Database Connections

Set via environment variables or edit `CONNECTIONS` in `read_sql.py`:

```bash
export AI_CLI_DB_PATH="/path/to/database.db"
```

### LLM Setup

```bash
# Set OpenRouter key
llm keys set openrouter

# List available models
llm models

# Test a model
echo "hello" | llm -m openrouter/minimax/minimax-m2.7
```

## Conventions

- All tools use `typer` for CLI argument parsing.
- Data tools (`read-sql`, `extract-field`) emit NDJSON to stdout.
- Text tools (`build-prompt`, `call-llm`, `review-code`) emit plain text to stdout.
- All diagnostic output goes to stderr via `log()`.
- All tools flush stdout immediately for pipe compatibility.
- Exit code 0 = success, non-zero = failure.

## Roadmap

- [ ] Connection config for `read-sql` (YAML/env-var based, Redshift, SQL Server RDS)
- [ ] `format-output` — convert NDJSON to CSV, markdown tables, etc.
- [ ] `cache-response` — hash prompts, cache LLM responses to SQLite
- [ ] `diff-reviews` — send code to two models, diff their feedback
- [ ] Thin orchestrator for multi-step pipelines with error handling
