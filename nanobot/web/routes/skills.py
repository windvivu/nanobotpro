"""Skills manager routes — list, view, and toggle skills."""

import asyncio
import re
import shutil as _shutil
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from nanobot.agent.skills import SkillsLoader

router = APIRouter()


def _get_workspace(request: Request) -> Path:
    return request.app.state.config.workspace_path


def _get_disabled_skills(request: Request) -> list[str]:
    return list(request.app.state.config.tools.disabled_skills)


@router.get("/skills", response_class=HTMLResponse)
async def skills_list(request: Request):
    """List all available skills."""
    workspace = _get_workspace(request)
    disabled_skills = _get_disabled_skills(request)
    loader = SkillsLoader(workspace)
    all_skills = loader.list_skills(filter_unavailable=False)

    # Determine which skills meet system requirements (ignoring manual disabling)
    available_set = {
        s["name"] for s in all_skills
        if loader._check_requirements(loader._get_skill_meta(s["name"]))
    }

    skills_data = []
    for s in all_skills:
        meta = loader.get_skill_metadata(s["name"])
        skill_meta = loader._get_skill_meta(s["name"])
        meets_requirements = s["name"] in available_set
        is_manually_disabled = s["name"] in disabled_skills

        missing_reason = ""
        install_hint = ""
        if not meets_requirements:
            missing_reason = loader._get_missing_requirements(skill_meta)
            installs = skill_meta.get("install", [])
            if installs:
                hints = []
                for i in installs:
                    kind = i.get("kind", "")
                    pkg = i.get("formula") or i.get("package", "")
                    label = i.get("label", "")
                    if kind and pkg:
                        hints.append(f"{kind} install {pkg}")
                    elif label:
                        hints.append(label)
                install_hint = "  |  ".join(hints)

        # Check if workspace skill also has a builtin counterpart
        has_builtin = False
        if s["source"] == "workspace" and loader.builtin_skills:
            has_builtin = (loader.builtin_skills / s["name"] / "SKILL.md").exists()

        skills_data.append({
            "name": s["name"],
            "source": s["source"],
            "path": s["path"],
            "description": meta.get("description", "") if meta else "",
            "meets_requirements": meets_requirements,
            "manually_disabled": is_manually_disabled,
            "available": meets_requirements and not is_manually_disabled,
            "missing_reason": missing_reason,
            "install_hint": install_hint,
            "has_builtin": has_builtin,
        })

    # Load MCP servers from config
    mcp_servers = {}
    mcp_presets = {}
    mcp_custom_preset_data = {}
    trader_mode_enabled = False
    try:
        from nanobot.config.loader import load_config
        from nanobot.web.mcp_presets import (
            MCP_PRESET_MIN_NODE,
            MCPPresetError,
            adopt_exact_builtin_preset,
            list_mcp_presets,
        )

        cfg = load_config()
        trader_mode_enabled = bool(getattr(cfg, "trader_mode", False))
        preset_state = list_mcp_presets(cfg.tools)
        ownership = {
            server_name: preset_id
            for preset_id, installation in cfg.tools.mcp_preset_installations.items()
            for server_name in installation.server_names
        }
        for name, srv in cfg.tools.mcp_servers.items():
            mcp_servers[name] = {
                "name": name,
                "type": srv.type or ("stdio" if srv.command else "sse"),
                "command": srv.command,
                "args": srv.args,
                "env": srv.env,
                "url": srv.url,
                "headers": srv.headers,
                "tool_timeout": srv.tool_timeout,
                "enabled_tools": srv.enabled_tools,
                "enabled": srv.enabled,
                "owner": ownership.get(name, ""),
            }
        for preset_id, item in preset_state.items():
            bundle = item["bundle"]
            adoptable = False
            if item["source"] == "builtin" and not item["installed"]:
                try:
                    adopt_exact_builtin_preset(cfg.tools.model_copy(deep=True), preset_id)
                    adoptable = True
                except MCPPresetError:
                    pass
            mcp_presets[preset_id] = {
                "id": preset_id,
                "label": bundle.label,
                "description": bundle.description,
                "source": item["source"],
                "installed": item["installed"],
                "enabled": item["enabled"],
                "server_count": len(bundle.servers),
                "server_names": list(bundle.servers),
                "adoptable": adoptable,
                "min_node": MCP_PRESET_MIN_NODE.get(preset_id, 0),
            }
        mcp_custom_preset_data = {
            preset_id: bundle.model_dump(mode="json")
            for preset_id, bundle in cfg.tools.mcp_custom_presets.items()
        }
    except Exception as e:
        logger.warning("[Skills] Failed to load MCP servers: {}", e)

    return request.app.state.templates.TemplateResponse(request, "skills.html", {"skills": skills_data,
        "total": len(skills_data),
        "workspace": str(workspace),
        "mcp_servers": mcp_servers,
        "mcp_presets": mcp_presets,
        "mcp_custom_preset_data": mcp_custom_preset_data,
        "trader_mode_enabled": trader_mode_enabled,
    })


