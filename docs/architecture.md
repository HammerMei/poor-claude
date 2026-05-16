# poor-claude Architecture

## Overview

`poor-claude` (`claude-no-p`) replaces `claude -p` headless invocations with a
persistent interactive session architecture. The goal is identical external behavior —
send a prompt, get a response — while avoiding per-call API billing by reusing a
long-lived `claude` process.

---

## Components

```
┌────────────────────────────────────────────────────────────────┐
│  Client (claude-no-p CLI / HTTP API)                          │
└───────────────────────────┬────────────────────────────────────┘
                            │ HTTP POST /requests
                            ▼
┌────────────────────────────────────────────────────────────────┐
│  Daemon (ControlServer + SessionRegistry)                      │
│                                                                │
│  • Manages N concurrent sessions, each keyed by               │
│    (session_id, workdir)                                       │
│  • Handles session lifecycle: create, freeze, restart, prune  │
│  • Writes per-session settings (hooks, MCP config, policy)    │
└──────────┬──────────────────────────┬──────────────────────────┘
           │ spawn                    │ HTTP callbacks
           ▼                          ▼
┌────────────────────┐   ┌────────────────────────────────────┐
│  claude process    │   │  Hooks                             │
│                    │   │  • Stop hook: POST /hook/stop      │
│  --session-id      │◄──│    signals response complete       │
│  --settings (MCP   │   │  • PreToolUse hook: permission     │
│    + hooks)        │   │    policy enforcement              │
│  --dangerously-    │   └────────────────────────────────────┘
│    load-dev-chans  │
└────────┬───────────┘
         │ MCP stdio
         ▼
┌────────────────────────────────────────────────────────────────┐
│  MCP Router (poor-claude MCP server)                          │
│  • Exposes a Channel tool for prompt delivery                 │
│  • Routes responses back to the waiting request handler       │
└────────────────────────────────────────────────────────────────┘
```

---

## Request flow

1. Client calls `POST /requests` with `{prompt, session_id, …}`.
2. Daemon resolves the session (creates if new, reuses if existing).
3. `prepare_launch_spec` writes:
   - A merged Claude settings file (hooks baked in)
   - A `tools-policy.json` with allow/disallow rules
   - A `.mcp.json` wiring the MCP Router into the session
4. Claude process is started (or reused if already running).
5. Prompt is published to the session's MCP Channel.
6. Claude processes the prompt; the Stop hook fires on completion.
7. Stop hook POSTs the response text to `/hook/stop`.
8. Daemon delivers the response to the waiting request handler.
9. Client receives the response.

---

## Session metadata and parameter classification

Session parameters are classified into three tiers:

### Hard params (immutable after session freeze)
Changing a hard param on an already-frozen session raises an error — the client
must stop and recreate the session to change these.

| Parameter | Reason |
|---|---|
| `settings_path` | Affects which settings file is loaded at launch |
| `permission_mode` | Determines hook injection and Claude's built-in permission behavior |
| `dangerously_load_development_channels` | Required for MCP Channel routing |

### Soft params (trigger restart + resume on change)
Changing a soft param stops the running Claude process and restarts it with
`--resume`, preserving conversation history.

| Parameter | |
|---|---|
| `effort` | Passed as `--effort` to the claude command |
| `model` | Passed as `--model` |
| `system_prompt` | Passed as `--system-prompt` |
| `append_system_prompt` | Passed as `--append-system-prompt` |
| `tools` | Passed as `--tools` |
| `add_dirs` | Passed as `--add-dir` (one per directory) |

### Immediate params (take effect on next tool call, no restart)
These are written to `tools-policy.json`; the hook reads the file fresh on every
tool invocation.

| Parameter | |
|---|---|
| `allowed_tools` | Allow-list rules (glob patterns per tool) |
| `disallowed_tools` | Deny-list rules — takes priority over allow |

---

## PreToolUse hook and permission model

`claude-no-p` injects a PreToolUse hook into every session (except
`bypassPermissions` mode, where it's omitted for efficiency).

### Why

In interactive `default` permission mode, Claude shows a dialog when an unlisted
tool is called. In a headless session this dialog blocks forever. The hook intercepts
every tool call and either approves or denies it before the dialog can appear.

### Policy

1. If `permission_mode != "default"` → pass through immediately (Claude's own logic handles it).
2. Check tool against the **disallow** list — match → deny.
3. Check tool against the **allow** list (settings hierarchy + policy file) — match → allow.
4. No match → deny.

### Rule syntax

Rules follow the same syntax as Claude Code's `permissions.allow`:

```
ToolName              # allow any call to this tool
ToolName(pattern)     # allow calls where the primary input matches a glob
```

Examples:
```
Bash                    # allow any bash command
Bash(ls *)              # allow ls with any arguments
Bash(python -m pytest *) # allow pytest
Read                    # allow any file read
Skill(text-to-speech)   # allow a specific skill
```

The "primary input" is tool-dependent: `command` for Bash, `file_path` for
Read/Write/Edit, `skill` for Skill, `url` for WebFetch, `query` for WebSearch.

### Policy file

At runtime, allow/disallow rules are written to `tools-policy.json` alongside the
session's other generated files. This file is re-read on every PreToolUse hook
invocation, so rules can be changed on any subsequent request without restarting
the Claude process.

---

## Stop hook

The Stop hook fires when Claude finishes generating a response. It POSTs to the
daemon's `/hook/stop` endpoint with the response text, unblocking the request
handler that is waiting for the reply.

If Claude produces no textual response (e.g. tool-only turns), the hook still fires
and the daemon delivers an empty string to the client.

---

## Daemon and state

The daemon is a single-threaded HTTP server running in a background process,
managed by a state file at `~/.poor-claude/state.json`. Multiple CLI invocations
share the same daemon via the state file's address.

State is stored per session:
- `SessionRegistry` — in-memory session map with metadata
- Route directories — per-session on-disk data (`~/.poor-claude/routes/<route-key>/`)
  containing the merged settings file, policy file, MCP config, and log files.

---

## MCP Channel routing

`claude -p` used `--output-format stream-json` to stream tokens over stdout.
`poor-claude` instead uses Claude Code's development MCP Channel feature:

- The MCP Router runs as an MCP server connected to the Claude process.
- It exposes a `Channel` tool that Claude can read from (prompt delivery) and write to (response delivery).
- Prompts are published to the channel; responses are read back via the channel or the Stop hook.

This allows the Claude process to remain interactive while still accepting
programmatic input and producing captured output.
