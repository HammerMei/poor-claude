# `poor-claude` / `claude-no-p` POC Implementation Proposal

> **Goal**: Build a standalone proof of concept that behaves like `claude -p`,
> but routes prompts through a persistent interactive Claude Code session using
> MCP Channels plus hooks. This intentionally avoids direct ACG integration until
> the mechanics are proven end-to-end.
>
> **Project name**: `poor-claude`
>
> **CLI name**: `claude-no-p`

---

## 1. Problem Statement

`claude -p` currently provides a simple request/response contract:

```bash
claude -p "hello"
```

The caller sends one prompt, waits for one assistant response, then exits.

The target architecture in `acg-no-claude-p-architecture.md` replaces that
subprocess-per-message model with:

1. a persistent interactive Claude Code session,
2. an MCP server that injects prompts through `notifications/claude/channel`, and
3. a `Stop` hook that sends Claude's final response back to a local callback.

Before wiring this into ACG, the POC should prove the same lifecycle locally with
the `claude-no-p` CLI:

```bash
claude-no-p "hello"
```

---

## 2. POC Scope

### In scope

- Create a local MCP Channels server.
- Create a local callback server for hook responses.
- Generate Claude Code settings containing the required hooks.
- Start or connect to persistent interactive Claude Code instances, addressed by
  `session_id` plus project/workdir scope.
- Implement a `claude-no-p` CLI that:
  - accepts prompt input from argv or stdin,
  - routes the request to the persistent Claude instance for the requested
    `session_id` in the requested project/workdir,
  - creates a new persistent Claude instance when no `session_id` is provided,
  - pushes the prompt into Claude through MCP Channels,
  - waits for the `Stop` hook callback,
  - prints the assistant response to stdout,
  - exits with a useful status code.

### Out of scope

- Direct ACG adapter rewrite.
- Rocket.Chat message routing.
- Multi-agent watcher orchestration.
- Production MCP plugin registration.
- Full `claude -p` flag compatibility.
- Local daemon authentication. The POC binds to localhost and is intended as a
  developer tool; bearer-token auth can be added later if the daemon is exposed
  beyond trusted local processes.

This POC is intentionally narrow: prove the transport loop first, then migrate
ACG only after the contract is stable.

---

## 3. Assumptions to Validate Early

These are not treated as facts until verified during implementation:

1. Claude Code can load a local development MCP channel server using
   `--channels <name>` and `--dangerously-load-development-channels`.
2. A development MCP server can emit `notifications/claude/channel` after Claude
   has connected to it.
3. The `Stop` hook payload includes a `transcript_path`; interactive sessions do
   not reliably include `last_assistant_message`, so the POC parses the latest
   assistant message from the transcript when needed.
4. `--settings <path>` applies both `Stop` and `PreToolUse` hooks in interactive
   mode.
5. A prompt injected through Channels produces exactly one relevant `Stop`
   callback for normal single-turn usage.
6. The user-facing `session_id` can be used directly as the Claude Code
   `--session-id <session_id>` value, and the Stop hook will report the same
   `session_id`.

If any assumption fails, the POC should stop and document the observed behavior
before adding workaround complexity.

---

## 4. Proposed Components

```text
┌─────────────────┐
│ claude-no-p CLI │
│ prompt in       │
└────────┬────────┘
         │ HTTP / local IPC: enqueue prompt + request_id
         ▼
┌──────────────────────────┐
│ POC control server       │
│ - session registry       │
│ - pending request map    │
│ - MCP push endpoint      │
│ - hook callback endpoint │
└───────┬──────────┬───────┘
        │          ▲
        │ MCP      │ Stop hook HTTP POST
        ▼          │
┌──────────────────────────┐
│ MCP Channels server      │
│ declares claude/channel  │
└───────┬──────────────────┘
        │ notifications/claude/channel
        ▼
┌──────────────────────────┐
│ persistent Claude Code   │
│ interactive, no -p       │
└──────────────────────────┘
```