@router.post("/skills/{name}/toggle")
async def toggle_skill(request: Request, name: str):
    """Toggle a skill's enabled/disabled state."""
    from nanobot.config.loader import load_config, save_config

    config = load_config()
    disabled = list(config.tools.disabled_skills)

    if name in disabled:
        disabled.remove(name)
        enabled = True
    else:
        disabled.append(name)
        enabled = False

    config.tools.disabled_skills = disabled
    save_config(config)
    request.app.state.config = config

    # Hot-reload: update live agent's SkillsLoader so change takes effect immediately
    agent = getattr(request.app.state, "agent", None)
    if agent and hasattr(agent, "context") and hasattr(agent.context, "skills"):
        agent.context.skills.disabled_skills = set(disabled)

    action = "enabled" if enabled else "disabled"
    logger.info("[Skills] Skill '{}' {} via web dashboard", name, action)
    return JSONResponse({"success": True, "enabled": enabled, "skill": name})


@router.get("/skills/trader-mode")
async def trader_mode_state(request: Request):
    """Return the persisted Trader Mode state."""
    from nanobot.config.loader import load_config

    config = load_config()
    return JSONResponse({"success": True, "enabled": bool(config.trader_mode)})


@router.post("/skills/trader-mode")
async def set_trader_mode(request: Request):
    """Persist Trader Mode and refresh the live agent's gated tools."""
    from nanobot.config.loader import load_config, save_config

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"success": False, "error": "enabled must be a boolean"},
            status_code=400,
        )

    config = load_config()
    config.trader_mode = enabled
    save_config(config)
    request.app.state.config = config

    agent = getattr(request.app.state, "agent", None)
    if agent is not None and hasattr(agent, "_reload_config"):
        agent._reload_config()

    logger.info("[Skills] Trader Mode {} via web dashboard", "enabled" if enabled else "disabled")
    return JSONResponse({"success": True, "enabled": enabled})


@router.get("/skills/{name}", response_class=HTMLResponse)
async def skill_detail(request: Request, name: str):
    """View a specific skill's SKILL.md content."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    content = loader.load_skill(name)
    if content is None:
        return HTMLResponse("Skill not found", status_code=404)

    # Get metadata
    meta = loader.get_skill_metadata(name)

    # Determine source and override state
    all_skills = loader.list_skills(filter_unavailable=False)
    source = "unknown"
    for s in all_skills:
        if s["name"] == name:
            source = s["source"]
            break

    # Check if both builtin and workspace versions exist
    builtin_path = loader.builtin_skills / name / "SKILL.md"
    workspace_path = loader.workspace_skills / name / "SKILL.md"
    has_builtin = builtin_path.exists()
    has_workspace = workspace_path.exists()
    is_overridden = has_builtin and has_workspace  # workspace overrides builtin

    return request.app.state.templates.TemplateResponse(request, "skill_detail.html", {"name": name,
        "source": source,
        "description": meta.get("description", "") if meta else "",
        "content": content,
        "has_builtin": has_builtin,
        "has_workspace": has_workspace,
        "is_overridden": is_overridden})


@router.post("/skills/{name}/override")
async def override_skill(request: Request, name: str):
    """Copy builtin skill to workspace for customization."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    builtin_path = loader.builtin_skills / name / "SKILL.md"
    if not builtin_path.exists():
        return JSONResponse({"success": False, "error": "Builtin skill not found"}, status_code=404)

    workspace_skill_dir = loader.workspace_skills / name
    workspace_path = workspace_skill_dir / "SKILL.md"

    if workspace_path.exists():
        return JSONResponse({"success": False, "error": "Workspace override already exists"})

    # Copy entire skill directory (SKILL.md + any scripts/resources)
    import shutil
    builtin_dir = loader.builtin_skills / name
    shutil.copytree(str(builtin_dir), str(workspace_skill_dir))

    logger.info("[Skills] Created workspace override for '{}' at {}", name, workspace_skill_dir)
    return JSONResponse({"success": True, "message": f"Skill '{name}' copied to workspace for editing"})


