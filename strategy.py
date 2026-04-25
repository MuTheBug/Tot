"""Tori Trade's Trendline Strategy — strict implementation.

Mirrors the parametric rules from the methodology deconstruction:

A+ Trendline parameters:
  * Minimum 3 distinct touchpoints (configurable; 2 allowed for break setups
    in rare cases, per the doc).
  * Anchored to candle wicks (highs / lows), never bodies.
  * Zero price intersection between anchor points: between the first and
    last touchpoint, neither a wick nor a close may breach the line.
  * Minimum 6 candles between consecutive touchpoints.
  * First touchpoint must be at least 3 weeks of bars before entry
    (126 candles on 4h).
  * Slope < 45° measured against a 3-month chart window
    (≈540 candles on 4h).

Bounce setup:
  * Wait for retrace to the trendline; entry when price touches AND closes
    on the supportive side.
  * Action Line == Safety Line.
  * Stop given a small "standard deviation" buffer beyond the line so
    routine wicks don't trigger it.
  * Exit on 4h candle CLOSE through the line.

Break setup:
  * 4h candle must CLOSE past the action line (a wick alone is insufficient).
  * Construct a new opposing Safety Line from recent pivots.
  * Initial stop = the price where the 4th candle after the breakout would
    geometrically intersect the Safety Line ("4th Candle Rule").
  * Only ONE attempt per trendline (caller enforces).
  * Exit on 4h candle CLOSE back through the Safety Line.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------- data classes ----------

@dataclass
class Pivot:
    idx: int
    price: float
    kind: str         # "high" or "low"


@dataclass
class Trendline:
    kind: str               # "up" (support, connects lows) or "down" (resistance, connects highs)
    slope: float
    intercept: float
    touches: List[Pivot]
    first_idx: int
    last_idx: int

    def value_at(self, idx: int) -> float:
        return self.slope * idx + self.intercept

    def fingerprint(self) -> str:
        """Stable hash so the trader can blacklist a line after one attempt."""
        return hashlib.md5(
            f"{self.kind}|{round(self.slope, 8)}|{round(self.intercept, 4)}|"
            f"{self.first_idx}|{self.last_idx}".encode()
        ).hexdigest()[:12]


@dataclass
class Signal:
    side: str               # "BUY" or "SELL"
    setup: str              # "bounce" | "break2" | "break3"
    entry_price: float
    stop_price: float
    action_line: Trendline
    safety_line: Trendline
    note: str = ""


# ---------- pivot / swing detection ----------

def find_pivots(df: pd.DataFrame, lookback: int = 3) -> List[Pivot]:
    """Confirmed swing highs/lows using fractal lookback. Anchors come from
    candle WICKS (high/low), per the methodology."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    pivots: List[Pivot] = []
    for i in range(lookback, n - lookback):
        left_h = highs[i - lookback:i]
        right_h = highs[i + 1:i + 1 + lookback]
        if highs[i] > left_h.max() and highs[i] > right_h.max():
            pivots.append(Pivot(i, float(highs[i]), "high"))
            continue
        left_l = lows[i - lookback:i]
        right_l = lows[i + 1:i + 1 + lookback]
        if lows[i] < left_l.min() and lows[i] < right_l.min():
            pivots.append(Pivot(i, float(lows[i]), "low"))
    return pivots


# ---------- trendline construction ----------

def _fit_line(a: Pivot, b: Pivot) -> Tuple[float, float]:
    slope = (b.price - a.price) / (b.idx - a.idx)
    intercept = a.price - slope * a.idx
    return slope, intercept


def _violates_wick(df: pd.DataFrame, slope: float, intercept: float, kind: str,
                   start_idx: int, end_idx: int, tolerance_frac: float) -> bool:
    """Strict wick-based intersection check: between start and end (inclusive),
    no wick may pierce the line beyond `tolerance_frac`. Per the doc, "the
    body or wick of a historical candle breaches the line" makes the line
    structurally compromised.
    """
    if end_idx <= start_idx:
        return False
    segment = df.iloc[start_idx:end_idx + 1]
    xs = np.arange(start_idx, end_idx + 1, dtype=float)
    line = slope * xs + intercept
    if kind == "up":
        # Support trendline: low wick should never go below the line.
        return bool(np.any(segment["low"].to_numpy() < line * (1 - tolerance_frac)))
    # Downtrend resistance: high wick should never go above the line.
    return bool(np.any(segment["high"].to_numpy() > line * (1 + tolerance_frac)))