For implementation simplicity, the POC can combine the control server and MCP
server in one Python process if the MCP transport permits it. The proposal keeps
them conceptually separate so the responsibilities stay clear.

---

## 5. Request/Response Contract

### CLI request

```bash
claude-no-p "summarize this"
echo "summarize this" | claude-no-p
```

The CLI creates a unique `request_id`. It also resolves a target `session_id`:

- If `--session-id <id>` is provided, the request is routed to that persistent
  Claude instance.
- If no session id is provided, the control server creates a new persistent
  Claude instance and returns its generated `session_id`.

Routing also includes project/workdir scope. Two requests with the same
`session_id` but different project directories must not accidentally share the
same persistent Claude process, because Claude Code session state and settings
are project-scoped.

The CLI sends:

```json
{
  "request_id": "uuid",
  "session_id": "optional-existing-session-id",
  "project_dir": "/absolute/project/path",
  "prompt": "summarize this",
  "timeout_seconds": 300
}
```

### Channel notification

The MCP side injects the prompt into Claude:

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/claude/channel",
  "params": {
    "content": "summarize this",
    "meta": {"request_id": "uuid"}
  }
}
```

This `content`/`meta` shape was validated against Claude Code v2.1.142. Earlier
drafts used `prompt`/`channel`, but that shape did not match the working channel
path in the local validation run.

Recommended prompt wrapper for correlation:

```text
<poor-claude-request id="uuid">
summarize this
</poor-claude-request>
```

The request id should also be tracked server-side. The wrapper is only a backup
for debugging; the primary correlation should be controlled by the POC runtime.

The MCP notification must be sent to the MCP connection associated with the
resolved `(project_dir, session_id)` route, not broadcast to every Claude
instance.

### Stop hook callback

The hook posts the final response to the local control server:

```json
{
  "session_id": "resolved-session-id",
  "request_id": "uuid-if-known",
  "response": "assistant response text",
  "transcript_path": "/path/to/session.jsonl"
}
```

If the hook cannot infer `request_id`, the control server should map the next
`Stop` callback for that `(project_dir, session_id)` route to the single
in-flight request for that same route. The POC should start with one request at a
time per route to avoid ambiguous correlation.

### Session identity

Prefer a single user-visible session id everywhere:

```text
user-facing session_id == Claude Code --session-id/--resume value == Stop hook session_id
```

For example:

```bash
claude-no-p --session-id demo -p "hello"
```

should launch or reuse Claude as:

```bash
claude --session-id demo ...
```

and the Stop hook should ideally report:

```json
{
  "session_id": "demo"
}
```

This keeps the mental model simple for standalone users and later ACG operators.

The route identity is still scoped by project/workdir:

```text
route_key = (canonical_project_dir, session_id)
```

This matches Claude Code's storage model where transcripts are under a
project-derived path. It also prevents a UUID collision or accidental reuse from
crossing project boundaries.

This must be validated before depending on it. If Claude Code canonicalizes or
replaces the provided `--session-id` value, the implementation should pause and
report the observed behavior before introducing a hidden mapping layer. A mapping
fallback is acceptable only if direct identity cannot be made to work.

---

## 6. Implementation Plan

### Phase 0 — Skeleton and CLI shape

Deliverables:

- `poor_claude/` Python package or equivalent small module layout.
- `claude-no-p` executable entrypoint.
- Basic CLI parsing:
  - prompt from argv,
  - prompt from stdin,
  - prompt from `-p/--print`,
  - `--timeout`,
  - `--session-id`,
  - `--workdir`,
  - `--ttl`,
  - `--keep-alive`,
  - `--debug`.

Success criteria:

- `claude-no-p --help` works.
- `echo hi | claude-no-p --dry-run` prints the normalized request envelope without
  launching Claude.
- Invalid prompt input combinations fail fast, e.g.
  `claude-no-p -p "foo" "bar"`.

### Phase 1 — Hook callback server

Deliverables:

- Local HTTP server with:
  - `POST /requests` from CLI,
  - `POST /hook/stop` from Claude Stop hook,
  - `GET /healthz`,
  - `GET /sessions` for debugging active persistent instances,
  - optional `GET /requests/<id>` for debugging.
- Session registry keyed by `session_id`, storing:
  - canonical project/workdir,
  - route key `(project_dir, session_id)`,
  - Claude process handle or supervisor metadata,
  - MCP connection/channel handle,
  - generated settings path,
  - workdir,
  - active pending request, if any.
- Pending request registry with timeout handling.
- Stop hook script that reads stdin JSON and posts to `/hook/stop`.
- Background daemon lifecycle:
  - first CLI request auto-starts the control daemon if it is not running,
  - daemon discovery uses a local state file containing the listen address and
    daemon pid,
  - stale state files are detected and replaced,
  - daemon owns all persistent Claude child processes.

Success criteria:

- A manually invoked hook command resolves a pending CLI request.
- Timeout returns non-zero exit code and useful stderr.
- Repeated CLI invocations connect to the same daemon instead of starting a new
  control server every time.

### Phase 2 — MCP Channels server

Deliverables:

- Minimal MCP server declaring:

```json
{
  "experimental": {
    "claude/channel": {}
  }
}
```

- Internal queue for outbound Claude channel notifications.
- Bridge from `POST /requests` to `notifications/claude/channel`.

Success criteria:

- Claude Code connects to the MCP server.
- Server logs show channel capability negotiation.
- A queued prompt is sent as `notifications/claude/channel`.
- Two different routes can hold independent MCP connections or queues, and a
  notification for one route is not broadcast to the other. A route differs when
  either `session_id` or `project_dir` differs.

### Phase 3 — Claude instance launcher

Deliverables:

- Settings generator that writes a temporary settings file with the Stop hook.
- Launcher command equivalent to:

```bash
claude --session-id <session-id> \
  --settings <generated-settings.json> \
  --channels <poc-mcp-server-name> \
  --dangerously-load-development-channels
