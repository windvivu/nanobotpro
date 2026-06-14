"""Registry helpers for Adminbot."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from adminbot.app.utils import atomic_write_json, utc_now_iso


@dataclass(slots=True)
class BotProcessState:
    pid: int | None = None
    created_at: str | None = None
    executable: str | None = None
    command: list[str] = field(default_factory=list)
    status: str = "stopped"
    last_started_at: str | None = None
    last_stopped_at: str | None = None
    exit_code: int | None = None


@dataclass(slots=True)
class BotRecord:
    id: str
    name: str
    workspace: str
    config_path: str
    web_port: int
    created_at: str
    updated_at: str
    last_run_at: str | None = None
    process: BotProcessState = field(default_factory=BotProcessState)


@dataclass(slots=True)
class RegistryData:
    version: int = 1
    bots: list[BotRecord] = field(default_factory=list)


def _bot_from_dict(raw: dict) -> BotRecord:
    process_raw = raw.get("process") or {}
    process = BotProcessState(
        pid=process_raw.get("pid"),
        created_at=process_raw.get("created_at"),
        executable=process_raw.get("executable"),
        command=list(process_raw.get("command") or []),
        status=process_raw.get("status") or "stopped",
        last_started_at=process_raw.get("last_started_at"),
        last_stopped_at=process_raw.get("last_stopped_at"),
        exit_code=process_raw.get("exit_code"),
    )
    return BotRecord(
        id=raw["id"],
        name=raw["name"],
        workspace=raw["workspace"],
        config_path=raw["config_path"],
        web_port=int(raw["web_port"]),
        created_at=raw["created_at"],
        updated_at=raw.get("updated_at") or raw["created_at"],
        last_run_at=raw.get("last_run_at"),
        process=process,
    )


class BotRegistry:
    """Load/save bot definitions in `.adminbot/bots.json`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure_exists(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(RegistryData())

    def load(self) -> RegistryData:
        self.ensure_exists()
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return RegistryData()
        parsed = json.loads(raw)
        bots = [_bot_from_dict(item) for item in parsed.get("bots") or []]
        return RegistryData(version=int(parsed.get("version", 1)), bots=bots)

    def save(self, data: RegistryData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": data.version,
            "bots": [asdict(bot) for bot in data.bots],
        }
        atomic_write_json(self.path, payload)

    def get_bot(self, bot_id: str) -> BotRecord | None:
        registry = self.load()
        for bot in registry.bots:
            if bot.id == bot_id:
                return bot
        return None

    def find_by_name(self, name: str) -> BotRecord | None:
        registry = self.load()
        for bot in registry.bots:
            if bot.name == name:
                return bot
        return None

    def upsert_bot(self, record: BotRecord) -> BotRecord:
        registry = self.load()
        for index, existing in enumerate(registry.bots):
            if existing.id == record.id:
                record.updated_at = utc_now_iso()
                registry.bots[index] = record
                self.save(registry)
                return record

        record.updated_at = utc_now_iso()
        registry.bots.append(record)
        self.save(registry)
        return record

    def list_bots(self) -> list[BotRecord]:
        return self.load().bots

    def remove_bot(self, bot_id: str) -> bool:
        registry = self.load()
        before = len(registry.bots)
        registry.bots = [b for b in registry.bots if b.id != bot_id]
        if len(registry.bots) == before:
            return False
        self.save(registry)
        return True

    def replace_bots(self, bots: list[BotRecord]) -> list[BotRecord]:
        registry = self.load()
        registry.bots = bots
        self.save(registry)
        return registry.bots
