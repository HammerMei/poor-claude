"""CLI entrypoint for claude-no-p."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

from poor_claude.compat import stream_json_lines
from poor_claude.config import resolve_timeout, resolve_ttl
from poor_claude.daemon import default_state_path, discover_state, start_daemon
from poor_claude.http_client import HttpClientError, request_json
from poor_claude.prompt import PromptError, resolve_prompt
from poor_claude.transcript import transcript_candidates


RESUME_PICKER_SENTINEL = "__poor_claude_resume_picker__"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-no-p")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("-p", "--print", dest="print_prompt", nargs="?", const=True)
    parser.add_argument("--timeout", type=int, default=None,
                        help="Request timeout in seconds (default: POOR_CLAUDE_TIMEOUT_SECONDS env var or 300)")
    parser.add_argument("--session-id")
    # --name is accepted for compatibility with ACG (agent-chat-gateway) which passes
    # it as a session label/title.  claude-no-p does not use it but must not reject it.
    parser.add_argument("--name")
    parser.add_argument("-r", "--resume", nargs="?", const=RESUME_PICKER_SENTINEL)
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--settings")
    parser.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"],
        default="default",
    )
    # Legacy alias: --dangerously-skip-permissions maps to --permission-mode bypassPermissions
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--model")
    parser.add_argument("--add-dir", action="append", dest="add_dirs", metavar="DIR",
                        help="Additional directory to allow tool access to; may be repeated")
    parser.add_argument("--tools", action="append", dest="tools", metavar="TOOL",
                        help="Restrict built-in tool set (e.g. 'Bash' 'Edit'); use '' to disable all; may be repeated")
    parser.add_argument("--system-prompt")
    parser.add_argument("--append-system-prompt")
    parser.add_argument(
        "--append-system-prompt-file",
        metavar="PATH",
        help="Read the value for --append-system-prompt from a file instead of the "
             "command line. Re-read on every invocation (not cached), so a caller "
             "that mutates the file in place has the new content picked up on the "
             "session's next request. Mutually exclusive with --append-system-prompt.",
    )
    parser.add_argument("--allowed-tools", action="append", dest="allowed_tools", metavar="RULE",
                        help="Allow a tool rule (e.g. 'Bash(ls *)'); may be repeated")
    parser.add_argument("--disallowed-tools", action="append", dest="disallowed_tools", metavar="RULE",
                        help="Deny a tool rule (takes priority over --allowed-tools); may be repeated")
    parser.add_argument("--workdir", default=os.getcwd())
    parser.add_argument("--ttl")
    parser.add_argument("--keep-alive", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--start-session", action="store_true")
    parser.add_argument("--stop-session", action="store_true")
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--sessions", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--prune-sessions", action="store_true")
    parser.add_argument("--view", metavar="SESSION_ID",
        help="Print the conversation transcript for a session to stdout")
    parser.add_argument("--with-tools", action="store_true",
        help="Include tool calls and results when using --view")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--launch-process", action="store_true")
    parser.set_defaults(auto_accept_startup_prompts=True)
    parser.add_argument("--auto-accept-startup-prompts", dest="auto_accept_startup_prompts", action="store_true")
    parser.add_argument("--no-auto-accept-startup-prompts", dest="auto_accept_startup_prompts", action="store_false")
    parser.add_argument(
        "--auto-accept-workspace-trust",
        dest="auto_accept_startup_prompts",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    if args.resume == RESUME_PICKER_SENTINEL:
        parser.error("bare -r/--resume picker is not supported; pass --resume <session-id>")
    if args.session_id and args.resume:
        parser.error("--session-id and --resume cannot be used together")
    if args.append_system_prompt and args.append_system_prompt_file:
        parser.error("--append-system-prompt and --append-system-prompt-file cannot be used together")
    target_session_id = args.session_id or args.resume
    if target_session_id is not None:
        try:
            target_session_id = str(uuid.UUID(target_session_id))
        except ValueError:
            if _uses_short_resume(raw_argv) and args.prompt is None:
                parser.error("-r with a non-UUID value is ambiguous; use --resume <session-id> for named sessions")
            pass
        if args.session_id:
            args.session_id = target_session_id
        else:
            args.resume = target_session_id

    try:
        ttl = resolve_ttl(
            session_id=target_session_id,
            cli_ttl=args.ttl,
            keep_alive=args.keep_alive,
        )
        timeout = resolve_timeout(cli_timeout=args.timeout)
    except ValueError as exc:
        parser.error(str(exc))

    control_commands = [args.start_session, args.stop_session, args.shutdown, args.sessions, args.prune_sessions, args.view]
    needs_prompt = not any(control_commands)
    resolved = None
    if needs_prompt or args.dry_run:
        try:
            resolved = resolve_prompt(
                print_prompt=args.print_prompt,
                positional_prompt=args.prompt,
                stdin=sys.stdin,
            )
        except PromptError as exc:
            parser.error(str(exc))

    if args.dry_run:
        envelope = {
            "session_id": args.session_id,
            "resume": args.resume,
            "prompt": resolved.prompt if resolved else None,
            "prompt_source": resolved.source if resolved else None,
            "print_mode": args.print_prompt is not None,
            "output_format": args.output_format,
            "settings": args.settings,
            "auto_accept_startup_prompts": args.auto_accept_startup_prompts,
            "timeout_seconds": timeout,
            "ttl_seconds": ttl.ttl_seconds,
            "keep_alive": ttl.keep_alive,
            "workdir": args.workdir,
        }
        print(json.dumps(envelope, indent=2, sort_keys=True))
        return 0

    try:
        resolved_append_system_prompt = _resolve_append_system_prompt(args)
        state_path = default_state_path()
        if args.shutdown:
            state = discover_state(state_path)
            if state is None:
                # discover_state() also returns None when the state file exists
                # but is corrupt/unparseable — that case is NOT "no daemon ever
                # ran," it's "we can't tell," and a daemon may still be alive
                # and serving. Warn rather than silently claiming success.
                if Path(state_path).exists():
                    print(
                        f"warning: {state_path} exists but could not be read; "
                        "unable to confirm whether a daemon is still running",
                        file=sys.stderr,
                    )
                return 0
            request_json("POST", f"{state.address}/shutdown", {})
            return 0

        state = start_daemon(state_path=state_path)

        if args.sessions:
            result = request_json("GET", f"{state.address}/sessions")
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(_format_sessions(result.get("sessions", [])))
            return 0

        if args.prune_sessions:
            result = request_json("POST", f"{state.address}/prune", {})
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                removed = result.get("removed_routes", [])
                count = len(removed) if isinstance(removed, list) else 0
                print(f"Pruned {count} session(s).")
            return 0

        if args.view:
            printed = _view_session(args.view, workdir=args.workdir, with_tools=args.with_tools)
            return 0 if printed else 1

        if args.stop_session:
            if not target_session_id:
                parser.error("--stop-session requires --session-id or --resume")
            result = request_json(
                "DELETE",
                f"{state.address}/sessions/{target_session_id}",
                headers={"X-Poor-Claude-Workdir": args.workdir},
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if args.start_session:
            result = request_json(
                "POST",
                f"{state.address}/sessions",
                {
                    "session_id": target_session_id,
                    "ttl_seconds": ttl.ttl_seconds,
                    "keep_alive": ttl.keep_alive,
                    "workdir": args.workdir,
                    "settings_path": args.settings,
                    "resume_session": bool(args.resume),
                    "permission_mode": "bypassPermissions" if args.dangerously_skip_permissions else args.permission_mode,
                    "dangerously_load_development_channels": True,
                    "launch_process": args.launch_process,
                    "auto_accept_workspace_trust": args.auto_accept_startup_prompts,
                    "effort": args.effort,
                    "model": args.model,
                    "add_dirs": args.add_dirs,
                    "tools": args.tools,
                    "system_prompt": args.system_prompt,
                    "append_system_prompt": resolved_append_system_prompt,
                    "allowed_tools": args.allowed_tools or [],
                    "disallowed_tools": args.disallowed_tools or [],
                },
            )
            for warning in result.get("warnings") or []:
                print(f"WARNING: {warning}", file=sys.stderr)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        result = request_json(
            "POST",
            f"{state.address}/requests",
            {
                "session_id": target_session_id,
                "settings_path": args.settings,
                "resume_session": bool(args.resume),
                "permission_mode": "bypassPermissions" if args.dangerously_skip_permissions else args.permission_mode,
                "dangerously_load_development_channels": True,
                "launch_process": True,
                "auto_accept_workspace_trust": args.auto_accept_startup_prompts,
                "wait_for_response": True,
                "prompt": resolved.prompt if resolved else "",
                "timeout_seconds": timeout,
                "ttl_seconds": ttl.ttl_seconds,
                "keep_alive": ttl.keep_alive,
                "workdir": args.workdir,
                "effort": args.effort,
                "model": args.model,
                "tools": args.tools,
                "system_prompt": args.system_prompt,
                "append_system_prompt": resolved_append_system_prompt,
                "allowed_tools": args.allowed_tools or [],
                "disallowed_tools": args.disallowed_tools or [],
            },
            timeout=timeout + 5,
        )
        if args.debug:
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
        for warning in result.get("warnings") or []:
            print(f"WARNING: {warning}", file=sys.stderr)
        session_id = str(result["session_id"])
        response_text = str(result.get("response", ""))
        if args.output_format == "stream-json":
            for line in stream_json_lines(session_id=session_id, text=response_text):
                print(line)
            return 0
        if args.output_format == "json":
            print(json.dumps({"session_id": session_id, "result": response_text}))
            return 0
        print(response_text)
        return 0
    except (TimeoutError, HttpClientError, OSError) as exc:
        print(f"claude-no-p failed: {exc}", file=sys.stderr)
        if isinstance(exc, HttpClientError) and isinstance(exc.payload, dict):
            diagnostics = exc.payload.get("diagnostics")
            if isinstance(diagnostics, dict):
                print(_format_diagnostics(diagnostics), file=sys.stderr)
        return 1


def _resolve_append_system_prompt(args: argparse.Namespace) -> str | None:
    """Return the value for the append_system_prompt request field.

    Reads --append-system-prompt-file fresh on every call rather than once at
    startup. This mirrors the real `claude` CLI, which re-reads the file on each
    invocation — and it's why a caller can mutate the file in place (e.g. ACG's
    durable system-prompt delivery, agent-chat-gateway#58) and have poor-claude
    notice on the session's next request: the existing soft-param comparison in
    control_server.py (session.metadata["append_system_prompt"] vs the incoming
    value) already restarts the persistent process when the *content* changes,
    regardless of whether that content arrived inline or via a file path. Without
    a fresh read here, an updated file would silently never reach a long-running
    session.
    """
    if args.append_system_prompt_file:
        with open(args.append_system_prompt_file, encoding="utf-8") as fh:
            return fh.read()
    return args.append_system_prompt


def _uses_short_resume(argv: list[str]) -> bool:
    return any(token == "-r" or (token.startswith("-r") and token != "-") for token in argv)


def _format_diagnostics(diagnostics: dict) -> str:
    lines = ["Diagnostics:"]
    for key in ("session_id", "route_key", "process_alive", "process_pid"):
        value = diagnostics.get(key)
        if value is not None:
            lines.append(f"  {key}: {value}")
    paths = diagnostics.get("paths")
    if isinstance(paths, dict) and paths:
        lines.append("  paths:")
        for key, value in paths.items():
            lines.append(f"    {key}: {value}")
    summaries = diagnostics.get("summaries")
    if isinstance(summaries, dict) and summaries:
        lines.append("  summaries:")
        for key, value in summaries.items():
            if value:
                lines.append(f"    {key}: {value}")
    return "\n".join(lines)


def _format_sessions(sessions: object) -> str:
    if not isinstance(sessions, list) or not sessions:
        return "No active sessions."
    rows = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        active = session.get("active_request")
        process_alive = metadata.get("process_alive")
        if active:
            status = "active"
        elif metadata.get("process_stopping") == "True":
            status = "stopping"
        elif process_alive == "True":
            status = "idle"
        elif process_alive == "False":
            status = "stopped"
        else:
            status = "ready"
        rows.append(
            {
                "SESSION": str(session.get("session_id", "")),
                "STATUS": status,
                "RESUME": "yes" if metadata.get("resume_on_launch") == "True" else "no",
                "TTL": "keep" if session.get("keep_alive") else str(session.get("ttl_seconds", "")),
                "WORKDIR": str(session.get("workdir", "")),
            }
        )
    headers = ["SESSION", "STATUS", "RESUME", "TTL", "WORKDIR"]
    widths = {header: max(len(header), *(len(row[header]) for row in rows)) for header in headers}
    lines = ["  ".join(header.ljust(widths[header]) for header in headers)]
    lines.append("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        lines.append("  ".join(row[header].ljust(widths[header]) for header in headers))
    return "\n".join(lines)


# ── session transcript viewer ──────────────────────────────────────────────────

_CHANNEL_PROMPT_RE = re.compile(
    r"USER PROMPT:\n(.*?)(?:\n</poor-claude-request>|$)",
    re.DOTALL,
)
_CHANNEL_WRAPPER_RE = re.compile(r'<channel source="poor-claude"[^>]*>.*?</channel>', re.DOTALL)


def _extract_prompt(content: str | list) -> str | None:
    """Pull the actual user prompt out of the poor-claude channel wrapper.

    Returns the raw prompt string, or None if the content contains no channel
    message (e.g. it's purely tool_result blocks returned after a tool call).
    """
    if isinstance(content, list):
        # Channel messages are delivered as a tool_result block whose inner
        # content is the XML-wrapped prompt string.
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        inner = " ".join(b.get("text", "") for b in inner if isinstance(b, dict))
                    texts.append(str(inner))
                elif block.get("type") == "text":
                    texts.append(block.get("text", ""))
        content = "\n".join(texts)
    if not isinstance(content, str):
        return None
    m = _CHANNEL_PROMPT_RE.search(content)
    if m:
        return m.group(1).strip()
    # Not a channel message — check if there's any non-wrapper text.
    stripped = _CHANNEL_WRAPPER_RE.sub("", content).strip()
    return stripped or None


def _extract_tool_results(content: str | list) -> list[tuple[str, str]]:
    """Return (tool_use_id, text) pairs from tool_result blocks in a user message."""
    if not isinstance(content, list):
        return []
    results = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id", "")
        inner = block.get("content", "")
        if isinstance(inner, list):
            text = "\n".join(b.get("text", "") for b in inner if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = str(inner) if inner else ""
        # Skip if it's actually the channel message wrapper (not a tool result).
        if "poor-claude-request" not in text:
            results.append((tool_use_id, text))
    return results


def _extract_response(content: str | list, *, with_tools: bool = False) -> str:
    """Extract the text (and optionally tool calls) from an assistant message."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                parts.append(text)
        elif block.get("type") == "tool_use" and with_tools:
            name = block.get("name", "unknown")
            inp = block.get("input", {})
            parts.append(f"── Tool: {name} {'─' * max(0, 46 - len(name))}")
            parts.append(json.dumps(inp, indent=2, ensure_ascii=False))
        # Skip thinking blocks — they're internal reasoning, not the response.
    return "\n".join(parts)


def _view_session(session_id: str, *, workdir: str, with_tools: bool = False) -> bool:
    """Print a human-readable transcript to stdout. Returns True if found."""
    candidates = transcript_candidates(session_id=session_id, workdir=workdir)
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        print(f"No transcript found for session {session_id!r}", file=sys.stderr)
        return False

    turn = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = obj.get("type")
            if msg_type == "user":
                content = obj.get("message", {}).get("content", "")
                prompt = _extract_prompt(content)
                if prompt:
                    turn += 1
                    print(f"{'─' * 60}")
                    print(f"[{turn}] USER")
                    print(f"{'─' * 60}")
                    print(prompt)
                    print()
                elif with_tools:
                    tool_results = _extract_tool_results(content)
                    for tool_use_id, text in tool_results:
                        print(f"{'─' * 60}")
                        print(f"TOOL RESULT  {tool_use_id}")
                        print(f"{'─' * 60}")
                        print(text)
                        print()
            elif msg_type == "assistant":
                content = obj.get("message", {}).get("content", "")
                response = _extract_response(content, with_tools=with_tools)
                if response:
                    print(f"{'─' * 60}")
                    print(f"[{turn}] ASSISTANT")
                    print(f"{'─' * 60}")
                    print(response)
                    print()
    if turn == 0:
        print(f"Transcript at {path} exists but contains no user/assistant turns.", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
