"""Feature extraction and deterministic market scanner scoring."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nanobot.market_scanner.indicators import atr_wilder, bollinger_width_pct, ema, rsi_wilder
from nanobot.market_scanner.models import Candidate, Candle, Ticker24h, TimeframeFeatures


def calculate_features(candles: list[Candle], timeframe: str) -> TimeframeFeatures:
    if not candles:
        raise ValueError("candles must not be empty")
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    current = candles[-1]

    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    ema200_series = ema(closes, 200)
    rsi_series = rsi_wilder(closes, 14)
    atr_series = atr_wilder(candles, 14)
    bb_width_series = bollinger_width_pct(closes, 20)

    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]
    ema200 = ema200_series[-1]
    confidence = 1.0
    warnings: list[str] = []
    if len(candles) < 220:
        confidence = 0.8 if len(candles) >= 80 else 0.55
        warnings.append("Reduced confidence: fewer than 220 candles.")

    ema_stack = _ema_stack(ema20, ema50, ema200)
    if ema200 is None:
        warnings.append("EMA200 unavailable; trend confidence reduced.")

    ema20_slope_pct = None
    if len(ema20_series) > 5 and ema20 is not None:
        previous = ema20_series[-6]
        if previous:
            ema20_slope_pct = (ema20 - previous) / previous * 100

    roc_12 = None
    if len(closes) > 12 and closes[-13] != 0:
        roc_12 = (closes[-1] - closes[-13]) / closes[-13] * 100

    atr14 = atr_series[-1]
    atr_pct = (atr14 / current.close * 100) if atr14 is not None and current.close else None

    relative_volume_20 = None
    if len(volumes) >= 21:
        previous_avg = sum(volumes[-21:-1]) / 20
        if previous_avg:
            relative_volume_20 = volumes[-1] / previous_avg

    previous_50 = candles[-51:-1] if len(candles) >= 51 else []
    range_high_50 = max((candle.high for candle in previous_50), default=None)
    range_low_50 = min((candle.low for candle in previous_50), default=None)
    dist_to_high_50_pct = None
    dist_to_low_50_pct = None
    if range_high_50 is not None and current.close:
        dist_to_high_50_pct = (range_high_50 - current.close) / current.close * 100
    if range_low_50 is not None and current.close:
        dist_to_low_50_pct = (current.close - range_low_50) / current.close * 100

    return TimeframeFeatures(
        timeframe=timeframe,
        close=current.close,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        ema_stack=ema_stack,
        ema20_slope_pct=ema20_slope_pct,
        rsi14=rsi_series[-1],
        roc_12=roc_12,
        atr14=atr14,
        atr_pct=atr_pct,
        bb_width_pct=bb_width_series[-1],
        relative_volume_20=relative_volume_20,
        range_high_50=range_high_50,
        range_low_50=range_low_50,
        dist_to_high_50_pct=dist_to_high_50_pct,
        dist_to_low_50_pct=dist_to_low_50_pct,
        breakout_50=range_high_50 is not None and current.close > range_high_50,
        breakdown_50=range_low_50 is not None and current.close < range_low_50,
        confidence=confidence,
        warnings=warnings,
    )


def rank_candidates(
    features_by_symbol: Mapping[str, Mapping[str, TimeframeFeatures]],
    tickers_by_symbol: Mapping[str, Ticker24h],
    *,
    min_quote_volume: float,
) -> list[Candidate]:
    sorted_by_volume = sorted(tickers_by_symbol.values(), key=lambda ticker: ticker.quote_volume)
    volume_rank = {
        ticker.symbol: (idx / max(len(sorted_by_volume) - 1, 1))
        for idx, ticker in enumerate(sorted_by_volume)
    }
    candidates: list[Candidate] = []
    for symbol, timeframe_features in features_by_symbol.items():
        ticker = tickers_by_symbol.get(symbol)
        if ticker is None or ticker.quote_volume < min_quote_volume:
            continue
        candidate = score_symbol(
            symbol,
            timeframe_features,
            ticker,
            liquidity_rank_pct=volume_rank.get(symbol, 0.0),
        )
        if candidate.direction != "ignore":
            candidates.append(candidate)
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def score_symbol(
    symbol: str,
    features_by_timeframe: Mapping[str, TimeframeFeatures],
    ticker: Ticker24h,
    *,
    liquidity_rank_pct: float,
) -> Candidate:
    one_h = features_by_timeframe.get("1h")
    four_h = features_by_timeframe.get("4h")
    weighted_long = 0.0
    weighted_short = 0.0
    total_weight = 0.0

    for timeframe, weight in (("4h", 0.55), ("1h", 0.45)):
        features = features_by_timeframe.get(timeframe)
        if features is None:
            continue
        long_points, short_points = _directional_points(features)
        weighted_long += long_points * weight * features.confidence
        weighted_short += short_points * weight * features.confidence
        total_weight += weight

    if total_weight:
        weighted_long /= total_weight
        weighted_short /= total_weight

    alignment = _timeframe_alignment(one_h, four_h)
    risk_score = _risk_score(one_h, four_h, min_quote_volume=ticker.quote_volume)
    liquidity_score = liquidity_rank_pct * 100
    risk_penalty = min(risk_score * 0.4, 25)
    liquidity_bonus = min(liquidity_score * 0.1, 10)
    long_score = _clamp(weighted_long - risk_penalty + liquidity_bonus)
    short_score = _clamp(weighted_short - risk_penalty + liquidity_bonus)
    watch_score = max(long_score, short_score)
    warnings = _collect_warnings(features_by_timeframe)

    extreme_volatility = one_h is not None and one_h.atr_pct is not None and one_h.atr_pct > 12
    if extreme_volatility:
        warnings.append("Volatility too high for directional bias.")

    if not extreme_volatility and long_score >= 65 and long_score - short_score >= 15:
        direction = "long"
        score = long_score
    elif not extreme_volatility and short_score >= 65 and short_score - long_score >= 15:
        direction = "short"
        score = short_score
    elif 55 <= watch_score <= 64 or (liquidity_rank_pct >= 0.75 and alignment == "mixed") or extreme_volatility:
        direction = "watch"
        score = watch_score
    else:
        direction = "ignore"
        score = watch_score

    return Candidate(
        symbol=symbol,
        direction=direction,
        score=round(score, 2),
        long_score=round(long_score, 2),
        short_score=round(short_score, 2),
        watch_score=round(watch_score, 2),
        liquidity_score=round(liquidity_score, 2),
        risk_score=round(risk_score, 2),
        reasons=_reasons(direction, one_h, four_h),
        warnings=warnings,
        metrics={
            "quote_volume_24h": ticker.quote_volume,
            "timeframe_alignment": alignment,
            "timeframes": {
                timeframe: _feature_metrics(features)
                for timeframe, features in features_by_timeframe.items()
            },
        },
    )


def candidate_to_dict(candidate: Candidate, *, include_metrics: bool = True) -> dict[str, Any]:
    payload = {
        "symbol": candidate.symbol,
        "tradingview_symbol": _tradingview_symbol(candidate.symbol),
        "direction": candidate.direction,
        "score": candidate.score,
        "long_score": candidate.long_score,
        "short_score": candidate.short_score,
        "watch_score": candidate.watch_score,
        "liquidity_score": candidate.liquidity_score,
        "risk_score": candidate.risk_score,
        "reasons": candidate.reasons,
        "warnings": candidate.warnings,
    }
    if include_metrics:
        payload["metrics"] = candidate.metrics
    return payload


def _tradingview_symbol(symbol: str) -> str:
    # Binance USD-M futures use the .P suffix on TradingView. Binance API raw
    # symbols such as 1000PEPEUSDT do not always exist as plain spot symbols.
    return f"BINANCE:{symbol}.P"


def _directional_points(features: TimeframeFeatures) -> tuple[float, float]:
    long = 0.0
    short = 0.0
    if features.ema_stack == "bullish":
        long += 25
    elif features.ema_stack == "bearish":
        short += 25
    if features.ema50 is not None:
        if features.close > features.ema50:
            long += 15
        elif features.close < features.ema50:
            short += 15
    if features.ema20_slope_pct is not None:
        if features.ema20_slope_pct > 0:
            long += 10
        elif features.ema20_slope_pct < 0:
            short += 10
    if features.rsi14 is not None:
        if 50 <= features.rsi14 <= 68:
            long += 15
        elif 45 <= features.rsi14 < 50:
            long += 5
        if 32 <= features.rsi14 <= 50:
            short += 15
        elif 50 < features.rsi14 <= 55:
            short += 5
    if features.roc_12 is not None:
        if features.roc_12 > 0:
            long += 10
        elif features.roc_12 < 0:
            short += 10
    if features.breakout_50:
        long += 15
    if features.breakdown_50:
        short += 15
    if features.relative_volume_20 is not None and features.relative_volume_20 >= 1.5:
        long += 10
        short += 10
    return long, short


def _risk_score(one_h: TimeframeFeatures | None, four_h: TimeframeFeatures | None, *, min_quote_volume: float) -> float:
    risk = 0.0
    for features in (one_h, four_h):
        if features and features.atr_pct is not None:
            if features.atr_pct > 10:
                risk += 40
            elif features.atr_pct > 6:
                risk += 25
    if one_h and one_h.ema20:
        distance = abs(one_h.close - one_h.ema20) / one_h.ema20 * 100
        if distance > 8:
            risk += 20
    if _timeframe_alignment(one_h, four_h) == "mixed":
        risk += 20
    if min_quote_volume <= 0:
        risk += 30
    return min(risk, 100)


def _ema_stack(ema20: float | None, ema50: float | None, ema200: float | None) -> str:
    if ema20 is None or ema50 is None:
        return "mixed"
    if ema200 is None:
        if ema20 > ema50:
            return "bullish"
        if ema20 < ema50:
            return "bearish"
        return "mixed"
    if ema20 > ema50 > ema200:
        return "bullish"
    if ema20 < ema50 < ema200:
        return "bearish"
    return "mixed"


def _timeframe_alignment(one_h: TimeframeFeatures | None, four_h: TimeframeFeatures | None) -> str:
    if one_h is None or four_h is None:
        return "mixed"
    if one_h.ema_stack == four_h.ema_stack and one_h.ema_stack in {"bullish", "bearish"}:
        return one_h.ema_stack
    return "mixed"


def _collect_warnings(features_by_timeframe: Mapping[str, TimeframeFeatures]) -> list[str]:
    warnings: list[str] = []
    for timeframe, features in features_by_timeframe.items():
        warnings.extend(f"{timeframe}: {warning}" for warning in features.warnings)
    return warnings


def _reasons(direction: str, one_h: TimeframeFeatures | None, four_h: TimeframeFeatures | None) -> list[str]:
    reasons: list[str] = []
    for label, features in (("4h", four_h), ("1h", one_h)):
        if features is None:
            continue
        reasons.append(f"{label} trend {features.ema_stack}: EMA stack {features.ema_stack}")
        if features.rsi14 is not None:
            reasons.append(f"{label} RSI {features.rsi14:.1f}")
        if features.relative_volume_20 is not None:
            reasons.append(f"{label} relative volume {features.relative_volume_20:.2f}x")
        if len(reasons) >= 4:
            break
    if not reasons:
        reasons.append(f"{direction} classification from deterministic scanner score.")
    return reasons


def _feature_metrics(features: TimeframeFeatures) -> dict[str, Any]:
    return {
        "close": features.close,
        "rsi14": features.rsi14,
        "atr_pct": features.atr_pct,
        "relative_volume_20": features.relative_volume_20,
        "ema_stack": features.ema_stack,
        "breakout_50": features.breakout_50,
        "breakdown_50": features.breakdown_50,
    }


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 100.0)
