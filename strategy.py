"""Trendline detection + bounce/break signals.

Implements the Tori Trade's Playbook trendline strategy:

- Action line: where trade is entered
- Safety line: where trade is exited (stop-loss)
- Bounce setup: price touches and respects an established trendline.
  Action line == Safety line (the trendline itself).
  Requires >= N clean touchpoints and >= 1 week of data between first
  touchpoint and the entry candle (42 bars on 4h).
- Break setup: price breaks AND CLOSES through an established trendline.
  Action line is the broken trendline. Safety line is a new opposing
  trendline drawn after the break. Invalidated on close beyond the
  safety line.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------- data classes ----------

@dataclass
class Pivot:
    idx: int          # integer bar index
    price: float
    kind: str         # "high" or "low"


@dataclass
class Trendline:
    kind: str               # "up" (support, connects lows) or "down" (resistance, connects highs)
    slope: float            # price per bar
    intercept: float        # price at bar 0
    touches: List[Pivot]    # chronological touchpoints used to fit
    first_idx: int          # first touchpoint bar index
    last_idx: int           # last touchpoint bar index

    def value_at(self, idx: int) -> float:
        return self.slope * idx + self.intercept


@dataclass
class Signal:
    side: str               # "BUY" or "SELL"
    setup: str              # "bounce" | "break2" | "break3"
    entry_price: float
    stop_price: float       # safety line value at entry bar
    action_line: Trendline
    safety_line: Trendline  # same as action for bounce
    note: str = ""


# ---------- pivot / swing detection ----------

def find_pivots(df: pd.DataFrame, lookback: int = 3) -> List[Pivot]:
    """Return confirmed swing highs/lows using a fractal lookback.

    A pivot high at i requires highs[i] strictly greater than the `lookback`
    bars on both sides. A pivot low uses strictly lower lows on both sides.
    The last `lookback` bars cannot be confirmed.
    """
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


def _count_touches(pivots: List[Pivot], kind: str, slope: float, intercept: float,
                   tolerance_frac: float) -> List[Pivot]:
    """Pivots that sit ON the line within tolerance, without violating it."""
    touches: List[Pivot] = []
    for p in pivots:
        if p.kind != kind:
            continue
        line_val = slope * p.idx + intercept
        if line_val <= 0:
            continue
        dist = abs(p.price - line_val) / line_val
        if dist <= tolerance_frac:
            touches.append(p)
    return touches


def _violates(df: pd.DataFrame, slope: float, intercept: float, kind: str,
              start_idx: int, end_idx: int) -> bool:
    """Between start_idx and end_idx (inclusive), no candle CLOSE may violate the line."""
    if end_idx <= start_idx:
        return False
    segment = df.iloc[start_idx:end_idx + 1]
    xs = np.arange(start_idx, end_idx + 1)
    line = slope * xs + intercept
    closes = segment["close"].to_numpy()
    if kind == "up":
        # support: closes below the line invalidate
        return bool(np.any(closes < line * (1 - 0.001)))
    # downtrend: closes above invalidate
    return bool(np.any(closes > line * (1 + 0.001)))


def build_trendline(df: pd.DataFrame, pivots: List[Pivot], kind: str,
                    min_touches: int, tolerance_frac: float,
                    min_bars_span: int, end_idx: Optional[int] = None) -> Optional[Trendline]:
    """Find the best recent trendline of the given kind.

    Strategy: take the two most-recent pivots of the matching kind, try every
    earlier pivot as the anchor; keep lines with the most touches whose first
    touchpoint is at least `min_bars_span` before `end_idx` and that no close
    has violated.  Among equals, prefer the line with the latest anchor (the
    line is the "freshest").
    """
    if end_idx is None:
        end_idx = len(df) - 1
    # relevant pivots for this line kind
    same = [p for p in pivots if p.kind == ("low" if kind == "up" else "high") and p.idx <= end_idx]
    if len(same) < 2:
        return None

    best: Optional[Trendline] = None

    # Use the most recent pivot as B, iterate earlier pivots as A.
    B = same[-1]
    for A in same[:-1]:
        if B.idx - A.idx < min_bars_span:
            continue
        slope, intercept = _fit_line(A, B)
        # Direction check
        if kind == "up" and slope <= 0:
            continue
        if kind == "down" and slope >= 0:
            continue
        # No close violations between A and B
        if _violates(df, slope, intercept, kind, A.idx, B.idx):
            continue
        # Count touches among all same-kind pivots in [A, end_idx]
        touches = _count_touches(
            [p for p in same if A.idx <= p.idx <= end_idx],
            "low" if kind == "up" else "high",
            slope, intercept, tolerance_frac,
        )
        if len(touches) < min_touches:
            continue
        # Keep the line with the most recent anchor that still has enough touches.
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

def _bar_touches_line(candle, line_val: float, tolerance_frac: float) -> bool:
    """True if the candle's range overlaps the line within tolerance."""
    tol = line_val * tolerance_frac
    lo, hi = candle["low"], candle["high"]
    return (lo - tol) <= line_val <= (hi + tol)


