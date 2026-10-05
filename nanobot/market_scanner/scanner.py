"""Async market scanner orchestration."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from nanobot.market_scanner.models import Candidate, MarketSymbol, Ticker24h
from nanobot.market_scanner.providers import MarketDataProvider, create_provider
from nanobot.market_scanner.scoring import calculate_features, candidate_to_dict, rank_candidates

_STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD"}
_FIAT_BASES = {"EUR", "TRY", "BRL", "AUD", "GBP"}


class MarketScanner:
    def __init__(self, config: Any, *, provider: MarketDataProvider | None = None) -> None:
        self.config = config
        self._provider = provider
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._scan_lock = asyncio.Lock()

    async def scan_market(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run one scan at a time to prevent duplicate in-flight market requests."""
        async with self._scan_lock:
            return await self._scan_market_locked(*args, **kwargs)

    async def _scan_market_locked(
        self,
        *,
        market: str | None = None,
        timeframes: list[str] | None = None,
        limit: int = 10,
        direction: str = "both",
        min_quote_volume: float | None = None,
        symbols: list[str] | None = None,
        include_metrics: bool = False,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        exchange = str(getattr(self.config, "exchange", "binance") or "binance").lower()
        market = market or getattr(self.config, "market", "futures")
        if market != "futures":
            raise ValueError("market scanner currently supports market='futures' only")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer between 1 and 50")
        if not isinstance(direction, str) or direction.lower() not in {"long", "short", "both", "watch", "all"}:
            raise ValueError("direction must be long, short, both, watch, or all")
        quote_asset = getattr(self.config, "quote_asset", "USDT")
        timeframes = _normalize_timeframes(timeframes or getattr(self.config, "timeframes", ["1h", "4h"]))
        max_symbols = int(getattr(self.config, "max_symbols", 50) or 50)
        candle_limit = int(getattr(self.config, "candle_limit", 240) or 240)
        concurrency = int(getattr(self.config, "concurrency", 8) or 8)
        min_quote_volume = float(
            min_quote_volume
            if min_quote_volume is not None
            else getattr(self.config, "min_quote_volume", 10_000_000.0)
        )
        if not math.isfinite(min_quote_volume) or min_quote_volume < 0:
            raise ValueError("min_quote_volume must be a finite non-negative number")
        provider = self._provider or create_provider(
            exchange,
            timeout=float(getattr(self.config, "request_timeout", 15) or 15),
        )

        cache_key = self._cache_key(
            exchange,
            market,
            quote_asset,
            timeframes,
            symbols,
            max_symbols=max_symbols,
            min_quote_volume=min_quote_volume,
        )
        cached = self._get_cache(cache_key)
        if cached is not None:
            cached_output = dict(cached)
            cached_output["cache_hit"] = True
            cached_output["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
            return _filter_output(cached_output, direction=direction, limit=limit, include_metrics=include_metrics)

        warnings: list[str] = []
        try:
            universe = await provider.list_symbols(market, quote_asset)
            universe = _filter_universe(universe)
            requested = {symbol.upper() for symbol in symbols} if symbols else None
            if requested is not None:
                universe = [item for item in universe if item.symbol in requested]

            tickers = await provider.get_24h_tickers([item.symbol for item in universe])
            tickers_by_symbol = {ticker.symbol: ticker for ticker in tickers}
            selected_symbols = _select_symbols(universe, tickers_by_symbol, max_symbols, min_quote_volume)
            estimated_requests = 2 + (len(selected_symbols) * len(timeframes))

            features_by_symbol: dict[str, dict[str, Any]] = {}
            semaphore = asyncio.Semaphore(max(concurrency, 1))

            async def fetch_features(symbol: str, timeframe: str) -> tuple[str, str, Any]:
                async with semaphore:
                    candles = await provider.get_ohlcv(symbol, timeframe, candle_limit)
                return symbol, timeframe, calculate_features(candles, timeframe)

            tasks = [
                asyncio.create_task(fetch_features(symbol, timeframe))
                for symbol in selected_symbols
                for timeframe in timeframes
            ]

            try:
                for task in asyncio.as_completed(tasks):
                    try:
                        symbol, timeframe, features = await task
                        features_by_symbol.setdefault(symbol, {})[timeframe] = features
                    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                        warnings.append(f"Market data fetch failed: {exc}")
                    except ValueError as exc:
                        warnings.append(f"Feature calculation skipped: {exc}")
            except asyncio.CancelledError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            complete_features = {
                symbol: frames
                for symbol, frames in features_by_symbol.items()
                if all(timeframe in frames for timeframe in timeframes)
            }
            candidates = rank_candidates(
                complete_features,
                tickers_by_symbol,
                min_quote_volume=min_quote_volume,
            )
            output = _build_output(
                exchange=exchange,
                market=market,
                quote_asset=quote_asset,
                timeframes=timeframes,
                scanned_symbols=len(selected_symbols),
                candidates=candidates,
                warnings=warnings,
                estimated_requests=estimated_requests,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                cache_hit=False,
            )
        except httpx.HTTPStatusError as exc:
            output = _error_output(exchange, market, quote_asset, timeframes, warnings, f"HTTP error: {exc}", duration_ms=round((time.perf_counter() - started_at) * 1000, 2))
        except httpx.RequestError as exc:
            output = _error_output(exchange, market, quote_asset, timeframes, warnings, f"Network error: {exc}", duration_ms=round((time.perf_counter() - started_at) * 1000, 2))
        except Exception as exc:
            output = _error_output(exchange, market, quote_asset, timeframes, warnings, str(exc), duration_ms=round((time.perf_counter() - started_at) * 1000, 2))

        if output.get("success"):
            self._set_cache(cache_key, output)
        return _filter_output(output, direction=direction, limit=limit, include_metrics=include_metrics)

    async def scan_symbols(
        self,
        symbols: list[str],
        *,
        market: str | None = None,
        timeframes: list[str] | None = None,
        limit: int = 10,
        direction: str = "both",
        min_quote_volume: float | None = None,
        include_metrics: bool = False,
    ) -> dict[str, Any]:
        return await self.scan_market(
            market=market,
            timeframes=timeframes,
            limit=limit,
            direction=direction,
            min_quote_volume=min_quote_volume,
            symbols=symbols,
            include_metrics=include_metrics,
        )

    async def explain_symbol(
        self,
        symbol: str,
        *,
        market: str | None = None,
        timeframes: list[str] | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        timeframes = _normalize_timeframes(timeframes or getattr(self.config, "timeframes", ["1h", "4h"]))
        cached = self._find_cached_candidate(
            symbol,
            market=market or getattr(self.config, "market", "futures"),
            quote_asset=getattr(self.config, "quote_asset", "USDT"),
            timeframes=timeframes,
        )
        if cached is not None:
            cached["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
            return cached

        result = await self.scan_symbols(
            [symbol],
            market=market,
            timeframes=timeframes,
            limit=1,
            direction="both",
            min_quote_volume=0,
            include_metrics=True,
        )
        candidates = [*result.get("top_long", []), *result.get("top_short", []), *result.get("watch", [])]
        result["symbol"] = symbol.upper()
        result["candidate"] = candidates[0] if candidates else None
        return result

    def _find_cached_candidate(
        self,
        symbol: str,
        *,
        market: str,
        quote_asset: str,
        timeframes: list[str],
    ) -> dict[str, Any] | None:
        symbol = symbol.upper()
        ttl = int(getattr(self.config, "cache_ttl_seconds", 60) or 60)
        now = time.time()
        for key, (created_at, output) in list(self._cache.items()):
            if now - created_at > ttl:
                self._cache.pop(key, None)
                continue
            if not isinstance(output, dict) or not output.get("success"):
                continue
            if output.get("market") != market or output.get("quote_asset") != quote_asset:
                continue
            if output.get("timeframes") != timeframes:
                continue
            for bucket in ("top_long", "top_short", "watch"):
                for candidate in output.get(bucket, []):
                    if candidate.get("symbol") == symbol:
                        return {
                            "success": True,
                            "exchange": output.get("exchange"),
                            "market": output.get("market"),
                            "quote_asset": output.get("quote_asset"),
                            "timeframes": output.get("timeframes"),
                            "scanned_symbols": output.get("scanned_symbols", 0),
                            "estimated_requests": 0,
                            "cache_hit": True,
                            "generated_at": output.get("generated_at"),
                            "warnings": output.get("warnings", []),
                            "compact": False,
                            "symbol": symbol,
                            "candidate": candidate,
                            "top_long": [candidate] if bucket == "top_long" else [],
                            "top_short": [candidate] if bucket == "top_short" else [],
                            "watch": [candidate] if bucket == "watch" else [],
                        }
        return None

    def _cache_key(
        self,
        exchange: str,
        market: str,
        quote_asset: str,
        timeframes: list[str],
        symbols: list[str] | None,
        max_symbols: int,
        min_quote_volume: float,
    ) -> tuple[Any, ...]:
        ttl = int(getattr(self.config, "cache_ttl_seconds", 60) or 60)
        bucket = int(time.time() // max(ttl, 1))
        symbol_key = tuple(sorted(symbol.upper() for symbol in symbols)) if symbols else ("*",)
        return (exchange, market, quote_asset, tuple(timeframes), symbol_key, max_symbols, min_quote_volume, bucket)

    def _get_cache(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        ttl = int(getattr(self.config, "cache_ttl_seconds", 60) or 60)
        cached = self._cache.get(key)
        if cached is None:
            return None
        created_at, value = cached
        if time.time() - created_at > ttl:
            self._cache.pop(key, None)
            return None
        return value

    def _set_cache(self, key: tuple[Any, ...], value: dict[str, Any]) -> None:
        self._cache[key] = (time.time(), value)


def _filter_universe(symbols: list[MarketSymbol]) -> list[MarketSymbol]:
    return [
        symbol
        for symbol in symbols
        if symbol.status.upper() == "TRADING"
        and symbol.base_asset.upper() not in _STABLE_BASES
        and symbol.base_asset.upper() not in _FIAT_BASES
    ]


def _select_symbols(
    universe: list[MarketSymbol],
    tickers_by_symbol: dict[str, Ticker24h],
    max_symbols: int,
    min_quote_volume: float,
) -> list[str]:
    ranked = sorted(
        (
            item
            for item in universe
            if (ticker := tickers_by_symbol.get(item.symbol)) is not None
            and ticker.quote_volume >= min_quote_volume
        ),
        key=lambda item: tickers_by_symbol[item.symbol].quote_volume,
        reverse=True,
    )
    return [item.symbol for item in ranked[:max_symbols]]


def _build_output(
    *,
    exchange: str,
    market: str,
    quote_asset: str,
    timeframes: list[str],
    scanned_symbols: int,
    candidates: list[Candidate],
    warnings: list[str],
    estimated_requests: int,
    duration_ms: float,
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        "success": True,
        "exchange": exchange,
        "market": market,
        "quote_asset": quote_asset,
        "timeframes": timeframes,
        "scanned_symbols": scanned_symbols,
        "estimated_requests": estimated_requests,
        "duration_ms": duration_ms,
        "cache_hit": cache_hit,
        "generated_at": datetime.now(UTC).isoformat(),
        "warnings": warnings,
        "top_long": [candidate_to_dict(c, include_metrics=True) for c in candidates if c.direction == "long"],
        "top_short": [candidate_to_dict(c, include_metrics=True) for c in candidates if c.direction == "short"],
        "watch": [candidate_to_dict(c, include_metrics=True) for c in candidates if c.direction == "watch"],
    }


def _filter_output(output: dict[str, Any], *, direction: str, limit: int, include_metrics: bool) -> dict[str, Any]:
    limit = max(min(int(limit or 10), 50), 1)
    direction = direction.lower()
    filtered = dict(output)
    filtered["compact"] = not include_metrics
    filtered["top_long"] = _slice_candidates(output.get("top_long", []), limit, include_metrics) if direction in {"long", "both", "all"} else []
    filtered["top_short"] = _slice_candidates(output.get("top_short", []), limit, include_metrics) if direction in {"short", "both", "all"} else []
    filtered["watch"] = _slice_candidates(output.get("watch", []), limit, include_metrics) if direction in {"watch", "both", "all"} else []
    return filtered


def _slice_candidates(candidates: list[dict[str, Any]], limit: int, include_metrics: bool) -> list[dict[str, Any]]:
    sliced = candidates[:limit]
    if include_metrics:
        return sliced
    return [{key: value for key, value in candidate.items() if key != "metrics"} for candidate in sliced]


def _error_output(
    exchange: str,
    market: str,
    quote_asset: str,
    timeframes: list[str],
    warnings: list[str],
    error: str,
    duration_ms: float,
) -> dict[str, Any]:
    return {
        "success": False,
        "exchange": exchange,
        "market": market,
        "quote_asset": quote_asset,
        "timeframes": timeframes,
        "scanned_symbols": 0,
        "estimated_requests": 0,
        "duration_ms": duration_ms,
        "cache_hit": False,
        "generated_at": datetime.now(UTC).isoformat(),
        "warnings": warnings,
        "error": error,
        "top_long": [],
        "top_short": [],
        "watch": [],
    }


def _normalize_timeframes(timeframes: list[str]) -> list[str]:
    allowed = {"1h", "4h"}
    normalized = []
    for timeframe in timeframes:
        value = str(timeframe).strip()
        if value not in allowed:
            raise ValueError(f"unsupported scanner timeframe: {value!r}; use 1h or 4h")
        if value and value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ValueError("at least one scanner timeframe is required")
    return normalized