@router.post("/skills/{name}/restore")
async def restore_skill(request: Request, name: str):
    """Remove workspace override, restoring the builtin version."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    builtin_path = loader.builtin_skills / name / "SKILL.md"
    workspace_skill_dir = loader.workspace_skills / name

    if not builtin_path.exists():
        return JSONResponse({"success": False, "error": "No builtin skill to restore to"}, status_code=400)

    if not workspace_skill_dir.exists():
        return JSONResponse({"success": False, "error": "No workspace override to remove"})

    # Remove workspace override directory
    import shutil
    shutil.rmtree(str(workspace_skill_dir))

    logger.info("[Skills] Restored builtin '{}' (removed workspace override)", name)
    return JSONResponse({"success": True, "message": f"Skill '{name}' restored to builtin version"})


@router.post("/skills/{name}/delete")
async def delete_skill(request: Request, name: str):
    """Delete a workspace-only skill (not a builtin override)."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    workspace_skill_dir = loader.workspace_skills / name
    if not workspace_skill_dir.exists():
        return JSONResponse({"success": False, "error": "Skill not found in workspace"}, status_code=404)

    # Prevent deleting builtin overrides (use restore instead)
    builtin_path = loader.builtin_skills / name / "SKILL.md"
    if builtin_path.exists():
        return JSONResponse({"success": False, "error": "This is a builtin override. Use 'Restore Builtin' instead."}, status_code=400)

    import shutil
    shutil.rmtree(str(workspace_skill_dir))

    logger.info("[Skills] Deleted workspace skill '{}'", name)
    return JSONResponse({"success": True, "message": f"Skill '{name}' deleted"})


@router.post("/skills/{name}/save")
async def save_skill(request: Request, name: str):
    """Save edited SKILL.md content for a workspace skill."""
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)

    workspace_path = loader.workspace_skills / name / "SKILL.md"
    if not workspace_path.exists():
        return JSONResponse({"success": False, "error": "Only workspace skills can be edited"}, status_code=400)

    body = await request.json()
    content = body.get("content", "")
    if not content.strip():
        return JSONResponse({"success": False, "error": "Content cannot be empty"})

    workspace_path.write_text(content, encoding="utf-8")
    logger.info("[Skills] Saved workspace skill '{}'", name)
    return JSONResponse({"success": True})


# ============================================================================
# ClawHub — Skill Marketplace
# ============================================================================


def _find_npx() -> str | None:
    """Find npx binary."""
    return _shutil.which('npx')


async def _run_clawhub(*args: str, workdir: str | None = None) -> tuple[int, str]:
    """Run clawhub CLI command and return (exit_code, output)."""
    parts = ['npx', '--yes', 'clawhub@latest', *args]
    if workdir:
        parts.extend(['--workdir', workdir])
    cmd = ' '.join(parts)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode('utf-8', errors='replace').strip()
        # Filter Node.js noise from stderr
        err_text = stderr.decode('utf-8', errors='replace').strip()
        err_text = '\n'.join(line for line in err_text.splitlines() if 'ExperimentalWarning' not in line and 'trace-warnings' not in line).strip()
        if proc.returncode != 0 and not output:
            output = err_text
        return proc.returncode, output
    except asyncio.TimeoutError:
        return 1, 'Command timed out (120s) — check your network connection'
    except Exception as e:
        return 1, str(e)


def _parse_search_results(output: str) -> list[dict]:
    """Parse clawhub search output: 'slug  Name  (score)' per line."""
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Match: slug  Name  (score)
        m = re.match(r'^(\S+)\s+(.+?)\s+\(([0-9.]+)\)$', line)
        if m:
            results.append({
                'slug': m.group(1),
                'name': m.group(2).strip(),
                'score': float(m.group(3)),
            })
    return results


