"""Tiny JSON-backed state for open positions and their trailing stops."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict
from typing import Dict, Optional

_LOCK = threading.Lock()


@dataclass
class ManagedTrade:
    symbol: str
    side: str               # LONG or SHORT
    setup: str
    entry_price: float
    stop_price: float
    quantity: float
    leverage: int
    opened_at: str
    # Trendline snapshot for trailing
    line_kind: str          # "up" | "down" | "flat"
    line_slope: float
    line_intercept: float
    line_first_idx: int
    opened_bar_idx: int     # bar index in the candle frame at entry time


class Store:
    def __init__(self, path: str = "state.json"):
        self.path = path
        self._data: Dict[str, ManagedTrade] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
            for sym, row in raw.items():
                self._data[sym] = ManagedTrade(**row)
        except Exception:
            self._data = {}

    def _save(self) -> None:
        with _LOCK:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({k: asdict(v) for k, v in self._data.items()}, f, indent=2)
            os.replace(tmp, self.path)

    def put(self, trade: ManagedTrade) -> None:
        self._data[trade.symbol] = trade
        self._save()

    def get(self, symbol: str) -> Optional[ManagedTrade]:
        return self._data.get(symbol)

    def drop(self, symbol: str) -> None:
        self._data.pop(symbol, None)
        self._save()

    def all(self) -> Dict[str, ManagedTrade]:
        return dict(self._data)
