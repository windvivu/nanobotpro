"""Read-only crypto market scanner tool."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.market_scanner.scanner import MarketScanner

_SYMBOL_RE = re.compile(r"^[A-Z0-9_]{1,64}$", re.IGNORECASE)
_TIMEFRAME_RE = re.compile(r"^(1h|4h)$")
_OPERATIONS = {"scan_market", "scan_symbols", "explain_symbol", "open_candidate_in_tradingview"}


@tool_parameters({
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": sorted(_OPERATIONS),
            "description": "Scanner operation to perform.",
        },
        "market": {
            "type": ["string", "null"],
            "enum": ["spot", "futures", None],
            "description": "Market type. First implementation supports futures.",
        },
        "timeframes": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "maxItems": 4,
            "description": "Timeframes to scan, default 1h and 4h.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Maximum candidates per bucket.",
        },
        "symbols": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "maxItems": 100,
            "description": "Optional symbols for scan_symbols.",
        },
        "symbol": {
            "type": ["string", "null"],
            "description": "One symbol for explain_symbol or TradingView bridge.",
        },
        "direction": {
            "type": "string",
            "enum": ["long", "short", "both", "watch", "all"],
            "description": "Candidate buckets to return.",
        },
        "min_quote_volume": {
            "type": ["number", "null"],
            "minimum": 0,
            "description": "Minimum 24h quote volume.",
        },
        "confirmed": {
            "type": "boolean",
            "description": "Required only for opening a selected candidate in TradingView.",
        },
    },
    "required": ["operation"],
})
class MarketScannerTool(Tool):
    def __init__(
        self,
        config: Any,
        *,
        scanner: MarketScanner | None = None,
        tradingview_tool: Any | None = None,
        registry: Any | None = None,
    ) -> None:
        self.config = config
        self.scanner = scanner or MarketScanner(config)
        self._tradingview_tool = tradingview_tool
        self._registry = registry

    @property
    def name(self) -> str:
        return "market_scanner"

    @property
    def description(self) -> str:
        return (
            "Crypto market scanner using exchange market-data APIs, not TradingView UI. "
            "Use it to rank long/short/watch candidates and explain objective scanner metrics. "
            "Opening a selected candidate in TradingView requires confirmation."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        operation: str,
        market: str | None = None,
        timeframes: list[str] | None = None,
        limit: int = 10,
        symbols: list[str] | None = None,
        symbol: str | None = None,
        direction: str = "both",
        min_quote_volume: float | None = None,
        confirmed: bool = False,
    ) -> str:
        if not getattr(self.config, "enabled", False):
            return "Error: market scanner is disabled. Set marketScanner.enabled=true."
        if operation not in _OPERATIONS:
            return f"Error: unsupported market scanner operation '{operation}'."
        try:
            _validate_scan_request(
                market=market,
                limit=limit,
                direction=direction,
                min_quote_volume=min_quote_volume,
            )
            timeframes = _validate_timeframes(timeframes)
            if operation == "scan_market":
                payload = await self.scanner.scan_market(
                    market=market,
                    timeframes=timeframes,
                    limit=limit,
                    direction=direction,
                    min_quote_volume=min_quote_volume,
                )
            elif operation == "scan_symbols":
                checked_symbols = _validate_symbols(symbols)
                payload = await self.scanner.scan_symbols(
                    checked_symbols,
                    market=market,
                    timeframes=timeframes,
                    limit=limit,
                    direction=direction,
                    min_quote_volume=min_quote_volume,
                )
            elif operation == "explain_symbol":
                checked_symbol = _validate_symbol(symbol)
                payload = await self.scanner.explain_symbol(
                    checked_symbol,
                    market=market,
                    timeframes=timeframes,
                )
            else:
                return await self._open_candidate_in_tradingview(symbol, confirmed=confirmed)
        except ValueError as exc:
            return f"Error: {exc}"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _open_candidate_in_tradingview(self, symbol: str | None, *, confirmed: bool) -> str:
        checked_symbol = _validate_symbol(symbol)
        tradingview_symbol = _tradingview_symbol(checked_symbol)
        if not confirmed:
            return (
                "Confirmation required: open selected market scanner candidate "
                f"{tradingview_symbol} in TradingView. Call again with confirmed=true."
            )
        if self._tradingview_tool is not None:
            return await self._tradingview_tool.execute(
                operation="set_symbol",
                symbol=tradingview_symbol,
                confirmed=True,
            )

        mcp_tool_name = "mcp_tradingview_chart_set_symbol"
        if self._registry is None or self._registry.get(mcp_tool_name) is None:
            return (
                "Error: managed TradingView MCP is not connected. "
                "Install and enable the TradingView MCP server before opening a candidate."
            )
        return await self._registry.execute(mcp_tool_name, {"symbol": tradingview_symbol})


def _validate_symbol(symbol: str | None) -> str:
    if not isinstance(symbol, str):
        raise ValueError("symbol is required and may only contain Binance symbol characters.")
    value = symbol.upper().removeprefix("BINANCE:")
    value = value.removesuffix(".P")
    if not _SYMBOL_RE.fullmatch(value):
        raise ValueError("symbol is required and may only contain letters, numbers, or _.")
    return value


def _tradingview_symbol(symbol: str) -> str:
    return f"BINANCE:{symbol}.P"


def _validate_symbols(symbols: list[str] | None) -> list[str]:
    if not symbols:
        raise ValueError("symbols is required for scan_symbols.")
    return [_validate_symbol(symbol) for symbol in symbols]


def _validate_timeframes(timeframes: list[str] | None) -> list[str] | None:
    if timeframes is None:
        return None
    cleaned: list[str] = []
    for timeframe in timeframes:
        if not isinstance(timeframe, str) or not _TIMEFRAME_RE.fullmatch(timeframe):
            raise ValueError(f"unsupported timeframe: {timeframe!r}.")
        cleaned.append(timeframe)
    return cleaned


def _validate_scan_request(
    *,
    market: str | None,
    limit: int,
    direction: str,
    min_quote_volume: float | None,
) -> None:
    if market is not None and market != "futures":
        raise ValueError("market scanner currently supports market='futures' only")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")
    if not isinstance(direction, str) or direction not in {"long", "short", "both", "watch", "all"}:
        raise ValueError("direction must be long, short, both, watch, or all")
    if min_quote_volume is not None and (
        not isinstance(min_quote_volume, (int, float))
        or isinstance(min_quote_volume, bool)
        or not math.isfinite(float(min_quote_volume))
        or min_quote_volume < 0
    ):
        raise ValueError("min_quote_volume must be a finite non-negative number")