@router.get('/skills/hub/search')
async def hub_search(request: Request, q: str = ''):
    """Search ClawHub registry via REST API."""
    if not q.strip():
        return JSONResponse({'success': True, 'results': []})

    import json as _json
    import urllib.request

    try:
        url = f'https://clawhub.ai/api/search?q={urllib.parse.quote(q)}&limit=12'
        req = urllib.request.Request(url, headers={'User-Agent': 'nanobot'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
    except Exception as e:
        return JSONResponse({'success': False, 'error': f'ClawHub API error: {e}'})

    results = []
    for r in data.get('results', []):
        results.append({
            'slug': r.get('slug', ''),
            'name': r.get('displayName', r.get('slug', '')),
            'summary': r.get('summary', ''),
            'score': round(r.get('score', 0), 1),
        })

    # Mark which ones are already installed
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)
    installed = {s['name'] for s in loader.list_skills(filter_unavailable=False)}
    for r in results:
        r['installed'] = r['slug'] in installed

    return JSONResponse({'success': True, 'results': results})


@router.post('/skills/hub/install')
async def hub_install(request: Request):
    """Install a skill from ClawHub."""
    body = await request.json()
    slug = body.get('slug', '').strip()
    if not slug:
        return JSONResponse({'success': False, 'error': 'No slug provided'})

    if not _find_npx():
        return JSONResponse({'success': False, 'error': 'npx not found'}, status_code=500)

    workspace = _get_workspace(request)
    code, output = await _run_clawhub('install', slug, workdir=str(workspace))
    if code != 0:
        return JSONResponse({'success': False, 'error': output or 'Install failed'})

    logger.info('[Skills Hub] Installed skill "{}" from ClawHub', slug)
    return JSONResponse({'success': True, 'message': f'Skill "{slug}" installed'})


@router.post('/skills/hub/uninstall')
async def hub_uninstall(request: Request):
    """Uninstall a ClawHub skill (remove from workspace)."""
    body = await request.json()
    slug = body.get('slug', '').strip()
    if not slug:
        return JSONResponse({'success': False, 'error': 'No slug provided'})

    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)
    skill_dir = loader.workspace_skills / slug

    if not skill_dir.exists():
        return JSONResponse({'success': False, 'error': f'Skill "{slug}" not found in workspace'})

    # Only allow uninstalling workspace skills (not builtin)
    builtin_path = loader.builtin_skills / slug
    if builtin_path.exists():
        return JSONResponse({'success': False, 'error': f'Cannot uninstall builtin skill "{slug}"'})

    import shutil
    shutil.rmtree(str(skill_dir))
    logger.info('[Skills Hub] Uninstalled skill "{}"', slug)
    return JSONResponse({'success': True, 'message': f'Skill "{slug}" uninstalled'})


