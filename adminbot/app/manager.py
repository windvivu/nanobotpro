"""High-level manager coordination for Adminbot."""

from __future__ import annotations

import shutil
import socket
import uuid
from dataclasses import asdict
from pathlib import Path

from adminbot.app.paths import RuntimePaths, ensure_runtime_dirs
from adminbot.app.process_manager import BotProcessManager
from adminbot.app.registry import BotProcessState, BotRecord, BotRegistry, utc_now_iso
from adminbot.app.runtime_config import initialize_bot_config, resolve_workspace_path


class AdminbotManager:
    """Coordinate registry, config initialization, and process lifecycle."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        ensure_runtime_dirs(paths)
        self.registry = BotRegistry(paths.registry_path)
        self.process_manager = BotProcessManager(paths)
        self.reconcile_runtime_state()

    def reconcile_runtime_state(self) -> int:
        bots = self.registry.list_bots()
        if not bots:
            return 0

        changed = 0
        now = utc_now_iso()
        refreshed: list[BotRecord] = []
        for bot in bots:
            before = asdict(bot)
            updated = self.process_manager.refresh_status(bot)
            if asdict(updated) != before:
                changed += 1
                updated.updated_at = now
            refreshed.append(updated)

        if changed:
            self.registry.replace_bots(refreshed)
        return changed

    def list_bots(self) -> list[BotRecord]:
        bots = self.registry.list_bots()
        refreshed: list[BotRecord] = []
        changed = False
        now = utc_now_iso()
        for bot in bots:
            before = asdict(bot)
            updated = self.process_manager.refresh_status(bot)
            if asdict(updated) != before:
                changed = True
                updated.updated_at = now
            refreshed.append(updated)
        if changed:
            self.registry.replace_bots(refreshed)
        return refreshed

    def get_bot(self, bot_id_or_name: str) -> BotRecord:
        for bot in self.registry.list_bots():
            if bot.id == bot_id_or_name or bot.name == bot_id_or_name:
                before = asdict(bot)
                updated = self.process_manager.refresh_status(bot)
                if asdict(updated) != before:
                    self.registry.upsert_bot(updated)
                return updated
        raise RuntimeError(f"Bot '{bot_id_or_name}' was not found.")

    def create_bot(self, workspace_input: str, name: str | None, web_port: int) -> BotRecord:
        if not 1 <= int(web_port) <= 65535:
            raise RuntimeError("Web port must be between 1 and 65535.")

        workspace_path = resolve_workspace_path(workspace_input, self.paths.repo_root)
        for existing in self.registry.list_bots():
            if Path(existing.workspace) == workspace_path:
                raise RuntimeError(
                    f"Workspace already registered by bot '{existing.name}' (port {existing.web_port})."
                )
            if existing.web_port == int(web_port):
                raise RuntimeError(
                    f"Web port {web_port} is already registered by bot '{existing.name}'."
                )

        if not self._is_port_free(web_port):
            raise RuntimeError(f"Web port {web_port} is already busy on localhost.")

        bot_name = name.strip() if name and name.strip() else workspace_path.name or "nanobot"
        for existing in self.registry.list_bots():
            if existing.name == bot_name:
                raise RuntimeError(f"Bot name '{bot_name}' is already registered.")

        bot_id = f"bot-{uuid.uuid4().hex[:12]}"
        config_dir = self.paths.instances_dir / bot_id
        config_path = config_dir / "config.json"
        initialize_bot_config(
            python_executable=self.paths.python_executable,
            repo_root=self.paths.repo_root,
            config_path=config_path,
            workspace_path=workspace_path,
            web_port=web_port,
        )
        record = BotRecord(
            id=bot_id,
            name=bot_name,
            workspace=str(workspace_path),
            config_path=str(config_path),
            web_port=int(web_port),
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            process=BotProcessState(),
        )
        return self.registry.upsert_bot(record)

    def start_bot(self, bot_id_or_name: str) -> BotRecord:
        bot = self.get_bot(bot_id_or_name)
        updated = self.process_manager.start(bot)
        return self.registry.upsert_bot(updated)

    def stop_bot(self, bot_id_or_name: str) -> BotRecord:
        bot = self.get_bot(bot_id_or_name)
        updated = self.process_manager.stop(bot)
        return self.registry.upsert_bot(updated)

    def restart_bot(self, bot_id_or_name: str) -> BotRecord:
        self.stop_bot(bot_id_or_name)
        return self.start_bot(bot_id_or_name)

    def suggest_web_port(self, *, start: int = 8901, stop: int = 8999) -> int:
        used_ports = {bot.web_port for bot in self.registry.list_bots()}
        for port in range(start, stop + 1):
            if port in used_ports:
                continue
            if self._is_port_free(port):
                return port
        raise RuntimeError(f"No free web port found in range {start}-{stop}.")

    def _validate_workspace_delete_path(self, workspace: str) -> Path:
        workspace_path = Path(workspace).expanduser().resolve()
        repo_root = self.paths.repo_root.resolve()
        runtime_root = self.paths.runtime_root.resolve()
        home = Path.home().resolve()

        if not workspace_path.exists():
            return workspace_path
        if workspace_path.is_symlink():
            raise RuntimeError(f"Refusing to delete symlink workspace: {workspace_path}")
        if not workspace_path.is_dir():
            raise RuntimeError(f"Workspace is not a directory: {workspace_path}")
        if workspace_path == Path(workspace_path.anchor).resolve():
            raise RuntimeError(f"Refusing to delete filesystem root: {workspace_path}")
        if workspace_path in {repo_root, runtime_root, home}:
            raise RuntimeError(f"Refusing to delete protected directory: {workspace_path}")
        if runtime_root.is_relative_to(workspace_path):
            raise RuntimeError(f"Refusing to delete workspace that contains Adminbot state: {workspace_path}")
        return workspace_path

    def delete_bot(self, bot_id_or_name: str, *, delete_workspace: bool = False) -> BotRecord:
        # Read saved state directly — do not refresh, so identity-lookup failures
        # cannot silently downgrade a running bot to stopped and bypass the guard.
        bot = None
        for candidate in self.registry.list_bots():
            if candidate.id == bot_id_or_name or candidate.name == bot_id_or_name:
                bot = candidate
                break
        if bot is None:
            raise RuntimeError(f"Bot '{bot_id_or_name}' was not found.")
        if bot.process.status == "running" or bot.process.pid is not None:
            raise RuntimeError(
                f"Bot '{bot.name}' may still be running (pid={bot.process.pid}). Stop it before deleting."
            )
        workspace_path = None
        if delete_workspace:
            workspace_path = self._validate_workspace_delete_path(bot.workspace)
        self.registry.remove_bot(bot.id)
        if workspace_path and workspace_path.exists():
            shutil.rmtree(workspace_path)
        return bot

    def open_shell_for_bot(self, bot_id_or_name: str) -> BotRecord:
        bot = self.get_bot(bot_id_or_name)
        self.process_manager.open_shell(bot)
        return bot

    @staticmethod
    def _is_port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True