```

- Process supervisor that can:
  - detect an already running POC session for a requested `session_id` when
    possible,
  - start Claude if the requested `session_id` is not running,
  - generate a new `session_id` and start a new Claude instance when the CLI did
    not provide one,
  - surface startup failures clearly.

- Session identity validation:
  - start Claude with a known `--session-id <session_id>` value,
  - trigger one response,
  - compare the Stop hook `session_id` with the requested value,
  - continue with direct identity only if they match.

Success criteria:

- `claude-no-p --start-session` starts the persistent interactive Claude process.
- `claude-no-p --start-session --session-id <id>` starts or reuses that specific
  persistent Claude process.
- The generated settings file contains the expected Stop hook.
- A manual prompt sent through the server appears in the Claude session.
- The observed Stop hook `session_id` matches the requested `--session-id` value, or
  the mismatch is documented and surfaced before continuing.

### Phase 4 — End-to-end `claude-no-p`

Deliverables:

- `claude-no-p "prompt"` performs the full flow:
  1. ensure control/MCP server is running,
  2. resolve the target `session_id`,
  3. ensure that session's Claude interactive process is running,
  4. enqueue prompt to that session's MCP connection,
  5. wait for that session's Stop callback,
  6. print response,
  7. exit.

Success criteria:

- `claude-no-p "reply with pong only"` prints `pong` or the expected assistant text.
- `claude-no-p --session-id <id> "reply with pong only"` routes to the matching
  persistent Claude instance.
- `claude-no-p "new topic"` without `--session-id` creates a new persistent Claude
  instance and returns or logs the generated `session_id`.
- Repeated invocations with the same `--session-id` reuse the same interactive
  Claude session.
- No `claude -p` process is spawned.

### Phase 5 — POC hardening

Deliverables:

- Single-flight enforcement with clear error if another request is active.
- Single-flight is scoped per `session_id`, so different Claude sessions can
  process requests independently.
- Request timeout and cancellation behavior.
- Debug logs for:
  - request id,
  - Claude session id,
  - MCP notification sent,
  - Stop hook received,
  - elapsed time.
- Minimal tests for request registry, hook parsing, and CLI stdin/argv behavior.

Success criteria:

- Unit tests pass.
- Failure modes are understandable without reading source.

---

## 7. Suggested File Layout

```text
poor_claude/
  __init__.py
  cli.py                 # claude-no-p entrypoint
  control_server.py      # HTTP API + pending request registry
  mcp_server.py          # MCP Channels server
  launcher.py            # Claude Code process startup
  settings.py            # generated settings + hook script paths
  hooks/
    stop_hook.py         # stdin JSON -> callback POST
  tests/
    test_cli.py
    test_hooks.py
    test_pending_requests.py