@router.post('/skills/hub/upload')
async def hub_upload(request: Request):
    """Install a skill from uploaded zip file."""
    import io
    import json as _json
    import re
    import zipfile

    form = await request.form()
    upload = form.get('file')
    if not upload:
        return JSONResponse({'success': False, 'error': 'No file uploaded'})

    # Read file into memory
    data = await upload.read()

    # Size limit: 10MB
    if len(data) > 10 * 1024 * 1024:
        return JSONResponse({'success': False, 'error': 'File too large (max 10MB)'})

    # Verify it is a valid zip
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return JSONResponse({'success': False, 'error': 'Invalid zip file'})

    names = zf.namelist()

    # SECURITY: Check for path traversal
    for name in names:
        if name.startswith('/') or '..' in name:
            return JSONResponse({'success': False, 'error': f'Unsafe path detected: {name}'})

    # Must contain SKILL.md at root level
    if 'SKILL.md' not in names:
        return JSONResponse({'success': False, 'error': 'Not a valid skill package: SKILL.md not found at root'})

    # SECURITY: Block executable/script files that are NOT part of known skill patterns
    BLOCKED_EXTENSIONS = {'.exe', '.bat', '.cmd', '.msi', '.dll', '.so', '.dylib', '.com', '.scr', '.pif'}  # noqa: N806 — security constant
    for name in names:
        ext = Path(name).suffix.lower()
        if ext in BLOCKED_EXTENSIONS:
            return JSONResponse({'success': False, 'error': f'Blocked file type: {name}'})

    # Determine skill slug from _meta.json or SKILL.md frontmatter
    slug = None
    if '_meta.json' in names:
        try:
            meta = _json.loads(zf.read('_meta.json').decode('utf-8'))
            slug = meta.get('slug', '').strip()
        except Exception:
            pass

    if not slug:
        # Try to extract name from SKILL.md frontmatter
        skill_md = zf.read('SKILL.md').decode('utf-8', errors='replace')
        m = re.search(r'^name:\s*(.+)$', skill_md, re.MULTILINE)
        if m:
            slug = m.group(1).strip().lower().replace(' ', '-')
            slug = re.sub(r'[^a-z0-9_-]', '', slug)

    if not slug:
        return JSONResponse({'success': False, 'error': 'Cannot determine skill name from zip'})

    # Extract to workspace/skills/<slug>/
    workspace = _get_workspace(request)
    loader = SkillsLoader(workspace)
    target_dir = loader.workspace_skills / slug

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Extract all files
    for member in names:
        target_path = target_dir / member
        if member.endswith('/'):
            target_path.mkdir(parents=True, exist_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(zf.read(member))

    logger.info('[Skills Hub] Installed skill "{}" from uploaded zip ({} files)', slug, len(names))
    return JSONResponse({'success': True, 'message': f'Skill "{slug}" installed from zip', 'slug': slug})


# ── MCP Server Management ─────────────────────────────────────────


def _mcp_preset_owner(tools, server_name: str) -> str | None:
    for preset_id, installation in tools.mcp_preset_installations.items():
        if server_name in installation.server_names:
            return preset_id
    return None


def _mcp_preset_error_response(exc: Exception) -> JSONResponse:
    from nanobot.web.mcp_presets import MCPPresetConflictError, MCPPresetInUseError

    status_code = 409 if isinstance(exc, (MCPPresetConflictError, MCPPresetInUseError)) else 400
    return JSONResponse({"success": False, "error": str(exc)}, status_code=status_code)


def _save_mcp_config(request: Request, config) -> None:
    from nanobot.config.loader import save_config

    save_config(config)
    request.app.state.config = config


@router.get("/skills/mcp/presets")
async def mcp_presets_catalog():
    """Return secret-free MCP preset catalog and installation state."""
    from nanobot.config.loader import load_config
    from nanobot.web.mcp_presets import list_mcp_presets

    items = list_mcp_presets(load_config().tools)
    result = {}
    for preset_id, item in items.items():
        bundle = item["bundle"]
        result[preset_id] = {
            "label": bundle.label,
            "description": bundle.description,
            "source": item["source"],
            "installed": item["installed"],
            "enabled": item["enabled"],
            "server_names": list(bundle.servers),
        }
    return JSONResponse({"success": True, "presets": result})


@router.post("/skills/mcp/presets/tradingview/install")
async def tradingview_mcp_install(request: Request):
    """Install the managed TradingView MCP backend and materialize its MCP config."""
    from nanobot.config.loader import load_config
    from nanobot.web.tradingview_mcp_installer import (
        TRADINGVIEW_MCP_SERVER_NAME,
        TradingViewMCPInstallError,
        build_tradingview_mcp_config,
        install_tradingview_mcp,
    )

    try:
        result = install_tradingview_mcp(force=False)
    except TradingViewMCPInstallError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("[TradingView MCP] Install failed unexpectedly")
        return JSONResponse(
            {"success": False, "error": f"Unexpected install error: {type(exc).__name__}"},
            status_code=500,
        )

    try:
        config = load_config()
        config.tools.mcp_servers[TRADINGVIEW_MCP_SERVER_NAME] = build_tradingview_mcp_config()
        _save_mcp_config(request, config)
    except Exception as exc:
        logger.exception("[TradingView MCP] Install succeeded but config save failed")
        return JSONResponse(
            {"success": False, "error": f"Backend installed, but config save failed: {type(exc).__name__}"},
            status_code=500,
        )
    return JSONResponse(
        {
            "success": True,
            "server_name": TRADINGVIEW_MCP_SERVER_NAME,
            "install": result.as_dict(),
            "restart_required": True,
        }
    )


@router.post("/skills/mcp/presets/tradingview/reinstall")
async def tradingview_mcp_reinstall(request: Request):
    """Reinstall the managed TradingView MCP backend from the pinned commit."""
    from nanobot.config.loader import load_config
    from nanobot.web.tradingview_mcp_installer import (
        TRADINGVIEW_MCP_SERVER_NAME,
        TradingViewMCPInstallError,
        build_tradingview_mcp_config,
        install_tradingview_mcp,
    )

    try:
        result = install_tradingview_mcp(force=True)
    except TradingViewMCPInstallError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("[TradingView MCP] Reinstall failed unexpectedly")
        return JSONResponse(
            {"success": False, "error": f"Unexpected reinstall error: {type(exc).__name__}"},
            status_code=500,
        )

    try:
        config = load_config()
        config.tools.mcp_servers[TRADINGVIEW_MCP_SERVER_NAME] = build_tradingview_mcp_config()
        _save_mcp_config(request, config)
    except Exception as exc:
        logger.exception("[TradingView MCP] Reinstall succeeded but config save failed")
        return JSONResponse(
            {"success": False, "error": f"Backend reinstalled, but config save failed: {type(exc).__name__}"},
            status_code=500,
        )
    return JSONResponse(
        {
            "success": True,
            "server_name": TRADINGVIEW_MCP_SERVER_NAME,
            "install": result.as_dict(),
            "restart_required": True,
        }
    )


@router.post("/skills/mcp/presets/tradingview/uninstall")
async def tradingview_mcp_uninstall(request: Request):
    """Remove TradingView MCP config while keeping the managed backend cache."""
    from nanobot.config.loader import load_config
    from nanobot.web.tradingview_mcp_installer import uninstall_tradingview_mcp_config

    config = load_config()
    removed = uninstall_tradingview_mcp_config(config)
    _save_mcp_config(request, config)
    return JSONResponse({"success": True, "removed": removed, "backend_cache_kept": True})


@router.get("/skills/mcp/presets/tradingview/health")
async def tradingview_mcp_health_route():
    """Return secret-free TradingView MCP readiness checks."""
    from nanobot.config.loader import load_config
    from nanobot.web.tradingview_mcp_installer import tradingview_mcp_health

    return JSONResponse(await tradingview_mcp_health(load_config()))


@router.post("/skills/mcp/presets/{preset_id}/install")
async def mcp_preset_install(request: Request, preset_id: str):
    from nanobot.config.loader import load_config
    from nanobot.web import managed_node
    from nanobot.web.mcp_presets import MCP_PRESET_MIN_NODE, MCPPresetError, install_mcp_preset

    config = load_config()
    node = None
    try:
        min_node = MCP_PRESET_MIN_NODE.get(preset_id.strip())
        if min_node:
            # Refuse a preset that cannot be installed before the download, not after it
            install_mcp_preset(config.tools.model_copy(deep=True), preset_id)
            node = await asyncio.to_thread(managed_node.ensure_node, min_node)
            config = load_config()  # a first download takes a minute: read the config again
        installation = install_mcp_preset(config.tools, preset_id)
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)
    except managed_node.ManagedNodeError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=502)
    if node is not None:
        for name in installation.server_names:
            managed_node.use_node(config.tools.mcp_servers[name], node)
    _save_mcp_config(request, config)
    return JSONResponse(
        {
            "success": True,
            "server_names": installation.server_names,
            "node": node.as_dict() if node is not None else None,
        }
    )


