"""Entry point for the portable Adminbot manager."""

from __future__ import annotations

import argparse

from adminbot.app.manager import AdminbotManager
from adminbot.app.paths import build_runtime_paths


def _read_int(prompt: str) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            value = int(raw)
        except ValueError:
            print("Enter a valid integer port.")
            continue
        if 1 <= value <= 65535:
            return value
        print("Port must be between 1 and 65535.")


def _print_bots(manager: AdminbotManager) -> None:
    bots = manager.list_bots()
    if not bots:
        print("No bots registered yet.")
        return

    for bot in bots:
        print(f"- {bot.name} [{bot.id}]")
        print(f"  status    : {bot.process.status}")
        print(f"  workspace : {bot.workspace}")
        print(f"  config    : {bot.config_path}")
        print(f"  web port  : {bot.web_port}")
        print(f"  pid       : {bot.process.pid or '-'}")


def _interactive_menu(manager: AdminbotManager) -> int:
    while True:
        print("")
        print("=== Adminbot Phase 1 ===")
        print("[1] List bots")
        print("[2] Create new bot")
        print("[3] Start bot")
        print("[4] Stop bot")
        print("[5] Restart bot")
        print("[6] Status")
        print("[7] Exit")
        choice = input("Choose action: ").strip()
        if choice == "1":
            _print_bots(manager)
        elif choice == "2":
            workspace = input("Workspace path: ").strip()
            name = input("Bot name (optional): ").strip() or None
            port = _read_int("Web port: ")
            bot = manager.create_bot(workspace, name, port)
            print(f"Created bot '{bot.name}' with id {bot.id}.")
        elif choice == "3":
            target = input("Bot id or name: ").strip()
            bot = manager.start_bot(target)
            print(f"Started '{bot.name}' on http://127.0.0.1:{bot.web_port}")
        elif choice == "4":
            target = input("Bot id or name: ").strip()
            bot = manager.stop_bot(target)
            print(f"Stopped '{bot.name}'.")
        elif choice == "5":
            target = input("Bot id or name: ").strip()
            bot = manager.restart_bot(target)
            print(f"Restarted '{bot.name}' on http://127.0.0.1:{bot.web_port}")
        elif choice == "6":
            target = input("Bot id or name: ").strip()
            bot = manager.get_bot(target)
            print(f"{bot.name}: {bot.process.status} (pid={bot.process.pid or '-'})")
        elif choice == "7":
            return 0
        else:
            print("Invalid choice.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Adminbot Phase 1 manager")
    subparsers = parser.add_subparsers(dest="command")

    create_parser = subparsers.add_parser("create", help="Create a new bot")
    create_parser.add_argument("--workspace", required=True)
    create_parser.add_argument("--name")
    create_parser.add_argument("--web-port", required=True, type=int)

    subparsers.add_parser("list", help="List bots")

    web_parser = subparsers.add_parser("web", help="Start the Adminbot web UI")
    web_parser.add_argument("--port", type=int, default=8900)
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind host for Adminbot web UI. Default is 127.0.0.1. "
            "Use 0.0.0.0 only with a strong Adminbot password."
        ),
    )

    for name in ("start", "stop", "restart", "status"):
        cmd = subparsers.add_parser(name, help=f"{name.title()} a bot")
        cmd.add_argument("target")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        paths = build_runtime_paths()
        manager = AdminbotManager(paths)

        if args.command is None:
            return _interactive_menu(manager)

        if args.command == "create":
            bot = manager.create_bot(args.workspace, args.name, args.web_port)
            print(f"Created bot '{bot.name}' with id {bot.id}.")
            return 0

        if args.command == "list":
            _print_bots(manager)
            return 0

        if args.command == "web":
            try:
                from adminbot.app.web.cli import start_web
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.startswith("adminbot.app.web"):
                    raise RuntimeError(
                        "Adminbot web UI is not installed in this Wave A build. "
                        "Use the CLI commands now; web UI lands in Wave B."
                    ) from exc
                raise
            start_web(port=args.port, host=args.host)
            return 0

        if args.command == "start":
            bot = manager.start_bot(args.target)
            print(f"Started '{bot.name}' on http://127.0.0.1:{bot.web_port}")
            return 0

        if args.command == "stop":
            bot = manager.stop_bot(args.target)
            print(f"Stopped '{bot.name}'.")
            return 0

        if args.command == "restart":
            bot = manager.restart_bot(args.target)
            print(f"Restarted '{bot.name}' on http://127.0.0.1:{bot.web_port}")
            return 0

        if args.command == "status":
            bot = manager.get_bot(args.target)
            print(f"{bot.name}: {bot.process.status} (pid={bot.process.pid or '-'})")
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
