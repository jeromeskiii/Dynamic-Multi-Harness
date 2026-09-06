"""Command-line interface for Dynamic Multi-Harness (DMH).

Usage:
  dmh run <task> [--runtime <id>] [--preset <preset>] [--store <dir>] [--resume <id>]
  dmh serve [--store <dir>]
  dmh sessions [list|show <id>|replay <id>|verify <id>] [--store <dir>]
  dmh doctor
  dmh gates
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

from . import cognihak_runtime, ev1h_runtime, qwen_runtime
from .child_runtime import ChildRuntime
from .errors import HarnessError
from .host import Host
from .native import ScriptedNative
from .persistence import SessionStore
from .server import DMHProtocolServer
from .telemetry import default_logger, redact_secrets


def build_default_host(store_dir: str = ".dmh_state") -> Host:
    """Construct a Host with all standard built-in and foreign runtimes registered."""
    host = Host(store_dir=store_dir)
    host.register("native", lambda: ScriptedNative(sandbox=host.sandbox))
    host.register("child", lambda: ChildRuntime(sandbox=host.sandbox))
    ev1h_runtime.register(host)
    cognihak_runtime.register(host)
    qwen_runtime.register(host)
    return host


def cmd_run(args: argparse.Namespace) -> int:
    host = build_default_host(args.store)
    host.attach_signal_handlers()

    if args.resume:
        session = host.resume_session(args.resume, runtime_id=args.runtime)
        print(f"Resumed session {session.id} (runtime: {session.runtime_id}, turn: {session.turn_no})")
    else:
        session = host.create_session(preset=args.preset)
        session.bind(args.runtime)
        print(f"Created session {session.id} (runtime: {session.runtime_id}, preset: {session.preset})")

    # Streaming sink for stdout
    def stdout_sink(item):
        kind, text = item
        if kind == "assistant/chunk":
            sys.stdout.write(text)
            sys.stdout.flush()

    session.register_sink(stdout_sink, capacity=500)

    print(f"\n[user] {args.task}")
    sys.stdout.write("[assistant] ")
    sys.stdout.flush()

    try:
        session.submit(args.task)
        print("\n")
        return 0
    except HarnessError as exc:
        print(f"\n[error] {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    finally:
        host.stop_all()


def cmd_serve(args: argparse.Namespace) -> int:
    host = build_default_host(args.store)
    host.attach_signal_handlers()
    server = DMHProtocolServer(host)
    server.run_forever()
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    store = SessionStore(args.store)
    action = args.session_action or "list"

    if action == "list":
        sessions = store.list_sessions()
        if not sessions:
            print(f"No sessions found in {store.store_dir}")
            return 0
        print(f"{'SESSION ID':<16} {'PRESET':<10} {'RUNTIME':<18} {'TURNS':<6} {'EVENTS':<8} {'UPDATED'}")
        print("-" * 75)
        for s in sessions:
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.updated_at))
            print(f"{s.session_id:<16} {s.preset:<10} {s.current_runtime or 'unbound':<18} {s.turn_count:<6} {s.event_count:<8} {ts_str}")
        return 0

    if action == "show":
        if not args.session_id:
            print("Error: session_id required for 'show'", file=sys.stderr)
            return 1
        meta = store.get_metadata(args.session_id)
        if not meta:
            print(f"Session {args.session_id} not found", file=sys.stderr)
            return 1
        print(f"Session: {meta.session_id}")
        print(f"  Preset:          {meta.preset}")
        print(f"  Current Runtime: {meta.current_runtime}")
        print(f"  Epoch:           {meta.epoch}")
        print(f"  Turns:           {meta.turn_count}")
        print(f"  Events:          {meta.event_count}")
        print(f"  Created:         {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta.created_at))}")
        print(f"  Updated:         {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta.updated_at))}")

        host = build_default_host(args.store)
        session = host.resume_session(args.session_id)
        print("\n--- Conversation Messages ---")
        for msg in session.log.derive_messages():
            print(f"[{msg['role']}] {msg['content']}")

        if getattr(args, "events", False):
            print("\n--- Raw Events ---")
            for ev in session.log.events:
                print(f"#{ev.seq:<3} [{ev.kind}] (runtime={ev.runtime_id}, epoch={ev.epoch}): {ev.payload}")
        return 0

    if action == "verify":
        if not args.session_id:
            print("Error: session_id required for 'verify'", file=sys.stderr)
            return 1
        ok, msg = store.verify_integrity(args.session_id)
        if ok:
            print(f"Session {args.session_id} integrity: OK ({msg})")
            return 0
        else:
            print(f"Session {args.session_id} integrity: FAILED ({msg})", file=sys.stderr)
            return 1

    if action == "replay":
        if not args.session_id:
            print("Error: session_id required for 'replay'", file=sys.stderr)
            return 1
        host = build_default_host(args.store)
        session = host.resume_session(args.session_id)
        print(f"Replaying session {args.session_id}:")
        for ev in session.log.events_until_switch():
            print(f"  [replay-surface] seq={ev.seq} {ev.kind}: {ev.payload}")
        return 0

    print(f"Unknown session action: {action}", file=sys.stderr)
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    print("=== DMH System Diagnostics ===")
    print(f"Python Version:   {sys.version.split()[0]} ({sys.executable})")
    print(f"Platform:         {sys.platform}")
    print(f"CWD:              {os.getcwd()}")
    print("\n=== Registered Runtime Drivers ===")

    runtimes = [
        ("native", True, "In-process deterministic native provider"),
        ("child", True, "Out-of-process subprocess DMH ABI provider"),
        ("ev1h-007", ev1h_runtime.available(), "Defensive evidence harness foreign provider"),
        ("cognihak", cognihak_runtime.available(), "cognihak security/cognitive corpus provider"),
        ("qwen3.8-flash-next", qwen_runtime.available(), "Qwen3.8-Flash-Next model harness provider"),
    ]

    for name, avail, desc in runtimes:
        status = "AVAILABLE" if avail else "NOT FOUND / SKIPPED"
        print(f"  {name:<22} [{status:<18}] - {desc}")

    print("\nDiagnostics complete.")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    import subprocess
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "verify-phase-gates.py")
    res = subprocess.run([sys.executable, script])
    return res.returncode


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dmh",
        description="Dynamic Multi-Harness (DMH) - Authoritative host with replaceable harness providers.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # run
    p_run = subparsers.add_parser("run", help="Run a task in a session")
    p_run.add_argument("task", type=str, help="Task prompt to execute")
    p_run.add_argument("--runtime", "-r", type=str, default="native", help="Initial runtime to bind (default: native)")
    p_run.add_argument("--preset", "-p", type=str, default="default", help="Preset capability requirements (default, coding, research)")
    p_run.add_argument("--store", "-s", type=str, default=".dmh_state", help="Directory for state & session persistence")
    p_run.add_argument("--resume", type=str, default=None, help="Resume an existing session ID")

    # serve
    p_serve = subparsers.add_parser("serve", help="Run JSON-RPC protocol server over stdio")
    p_serve.add_argument("--store", "-s", type=str, default=".dmh_state", help="State storage directory")

    # sessions
    p_sess = subparsers.add_parser("sessions", help="Manage and inspect sessions")
    p_sess.add_argument("session_action", choices=["list", "show", "replay", "verify"], default="list", nargs="?", help="Action to perform")
    p_sess.add_argument("session_id", type=str, nargs="?", help="Session ID (for show/replay/verify)")
    p_sess.add_argument("--events", "-e", action="store_true", help="Include raw events in show output")
    p_sess.add_argument("--store", "-s", type=str, default=".dmh_state", help="State storage directory")

    # doctor
    subparsers.add_parser("doctor", help="Inspect environment and runtime driver availability")

    # gates
    subparsers.add_parser("gates", help="Execute all 9 phase verification gates")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    if args.subcommand == "run":
        return cmd_run(args)
    elif args.subcommand == "serve":
        return cmd_serve(args)
    elif args.subcommand == "sessions":
        return cmd_sessions(args)
    elif args.subcommand == "doctor":
        return cmd_doctor(args)
    elif args.subcommand == "gates":
        return cmd_gates(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