@router.post("/skills/mcp/presets/{preset_id}/test")
async def mcp_preset_test(preset_id: str):
    """Inspect every server in a bundle without saving or registering tools."""
    from nanobot.agent.tools.mcp import inspect_mcp_server
    from nanobot.config.loader import load_config
    from nanobot.web import managed_node
    from nanobot.web.mcp_presets import (
        MCP_PRESET_MIN_NODE,
        MCPPresetError,
        list_mcp_presets,
        validate_preset_id,
    )

    try:
        preset_id = validate_preset_id(preset_id)
        item = list_mcp_presets(load_config().tools).get(preset_id)
        if item is None:
            raise MCPPresetError(f"MCP preset '{preset_id}' is not defined")
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)

    bundle = item["bundle"]
    node = None
    min_node = MCP_PRESET_MIN_NODE.get(preset_id)
    if min_node:
        # With the Node.js an install would use; a test downloads nothing
        node = await asyncio.to_thread(managed_node.find_node, min_node)
        if node is None:
            return JSONResponse(
                {
                    "success": False,
                    "error": (
                        f"This bundle needs Node.js {min_node} or newer and this machine has none. "
                        "Install the bundle: nanobot then downloads its own copy."
                    ),
                }
            )
    results = {}
    success = True
    for server_name, server in bundle.servers.items():
        if node is not None:
            managed_node.use_node(server, node)
        inspection = await inspect_mcp_server(server_name, server)
        results[server_name] = inspection.as_dict()
        success = success and inspection.success
    return JSONResponse({"success": success, "servers": results})


@router.post("/skills/mcp/presets/{preset_id}/toggle")
async def mcp_preset_toggle(request: Request, preset_id: str):
    from nanobot.config.loader import load_config
    from nanobot.web.mcp_presets import MCPPresetError, set_mcp_preset_enabled

    body = await request.json()
    if not isinstance(body.get("enabled"), bool):
        return JSONResponse({"success": False, "error": "enabled must be boolean"}, status_code=400)
    config = load_config()
    try:
        installation = set_mcp_preset_enabled(config.tools, preset_id, body["enabled"])
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)
    _save_mcp_config(request, config)
    return JSONResponse({"success": True, "enabled": installation.enabled})


@router.post("/skills/mcp/presets/{preset_id}/uninstall")
async def mcp_preset_uninstall(request: Request, preset_id: str):
    from nanobot.config.loader import load_config
    from nanobot.web.mcp_presets import MCPPresetError, uninstall_mcp_preset

    config = load_config()
    try:
        uninstall_mcp_preset(config.tools, preset_id)
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)
    _save_mcp_config(request, config)
    return JSONResponse({"success": True})


