"""Position sizing with dynamic leverage to support very small capital.

The bot preserves the "risk a fixed fraction of balance per trade" rule from
the playbook, but when the resulting notional is below the exchange's
minimum (typically 5 USDT) we auto-scale leverage upward (capped at
MAX_LEVERAGE) so small accounts can still take the trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple


@dataclass
class SizingResult:
    quantity: float
    leverage: int
    notional: float
    margin: float
    risk_usdt: float


def calc_sizing(*,
                balance_usdt: float,
                entry_price: float,
                stop_price: float,
                risk_fraction: float,
                min_notional: float,
                step_size: float,
                min_qty: float,
                min_leverage: int,
                max_leverage: int) -> SizingResult:
    """Return qty/leverage so the dollar risk equals risk_fraction * balance
    (or as close as is achievable given min qty/notional constraints).

    With small capital we raise leverage until the required margin is
    affordable AND the notional meets the exchange minimum.
    """
    if balance_usdt <= 0:
        raise ValueError("Zero balance")
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0:
        raise ValueError("Stop distance is zero")

    risk_budget = balance_usdt * risk_fraction
    # qty that risks exactly risk_budget if SL hits
    raw_qty = risk_budget / stop_dist
    qty = _floor_step(raw_qty, step_size)
    if qty < min_qty:
        qty = min_qty

    notional = qty * entry_price

    # Bump qty so we clear min notional even if it means taking slightly more risk.
    if notional < min_notional:
        qty = _ceil_step(min_notional / entry_price, step_size)
        if qty < min_qty:
            qty = min_qty
        notional = qty * entry_price

    # Pick smallest leverage such that margin = notional / lev <= balance,
    # but never exceed configured max_leverage.
    leverage = min_leverage
    for lev in range(min_leverage, max_leverage + 1):
        if notional / lev <= balance_usdt * 0.98:
            leverage = lev
            break
    else:
        leverage = max_leverage

    margin = notional / leverage
    risk_usdt = qty * stop_dist
    return SizingResult(
        quantity=qty,
        leverage=leverage,
        notional=notional,
        margin=margin,
        risk_usdt=risk_usdt,
    )


def _floor_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step


def _ceil_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.ceil(x / step) * step
