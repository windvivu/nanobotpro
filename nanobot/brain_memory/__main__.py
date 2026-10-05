"""Local migration helper: python -m nanobot.brain_memory --help."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from nanobot.brain_memory.config import BrainMemoryConfig
from nanobot.brain_memory.migration import migrate_source
from nanobot.brain_memory.store import BrainStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or stage selected local memory sources; no LLM calls.")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--root", default="memory/brain", help="Relative Brain Memory directory")
    parser.add_argument("--source", action="append", required=True, help="Selected workspace-relative .md/.jsonl")
    parser.add_argument("--stage", action="store_true", help="Write pending snapshots; never promote facts")
    args = parser.parse_args()
    store = BrainStore(args.workspace, BrainMemoryConfig(enabled=args.stage, root=args.root))
    for source in args.source:
        result = migrate_source(store, source, stage=args.stage)
        print(json.dumps(asdict(result), ensure_ascii=False))


if __name__ == "__main__":
    main()
