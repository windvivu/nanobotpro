"""Task-oriented TradingView Web wrapper backed by the local CDP CLI."""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.config.paths import get_data_dir, get_media_dir

_SYMBOL_RE = re.compile(r"^[A-Z0-9_./:-]{1,64}$", re.IGNORECASE)
_TIMEFRAME_RE = re.compile(r"^(\d{1,4}|[DWM]|[1-9]\d*[DWM])$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

_CONFIRM_REQUIRED = {"set_symbol", "set_timeframe", "clear_all"}
_INDICATOR_MUTATION_OPS = {
    "indicator_add",
    "indicator_remove",
    "indicator_toggle",
    "indicator_set_inputs",
}
_PINE_SOURCE_OPS = {"pine_set", "pine_analyze", "pine_check"}
_PINE_MUTATION_OPS = {"pine_set", "pine_compile"}
_DRAWING_OPS = {
    "draw_horizontal_line",
    "draw_trend_line",
    "draw_rectangle",
    "draw_text",
}
_ALLOWED_OPS = {
    "set_symbol",
    "set_timeframe",
    "scroll",
    "range",
    "draw_horizontal_line",
    "draw_trend_line",
    "draw_rectangle",
    "draw_text",
    "clear_all",
    "screenshot",
    "indicator_add",
    "indicator_remove",
    "indicator_toggle",
    "indicator_set_inputs",
    "indicator_get",
    "pine_list",
    "pine_read",
    "pine_errors",
    "pine_console",
    "pine_debug_status",
    "pine_analyze",
    "pine_check",
    "pine_set",
    "pine_compile",
}
_READINESS_TIMEOUT_SECONDS = 10.0
_READINESS_POLL_SECONDS = 0.5
_PINE_SOURCE_MAX_CHARS = 200_000
_INDICATOR_ALIASES = {
    "dmi": "Directional Movement",
    "directional movement index": "Directional Movement",
}


def get_managed_tradingview_backend_path() -> Path:
    """Return the externally managed TradingView backend path."""
    return get_data_dir() / "managed_mcp" / "tradingview"


@tool_parameters({
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": sorted(_ALLOWED_OPS),
            "description": "TradingView action to perform.",
        },
        "symbol": {"type": ["string", "null"], "description": "Symbol for set_symbol, e.g. BINANCE:XRPUSDT."},
        "timeframe": {"type": ["string", "null"], "description": "Timeframe for set_timeframe, e.g. 1, 5, 60, D, W."},
        "date": {"type": ["string", "null"], "description": "Date for scroll in YYYY-MM-DD format."},
        "from_ts": {"type": ["integer", "null"], "description": "Visible range start as Unix seconds."},
        "to_ts": {"type": ["integer", "null"], "description": "Visible range end as Unix seconds."},
        "price": {"type": ["number", "null"], "description": "Primary price for drawing operations."},
        "time": {"type": ["integer", "null"], "description": "Primary Unix timestamp for drawing operations."},
        "price2": {"type": ["number", "null"], "description": "Second price for trend line or rectangle."},
        "time2": {"type": ["integer", "null"], "description": "Second Unix timestamp for trend line or rectangle."},
        "text": {"type": ["string", "null"], "maxLength": 200, "description": "Text for draw_text."},
        "region": {
            "type": "string",
            "enum": ["chart", "full", "strategy_tester"],
            "description": "Screenshot region.",
        },
        "filename": {"type": ["string", "null"], "maxLength": 80, "description": "Screenshot filename without extension."},
        "indicator_name": {
            "type": ["string", "null"],
            "maxLength": 120,
            "description": "Full indicator name for indicator_add, e.g. Relative Strength Index.",
        },
        "entity_id": {
            "type": ["string", "null"],
            "maxLength": 120,
            "description": "TradingView study/entity ID for indicator operations. Use chart_get_state first.",
        },
        "visible": {
            "type": ["boolean", "null"],
            "description": "Visibility target for indicator_toggle.",
        },
        "indicator_inputs": {
            "type": ["object", "null"],
            "description": "Indicator input overrides for indicator_add or indicator_set_inputs.",
        },
        "pine_source": {
            "type": ["string", "null"],
            "maxLength": _PINE_SOURCE_MAX_CHARS,
            "description": "Pine Script source for pine_set, pine_analyze, or pine_check.",
        },
        "confirmed": {
            "type": "boolean",
            "description": "Required for destructive or chart-changing operations after preview/approval.",
        },
    },
    "required": ["operation"],
})
class TradingViewTool(Tool):
    """Small, explicit wrapper around the external TradingView CLI."""

    def __init__(self, config: Any) -> None:
        self.config = config
        configured_path = getattr(config, "repo_path", "") or ""
        self.repo_path = (
            Path(configured_path).expanduser()
            if configured_path
            else get_managed_tradingview_backend_path()
        )
        self.timeout = min(max(int(getattr(config, "timeout", 30) or 30), 1), 120)

    @property
    def name(self) -> str:
        return "tradingview"

    @property
    def description(self) -> str:
        return (
            "Operate the active TradingView Web chart through Chrome CDP localhost:9222. "
            "Reads should prefer the TradingView MCP tools; use this for guarded navigation, drawing, clearing, "
            "indicator management, and screenshots copied into Nanobot media."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        operation: str,
        symbol: str | None = None,
        timeframe: str | None = None,
        date: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        price: float | None = None,
        time: int | None = None,
        price2: float | None = None,
        time2: int | None = None,
        text: str | None = None,
        region: str = "chart",
        filename: str | None = None,
        indicator_name: str | None = None,
        entity_id: str | None = None,
        visible: bool | None = None,
        indicator_inputs: dict[str, Any] | None = None,
        pine_source: str | None = None,
        confirmed: bool = False,
    ) -> str:
        operation = operation.strip()
        if operation not in _ALLOWED_OPS:
            return f"Error: unsupported TradingView operation '{operation}'."
        if not getattr(self.config, "enabled", False):
            return "Error: TradingView guarded wrapper is disabled. Enable Trader Mode first."

        setup_error = self._validate_setup()
        if setup_error:
            return setup_error

        if operation in _CONFIRM_REQUIRED and not confirmed:
            return self._confirmation_required(operation, symbol=symbol, timeframe=timeframe)
        if operation in _DRAWING_OPS and not confirmed:
            return self._drawing_preview(
                operation, price=price, time=time, price2=price2, time2=time2, text=text
            )
        if operation in _INDICATOR_MUTATION_OPS and not confirmed:
            return self._indicator_preview(
                operation,
                indicator_name=indicator_name,
                entity_id=entity_id,
                visible=visible,
                indicator_inputs=indicator_inputs,
            )
        if operation in _PINE_MUTATION_OPS and not confirmed:
            return self._pine_preview(operation, pine_source=pine_source)

        try:
            args = self._build_args(
                operation,
                symbol=symbol,
                timeframe=timeframe,
                date=date,
                from_ts=from_ts,
                to_ts=to_ts,
                price=price,
                time=time,
                price2=price2,
                time2=time2,
                text=text,
                region=region,
                filename=filename,
                indicator_name=indicator_name,
                entity_id=entity_id,
                visible=visible,
                indicator_inputs=indicator_inputs,
                pine_source=pine_source,
            )
        except ValueError as exc:
            return f"Error: {exc}"

        stdin_text = pine_source if operation in _PINE_SOURCE_OPS else None
        result = await self._run_cli(args, input_text=stdin_text, timeout=self._timeout_for_operation(operation))
        if result.startswith("Error:"):
            return result

        if operation in {"set_symbol", "set_timeframe"}:
            return await self._with_confirmed_chart_state(
                result,
                expected_symbol=_require_symbol(symbol) if operation == "set_symbol" else None,
                expected_timeframe=_require_timeframe(timeframe) if operation == "set_timeframe" else None,
            )

        if operation == "screenshot":
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                return result
            if not isinstance(payload, dict):
                return "Error: TradingView screenshot returned non-object JSON."
            relocated = self._relocate_screenshot(payload)
            return json.dumps(relocated, ensure_ascii=False, indent=2)

        if operation in _INDICATOR_MUTATION_OPS:
            return await self._with_updated_chart_state(result)

        if operation == "pine_compile":
            return await self._with_updated_chart_state(result)

        return result

    def _validate_setup(self) -> str | None:
        if shutil.which("node") is None:
            return "Error: Node.js was not found on PATH. Install Node.js or add node to PATH."
        if int(getattr(self.config, "cdp_port", 9222) or 9222) != 9222:
            return (
                "Error: the current TradingView CLI backend only supports Chrome CDP on "
                "localhost:9222. Set tradingview.cdp_port back to 9222."
            )
        repo = self.repo_path.resolve()
        if not repo.exists():
            return (
                f"Error: TradingView backend path does not exist: {repo}. "
                "Install the managed TradingView MCP backend from the dashboard."
            )
        cli = repo / "src" / "cli" / "index.js"
        if not cli.exists():
            return f"Error: TradingView CLI not found: {cli}"
        return None

    def _build_args(self, operation: str, **kwargs: Any) -> list[str]:
        assert self.repo_path is not None
        cli = self._cli_path()

        if operation == "set_symbol":
            symbol = _require_symbol(kwargs.get("symbol"))
            return [cli, "symbol", symbol]
        if operation == "set_timeframe":
            timeframe = _require_timeframe(kwargs.get("timeframe"))
            return [cli, "timeframe", timeframe]
        if operation == "scroll":
            date = _require_date(kwargs.get("date"))
            return [cli, "scroll", date]
        if operation == "range":
            from_ts = _require_timestamp(kwargs.get("from_ts"), "from_ts")
            to_ts = _require_timestamp(kwargs.get("to_ts"), "to_ts")
            if from_ts >= to_ts:
                raise ValueError("from_ts must be smaller than to_ts.")
            return [cli, "range", "--from", str(from_ts), "--to", str(to_ts)]
        if operation == "clear_all":
            return [cli, "draw", "clear"]
        if operation == "screenshot":
            region = kwargs.get("region") or "chart"
            if region not in {"chart", "full", "strategy_tester"}:
                raise ValueError("region must be chart, full, or strategy_tester.")
            filename = _safe_filename(kwargs.get("filename") or "nanobot-tradingview")
            return [cli, "screenshot", "--region", region, "--output", filename]
        if operation.startswith("indicator_"):
            return self._indicator_args(cli, operation, **kwargs)
        if operation.startswith("pine_"):
            return self._pine_args(cli, operation, **kwargs)
        return self._drawing_args(cli, operation, **kwargs)

    def _cli_path(self) -> str:
        return str(self.repo_path.resolve() / "src" / "cli" / "index.js")

    def _drawing_args(self, cli: str, operation: str, **kwargs: Any) -> list[str]:
        shape = {
            "draw_horizontal_line": "horizontal_line",
            "draw_trend_line": "trend_line",
            "draw_rectangle": "rectangle",
            "draw_text": "text",
        }[operation]
        price = _require_price(kwargs.get("price"), "price")
        ts = _require_timestamp(kwargs.get("time"), "time")
        args = [cli, "draw", "shape", "--type", shape, "--price", str(price), "--time", str(ts)]
        if operation in {"draw_trend_line", "draw_rectangle"}:
            args.extend([
                "--price2",
                str(_require_price(kwargs.get("price2"), "price2")),
                "--time2",
                str(_require_timestamp(kwargs.get("time2"), "time2")),
            ])
        if operation == "draw_text":
            text = kwargs.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text is required for draw_text.")
            args.extend(["--text", text[:200]])
        return args

    def _indicator_args(self, cli: str, operation: str, **kwargs: Any) -> list[str]:
        if operation == "indicator_add":
            indicator_name = _require_indicator_name(kwargs.get("indicator_name"))
            args = [cli, "indicator", "add", indicator_name]
            if kwargs.get("indicator_inputs") is not None:
                args.extend(["--inputs", _indicator_inputs_json(kwargs.get("indicator_inputs"))])
            return args
        if operation == "indicator_remove":
            return [cli, "indicator", "remove", _require_entity_id(kwargs.get("entity_id"))]
        if operation == "indicator_toggle":
            visible = kwargs.get("visible")
            if not isinstance(visible, bool):
                raise ValueError("visible must be true or false for indicator_toggle.")
            args = [cli, "indicator", "toggle", _require_entity_id(kwargs.get("entity_id"))]
            args.append("--visible" if visible else "--hidden")
            return args
        if operation == "indicator_set_inputs":
            return [
                cli,
                "indicator",
                "set",
                _require_entity_id(kwargs.get("entity_id")),
                "--inputs",
                _indicator_inputs_json(kwargs.get("indicator_inputs")),
            ]
        if operation == "indicator_get":
            return [cli, "indicator", "get", _require_entity_id(kwargs.get("entity_id"))]
        raise ValueError(f"unsupported indicator operation '{operation}'.")

    def _pine_args(self, cli: str, operation: str, **kwargs: Any) -> list[str]:
        if operation == "pine_list":
            return [cli, "pine", "list"]
        if operation == "pine_read":
            return [cli, "pine", "get"]
        if operation == "pine_errors":
            return [cli, "pine", "errors"]
        if operation == "pine_console":
            return [cli, "pine", "console"]
        if operation == "pine_debug_status":
            return [cli, "pine", "debug-status"]
        if operation == "pine_analyze":
            _require_pine_source(kwargs.get("pine_source"))
            return [cli, "pine", "analyze"]
        if operation == "pine_check":
            _require_pine_source(kwargs.get("pine_source"))
            return [cli, "pine", "check"]
        if operation == "pine_set":
            _require_pine_source(kwargs.get("pine_source"))
            return [cli, "pine", "set"]
        if operation == "pine_compile":
            return [cli, "pine", "compile"]
        raise ValueError(f"unsupported Pine operation '{operation}'.")

    def _timeout_for_operation(self, operation: str) -> int:
        if operation in {"pine_check", "pine_compile"}:
            return min(max(self.timeout, 60), 120)
        return self.timeout

    async def _run_cli(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        timeout: int | None = None,
    ) -> str:
        node = shutil.which("node")
        assert node is not None
        effective_timeout = timeout or self.timeout

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [node, *args],
                cwd=str(self.repo_path.resolve()) if self.repo_path else None,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                shell=False,
            )

        try:
            completed = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return f"Error: TradingView CLI timed out after {effective_timeout}s."

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            return f"Error: TradingView CLI failed: {detail}"
        if not stdout:
            return "Error: TradingView CLI returned empty output."
        try:
            return json.dumps(json.loads(stdout), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return f"Error: TradingView CLI returned non-JSON output: {stdout[:500]}"

    async def _with_confirmed_chart_state(
        self,
        result: str,
        *,
        expected_symbol: str | None = None,
        expected_timeframe: str | None = None,
    ) -> str:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return result
        if not isinstance(payload, dict):
            return "Error: TradingView chart action returned non-object JSON."

        confirmed = await self._wait_for_chart_state(
            expected_symbol=expected_symbol,
            expected_timeframe=expected_timeframe,
        )
        payload["chart_ready"] = confirmed.get("success", False)
        if confirmed.get("success"):
            payload["confirmed_state"] = confirmed["state"]
        else:
            payload["readiness_error"] = confirmed.get("error", "Chart readiness timed out.")
            if "last_state" in confirmed:
                payload["last_state"] = confirmed["last_state"]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _with_updated_chart_state(self, result: str) -> str:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return result
        if not isinstance(payload, dict):
            return "Error: TradingView mutation returned non-object JSON."

        state_result = await self._run_cli([self._cli_path(), "state"])
        if state_result.startswith("Error:"):
            payload["updated_state_error"] = state_result
            return json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            payload["updated_state"] = json.loads(state_result)
        except json.JSONDecodeError:
            payload["updated_state_error"] = "state returned non-JSON output"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _wait_for_chart_state(
        self,
        *,
        expected_symbol: str | None = None,
        expected_timeframe: str | None = None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + min(
            _READINESS_TIMEOUT_SECONDS,
            max(1.0, self.timeout - 1.0),
        )
        state_args = [self._cli_path(), "state"]
        last_state: dict[str, Any] | None = None
        last_error: str | None = None

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {
                    "success": False,
                    "error": last_error or "Timed out waiting for TradingView chart state.",
                    **({"last_state": last_state} if last_state is not None else {}),
                }
            result = await self._run_cli(
                state_args,
                timeout=max(1, min(self.timeout, math.ceil(remaining))),
            )
            if result.startswith("Error:"):
                last_error = result
            else:
                try:
                    state = json.loads(result)
                    if not isinstance(state, dict):
                        last_error = "state returned non-object JSON"
                    else:
                        last_state = state
                        if self._state_matches(
                            state,
                            expected_symbol=expected_symbol,
                            expected_timeframe=expected_timeframe,
                        ):
                            return {"success": True, "state": state}
                except json.JSONDecodeError:
                    last_error = "state returned non-JSON output"

            if asyncio.get_running_loop().time() >= deadline:
                payload: dict[str, Any] = {
                    "success": False,
                    "error": last_error or "Timed out waiting for TradingView chart state.",
                }
                if last_state is not None:
                    payload["last_state"] = last_state
                return payload
            await asyncio.sleep(_READINESS_POLL_SECONDS)

    @staticmethod
    def _state_matches(
        state: dict[str, Any],
        *,
        expected_symbol: str | None = None,
        expected_timeframe: str | None = None,
    ) -> bool:
        if not isinstance(state, dict):
            return False
        if not state.get("success", False):
            return False
        if expected_symbol and str(state.get("symbol", "")).upper() != expected_symbol:
            return False
        if expected_timeframe and not _timeframe_matches(state.get("resolution"), expected_timeframe):
            return False
        return True

    def _relocate_screenshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_text = payload.get("file_path") or payload.get("path")
        if not isinstance(source_text, str) or not source_text:
            payload["nanobot_media_path"] = None
            payload["media_copy_error"] = "CLI result did not include file_path."
            return payload

        source = Path(source_text)
        try:
            source_resolved = source.resolve(strict=False)
            backend_root = self.repo_path.resolve(strict=False)
        except OSError as exc:
            payload["nanobot_media_path"] = None
            payload["media_copy_error"] = f"Invalid screenshot path: {exc}"
            return payload
        if not source_resolved.is_relative_to(backend_root):
            payload["nanobot_media_path"] = None
            payload["media_copy_error"] = "Screenshot path is outside the managed TradingView backend."
            return payload
        if source_resolved.suffix.lower() != ".png" or not source_resolved.is_file():
            payload["nanobot_media_path"] = None
            payload["media_copy_error"] = f"Screenshot file is missing or not a PNG: {source}"
            return payload

        media_dir = get_media_dir("tradingview")
        destination = media_dir / source.name
        try:
            shutil.copy2(source_resolved, destination)
        except OSError as exc:
            payload["nanobot_media_path"] = None
            payload["media_copy_error"] = f"Could not copy screenshot: {exc}"
            return payload
        payload["original_file_path"] = str(source_resolved)
        payload["nanobot_media_path"] = str(destination)
        payload["file_path"] = str(destination)
        return payload

    @staticmethod
    def _confirmation_required(operation: str, **kwargs: Any) -> str:
        if operation == "set_symbol":
            target = kwargs.get("symbol") or "(missing symbol)"
            return f"Confirmation required: set TradingView symbol to {target}. Call again with confirmed=true."
        if operation == "set_timeframe":
            target = kwargs.get("timeframe") or "(missing timeframe)"
            return f"Confirmation required: set TradingView timeframe to {target}. Call again with confirmed=true."
        return "Confirmation required: clear all TradingView drawings. Call again with confirmed=true."

    @staticmethod
    def _drawing_preview(operation: str, **kwargs: Any) -> str:
        return (
            "Preview only: "
            f"{operation} with price={kwargs.get('price')}, time={kwargs.get('time')}, "
            f"price2={kwargs.get('price2')}, time2={kwargs.get('time2')}, text={kwargs.get('text')!r}. "
            "Call again with confirmed=true to draw."
        )

    @staticmethod
    def _indicator_preview(operation: str, **kwargs: Any) -> str:
        return (
            "Confirmation required: "
            f"{operation} with indicator_name={kwargs.get('indicator_name')!r}, "
            f"entity_id={kwargs.get('entity_id')!r}, visible={kwargs.get('visible')!r}, "
            f"indicator_inputs={kwargs.get('indicator_inputs')!r}. "
            "Call again with confirmed=true to apply the indicator change."
        )

    @staticmethod
    def _pine_preview(operation: str, **kwargs: Any) -> str:
        source = kwargs.get("pine_source")
        if isinstance(source, str):
            source_info = f"{len(source)} chars, {source.count(chr(10)) + 1} lines"
        else:
            source_info = "no source provided"
        if operation == "pine_set":
            return (
                "Confirmation required: replace the current Pine Editor source "
                f"with Pine Script ({source_info}). Call again with confirmed=true to apply."
            )
        return (
            "Confirmation required: compile/add the current Pine Script on the TradingView chart. "
            "Call again with confirmed=true to compile."
        )


def _require_symbol(value: Any) -> str:
    if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value):
        raise ValueError("symbol is required and may only contain letters, numbers, _, ., /, :, or -.")
    return value.upper()


def _require_timeframe(value: Any) -> str:
    if not isinstance(value, str) or not _TIMEFRAME_RE.fullmatch(value):
        raise ValueError("timeframe is required, e.g. 1, 5, 15, 60, D, W, or M.")
    return value.upper()


def _timeframe_matches(actual: Any, expected: str) -> bool:
    actual_text = str(actual).upper()
    expected_text = expected.upper()
    if actual_text == expected_text:
        return True
    if expected_text in {"D", "W", "M"} and actual_text == f"1{expected_text}":
        return True
    if actual_text in {"D", "W", "M"} and expected_text == f"1{actual_text}":
        return True
    return False


def _require_date(value: Any) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("date is required in YYYY-MM-DD format.")
    return value


def _require_price(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    return float(value)


def _require_timestamp(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive Unix timestamp in seconds.")
    return value


def _safe_filename(value: str) -> str:
    safe = _SAFE_FILENAME_RE.sub("-", value.strip()).strip(".-")
    if not safe:
        raise ValueError("filename must contain at least one safe character.")
    return safe[:80].removesuffix(".png")


def _require_indicator_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("indicator_name is required for indicator_add.")
    name = value.strip()
    if not name:
        raise ValueError("indicator_name is required for indicator_add.")
    if len(name) > 120:
        raise ValueError("indicator_name must be 120 characters or fewer.")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("indicator_name may not contain control characters.")
    return _INDICATOR_ALIASES.get(name.lower(), name)


def _require_entity_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("entity_id is required for this indicator operation.")
    entity_id = value.strip()
    if not entity_id:
        raise ValueError("entity_id is required for this indicator operation.")
    if len(entity_id) > 120:
        raise ValueError("entity_id must be 120 characters or fewer.")
    if any(ord(ch) < 33 for ch in entity_id):
        raise ValueError("entity_id may not contain whitespace or control characters.")
    return entity_id


def _indicator_inputs_json(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        raise ValueError("indicator_inputs must be a non-empty object.")
    if len(value) > 50:
        raise ValueError("indicator_inputs may not contain more than 50 keys.")
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"indicator_inputs must be JSON-serializable: {exc}") from exc


def _require_pine_source(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("pine_source is required for this Pine operation.")
    if not value.strip():
        raise ValueError("pine_source must not be empty.")
    if len(value) > _PINE_SOURCE_MAX_CHARS:
        raise ValueError(f"pine_source must be {_PINE_SOURCE_MAX_CHARS} characters or fewer.")
    return value
