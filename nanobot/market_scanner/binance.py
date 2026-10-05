"""Binance public market-data provider."""

from __future__ import annotations

from typing import Any

import httpx

from nanobot.market_scanner.models import Candle, MarketSymbol, Ticker24h
from nanobot.market_scanner.providers import MarketDataProvider

_FAPI_BASE_URL = "https://fapi.binance.com"
_TIMEFRAME_MAP = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}


class BinanceProvider(MarketDataProvider):
    exchange = "binance"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
        base_url: str = _FAPI_BASE_URL,
    ) -> None:
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def list_symbols(self, market: str, quote_asset: str) -> list[MarketSymbol]:
        if market != "futures":
            raise ValueError("BinanceProvider first version supports market='futures' only.")
        payload = await self._get_json("/fapi/v1/exchangeInfo")
        symbols = payload.get("symbols", [])
        results: list[MarketSymbol] = []
        for item in symbols:
            if not isinstance(item, dict):
                continue
            raw_symbol = str(item.get("symbol") or "")
            if not raw_symbol:
                continue
            if str(item.get("quoteAsset") or "").upper() != quote_asset.upper():
                continue
            results.append(
                MarketSymbol(
                    symbol=raw_symbol.upper(),
                    base_asset=str(item.get("baseAsset") or "").upper(),
                    quote_asset=str(item.get("quoteAsset") or "").upper(),
                    market="futures",
                    status=str(item.get("status") or ""),
                    exchange=self.exchange,
                    raw_symbol=raw_symbol,
                )
            )
        return results

    async def get_24h_tickers(self, symbols: list[str] | None = None) -> list[Ticker24h]:
        payload = await self._get_json("/fapi/v1/ticker/24hr")
        rows = payload if isinstance(payload, list) else [payload]
        allow = {symbol.upper() for symbol in symbols} if symbols else None
        tickers: list[Ticker24h] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if not symbol or (allow is not None and symbol not in allow):
                continue
            tickers.append(
                Ticker24h(
                    symbol=symbol,
                    last_price=_to_float(item.get("lastPrice")),
                    quote_volume=_to_float(item.get("quoteVolume")),
                    price_change_percent=_to_float(item.get("priceChangePercent")),
                    high=_to_float(item.get("highPrice")),
                    low=_to_float(item.get("lowPrice")),
                    exchange=self.exchange,
                )
            )
        return tickers

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        interval = _TIMEFRAME_MAP.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported Binance timeframe: {timeframe}")
        payload = await self._get_json(
            "/fapi/v1/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
        )
        candles: list[Candle] = []
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, list) or len(row) < 8:
                continue
            candles.append(
                Candle(
                    open_time=int(row[0]),
                    open=_to_float(row[1]),
                    high=_to_float(row[2]),
                    low=_to_float(row[3]),
                    close=_to_float(row[4]),
                    volume=_to_float(row[5]),
                    quote_volume=_to_float(row[7]),
                )
            )
        return candles

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._client is not None:
            response = await self._client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
