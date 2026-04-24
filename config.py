"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v is not None and v != "" else default


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v is not None and v != "" else default


def _get_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v.strip() if v is not None and v != "" else default


def _is_ascii_symbol(s: str) -> bool:
    return bool(s) and all(c.isascii() and (c.isalnum() or c in "_-") for c in s)


def _get_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    cleaned = []
    for s in raw.split(","):
        s = s.strip().upper()
        if not s:
            continue
        if not _is_ascii_symbol(s):
            raise RuntimeError(
                f"{name} contains invalid symbol {s!r} — Binance symbols must be "
                "plain ASCII alphanumerics, e.g. BTCUSDT,ETHUSDT,SOLUSDT"
            )
        cleaned.append(s)
    return cleaned


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: _get_str("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _get_str("BINANCE_API_SECRET", ""))
    testnet: bool = field(default_factory=lambda: _get_bool("BINANCE_TESTNET", True))

    telegram_token: str = field(default_factory=lambda: _get_str("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _get_str("TELEGRAM_CHAT_ID", ""))

    symbols: List[str] = field(default_factory=lambda: _get_list("SYMBOLS", ["BTCUSDT"]))
    timeframe: str = field(default_factory=lambda: _get_str("TIMEFRAME", "4h"))

    risk_per_trade: float = field(default_factory=lambda: _get_float("RISK_PER_TRADE", 0.01))
    max_leverage: int = field(default_factory=lambda: _get_int("MAX_LEVERAGE", 50))
    min_leverage: int = field(default_factory=lambda: _get_int("MIN_LEVERAGE", 1))
    margin_type: str = field(default_factory=lambda: _get_str("MARGIN_TYPE", "ISOLATED").upper())

    pivot_lookback: int = field(default_factory=lambda: _get_int("PIVOT_LOOKBACK", 3))
    bounce_min_touches: int = field(default_factory=lambda: _get_int("BOUNCE_MIN_TOUCHES", 3))
    break_min_touches: int = field(default_factory=lambda: _get_int("BREAK_MIN_TOUCHES", 2))
    min_bars_between_touches: int = field(
        default_factory=lambda: _get_int("MIN_BARS_BETWEEN_TOUCHES", 42)
    )
    trendline_tolerance: float = field(
        default_factory=lambda: _get_float("TRENDLINE_TOLERANCE", 0.0035)
    )
    candle_limit: int = field(default_factory=lambda: _get_int("CANDLE_LIMIT", 500))

    poll_interval: int = field(default_factory=lambda: _get_int("POLL_INTERVAL", 60))
    dry_run: bool = field(default_factory=lambda: _get_bool("DRY_RUN", False))

    def validate(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("BINANCE_API_KEY")
        if not self.api_secret:
            missing.append("BINANCE_API_SECRET")
        if not self.telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
        if self.margin_type not in ("ISOLATED", "CROSSED"):
            raise RuntimeError("MARGIN_TYPE must be ISOLATED or CROSSED")
        if self.min_leverage < 1 or self.max_leverage < self.min_leverage:
            raise RuntimeError("Invalid leverage bounds")