@router.post("/skills/mcp/presets/{preset_id}/adopt")
async def mcp_preset_adopt(request: Request, preset_id: str):
    from nanobot.config.loader import load_config
    from nanobot.web.mcp_presets import MCPPresetError, adopt_exact_builtin_preset

    config = load_config()
    try:
        installation = adopt_exact_builtin_preset(config.tools, preset_id)
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)
    _save_mcp_config(request, config)
    return JSONResponse({"success": True, "server_names": installation.server_names})


@router.post("/skills/mcp/presets/custom/save")
async def mcp_custom_preset_save(request: Request):
    from pydantic import ValidationError

    from nanobot.config.loader import load_config
    from nanobot.config.schema import MCPPresetBundleConfig
    from nanobot.web.mcp_presets import MCPPresetError, save_custom_mcp_preset

    body = await request.json()
    try:
        preset_id = str(body.get("preset_id", ""))
        raw_bundle = body.get("bundle")
        if isinstance(raw_bundle, str):
            bundle = MCPPresetBundleConfig.model_validate_json(raw_bundle)
        else:
            bundle = MCPPresetBundleConfig.model_validate(raw_bundle)
    except (ValidationError, TypeError, ValueError):
        return JSONResponse({"success": False, "error": "Invalid MCP preset JSON"}, status_code=400)

    config = load_config()
    try:
        save_custom_mcp_preset(config.tools, preset_id, bundle)
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)
    _save_mcp_config(request, config)
    return JSONResponse({"success": True, "preset_id": preset_id})


@router.post("/skills/mcp/presets/custom/{preset_id}/delete")
async def mcp_custom_preset_delete(request: Request, preset_id: str):
    from nanobot.config.loader import load_config
    from nanobot.web.mcp_presets import MCPPresetError, delete_custom_mcp_preset

    config = load_config()
    try:
        delete_custom_mcp_preset(config.tools, preset_id)
    except MCPPresetError as exc:
        return _mcp_preset_error_response(exc)
    _save_mcp_config(request, config)
    return JSONResponse({"success": True})

@router.post("/skills/mcp/add")
async def mcp_add(request: Request):
    """Add a new MCP server to config."""
    from nanobot.config.loader import load_config, save_config
    from nanobot.config.schema import MCPServerConfig

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"success": False, "error": "Server name is required"})
    # Sanitize name
    import re
    name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    if not name:
        return JSONResponse({"success": False, "error": "Invalid name (use letters, numbers, - or _)"})

    cfg = load_config()
    if name in cfg.tools.mcp_servers:
        return JSONResponse({"success": False, "error": f"Server '{name}' already exists"})

    srv = MCPServerConfig(
        type=body.get("type") or None,
        command=body.get("command", ""),
        args=body.get("args", []),
        env=body.get("env", {}),
        url=body.get("url", ""),
        headers=body.get("headers", {}),
        tool_timeout=int(body.get("tool_timeout", 30)),
        enabled_tools=body.get("enabled_tools", ["*"]),
    )

    # Validate: must have command (stdio) or url (sse/http)
    if not srv.command and not srv.url:
        return JSONResponse({"success": False, "error": "Either command (stdio) or url (sse/http) is required"})

    cfg.tools.mcp_servers[name] = srv
    save_config(cfg)
    logger.info("[MCP] Added server '{}' ({})", name, srv.type or "auto")
    return JSONResponse({"success": True, "message": f"MCP server '{name}' added. Restart gateway to connect."})


@router.post("/skills/mcp/{name}/edit")
async def mcp_edit(request: Request, name: str):
    """Edit an existing MCP server config."""
    from nanobot.config.loader import load_config, save_config

    cfg = load_config()
    if name not in cfg.tools.mcp_servers:
        return JSONResponse({"success": False, "error": f"Server '{name}' not found"}, status_code=404)

    body = await request.json()
    srv = cfg.tools.mcp_servers[name]
    owner = _mcp_preset_owner(cfg.tools, name)
    protected_fields = ("type", "command", "args", "env", "url", "headers")
    if owner and any(key in body for key in protected_fields):
        return JSONResponse(
            {"success": False, "error": f"Server '{name}' is owned by preset '{owner}'"},
            status_code=409,
        )

    if "command" in body:
        srv.command = body["command"]
    if "args" in body:
        srv.args = body["args"]
    if "env" in body:
        srv.env = body["env"]
    if "url" in body:
        srv.url = body["url"]
    if "headers" in body:
        srv.headers = body["headers"]
    if "tool_timeout" in body:
        srv.tool_timeout = int(body["tool_timeout"])
    if "enabled_tools" in body:
        srv.enabled_tools = body["enabled_tools"]
    if "enabled" in body:
        srv.enabled = bool(body["enabled"])
    if "type" in body:
        srv.type = body["type"] or None

    save_config(cfg)
    logger.info("[MCP] Updated server '{}'", name)
    return JSONResponse({"success": True, "message": f"MCP server '{name}' updated. Restart gateway to apply."})