def _count_touches(pivots: List[Pivot], kind: str, slope: float, intercept: float,
                   tolerance_frac: float, min_spacing_bars: int) -> List[Pivot]:
    """Pivots ON the line within tolerance, with at least `min_spacing_bars`
    between consecutive touches."""
    target_kind = "low" if kind == "up" else "high"
    candidates: List[Pivot] = []
    for p in pivots:
        if p.kind != target_kind:
            continue
        line_val = slope * p.idx + intercept
        if line_val <= 0:
            continue
        if abs(p.price - line_val) / line_val <= tolerance_frac:
            candidates.append(p)
    candidates.sort(key=lambda p: p.idx)
    # Greedy spacing filter
    kept: List[Pivot] = []
    for p in candidates:
        if not kept or (p.idx - kept[-1].idx) >= min_spacing_bars:
            kept.append(p)
    return kept


def _angle_ok(slope: float, df: pd.DataFrame, end_idx: int,
              max_deg: float, ref_bars: int) -> bool:
    """Approximate the visual angle: when ~ref_bars candles fill the x axis
    and the price range over those bars fills the y axis, slope = range/N
    corresponds to a 45° line. We require |slope| / (range/N) <= tan(max).

    Uses the actual visible segment length (capped at ref_bars), so the
    test still makes sense when the user has fewer bars loaded than the
    canonical 3-month window.
    """
    start = max(0, end_idx - ref_bars)
    seg = df.iloc[start:end_idx + 1]
    seg_len = len(seg)
    if seg_len < 2:
        return True
    price_range = float(seg["high"].max() - seg["low"].min())
    if price_range <= 0:
        return True
    ref_slope = price_range / seg_len
    if ref_slope <= 0:
        return True
    normalized = abs(slope) / ref_slope
    return normalized <= math.tan(math.radians(max_deg))


def build_trendline(df: pd.DataFrame, pivots: List[Pivot], kind: str,
                    min_touches: int, tolerance_frac: float,
                    min_bars_first_to_end: int,
                    min_bars_between_taps: int,
                    max_slope_deg: float,
                    slope_ref_bars: int,
                    end_idx: Optional[int] = None) -> Optional[Trendline]:
    """Find the best A+ trendline of the given kind that obeys every rule."""
    if end_idx is None:
        end_idx = len(df) - 1
    same = [p for p in pivots if p.kind == ("low" if kind == "up" else "high") and p.idx <= end_idx]
    if len(same) < 2:
        return None

    best: Optional[Trendline] = None
    B = same[-1]
    for A in same[:-1]:
        # Rule: first touchpoint at least 3 weeks before "now"
        if (end_idx - A.idx) < min_bars_first_to_end:
            continue
        # Rule: 6 candles between taps (A and B are taps)
        if (B.idx - A.idx) < min_bars_between_taps:
            continue
        slope, intercept = _fit_line(A, B)
        if kind == "up" and slope <= 0:
            continue
        if kind == "down" and slope >= 0:
            continue
        # Rule: angle < 45° (ish) on the configured reference window
        if not _angle_ok(slope, df, end_idx, max_slope_deg, slope_ref_bars):
            continue
        # Rule: zero wick intersection between A and B
        if _violates_wick(df, slope, intercept, kind, A.idx, B.idx, tolerance_frac):
            continue
        # Touchpoints with 6-candle spacing
        touches = _count_touches(
            [p for p in same if A.idx <= p.idx <= end_idx],
            kind, slope, intercept, tolerance_frac, min_bars_between_taps,
        )
        if len(touches) < min_touches:
            continue
        # Prefer the line with the most touches; tiebreak by latest anchor.
        if best is None or len(touches) > len(best.touches) or (
            len(touches) == len(best.touches) and A.idx > best.first_idx
        ):
            best = Trendline(
                kind=kind, slope=slope, intercept=intercept,
                touches=sorted(touches, key=lambda p: p.idx),
                first_idx=A.idx, last_idx=B.idx,
            )
    return best


# ---------- signal detection ----------

def _build_safety_line(df: pd.DataFrame, pivots: List[Pivot], kind: str,
                       end_idx: int, tolerance_frac: float,
                       min_bars_between_taps: int) -> Optional[Trendline]:
    """For break setups: build the new opposing trendline that becomes the
    Safety Line. Less strict than an A+ line (post-break the structure is
    fresh — 2 pivots with proper spacing is acceptable per the doc).
    """
    same = [p for p in pivots if p.kind == ("low" if kind == "up" else "high") and p.idx <= end_idx]
    if len(same) < 2:
        return None
    B = same[-1]
    for A in reversed(same[:-1]):
        if (B.idx - A.idx) < min_bars_between_taps:
            continue
        slope, intercept = _fit_line(A, B)
        if kind == "up" and slope <= 0:
            continue
        if kind == "down" and slope >= 0:
            continue
        if _violates_wick(df, slope, intercept, kind, A.idx, B.idx, tolerance_frac * 1.5):
            continue
        return Trendline(kind=kind, slope=slope, intercept=intercept,
                         touches=[A, B], first_idx=A.idx, last_idx=B.idx)
    return None


