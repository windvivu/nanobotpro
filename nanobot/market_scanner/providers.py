"""Market data provider interface and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from nanobot.market_scanner.models import Candle, MarketSymbol, Ticker24h


class MarketDataProvider(ABC):
    exchange: str

    @abstractmethod
    async def list_symbols(self, market: str, quote_asset: str) -> list[MarketSymbol]:
        """Return normalized market symbols."""

    @abstractmethod
    async def get_24h_tickers(self, symbols: list[str] | None = None) -> list[Ticker24h]:
        """Return normalized 24h tickers."""

    @abstractmethod
    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Return normalized OHLCV candles."""


def create_provider(exchange: str, *, timeout: float = 15.0, client: Any | None = None) -> MarketDataProvider:
    normalized = exchange.strip().lower()
    if normalized == "binance":
        from nanobot.market_scanner.binance import BinanceProvider

        return BinanceProvider(timeout=timeout, client=client)
    raise ValueError(f"Unsupported market scanner exchange: {exchange}")