@router.post("/skills/mcp/{name}/delete")
async def mcp_delete(request: Request, name: str):
    """Delete an MCP server from config."""
    from nanobot.config.loader import load_config, save_config

    cfg = load_config()
    if name not in cfg.tools.mcp_servers:
        return JSONResponse({"success": False, "error": f"Server '{name}' not found"}, status_code=404)

    owner = _mcp_preset_owner(cfg.tools, name)
    if owner:
        return JSONResponse(
            {
                "success": False,
                "error": f"Server '{name}' is owned by preset '{owner}'; uninstall the preset instead",
            },
            status_code=409,
        )

    del cfg.tools.mcp_servers[name]
    save_config(cfg)
    logger.info("[MCP] Deleted server '{}'", name)
    return JSONResponse({"success": True, "message": f"MCP server '{name}' removed. Restart gateway to apply."})


def _mcp_test_config(body: dict):
    """Validate a test payload without returning Pydantic input details."""
    from nanobot.config.schema import MCPServerConfig

    allowed = {
        "enabled",
        "type",
        "command",
        "args",
        "env",
        "url",
        "headers",
        "tool_timeout",
        "enabled_tools",
    }
    return MCPServerConfig.model_validate({key: body[key] for key in allowed if key in body})


@router.post("/skills/mcp/test")
async def mcp_test_unsaved(request: Request):
    """Inspect an unsaved MCP server payload in an isolated connection stack."""
    from pydantic import ValidationError

    from nanobot.agent.tools.mcp import inspect_mcp_server

    body = await request.json()
    server_payload = body.get("server", body)
    if not isinstance(server_payload, dict):
        return JSONResponse(
            {"success": False, "error": "Invalid MCP server configuration"},
            status_code=400,
        )
    try:
        server = _mcp_test_config(server_payload)
        timeout_seconds = float(body.get("test_timeout", 15))
    except (ValidationError, TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "Invalid MCP server configuration"},
            status_code=400,
        )

    result = await inspect_mcp_server(
        str(body.get("name", "unsaved")),
        server,
        timeout_seconds=timeout_seconds,
    )
    return JSONResponse(result.as_dict())


@router.post("/skills/mcp/{name}/test")
async def mcp_test_saved(request: Request, name: str):
    """Inspect a saved MCP server without touching the live agent registry."""
    from nanobot.agent.tools.mcp import inspect_mcp_server
    from nanobot.config.loader import load_config

    cfg = load_config()
    server = cfg.tools.mcp_servers.get(name)
    if server is None:
        return JSONResponse(
            {"success": False, "error": f"Server '{name}' not found"},
            status_code=404,
        )

    body = await request.json()
    try:
        timeout_seconds = float(body.get("test_timeout", 15))
    except (TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "Invalid inspection timeout"},
            status_code=400,
        )
    result = await inspect_mcp_server(name, server, timeout_seconds=timeout_seconds)
    return JSONResponse(result.as_dict())


@router.get("/skills/mcp/status")
async def mcp_status(request: Request):
    """Return connection status for each configured MCP server.

    Checks which servers have tools registered in the agent's tool registry.
    A server is 'connected' if at least 1 tool with prefix mcp_{name}_ exists.
    Returns: { "browser": { "connected": true, "tool_count": 22 }, ... }
    """
    from nanobot.config.loader import load_config

    cfg = load_config()
    result = {}

    # Get registered tool names from live agent (if available)
    registered_tool_names: list[str] = []
    agent = getattr(request.app.state, "agent", None)
    if agent and hasattr(agent, "tools"):
        try:
            registered_tool_names = list(agent.tools.tool_names)
        except Exception:
            pass

    for name in cfg.tools.mcp_servers:
        prefix = f"mcp_{name}_"
        matching = [t for t in registered_tool_names if t.startswith(prefix)]

        if len(matching) > 0:
            state = "connected"
        elif agent is None:
            # No agent info available at all
            state = "unknown"
        else:
            # Check if agent even knows about this server
            # If not in agent._mcp_servers, it was added after gateway start → pending restart
            agent_mcp = getattr(agent, "_mcp_servers", {})
            if name not in agent_mcp:
                state = "pending"   # added after startup, not yet tried
            else:
                state = "failed"    # agent tried but failed to connect

        result[name] = {
            "state": state,
            "connected": state == "connected",
            "tool_count": len(matching),
        }

    return JSONResponse(result)
