"""Process lifecycle helpers for Adminbot."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from adminbot.app.paths import RuntimePaths
from adminbot.app.registry import BotProcessState, BotRecord
from adminbot.app.runtime_config import sync_bot_runtime_config
from adminbot.app.utils import atomic_write_json, utc_now_iso

MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_LOG_ARCHIVES = 3


@dataclass(slots=True)
class ProcessIdentity:
    pid: int
    executable: str | None
    created_at: str | None
    command_line: str | None = None


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_hidden_startupinfo():
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if not startupinfo_cls:
        return None
    startupinfo = startupinfo_cls()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startupinfo


def _windows_hidden_subprocess_kwargs() -> dict:
    kwargs = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    startupinfo = _windows_hidden_startupinfo()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _powershell_json(command: str) -> dict | None:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command,
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
        **_windows_hidden_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return None
    raw = completed.stdout.strip()
    if not raw:
        return None
    return json.loads(raw)


def get_process_identity(pid: int) -> ProcessIdentity | None:
    if _is_windows():
        command = (
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = %d\"; "
            "if ($null -eq $p) { exit 1 }; "
            "[pscustomobject]@{ "
            "Pid = [int]$p.ProcessId; "
            "ExecutablePath = $p.ExecutablePath; "
            "CreationDate = $p.CreationDate; "
            "CommandLine = $p.CommandLine "
            "} | ConvertTo-Json -Compress"
        ) % pid
        data = _powershell_json(command)
        if not data:
            return None
        return ProcessIdentity(
            pid=int(data["Pid"]),
            executable=data.get("ExecutablePath"),
            created_at=data.get("CreationDate"),
            command_line=data.get("CommandLine"),
        )

    try:
        os.kill(pid, 0)
    except OSError:
        return None

    executable: str | None = None
    created_at: str | None = None
    proc_root = Path("/proc") / str(pid)
    try:
        executable = str((proc_root / "exe").resolve())
    except OSError:
        executable = None
    try:
        stat_content = (proc_root / "stat").read_text(encoding="utf-8", errors="replace")
        parts = stat_content.rsplit(") ", 1)
        if len(parts) == 2:
            fields = parts[1].split()
            # Linux /proc/<pid>/stat field 22 is starttime. After removing pid+comm,
            # it is index 19 in the remaining field list.
            if len(fields) > 19:
                created_at = fields[19]
    except OSError:
        created_at = None
    try:
        raw_cmdline = (proc_root / "cmdline").read_bytes()
        command_line = raw_cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        command_line = None
    return ProcessIdentity(
        pid=pid,
        executable=executable,
        created_at=created_at,
        command_line=command_line,
    )


def _listener_pid_for_port(port: int) -> int | None:
    if not _is_windows():
        return None
    command = (
        "$c = Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; "
        "if ($null -eq $c) { exit 1 }; "
        "[pscustomobject]@{ Pid = [int]$c.OwningProcess } | ConvertTo-Json -Compress"
    ) % int(port)
    data = _powershell_json(command)
    if not data:
        return None
    try:
        return int(data["Pid"])
    except (KeyError, TypeError, ValueError):
        return None


class BotProcessManager:
    """Start, stop, and inspect Nanobot bot processes."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def _state_path(self, bot: BotRecord) -> Path:
        return self.paths.run_dir / f"{bot.id}.json"

    def _stdout_log_path(self, bot: BotRecord) -> Path:
        return self.paths.logs_dir / f"{bot.id}.stdout.log"

    def _stderr_log_path(self, bot: BotRecord) -> Path:
        return self.paths.logs_dir / f"{bot.id}.stderr.log"

    def _archived_log_path(self, path: Path, index: int) -> Path:
        return path.with_name(f"{path.name}.{index}")

    def _background_python_executable(self) -> Path:
        if not _is_windows():
            return self.paths.python_executable
        pythonw = self.paths.python_executable.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
        return self.paths.python_executable

    def _build_gateway_command(self, bot: BotRecord) -> list[str]:
        return [
            str(self._background_python_executable()),
            "-m",
            "nanobot.cli.commands",
            "gateway",
            "--web",
            "--config",
            bot.config_path,
            "--workspace",
            bot.workspace,
        ]

    @staticmethod
    def _quote_powershell(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _write_state_file(self, bot: BotRecord) -> None:
        atomic_write_json(self._state_path(bot), asdict(bot.process))

    def _identity_matches_bot_command(self, bot: BotRecord, identity: ProcessIdentity) -> bool:
        command_line = identity.command_line or ""
        if not command_line:
            return False
        expected_parts = [str(Path(bot.config_path)), str(Path(bot.workspace))]
        if _is_windows():
            command_line = command_line.lower()
            expected_parts = [part.lower() for part in expected_parts]
        return all(part in command_line for part in expected_parts)

    def _recover_running_process_from_port(self, bot: BotRecord) -> ProcessIdentity | None:
        listener_pid = _listener_pid_for_port(bot.web_port)
        if not listener_pid:
            return None
        identity = get_process_identity(listener_pid)
        if identity and self._identity_matches_bot_command(bot, identity):
            return identity
        return None

    def _mark_running_from_identity(self, bot: BotRecord, identity: ProcessIdentity) -> BotRecord:
        bot.process.pid = identity.pid
        bot.process.created_at = identity.created_at
        bot.process.executable = identity.executable
        bot.process.status = "running"
        bot.process.exit_code = None
        self._write_state_file(bot)
        return bot

    def _rotate_log_if_needed(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            if path.stat().st_size < MAX_LOG_BYTES:
                return
        except OSError:
            return

        oldest = self._archived_log_path(path, MAX_LOG_ARCHIVES)
        oldest.unlink(missing_ok=True)
        for index in range(MAX_LOG_ARCHIVES - 1, 0, -1):
            source = self._archived_log_path(path, index)
            target = self._archived_log_path(path, index + 1)
            if source.exists():
                source.replace(target)
        path.replace(self._archived_log_path(path, 1))

    def _mark_stopped(self, bot: BotRecord, *, exit_code: int) -> BotRecord:
        old_command = bot.process.command
        bot.process.status = "stopped"
        bot.process.pid = None
        bot.process.created_at = None
        bot.process.executable = None
        bot.process.command = old_command
        bot.process.last_stopped_at = utc_now_iso()
        bot.process.exit_code = exit_code
        self._write_state_file(bot)
        return bot

    def refresh_status(self, bot: BotRecord) -> BotRecord:
        process = bot.process
        pid = process.pid
        if not pid:
            process.status = "stopped"
            return bot

        current = get_process_identity(pid)
        if not current:
            recovered = self._recover_running_process_from_port(bot)
            if recovered:
                return self._mark_running_from_identity(bot, recovered)
            process.status = "stopped"
            process.exit_code = process.exit_code if process.exit_code is not None else -1
            return bot

        expected_created = process.created_at
        expected_executable = process.executable
        if expected_created and current.created_at and expected_created != current.created_at:
            recovered = self._recover_running_process_from_port(bot)
            if recovered:
                return self._mark_running_from_identity(bot, recovered)
            process.status = "stopped"
            process.exit_code = -1
            return bot
        if expected_executable and current.executable and (
            Path(expected_executable).name.lower() != Path(current.executable).name.lower()
        ):
            recovered = self._recover_running_process_from_port(bot)
            if recovered:
                return self._mark_running_from_identity(bot, recovered)
            process.status = "stopped"
            process.exit_code = -1
            return bot

        process.status = "running"
        process.exit_code = None
        return bot

    def start(self, bot: BotRecord) -> BotRecord:
        bot = self.refresh_status(bot)
        if bot.process.status == "running":
            raise RuntimeError(f"Bot '{bot.name}' is already running.")

        sync_bot_runtime_config(Path(bot.config_path), Path(bot.workspace), bot.web_port)

        stdout_path = self._stdout_log_path(bot)
        stderr_path = self._stderr_log_path(bot)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log_if_needed(stdout_path)
        self._rotate_log_if_needed(stderr_path)

        creationflags = 0
        popen_kwargs = {}
        if _is_windows():
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            popen_kwargs["creationflags"] = creationflags
            startupinfo = _windows_hidden_startupinfo()
            if startupinfo is not None:
                popen_kwargs["startupinfo"] = startupinfo
        else:
            popen_kwargs["start_new_session"] = True

        with stdout_path.open("a", encoding="utf-8") as stdout_handle, stderr_path.open(
            "a", encoding="utf-8"
        ) as stderr_handle:
            process = subprocess.Popen(
                self._build_gateway_command(bot),
                cwd=self.paths.repo_root,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                **popen_kwargs,
            )

        identity = get_process_identity(process.pid)
        bot.process = BotProcessState(
            pid=process.pid,
            created_at=identity.created_at if identity else None,
            executable=identity.executable if identity else str(self.paths.python_executable),
            command=self._build_gateway_command(bot),
            status="running",
            last_started_at=utc_now_iso(),
            last_stopped_at=bot.process.last_stopped_at,
            exit_code=None,
        )
        bot.last_run_at = utc_now_iso()
        self._write_state_file(bot)
        return bot

    def stop(self, bot: BotRecord) -> BotRecord:
        bot = self.refresh_status(bot)
        if bot.process.status != "running" or not bot.process.pid:
            bot.process.status = "stopped"
            return bot

        identity = get_process_identity(bot.process.pid)
        if not identity:
            bot.process.status = "stopped"
            bot.process.last_stopped_at = utc_now_iso()
            self._write_state_file(bot)
            return bot

        if bot.process.created_at and identity.created_at != bot.process.created_at:
            raise RuntimeError(
                f"Refusing to stop PID {bot.process.pid} for bot '{bot.name}' because process identity changed."
            )

        if _is_windows():
            pid = bot.process.pid
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                **_windows_hidden_subprocess_kwargs(),
            )
            if completed.returncode != 0:
                # The process may have exited naturally after the identity check but before taskkill.
                if pid and get_process_identity(pid) is None:
                    return self._mark_stopped(bot, exit_code=0)
                stderr = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(
                    f"Failed to stop bot '{bot.name}' with taskkill."
                    + (f" Details: {stderr}" if stderr else "")
                )
        else:
            try:
                os.killpg(bot.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return self._mark_stopped(bot, exit_code=0)
            except PermissionError as exc:
                raise RuntimeError(
                    f"Failed to stop bot '{bot.name}' process group {bot.process.pid}: permission denied."
                ) from exc

        return self._mark_stopped(bot, exit_code=0)

    def open_shell(self, bot: BotRecord) -> None:
        workspace = Path(bot.workspace)
        if not workspace.exists():
            raise RuntimeError(f"Workspace does not exist: {workspace}")

        if not _is_windows():
            raise RuntimeError(
                "Open Workspace Shell is only supported on Windows. "
                "On Linux/macOS, connect over SSH and cd into the workspace directly."
            )

        self._open_windows_shell(bot)

    def _open_windows_shell(self, bot: BotRecord) -> None:
        workspace = str(Path(bot.workspace))
        title = f"Adminbot - {bot.name}"
        wt_path = shutil.which("wt")
        if wt_path:
            command = [
                wt_path,
                "new-tab",
                "--title",
                title,
                "-d",
                workspace,
                "powershell",
                "-NoExit",
                "-Command",
                f"Set-Location -LiteralPath {self._quote_powershell(workspace)}",
            ]
            subprocess.Popen(command, cwd=self.paths.repo_root)
            return

        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen(
            [
                "powershell",
                "-NoExit",
                "-Command",
                f"$Host.UI.RawUI.WindowTitle = {self._quote_powershell(title)}; "
                f"Set-Location -LiteralPath {self._quote_powershell(workspace)}",
            ],
            cwd=self.paths.repo_root,
            creationflags=creationflags,
        )
