# poor-claude (`claude-no-p`)

A drop-in replacement for `claude -p` (headless mode) that routes prompts through
**persistent interactive Claude Code sessions** instead of spawning a new subprocess
per call — keeping your usage under a Claude subscription rather than billing as API
calls.

## Why

Starting June 2025, `claude -p` is billed as API usage separate from the subscription.
Interactive `claude` sessions (no `-p`) remain subscription-included. `poor-claude`
bridges the gap: it presents the same request/response interface as `claude -p`, but
under the hood it talks to a long-lived interactive session via MCP Channels and a
Stop hook.

## How it works

```
claude-no-p "your prompt"
     │
     ▼
 Daemon (HTTP)
     │   ┌─────────────────────────────────────┐
     │   │  Persistent claude session          │
     ├──►│  ┌──────────────┐  ┌─────────────┐ │
     │   │  │ MCP Channel  │  │  Stop hook  │ │
     │   │  │  (delivery)  │  │ (completion)│ │
     │   │  └──────────────┘  └─────────────┘ │
     │   └─────────────────────────────────────┘
     │
     ▼
  response text
```

- A local HTTP daemon manages one or more named sessions.
- Each session is a single long-lived `claude` process with `--dangerously-load-development-channels`.
- Prompts are delivered via an MCP Channel; the Stop hook signals completion back to the daemon.
- A PreToolUse hook enforces a deny-by-default permission policy (configurable via `--allowed-tools` / `--disallowed-tools`).

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on `PATH`)
- A Claude subscription (Max or higher recommended for heavy use)

## Installation

### Via uv (recommended)

```bash
# Install into your environment
uv pip install .

# Or install a stable wrapper script to ~/.local/bin
scripts/install-claude-no-p-wrapper
```

### Via pip

```bash
pip install .
```

## Quick start

```bash
# One-shot prompt (like claude -p)
claude-no-p "what is 2+2"

# Resume a named session
claude-no-p --session-id my-bot "follow-up question"

# With a specific model and effort level
claude-no-p --model claude-opus-4-5 --effort high "write me a haiku"

# Pipe input
echo "summarise this" | claude-no-p -p

# JSON output
claude-no-p --output-format json "hello"
```

## Session lifecycle

Sessions are persistent by default. The first request to a session ID starts a
Claude process; subsequent requests reuse it with full conversation history.

```bash
# Explicitly pre-create a session (optional)
claude-no-p --start-session --session-id my-bot

# List active sessions
claude-no-p --sessions

# Stop a session
claude-no-p --stop-session --session-id my-bot

# Prune expired sessions
claude-no-p --prune-sessions
```

## Tool permissions

`claude-no-p` defaults to **deny all tools** in interactive sessions to prevent
permission dialogs from blocking headless use. Explicitly allow what you need:

```bash
# Allow specific bash commands
claude-no-p --allowed-tools "Bash(ls *)" --allowed-tools "Bash(git *)" "what files are here"

# Allow all bash, deny destructive commands
claude-no-p --allowed-tools "Bash" --disallowed-tools "Bash(rm *)" "run the tests"

# Bypass permission checks entirely (trusted environments only)
claude-no-p --permission-mode bypassPermissions "do anything"
```

Permission rules persist for the lifetime of the session and can be updated on any
subsequent request without restarting — the hook reads the policy file fresh on each
tool call.

## CLI reference

### Prompt

| Flag | Description |
|---|---|
| `prompt` | Positional prompt argument |
| `-p`, `--print` | Read prompt from stdin |
| `--output-format` | `text` (default), `json`, `stream-json` |
| `--timeout` | Response timeout in seconds (default: 300) |

### Session

| Flag | Description |
|---|---|
| `--session-id UUID` | Reuse a specific session |
| `-r`, `--resume UUID` | Alias for `--session-id` |
| `--ttl DURATION` | Session TTL (e.g. `30m`, `2h`, `1d`) |
| `--keep-alive` | Session never expires |
| `--workdir PATH` | Working directory for the session |
| `--start-session` | Pre-create a session without sending a prompt |
| `--stop-session` | Stop a running session |
| `--sessions` | List all sessions |
| `--prune-sessions` | Remove expired sessions |
| `--shutdown` | Shut down the daemon |

### Model & runtime

| Flag | Description |
|---|---|
| `--model MODEL` | Model name or alias (`sonnet`, `opus`, …) |
| `--effort LEVEL` | `low`, `medium` (default), `high`, `xhigh`, `max` |
| `--system-prompt TEXT` | Replace the default system prompt |
| `--append-system-prompt TEXT` | Append to the default system prompt |
| `--tools TOOL …` | Restrict built-in tool set (repeat for multiple; `""` = disable all) |
| `--add-dir PATH` | Add a directory to Claude's tool access (repeat for multiple) |
| `--settings FILE` | Path to a Claude settings JSON file |

### Permissions

| Flag | Description |
|---|---|
| `--permission-mode MODE` | `default`, `auto`, `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan` |
| `--dangerously-skip-permissions` | Alias for `--permission-mode bypassPermissions` |
| `--allowed-tools RULE` | Allow a tool rule, e.g. `Bash(ls *)` (repeat for multiple) |
| `--disallowed-tools RULE` | Deny a tool rule — takes priority over allow (repeat for multiple) |

### Misc

| Flag | Description |
|---|---|
| `--dry-run` | Print the resolved request envelope without sending |
| `--debug` | Print raw server response to stderr |
| `--json` | Use JSON output for `--sessions` / `--prune-sessions` |

## Architecture notes

See [`docs/architecture.md`](docs/architecture.md) for a detailed description of the
daemon, session routing, hook design, and permission model.

## Development

```bash
# Install in editable mode
pip install -e .

# Run tests
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE).
