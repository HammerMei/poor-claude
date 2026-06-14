# No-response watchdog — validation guide

The watchdog tries to recover a Claude process that hangs mid-turn on a stalled SSE
stream (it stops writing to the transcript and never fires its Stop hook, so the
request would otherwise sit idle until the hard timeout kills it — see
[claude-code#26224](https://github.com/anthropics/claude-code/issues/26224),
[#57103](https://github.com/anthropics/claude-code/issues/57103)).

It is **disabled by default** (`POOR_CLAUDE_STALL_SECONDS=0`) and ships unvalidated:
the tests prove the *plumbing*, not that a nudge/restart actually makes a hung request
complete. This guide is how you confirm it on a box where the hang reproduces.

---

## ⚠️ Read this first: how to pick `POOR_CLAUDE_STALL_SECONDS`

Stall detection keys off **transcript byte growth**. It cannot tell a genuine hang
apart from a *healthy but quiet* foreground tool call — a long `Bash` test/build/install,
or an extended thinking block, writes nothing to the transcript until it returns and
looks identical to a hang.

The cost of a false trigger is asymmetric:

- **`nudge`** false trigger -> injects "continue" into a healthy turn. Mildly polluting,
  not fatal.
- **`restart`** false trigger -> **kills a healthy in-flight process** and re-injects the
  prompt. A long-running task can be killed, restarted, re-run, killed again — a
  **kill loop**. This is the dangerous one.

**Therefore the window must be:**

```
longest normal quiet period  <  POOR_CLAUDE_STALL_SECONDS  <  request timeout
```

- Below the **request timeout** (`POOR_CLAUDE_TIMEOUT_SECONDS`, or whatever the caller
  passes — ACG currently uses ~1500-1800s) or the hard timeout fires first and the
  watchdog never acts.
- Well **above** the longest legitimate quiet stretch your agents produce, with margin.

**Suggested starting point for ACG (timeout ~= 1800s):** `POOR_CLAUDE_STALL_SECONDS=600`
to `900` (i.e. only treat 10-15 min of total silence as a hang). Do **not** use the
small value (e.g. 60s) shown in the test steps below for anything other than forcing a
quick trigger during a controlled test — it *will* kill healthy long tasks in production.

`nudge_then_restart` is safer than `restart` because the restart only happens *after*
the nudge budget is spent, i.e. several stall windows later.

---

## Step-by-step

### A. Get the branch on the box
1. SSH to the server, `cd` to the poor-claude repo.
2. `git fetch origin`
3. `git checkout fix/no-response-watchdog` (or `git pull` if already on it). Confirm
   `git rev-parse HEAD` is `88a784d...`.
4. If poor-claude was installed editable (`pip install -e .`), a daemon restart picks up
   the new code. Otherwise reinstall: `pip install -e .`.

### B. Restart the daemon WITH the watchdog config
The watchdog runs inside the daemon process, so the env vars must be set **for the
daemon**, before it starts.
5. Stop the running daemon (launchd / systemd / manual kill — however it's managed).
6. Export config, then start. For a **controlled test** use a low stall to trigger fast;
   for real use, see the sizing rule above.
   ```bash
   export POOR_CLAUDE_STALL_SECONDS=60        # TEST ONLY — production: 600-900 for ACG
   export POOR_CLAUDE_STALL_ACTION=nudge      # start with nudge (harmless if it misfires)
   poor-claude daemon
   ```

### C. Point RC agents at it
7. The agents don't change — they keep injecting prompts via poor-claude. Just make sure
   they talk to **this** daemon (the one running the branch). Restart their sessions, or
   let their next request flow through.

### D. Validate — the counter moving is NOT success
8. Reproduce or wait for a hang (an agent whose Claude stops responding mid-turn).
9. Observe:
   - `poor-claude status` — the `RESUME` column flips to `yes` after a restart.
   - Raw counters: query the daemon's `GET /sessions` (curl the control server) and look
     at the session `metadata`: `nudges_sent`, `stall_restarts`, `last_nudge_at`,
     `last_restart_at`.
10. **The decisive check:** did the stuck request **actually return a response**?
    - counters moved **and** the request completed -> that action works (good)
    - counters moved **but** the request never returned -> that action is inert for this
      hang.
11. If `nudge` is inert -> `export POOR_CLAUDE_STALL_ACTION=restart` (or
    `nudge_then_restart`), restart the daemon, repeat. Watch for `RESUME=yes` **and**
    actual completion.

### E. Lock in the production values
12. Once you know which action actually recovers a stuck request, set:
    - `POOR_CLAUDE_STALL_SECONDS` to a safe value (above longest normal quiet period,
      below timeout — see sizing rule; ~600-900 for ACG's 1800s timeout).
    - `POOR_CLAUDE_STALL_ACTION` to the action you validated.
13. Watch for false triggers in normal operation (healthy long tasks being nudged or, worse,
    restarted). If you see any, raise `POOR_CLAUDE_STALL_SECONDS`.

---

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `POOR_CLAUDE_STALL_SECONDS` | Stall window (s). `0` disables the watchdog. | `0` |
| `POOR_CLAUDE_STALL_ACTION` | `off` \| `nudge` \| `restart` \| `nudge_then_restart` | `nudge_then_restart` |
| `POOR_CLAUDE_MAX_NUDGES` | Nudges per stall before escalating / giving up. | `3` |
| `POOR_CLAUDE_MAX_RESTARTS` | Kill+resume relaunches per request before giving up. | `1` |
| `POOR_CLAUDE_NUDGE_PROMPT` | Nudge message text. | `Please continue your previous response.` |

Session metadata exposed for observability: `nudges_sent`, `stall_restarts`,
`last_nudge_at`, `last_restart_at`, plus `RESUME` in `poor-claude status`.
</parameter>
</invoke>
