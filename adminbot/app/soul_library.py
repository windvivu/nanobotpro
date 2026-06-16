"""Built-in Soul Library loader for Adminbot."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SoulTemplate:
    id: str
    title: str
    description: str
    content: str
    custom: bool = False


_ADMINBOT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOUL_LIBRARY_DIR = _ADMINBOT_ROOT / "soul_library"
_SOUL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _title_from_stem(stem: str) -> str:
    return stem.replace("_", " ").replace("-", " ").title()


def _parse_markdown(path: Path, *, custom: bool = False) -> SoulTemplate:
    content = path.read_text(encoding="utf-8").strip()
    title = _title_from_stem(path.stem)
    description_lines: list[str] = []
    in_description = False
    body_lines: list[str] = []
    in_body = False

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if in_body:
            body_lines.append(raw_line)
            continue
        if line.startswith("# "):
            title = line[2:].strip() or title
            in_description = True
            continue
        if not in_description:
            continue
        if line == "---":
            in_body = True
            continue
        if line.startswith("#"):
            break
        if line:
            description_lines.append(line)
        elif description_lines:
            continue

    description = " ".join(description_lines).strip()
    if len(description) > 220:
        description = description[:217].rstrip() + "..."
    copy_content = "\n".join(body_lines).strip() if custom and body_lines else content

    return SoulTemplate(
        id=path.stem,
        title=title,
        description=description or "Reusable soul prompt content.",
        content=copy_content,
        custom=custom,
    )


def load_soul_templates(root: Path | None = None) -> list[SoulTemplate]:
    """Load built-in soul templates sorted by title."""

    library_root = root or DEFAULT_SOUL_LIBRARY_DIR
    if not library_root.exists():
        return []

    templates = [
        _parse_markdown(path, custom=False)
        for path in sorted(library_root.glob("*.md"))
        if path.is_file() and not path.name.startswith(".")
    ]
    return sorted(templates, key=lambda item: item.title.lower())


def _safe_custom_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _safe_custom_path(root: Path, soul_id: str) -> Path:
    if not _SOUL_ID_RE.fullmatch(soul_id):
        raise ValueError("Invalid soul id")
    base = _safe_custom_root(root)
    path = (base / f"{soul_id}.md").resolve()
    if not path.is_relative_to(base):
        raise ValueError("Invalid soul path")
    return path


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48].strip("-") or "soul"


def _render_custom_soul(title: str, description: str, content: str) -> str:
    return (
        f"# {title.strip()}\n\n"
        f"{description.strip()}\n\n"
        "---\n\n"
        f"{content.strip()}\n"
    )


def load_custom_souls(root: Path) -> list[SoulTemplate]:
    library_root = _safe_custom_root(root)
    templates = [
        _parse_markdown(path, custom=True)
        for path in sorted(library_root.glob("*.md"))
        if path.is_file() and not path.name.startswith(".")
    ]
    return sorted(templates, key=lambda item: item.title.lower())


def get_custom_soul(root: Path, soul_id: str) -> SoulTemplate:
    path = _safe_custom_path(root, soul_id)
    if not path.exists():
        raise FileNotFoundError(f"Custom soul '{soul_id}' not found")
    return _parse_markdown(path, custom=True)


def create_custom_soul(root: Path, *, title: str, description: str, content: str) -> SoulTemplate:
    clean_title = title.strip()
    clean_content = content.strip()
    if not clean_title:
        raise ValueError("Soul title is required")
    if not clean_content:
        raise ValueError("Soul content is required")

    soul_id = f"{_slugify(clean_title)}-{secrets.token_hex(4)}"
    path = _safe_custom_path(root, soul_id)
    path.write_text(_render_custom_soul(clean_title, description, clean_content), encoding="utf-8")
    return get_custom_soul(root, soul_id)


def update_custom_soul(
    root: Path, soul_id: str, *, title: str, description: str, content: str
) -> SoulTemplate:
    clean_title = title.strip()
    clean_content = content.strip()
    if not clean_title:
        raise ValueError("Soul title is required")
    if not clean_content:
        raise ValueError("Soul content is required")

    path = _safe_custom_path(root, soul_id)
    if not path.exists():
        raise FileNotFoundError(f"Custom soul '{soul_id}' not found")
    path.write_text(_render_custom_soul(clean_title, description, clean_content), encoding="utf-8")
    return get_custom_soul(root, soul_id)


def delete_custom_soul(root: Path, soul_id: str) -> None:
    path = _safe_custom_path(root, soul_id)
    if not path.exists():
        raise FileNotFoundError(f"Custom soul '{soul_id}' not found")
    path.unlink()
