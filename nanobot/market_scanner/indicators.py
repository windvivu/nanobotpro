"""Pure Python indicator helpers for the market scanner."""

from __future__ import annotations

from collections.abc import Sequence

from nanobot.market_scanner.models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    multiplier = 2 / (period + 1)
    prev = sma
    for idx in range(period, len(values)):
        prev = (values[idx] - prev) * multiplier + prev
        result[idx] = prev
    return result


def rsi_wilder(values: Sequence[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, period + 1):
        change = values[idx] - values[idx - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = _rsi_from_avgs(avg_gain, avg_loss)

    for idx in range(period + 1, len(values)):
        change = values[idx] - values[idx - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[idx] = _rsi_from_avgs(avg_gain, avg_loss)
    return result


def atr_wilder(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return result

    true_ranges: list[float] = [candles[0].high - candles[0].low]
    for idx in range(1, len(candles)):
        current = candles[idx]
        prev_close = candles[idx - 1].close
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - prev_close),
                abs(current.low - prev_close),
            )
        )

    atr = sum(true_ranges[1 : period + 1]) / period
    result[period] = atr
    for idx in range(period + 1, len(candles)):
        atr = ((atr * (period - 1)) + true_ranges[idx]) / period
        result[idx] = atr
    return result


def bollinger_width_pct(values: Sequence[float], period: int = 20, std_mult: float = 2.0) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    for idx in range(period - 1, len(values)):
        window = values[idx - period + 1 : idx + 1]
        middle = sum(window) / period
        if middle == 0:
            continue
        variance = sum((value - middle) ** 2 for value in window) / period
        std = variance ** 0.5
        upper = middle + (std_mult * std)
        lower = middle - (std_mult * std)
        result[idx] = (upper - lower) / middle * 100
    return result


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
