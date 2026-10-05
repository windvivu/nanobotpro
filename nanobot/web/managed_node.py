"""A Node.js of nanobot's own, for MCP servers that need a newer one than the machine has.

Playwright MCP needs Node.js 20 or newer and exits at once on an older one. Upgrading the machine's
Node needs root on Linux and can break whatever else uses it, so nanobot downloads the official build
into its data directory instead and puts it first in the PATH of the MCP servers that need it. The
machine's own Node is left alone.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.config.paths import get_data_dir
from nanobot.config.schema import MCPServerConfig

MANAGED_NODE_MAJOR = 24  # the LTS line nanobot downloads; its newest release is taken each time
NODE_DIST_URL = "https://nodejs.org/dist/latest-v{major}.x/"

_NODE_EXE = "node.exe" if os.name == "nt" else "node"
_NODE_LAUNCHERS = frozenset(("node", "npm", "npx"))
_MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
_DOWNLOAD_SECONDS = 600
_LOCK = threading.Lock()  # one download at a time


class ManagedNodeError(RuntimeError):
    """Raised when the machine has no usable Node.js and nanobot could not get its own."""


@dataclass(frozen=True)
class NodeRuntime:
    """The Node.js an MCP server is going to run on."""

    source: str  # "system": the one on PATH; "managed": nanobot's own copy
    version: str
    bin_dir: Path | None = None  # managed only: the directory holding node and npx
    downloaded: bool = False  # nanobot downloaded it just now
    system_version: str = ""  # what the machine has on PATH; "" when it has none

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "downloaded": self.downloaded,
            "system_version": self.system_version,
        }


def managed_node_dir() -> Path:
    """Where nanobot keeps its own Node.js."""
    return get_data_dir() / "managed_runtime" / "node"


def find_node(min_major: int) -> NodeRuntime | None:
    """The Node.js to use, without downloading anything.

    The machine's own when it is new enough, else nanobot's copy when that is already there; None
    when neither will do.
    """
    node = shutil.which("node")
    system = _version_of(node) if node else None
    system_text = _dotted(system) if system else ""
    # Some distributions package npx apart from node: a node without it cannot start these servers
    if system and system[0] >= min_major and shutil.which("npx"):
        return NodeRuntime("system", system_text, system_version=system_text)
    bin_dir = _bin_dir(managed_node_dir())
    managed = _version_of(bin_dir / _NODE_EXE)
    if managed and managed[0] >= min_major:
        return NodeRuntime("managed", _dotted(managed), bin_dir=bin_dir, system_version=system_text)
    return None


def ensure_node(min_major: int) -> NodeRuntime:
    """find_node(), downloading nanobot's own Node.js when nothing on the machine will do.

    Blocking, and a first download takes about a minute: call it from a worker thread.
    """
    with _LOCK:
        found = find_node(min_major)
        if found is not None:
            return found
        _install_managed_node()
        found = find_node(min_major)
        if found is None or found.bin_dir is None:
            raise ManagedNodeError(
                f"Node.js {MANAGED_NODE_MAJOR} was downloaded but is older than the Node.js "
                f"{min_major} this needs."
            )
        return NodeRuntime(
            found.source,
            found.version,
            bin_dir=found.bin_dir,
            downloaded=True,
            system_version=found.system_version,
        )


def use_node(server: MCPServerConfig, runtime: NodeRuntime) -> None:
    """Make an MCP server run on nanobot's own Node.js.

    Its directory goes first in the server's PATH, so that `npx` and the `node` which npx starts both
    resolve to it. The command stays as it is. Nothing to do when the machine's own Node is used.
    """
    if runtime.bin_dir is None or server.command not in _NODE_LAUNCHERS:
        return
    inherited = os.environ.get("PATH", "")
    path = os.pathsep.join(part for part in (str(runtime.bin_dir), inherited) if part)
    server.env = {**server.env, "PATH": path}


def _bin_dir(root: Path) -> Path:
    """The official builds keep node and npx at the top on Windows, under bin/ elsewhere."""
    return root if os.name == "nt" else root / "bin"


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _version_of(node: str | Path) -> tuple[int, int, int] | None:
    """The version a node executable reports; None when it is missing or does not run."""
    try:
        proc = subprocess.run(
            [str(node), "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", proc.stdout.strip())
    if proc.returncode != 0 or not match:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def _platform_archive(system: str = sys.platform, machine: str | None = None) -> tuple[str, str]:
    """The platform part and the extension of the official archive, for this machine by default."""
    machine = machine or platform.machine()
    family = {"win32": "win", "darwin": "darwin"}.get(system) or (
        "linux" if system.startswith("linux") else None
    )
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine.lower()
    )
    if family is None or arch is None:
        raise ManagedNodeError(
            f"nodejs.org has no build for this machine ({system}, {machine}). "
            "Install Node.js yourself."
        )
    return f"{family}-{arch}", "zip" if family == "win" else "tar.gz"


def _install_managed_node() -> None:
    """Download the newest release of the managed LTS line, check it, and put it in place."""
    base = NODE_DIST_URL.format(major=MANAGED_NODE_MAJOR)
    platform_part, extension = _platform_archive()
    target = managed_node_dir()
    work = target.parent / f".node-{uuid.uuid4().hex}.tmp"
    try:
        work.mkdir(parents=True)
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            listing = client.get(base + "SHASUMS256.txt")
            listing.raise_for_status()
            entry = re.search(
                rf"^([0-9a-f]{{64}})\s+(node-v{MANAGED_NODE_MAJOR}\.\d+\.\d+-"
                rf"{re.escape(platform_part)}\.{re.escape(extension)})$",
                listing.text,
                re.MULTILINE,
            )
            if entry is None:
                raise ManagedNodeError(
                    f"nodejs.org lists no Node.js {MANAGED_NODE_MAJOR} build for {platform_part}. "
                    "Install Node.js yourself."
                )
            checksum, filename = entry.groups()
            logger.info("[Node] Downloading {} for nanobot's own use", filename)
            archive = work / filename
            if _download(client, base + filename, archive) != checksum:
                raise ManagedNodeError(
                    "The downloaded Node.js does not match its published checksum; "
                    "nothing was installed."
                )
        unpacked = _unpack(archive, work / "unpacked")
        version = _version_of(_bin_dir(unpacked) / _NODE_EXE)
        if version is None:
            raise ManagedNodeError(
                "The downloaded Node.js does not run on this machine. Install Node.js yourself."
            )
        if target.exists():
            shutil.rmtree(target)
        unpacked.rename(target)
        logger.info("[Node] Node.js {} installed at {}", _dotted(version), target)
    except httpx.HTTPError as exc:
        raise ManagedNodeError(
            f"Could not download Node.js from nodejs.org ({type(exc).__name__}). Check this "
            "machine's internet access, or install Node.js yourself."
        ) from exc
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise ManagedNodeError(f"Could not install Node.js ({type(exc).__name__}: {exc})") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _download(client: httpx.Client, url: str, target: Path) -> str:
    """Stream ``url`` into ``target`` and return the SHA-256 of what arrived."""
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + _DOWNLOAD_SECONDS
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with target.open("wb") as out:
            for chunk in response.iter_bytes(1 << 16):
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES or time.monotonic() > deadline:
                    raise ManagedNodeError(
                        "The Node.js download was too large or too slow; nothing was installed."
                    )
                digest.update(chunk)
                out.write(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, into: Path) -> Path:
    """Unpack the archive and return the one directory at its top."""
    into.mkdir()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            # "data" refuses members that would land outside `into`; Python before 3.11.4 lacks it
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            bundle.extractall(into, **safe)
    tops = list(into.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise ManagedNodeError("Unexpected layout in the Node.js archive; nothing was installed.")
    return tops[0]
