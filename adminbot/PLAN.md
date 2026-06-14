# Adminbot Plan

## Goal

Build `adminbot` as a portable multi-bot manager that can be copied into another Nanobot repo and used with minimal setup.

## Scope

`adminbot` is responsible for:

- managing a registry of local bot instances
- creating per-bot runtime/config directories
- starting and stopping multiple Nanobot processes
- providing one shared control UI for operators
- exposing logs and, later, optional terminal tabs

`adminbot` is not responsible for:

- changing core Nanobot agent logic
- replacing each bot's own web dashboard
- bundling external deployment infrastructure

## Portability Rules

- Keep all `adminbot` code inside `adminbot/`.
- Keep runtime state inside `.adminbot/`.
- Avoid hardcoded machine-specific paths.
- Detect repo-local Python/venv at runtime when possible.
- Interact with Nanobot through stable CLI entrypoints first.

## Nanobot CLI Assumptions

`adminbot` should prefer these Nanobot CLI entrypoints unless a later integration path proves more stable:

- `nanobot.cli.commands onboard`
- `nanobot.cli.commands gateway`

Before implementation advances, these entrypoints must be verified as stable enough for:

- per-bot config initialization
- per-bot workspace override
- per-bot web dashboard startup

## Architecture

### Layer 1: Registry

- Persistent bot list in `.adminbot/bots.json`
- Bot metadata:
  - id
  - name
  - workspace
  - config path
  - web port
  - process metadata
  - timestamps

### Layer 2: Process Manager

- Start Nanobot bots as background processes
- Track pid/process handle
- Stop gracefully where possible
- Detect dead processes and refresh status
- Capture stdout/stderr to per-bot log files
- Verify pid ownership before stop/status actions to avoid PID reuse mistakes on Windows

### Layer 3: Manager UI

- Shared web UI served by `adminbot`
- Screens:
  - bot list
  - create bot
  - bot detail
  - log viewer
  - links/open actions to each bot dashboard

### Layer 4: Optional Terminal Integration

- Phase 1: no interactive terminal, logs only
- Phase 2: optional web terminal tab via PTY/ConPTY on Windows
- Phase 3: if PTY is too costly, allow "open shell tab/window" fallback

## Delivery Phases

### Phase 1

- scaffold `adminbot/`
- move launcher logic into dedicated module area
- implement registry persistence basics
- implement process manager basics
- support background bot start/stop/status without one visible terminal per bot
- define runtime data layout in `.adminbot/`
- create pid/state/log handling foundations

### Phase 2

- add simple local web UI
- connect UI actions to process control
- refine runtime/log presentation

### Phase 3

- richer manager dashboard
- realtime log streaming
- health checks
- dashboard links/embedding decisions

### Phase 4

- optional interactive terminal tab
- portability verification in another Nanobot repo
- packaging/refinement

## Open Decisions

- Whether `adminbot` should run as `python -m adminbot.app.main` or via a thin script wrapper
- Whether to embed per-bot dashboards in iframes or open them separately
- Whether process tracking should rely only on pid files or a richer state file
- Whether Windows Terminal tab integration remains as a fallback convenience feature
- Whether process lifecycle on Windows should remain pid/state based or move to a stronger model such as Job Objects
- How `adminbot` should reconcile orphaned bot subprocesses after manager crash/restart
- When log rotation should be introduced and whether it belongs in `adminbot` or external logging policy
- What bind address and auth model the Manager UI should use by default to stay local-only and safe

## Resolved Decisions

- Manager UI will use `FastAPI + Jinja2 + Uvicorn` to match the existing Nanobot web stack.

## Phase 4 Start Notes

- Logs-first operation remains the default path.
- Shell-tab fallback is preferred before any embedded PTY implementation.
- PTY/ConPTY work should be treated as an explicit evaluation track, not assumed delivery.

## Success Criteria

- One manager can create and run multiple bots without spawning many visible terminals
- A copied `adminbot/` folder can be reused in another compatible repo
- The manager can recover bot definitions from `.adminbot/bots.json`
- Operators can inspect logs and reach each bot dashboard from one place