def detect_signal(df: pd.DataFrame, *,
                  pivot_lookback: int,
                  bounce_min_touches: int,
                  break_min_touches: int,
                  min_bars_first_to_end: int,
                  min_bars_between_taps: int,
                  tolerance_frac: float,
                  max_slope_deg: float,
                  slope_ref_bars: int,
                  bounce_stop_buffer_frac: float,
                  fourth_candle_offset: int = 4) -> Optional[Signal]:
    """Evaluate the JUST-CLOSED candle (df.iloc[-1]) for an A+ bounce or
    break entry per the methodology.
    """
    if len(df) < min_bars_first_to_end + 5:
        return None

    pivots = find_pivots(df.iloc[:-1], lookback=pivot_lookback)
    if len(pivots) < 2:
        return None

    last = df.iloc[-1]
    last_idx = len(df) - 1

    common = dict(
        df=df, pivots=pivots,
        tolerance_frac=tolerance_frac,
        min_bars_first_to_end=min_bars_first_to_end,
        min_bars_between_taps=min_bars_between_taps,
        max_slope_deg=max_slope_deg,
        slope_ref_bars=slope_ref_bars,
        end_idx=last_idx - 1,
    )
    up = build_trendline(kind="up",
                         min_touches=max(bounce_min_touches, break_min_touches),
                         **common)
    down = build_trendline(kind="down",
                           min_touches=max(bounce_min_touches, break_min_touches),
                           **common)

    # --- Break setups (reversal): require full candle BODY close past line ---
    if up is not None and len(up.touches) >= break_min_touches:
        line_val = up.value_at(last_idx)
        if last["close"] < line_val and last["open"] >= line_val * (1 - tolerance_frac):
            # Confirmed close below uptrend support => SHORT break.
            safety = _build_safety_line(
                df, pivots, "down", last_idx - 1,
                tolerance_frac, min_bars_between_taps,
            )
            if safety is None:
                # No structural pivots yet — per doc, the rule is to wait. Skip.
                return None
            # 4th-Candle Rule for stop: project safety line forward.
            stop_idx = last_idx + fourth_candle_offset
            stop_price = safety.value_at(stop_idx)
            setup = "break3" if len(up.touches) >= 3 else "break2"
            return Signal(
                side="SELL", setup=setup,
                entry_price=float(last["close"]),
                stop_price=stop_price,
                action_line=up, safety_line=safety,
                note=f"{setup} of uptrend (touches={len(up.touches)}, 4th-candle SL)",
            )

    if down is not None and len(down.touches) >= break_min_touches:
        line_val = down.value_at(last_idx)
        if last["close"] > line_val and last["open"] <= line_val * (1 + tolerance_frac):
            safety = _build_safety_line(
                df, pivots, "up", last_idx - 1,
                tolerance_frac, min_bars_between_taps,
            )
            if safety is None:
                return None
            stop_idx = last_idx + fourth_candle_offset
            stop_price = safety.value_at(stop_idx)
            setup = "break3" if len(down.touches) >= 3 else "break2"
            return Signal(
                side="BUY", setup=setup,
                entry_price=float(last["close"]),
                stop_price=stop_price,
                action_line=down, safety_line=safety,
                note=f"{setup} of downtrend (touches={len(down.touches)}, 4th-candle SL)",
            )

    # --- Bounce setups (continuation) ---
    if up is not None and len(up.touches) >= bounce_min_touches:
        line_val = up.value_at(last_idx)
        # Tested the line (low touched within tolerance) AND closed above.
        pierced = last["low"] <= line_val * (1 + tolerance_frac)
        closed_above = last["close"] > line_val
        if pierced and closed_above:
            stop_price = line_val * (1 - bounce_stop_buffer_frac)
            return Signal(
                side="BUY", setup="bounce",
                entry_price=float(last["close"]),
                stop_price=stop_price,
                action_line=up, safety_line=up,
                note=f"bounce off uptrend (touches={len(up.touches)})",
            )

    if down is not None and len(down.touches) >= bounce_min_touches:
        line_val = down.value_at(last_idx)
        pierced = last["high"] >= line_val * (1 - tolerance_frac)
        closed_below = last["close"] < line_val
        if pierced and closed_below:
            stop_price = line_val * (1 + bounce_stop_buffer_frac)
            return Signal(
                side="SELL", setup="bounce",
                entry_price=float(last["close"]),
                stop_price=stop_price,
                action_line=down, safety_line=down,
                note=f"bounce off downtrend (touches={len(down.touches)})",
            )

    return None
