# ACG Migration: Replacing `claude -p` with Channels + Stop Hook

> **Context**: Starting 6/15, `claude -p` (headless mode) will be billed as API usage,
> separate from the subscription. Since ACG drives all Claude agents (老妹, 浪哥, 小浪錘)
> via `claude -p`, this creates extra cost. `claude` (interactive, no `-p`) stays
> subscription-included.
>
> **Goal**: Replace `claude -p` with an architecture that uses interactive Claude sessions.

---

## Architecture Overview

### Current (claude -p, per-message subprocess)

```
RC message
  → ACG connector
  → spawn: claude -p --resume <id> --output-format stream-json --verbose
  → read stdout stream for response
  → kill subprocess
```

### New (persistent session + Channels + Stop hook)

```
RC message
  → ACG connector
  → MCP notification: notifications/claude/channel { prompt: "..." }
  → Claude (persistent interactive session, no -p)
  → Claude replies
  → Stop hook fires → POST last_assistant_message → ACG callback
  → ACG sends response to RC
```

---

## Input: Channels API (MCP Push)

Claude Code v2.1.80+ supports receiving messages from MCP servers that declare
the `claude/channel` capability.

**MCP server capability declaration:**
```json
{
  "experimental": {
    "claude/channel": {}
  }
}
```

Validated against Claude Code v2.1.142 during this POC. Earlier notes that used
`true` did not keep the stdio MCP session alive reliably.

**Notification to inject a message:**
```json
{
  "jsonrpc": "2.0",
  "method": "notifications/claude/channel",
  "params": {
    "content": "<RC message content here>",
    "meta": {"request_id": "..."}
  }
}
```

Validated shape uses `content` plus optional `meta`. The older `prompt/channel`
shape is kept out of the implementation because it did not trigger the validated
Claude Code channel path in this environment.

**Session startup** (once per watcher, persistent):
```bash
claude --resume <session-id> \
  --settings /tmp/acg-claude-settings.json \
  --channels acg-mcp-server \
  --dangerously-load-development-channels \
  --dangerously-skip-permissions
```

---

## Output: Stop Hook

The Stop hook fires after every Claude response. Its stdin JSON includes
`last_assistant_message` directly — **no JSONL parsing needed**.

**Stop hook stdin (provided by Claude Code):**
```json
{
  "hook_event_name": "Stop",
  "session_id": "abc-123",
  "transcript_path": "/path/to/session.jsonl",
  "last_assistant_message": "Claude's response text here...",
  "cwd": "/path/to/workdir",
  "model": { "id": "...", "display_name": "..." }
}
```

**Stop hook script** (added to Claude settings):
```bash
#!/bin/bash
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
RESPONSE=$(echo "$INPUT"   | jq -r '.last_assistant_message')
curl -s -X POST "http://127.0.0.1:<ACG_PORT>/response-callback" \
     -H "Content-Type: application/json" \
     -d "{\"session_id\": \"$SESSION_ID\", \"response\": $(echo "$RESPONSE" | jq -Rs .)}"
```

---

## Permission Approval Flow — ✅ Unchanged

The current permission system uses a **`PreToolUse` HTTP hook** written into
a settings JSON file (`--settings <path>`). This is **not** a `-p`-only feature.

**How it works (same as today):**
1. `ClaudePermissionBroker` starts a local HTTP server on a random port
2. Writes settings JSON with `PreToolUse → http://127.0.0.1:<port>/hook`
3. Passes `--settings <path>` to the Claude process
4. When Claude wants to use a tool → POST to hook endpoint
5. Broker holds HTTP connection open → posts approval request to RC
6. Owner replies `/approve <id>` or `/deny <id>` in RC
7. Returns `{"decision": "allow/deny"}` → Claude continues or skips

**The `--settings` flag works identically in interactive mode.**
`ClaudePermissionBroker` and `ClaudeSettingsAdapter` require **zero changes**.

---

## What Changes vs. What Stays the Same

| Component | Change? | Notes |
|-----------|---------|-------|
| `ClaudePermissionBroker` | ❌ No change | HTTP server logic identical |
| `ClaudeSettingsAdapter` | ❌ No change | `--settings` still passed same way |
| `adapter.py` | ✅ Needs rewrite | Persistent session + MCP push instead of per-message subprocess |
| Stop hook script | ✅ New | Reads `last_assistant_message`, POSTs to ACG callback |
| ACG MCP server | ✅ New | Declares `claude/channel`, receives ACG push, sends notification |
| ACG callback endpoint | ✅ New | Small HTTP endpoint to receive stop hook responses |

---

## Implementation Plan

### Step 1 — ACG MCP Server (Python)
- Implement a minimal MCP server (JSON-RPC over stdio or HTTP)
- Declare `experimental["claude/channel"]: true` in capabilities
- Expose an internal API for ACG to push messages (e.g. `push_message(session_id, prompt)`)
- Send `notifications/claude/channel` when ACG has a new RC message

### Step 2 — ACG Callback Endpoint
- Add a small `asyncio` HTTP endpoint inside ACG
- Receives Stop hook POSTs with `{ session_id, response }`
- Resolves the pending `asyncio.Future` for that session

### Step 3 — Rewrite adapter.py
- Persistent session management (start once, keep alive)
- Remove per-message `claude -p` subprocess spawning
- Replace with: push via MCP → await callback future (with timeout)
- Pass both `--settings` (for PreToolUse hook) and `--channels acg-mcp`

### Step 4 — Settings file update
- Add Stop hook to the generated settings JSON:
```json
{
  "hooks": {
    "PreToolUse": [{ ... existing HTTP hook ... }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "/path/to/acg-stop-hook.sh"
      }]
    }]
  }
}
```

---

## Key Constraints

- **`provider must be firstParty`**: Channels only work with direct Anthropic API
  (subscription or API key direct). Bedrock/Vertex is not supported. ✅ We use direct.
- **`--dangerously-load-development-channels`**: Needed during dev to bypass allowlist.
  For production, the MCP server would need to be registered as a plugin.
- **Async flow change**: Current adapter is synchronous request-response.
  New adapter uses push + callback — requires `asyncio.Future` per in-flight message.

---

## Estimated Work

| Task | Complexity |
|------|-----------|
| MCP server with `claude/channel` | Medium (1 day) |
| Stop hook script + ACG callback endpoint | Easy (half day) |
| adapter.py rewrite (persistent session mgmt) | Medium (1 day) |
| Settings file update + integration testing | Easy (half day) |
| **Total** | **~3 days** |

---

*Analysis by 老妹 · 2026-05-14*
