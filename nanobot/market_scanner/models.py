"""Normalized market scanner data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MarketSymbol:
    symbol: str
    base_asset: str
    quote_asset: str
    market: str
    status: str
    exchange: str
    raw_symbol: str


@dataclass(slots=True)
class Ticker24h:
    symbol: str
    last_price: float
    quote_volume: float
    price_change_percent: float
    high: float
    low: float
    exchange: str


@dataclass(slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass(slots=True)
class TimeframeFeatures:
    timeframe: str
    close: float
    ema20: float | None
    ema50: float | None
    ema200: float | None
    ema_stack: str
    ema20_slope_pct: float | None
    rsi14: float | None
    roc_12: float | None
    atr14: float | None
    atr_pct: float | None
    bb_width_pct: float | None
    relative_volume_20: float | None
    range_high_50: float | None
    range_low_50: float | None
    dist_to_high_50_pct: float | None
    dist_to_low_50_pct: float | None
    breakout_50: bool
    breakdown_50: bool
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Candidate:
    symbol: str
    direction: str
    score: float
    long_score: float
    short_score: float
    watch_score: float
    liquidity_score: float
    risk_score: float
    reasons: list[str]
    warnings: list[str]
    metrics: dict[str, Any]
