"""CLI entrypoint for claude-no-p."""

from __future__ import annotations

import argparse
import json
import os
import sys

from poor_claude.compat import stream_json_lines
from poor_claude.config import resolve_ttl
from poor_claude.daemon import default_state_path, discover_state, start_daemon
from poor_claude.http_client import HttpClientError, request_json
from poor_claude.prompt import PromptError, resolve_prompt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-no-p")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--session-id")
    parser.add_argument("--resume")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--settings")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--workdir", default=os.getcwd())
    parser.add_argument("--ttl")
    parser.add_argument("--keep-alive", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--start-session", action="store_true")
    parser.add_argument("--stop-session", action="store_true")
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--sessions", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--launch-process", action="store_true")
    parser.add_argument("--auto-accept-workspace-trust", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.session_id and args.resume:
        parser.error("--session-id and --resume cannot be used together")
    target_session_id = args.session_id or args.resume

    try:
        ttl = resolve_ttl(
            session_id=target_session_id,
            cli_ttl=args.ttl,
            keep_alive=args.keep_alive,
        )
    except ValueError as exc:
        parser.error(str(exc))

    control_commands = [args.start_session, args.stop_session, args.shutdown, args.sessions]
    needs_prompt = not any(control_commands)
    resolved = None
    if needs_prompt or args.dry_run:
        try:
            resolved = resolve_prompt(
                print_prompt=None,
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
            "print_mode": args.print_mode,
            "output_format": args.output_format,
            "settings": args.settings,
            "timeout_seconds": args.timeout,
            "ttl_seconds": ttl.ttl_seconds,
            "keep_alive": ttl.keep_alive,
            "workdir": args.workdir,
        }
        print(json.dumps(envelope, indent=2, sort_keys=True))
        return 0

    try:
        state_path = default_state_path()
        if args.shutdown:
            state = discover_state(state_path)
            if state is None:
                return 0
            request_json("POST", f"{state.address}/shutdown", {})
            return 0

        state = start_daemon(state_path=state_path)

        if args.sessions:
            result = request_json("GET", f"{state.address}/sessions")
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

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
                    "dangerously_skip_permissions": args.dangerously_skip_permissions,
                    "dangerously_load_development_channels": True,
                    "launch_process": args.launch_process,
                    "auto_accept_workspace_trust": args.auto_accept_workspace_trust,
                },
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        result = request_json(
            "POST",
            f"{state.address}/requests",
            {
                "session_id": target_session_id,
                "settings_path": args.settings,
                "dangerously_skip_permissions": args.dangerously_skip_permissions,
                "dangerously_load_development_channels": True,
                "launch_process": True,
                "auto_accept_workspace_trust": args.auto_accept_workspace_trust,
                "wait_for_response": True,
                "prompt": resolved.prompt if resolved else "",
                "timeout_seconds": args.timeout,
                "ttl_seconds": ttl.ttl_seconds,
                "keep_alive": ttl.keep_alive,
                "workdir": args.workdir,
            },
            timeout=args.timeout + 5,
        )
        if args.debug:
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
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
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