def detect_signal(df: pd.DataFrame,
                  pivot_lookback: int,
                  bounce_min_touches: int,
                  break_min_touches: int,
                  min_bars_span: int,
                  tolerance_frac: float) -> Optional[Signal]:
    """Evaluate the JUST-CLOSED candle for a bounce or break entry.

    The bot is called right after a candle closes; we treat df.iloc[-1] as the
    most-recent confirmed bar.
    """
    if len(df) < min_bars_span + 5:
        return None

    pivots = find_pivots(df.iloc[:-1], lookback=pivot_lookback)
    if len(pivots) < 2:
        return None

    last = df.iloc[-1]
    last_idx = len(df) - 1

    up = build_trendline(df, pivots, "up",
                        min_touches=max(bounce_min_touches, break_min_touches),
                        tolerance_frac=tolerance_frac,
                        min_bars_span=min_bars_span,
                        end_idx=last_idx - 1)
    down = build_trendline(df, pivots, "down",
                          min_touches=max(bounce_min_touches, break_min_touches),
                          tolerance_frac=tolerance_frac,
                          min_bars_span=min_bars_span,
                          end_idx=last_idx - 1)

    # --- Break setups first (reversals) ---
    if up is not None:
        line_val = up.value_at(last_idx)
        # Close below uptrend line = break -> SHORT
        if last["close"] < line_val * (1 - 0.0005) and len(up.touches) >= break_min_touches:
            # safety line: new opposing DOWN trendline drawn with most recent pivot highs
            safety = build_trendline(df, pivots, "down",
                                     min_touches=2, tolerance_frac=tolerance_frac * 1.5,
                                     min_bars_span=max(3, min_bars_span // 8),
                                     end_idx=last_idx - 1)
            if safety is None:
                # fallback: last confirmed swing high as a flat safety line
                highs = [p for p in pivots if p.kind == "high"]
                if highs:
                    ph = highs[-1]
                    safety = Trendline("down", 0.0, ph.price, [ph], ph.idx, ph.idx)
            if safety is not None:
                setup = "break3" if len(up.touches) >= 3 else "break2"
                return Signal(
                    side="SELL", setup=setup,
                    entry_price=float(last["close"]),
                    stop_price=safety.value_at(last_idx),
                    action_line=up, safety_line=safety,
                    note=f"{setup} of uptrend line (touches={len(up.touches)})",
                )

    if down is not None:
        line_val = down.value_at(last_idx)
        # Close above downtrend line = break -> LONG
        if last["close"] > line_val * (1 + 0.0005) and len(down.touches) >= break_min_touches:
            safety = build_trendline(df, pivots, "up",
                                     min_touches=2, tolerance_frac=tolerance_frac * 1.5,
                                     min_bars_span=max(3, min_bars_span // 8),
                                     end_idx=last_idx - 1)
            if safety is None:
                lows = [p for p in pivots if p.kind == "low"]
                if lows:
                    pl = lows[-1]
                    safety = Trendline("up", 0.0, pl.price, [pl], pl.idx, pl.idx)
            if safety is not None:
                setup = "break3" if len(down.touches) >= 3 else "break2"
                return Signal(
                    side="BUY", setup=setup,
                    entry_price=float(last["close"]),
                    stop_price=safety.value_at(last_idx),
                    action_line=down, safety_line=safety,
                    note=f"{setup} of downtrend line (touches={len(down.touches)})",
                )

    # --- Bounce setups ---
    if up is not None and len(up.touches) >= bounce_min_touches:
        line_val = up.value_at(last_idx)
        # Candle tested the line (low pierced or within tolerance) AND closed above it
        pierced = last["low"] <= line_val * (1 + tolerance_frac)
        closed_above = last["close"] > line_val
        if pierced and closed_above:
            return Signal(
                side="BUY", setup="bounce",
                entry_price=float(last["close"]),
                stop_price=line_val,   # action line == safety line
                action_line=up, safety_line=up,
                note=f"bounce off uptrend (touches={len(up.touches)})",
            )

    if down is not None and len(down.touches) >= bounce_min_touches:
        line_val = down.value_at(last_idx)
        pierced = last["high"] >= line_val * (1 - tolerance_frac)
        closed_below = last["close"] < line_val
        if pierced and closed_below:
            return Signal(
                side="SELL", setup="bounce",
                entry_price=float(last["close"]),
                stop_price=line_val,
                action_line=down, safety_line=down,
                note=f"bounce off downtrend (touches={len(down.touches)})",
            )

    return None


# ---------- trailing stop helper ----------

def trailing_stop(df: pd.DataFrame, side: str, line: Trendline) -> float:
    """Current price value of the trendline at the most recent bar.

    For a bounce trade the playbook says "trail the stop along the trendline,
    adjusting below each new valid swing low" — this returns the line value
    at the last bar; callers can also snap it to a recent swing.
    """
    return float(line.value_at(len(df) - 1))