```

If the repo remains documentation-only, this layout should be created when the
POC implementation branch starts.

---

## 8. CLI Behavior

### Minimum flags

```text
claude-no-p [OPTIONS] [PROMPT]

Options:
  --timeout SECONDS       default: 300
  --session-id ID         route to an existing persistent Claude instance;
                         if omitted, create a new persistent Claude instance
  --resume ID             compatibility alias for --session-id
  -p, --print PROMPT      compatibility-style prompt input; creates or routes to
                         a persistent Claude instance instead of spawning
                         headless `claude -p`
  --workdir PATH          default: current directory
  --ttl SECONDS           idle shutdown TTL for auto-created sessions
  --keep-alive            disable idle shutdown for this session
  --debug                 verbose logs to stderr
  --start-session         start services and Claude, then keep running
  --stop-session          stop POC-managed processes
  --shutdown              stop all POC-managed sessions and the daemon
  --sessions              list known sessions
  --dry-run               print request envelope only
```

### Prompt input precedence

`claude-no-p` should support standalone utility usage without making prompt input
ambiguous.

Allowed prompt sources:

1. `-p/--print PROMPT`
2. positional `PROMPT`
3. stdin, when no prompt argument is provided and stdin is not a TTY

Rules:

- Exactly one prompt source may be used.
- `-p/--print` plus positional prompt is a usage error.
- `-p/--print` plus piped stdin is a usage error unless a future explicit flag
  chooses one source.
- positional prompt plus piped stdin is a usage error for the POC.
- If no prompt source is provided, show usage and exit with code `2`.

This avoids surprising behavior in shell scripts.

### Exit codes

```text
0  success
1  generic runtime failure
2  invalid CLI usage
3  timeout waiting for Claude response
4  Claude session startup failed
5  MCP/channel negotiation failed
6  hook callback failed or malformed
```

---

## 9. Correlation Strategy

Start with **single-flight per session** semantics:

- multiple persistent Claude Code instances are supported,
- each instance is keyed by `(canonical_project_dir, session_id)`,
- `claude-no-p --session-id <id> "prompt"` routes to that specific instance,
- `claude-no-p --resume <id> "prompt"` is a compatibility alias for the same
  route,
- `claude-no-p "prompt"` with no `--session-id` creates a new persistent instance,
- only one active request is allowed per `(project_dir, session_id)` route,
- the next `Stop` callback for that route resolves that route's active request,
- concurrent CLI calls to the same route fail fast with
  `request already in progress`,
- concurrent CLI calls to different routes may proceed in parallel if the
  MCP/control server can support independent connections.

This keeps the POC honest and avoids pretending Claude interactive sessions are
safe for concurrent prompt injection before that is proven.

This also maps directly to ACG: one persistent Claude session per watcher or
agent, keyed by session id, while preserving simple per-session request ordering.

---

## 10. Session Lifecycle and Idle Shutdown

`poor-claude` should support two usage styles:

1. **Standalone utility mode** — convenient replacement for ad-hoc `claude -p`
   calls.
2. **Managed session mode** — explicit `session_id` routing for ACG-like callers
   or users who want to reuse the same context.

### Auto-created sessions

When the CLI is called without `--session-id`, for example:

```bash
claude-no-p -p "summarize this"
```

the control server should create a new persistent Claude instance, route the
request to it, and mark the session as **auto-created**.

Auto-created sessions should have an idle TTL so the POC does not leak Claude
processes after one-off utility calls.

Recommended default:

```text
idle TTL: 15 minutes
```

The TTL must be configurable so standalone users can tune process reuse vs.
resource cleanup for their own workflow.

Configuration precedence:

1. CLI flag: `--ttl SECONDS`
2. Environment variable: `POOR_CLAUDE_TTL_SECONDS`
3. User config file value, e.g. `default_ttl_seconds`
4. Built-in default: `900` seconds

Recommended built-in defaults:

```text
auto-created session TTL: 900 seconds  (15 minutes)
named session TTL:       3600 seconds  (1 hour)
```

Reasoning: auto-created one-off sessions should clean up quickly, while named
sessions are more likely to be intentionally reused.

Special values:

- `--ttl 0` means shut down immediately after the current request finishes.
- `--keep-alive` disables idle shutdown for the target session.

If both `--ttl` and `--keep-alive` are provided, the CLI should fail fast with a
clear usage error instead of guessing which one wins.

Rationale:

- short enough to clean up accidental one-off sessions,
- long enough for a user to run follow-up commands if the CLI prints/logs the
  generated `session_id`,
- easy to tune later based on real usage.

### Named sessions

When the CLI is called with `--session-id <id>`, the session should be treated as
user-addressable. Recommended behavior:

- reuse the existing session if it is alive,
- start it if it is known but not running,
- create it if it does not exist,
- apply the named-session idle TTL by default, but allow callers to opt out with
  `--keep-alive`.
- allow callers to override the default with `--ttl SECONDS`.

For ACG integration later, watcher/agent sessions should likely use
`--keep-alive` or a much longer TTL because they are managed service sessions,
not one-off utility calls.

### Launch configuration immutability

Once a `(project_dir, session_id)` route has a running or known persistent Claude
instance, launch-affecting settings are treated as immutable for that route:

- `--settings <path-or-json>`
- `--dangerously-skip-permissions`
- development Channels loading / MCP channel configuration

If a later CLI call targets the same route but passes different launch-affecting
settings, `claude-no-p` should fail fast with a clear error instead of silently
reusing a Claude process that was launched with different hooks or permissions.

Reason: if a session was originally created without ACG's PreToolUse settings or
without the poor-claude Stop hook / Channels configuration, later requests cannot
assume those hooks are active inside the already-running process. The safe fix is
to stop/recreate the route or call it with the same flags.

### Idle reaper behavior

The control server should run an idle reaper loop:

1. Track `last_request_finished_at` per `session_id`.
2. Never kill a session with an active pending request.
3. If `now - last_request_finished_at > ttl_seconds`, gracefully terminate the
   Claude process for that session.
4. If graceful termination times out, force-kill the process and mark the session
   stopped.
5. Keep lightweight session metadata so a later `--session-id <id>` call can
   start a fresh Claude process with the same logical id.

### Daemon lifecycle commands

The CLI should distinguish session shutdown from daemon shutdown:

```text
claude-no-p --stop-session --session-id <id>   stop one persistent Claude session
claude-no-p --shutdown                         stop all sessions and the daemon
claude-no-p --sessions                         list known sessions for debugging
```

For the POC, `--shutdown` is acceptable as a developer convenience even if a
future production integration manages the daemon through launchd/systemd.

### CLI visibility

For auto-created sessions, the CLI should print the generated `session_id` to
stderr in debug mode, and optionally in normal mode with a concise hint:

```text
[poor-claude] session_id=11111111-1111-4111-8111-111111111111 idle_ttl=900s
```

stdout should remain reserved for the assistant response so shell pipelines do
not break.

---

## 11. Testing Strategy

### Unit tests

- CLI prompt normalization from argv and stdin.
- CLI prompt-source conflict handling for `-p`, positional prompt, and stdin.
- Stop hook stdin JSON parsing.
- Session registry create/reuse behavior.
- Route registry isolation by project/workdir and session id.
- Pending request registry resolve/timeout behavior per session.
- Idle TTL eligibility: expired idle sessions stop; active sessions do not.
- Daemon discovery and stale state-file handling.
- Settings JSON generation.

### Integration tests

- Start control server.
- Simulate Stop hook callback manually.
- Verify CLI receives callback response.
- Create two sessions and verify callbacks resolve only the matching session's
  pending request.
- Create two routes and verify a channel notification for route A does not appear
  in route B, including the case where the same `session_id` is used under two
  different project dirs.
- Create an auto session, advance/fake time past TTL, and verify the reaper stops
  only that idle session.

### Manual end-to-end test

```bash
claude-no-p --debug --session-id demo "Reply with exactly: POC_OK"
```

Expected:

- stdout contains `POC_OK`,
- debug logs show MCP notification and Stop hook callback,
- debug logs show the resolved `session_id`,
- process list confirms no `claude -p` invocation.

---

## 12. Main Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Channels API behavior differs from expectation | POC blocked | Validate capability negotiation before building CLI polish |
| Stop hook lacks `last_assistant_message` in interactive mode | Output path blocked | Fall back to transcript parsing only if verified necessary |
| Request/response correlation is ambiguous | Wrong response returned | Enforce one in-flight request per Claude session |
| MCP notifications cannot target a specific Claude instance cleanly | Multi-session routing blocked | Keep one MCP connection/queue per `session_id`; validate with two-session test early |
| Auto-created sessions leak processes | Resource leak | Add idle TTL reaper and make TTL visible in debug logs |
| CLI starts multiple control daemons | Split-brain session registry | Use daemon discovery state file plus lock/stale-pid handling |
| Prompt source is ambiguous | Unexpected prompt content | Fail fast when `-p`, positional prompt, and stdin conflict |
| Interactive Claude startup requires TTY behavior | Launcher complexity | Start with explicit `--start-session`; automate only after manual flow works |
| Development channels require local config not yet known | Startup blocked | Document exact Claude Code version/config and keep setup reproducible |

---

## 13. Milestone Definition of Done

The POC is complete when:

1. `claude-no-p "hello"` returns one assistant response through the MCP + Stop hook
   path.
2. `claude-no-p --session-id <id> "hello"` routes to the matching persistent Claude
   instance.
3. `claude-no-p "hello"` without a session id creates a new persistent Claude
   instance.
4. The same Claude interactive session is reused across multiple CLI calls when
   the same `session_id` is supplied.
5. Two different `session_id` values route to two different persistent Claude
   instances without cross-resolving callbacks.
6. Auto-created sessions shut down after their idle TTL unless explicitly kept
   alive.
7. Repeated CLI calls reuse one control daemon and do not create split-brain
   session registries.
8. Ambiguous prompt input combinations fail with usage error.
9. The implementation never invokes `claude -p`.
10. Timeout and startup failures produce clear non-zero exits.
11. The code has unit coverage for CLI parsing, hook parsing, settings generation,
   and pending request behavior.
12. The final notes document which assumptions were validated, invalidated, or
   still unknown.

---

## 14. Follow-up After POC

If the POC succeeds, the ACG migration can reuse the proven parts:

- MCP Channels server → ACG message injection path.
- Stop hook callback server → ACG response callback endpoint.
- Single-flight request registry → watcher pending-response map.
- Claude launcher/settings generator → adapter persistent session management.

The ACG adapter rewrite should happen only after this POC demonstrates reliable
request/response semantics outside ACG.
