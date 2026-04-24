"""Thin wrapper around Binance USDT-M Futures (python-binance)."""
from __future__ import annotations

import logging
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException

log = logging.getLogger(__name__)


class FuturesExchange:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        # Strip to defend against stray newlines copied from dashboards.
        api_key = (api_key or "").strip()
        api_secret = (api_secret or "").strip()
        self.testnet = testnet
        self.client = Client(api_key, api_secret, testnet=testnet)
        self._filters_cache: Dict[str, Dict[str, Any]] = {}
        self._leverage_cache: Dict[str, int] = {}
        self._valid_symbols: set[str] = set()

    def preflight(self) -> None:
        """Validate credentials + clock against the futures endpoint.

        Raises a clear RuntimeError instead of letting -1022 leak out of
        the first trading call.
        """
        try:
            # Signed call that simply tests the key.
            self.client.futures_account_balance()
        except BinanceAPIException as e:
            hint = ""
            if getattr(e, "code", None) == -1022:
                network = "TESTNET" if self.testnet else "LIVE"
                hint = (
                    f"\n  -> Signature rejected on {network}. Likely causes:\n"
                    "     * Wrong network: testnet keys (from "
                    "https://testnet.binancefuture.com) do NOT work on live, "
                    "and live keys do NOT work on testnet. Check BINANCE_TESTNET.\n"
                    "     * Whitespace/newline in BINANCE_API_SECRET.\n"
                    "     * API key missing Futures Trading permission.\n"
                    "     * System clock skew > 1s (run `ntpdate` / enable NTP)."
                )
            elif getattr(e, "code", None) == -2015:
                hint = (
                    "\n  -> -2015: Invalid API-key, IP, or permission. Check "
                    "the key is enabled for Futures and your IP whitelist."
                )
            raise RuntimeError(f"Binance auth preflight failed: {e}{hint}") from e

    def validate_symbols(self, symbols: list[str]) -> list[str]:
        """Return the subset of `symbols` that are actual USDT-M futures
        trading pairs. Unknown ones are dropped with a warning."""
        if not self._valid_symbols:
            info = self.client.futures_exchange_info()
            self._valid_symbols = {
                s["symbol"] for s in info["symbols"]
                if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
            }
        good, bad = [], []
        for s in symbols:
            (good if s in self._valid_symbols else bad).append(s)
        if bad:
            log.warning("Unknown/invalid Binance futures symbols, skipping: %s", bad)
        return good

    def top_volume_symbols(self, limit: int) -> list[str]:
        """Top-N USDT-M perpetual symbols by 24h quote volume.

        Excludes anything that isn't currently trading or isn't a perpetual,
        and restricts to USDT-quoted pairs (skips USDC-margined / BUSD / etc).
        """
        # warm the perpetual set
        self.validate_symbols([])
        tickers = self.client.futures_ticker()
        ranked = sorted(
            (t for t in tickers
             if t["symbol"] in self._valid_symbols
             and t["symbol"].endswith("USDT")),
            key=lambda t: float(t.get("quoteVolume", 0)),
            reverse=True,
        )
        return [t["symbol"] for t in ranked[:limit]]

    # ---------- symbol info / filters ----------
    def symbol_filters(self, symbol: str) -> Dict[str, Any]:
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                f = {flt["filterType"]: flt for flt in s["filters"]}
                tick = Decimal(f["PRICE_FILTER"]["tickSize"])
                step = Decimal(f["LOT_SIZE"]["stepSize"])
                min_qty = Decimal(f["LOT_SIZE"]["minQty"])
                # MIN_NOTIONAL lives in a filter called "MIN_NOTIONAL" or "NOTIONAL"
                min_notional = Decimal("5")
                for key in ("MIN_NOTIONAL", "NOTIONAL"):
                    if key in f and "notional" in f[key]:
                        min_notional = Decimal(f[key]["notional"])
                        break
                out = {
                    "tickSize": tick,
                    "stepSize": step,
                    "minQty": min_qty,
                    "minNotional": min_notional,
                    "pricePrecision": int(s["pricePrecision"]),
                    "quantityPrecision": int(s["quantityPrecision"]),
                }
                self._filters_cache[symbol] = out
                return out
        raise ValueError(f"Symbol {symbol} not found on Binance Futures")

    def round_price(self, symbol: str, price: float) -> float:
        f = self.symbol_filters(symbol)
        tick = f["tickSize"]
        q = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return float(q)

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self.symbol_filters(symbol)
        step = f["stepSize"]
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(q)

    # ---------- market data ----------
    def klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        raw = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df

    def mark_price(self, symbol: str) -> float:
        return float(self.client.futures_mark_price(symbol=symbol)["markPrice"])

    # ---------- account / positions ----------
    def wallet_balance_usdt(self) -> float:
        for b in self.client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    def position(self, symbol: str) -> Dict[str, Any]:
        data = self.client.futures_position_information(symbol=symbol)
        for p in data:
            if p["symbol"] == symbol:
                return p
        return {}

    def has_open_position(self, symbol: str) -> bool:
        p = self.position(symbol)
        return bool(p) and float(p.get("positionAmt", 0)) != 0.0

    # ---------- configuration ----------
    def is_hedge_mode(self) -> bool:
        try:
            return bool(self.client.futures_get_position_mode().get("dualSidePosition"))
        except BinanceAPIException as e:
            log.warning("position mode lookup failed: %s", e)
            return False

    def set_leverage(self, symbol: str, leverage: int) -> None:
        if self._leverage_cache.get(symbol) == leverage:
            return
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            self._leverage_cache[symbol] = leverage
        except BinanceAPIException as e:
            log.warning("set_leverage(%s,%s) failed: %s", symbol, leverage, e)

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
        except BinanceAPIException as e:
            # -4046 = no need to change margin type (already set)
            if getattr(e, "code", None) != -4046:
                log.warning("set_margin_type(%s,%s) failed: %s", symbol, margin_type, e)

    # ---------- orders ----------
    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False,
                     position_side: Optional[str] = None) -> Dict[str, Any]:
        params = dict(symbol=symbol, side=side, type="MARKET", quantity=qty)
        if position_side:
            params["positionSide"] = position_side
        elif reduce_only:
            params["reduceOnly"] = "true"
        return self.client.futures_create_order(**params)

    def stop_market(self, symbol: str, side: str, stop_price: float, close_position: bool = True,
                    qty: Optional[float] = None,
                    position_side: Optional[str] = None) -> Dict[str, Any]:
        """Place a STOP_MARKET. Some Binance accounts reject the
        `closePosition=true` variant with -4120 ("use Algo Order API") when
        extra parameters like timeInForce/workingType are included — so we
        send the minimal param set and fall back to a plain reduce-only
        stop with explicit quantity if the closePosition form is refused.
        """
        stop_price = self.round_price(symbol, stop_price)

        def _place(close_pos: bool) -> Dict[str, Any]:
            params = dict(
                symbol=symbol,
                side=side,
                type="STOP_MARKET",
                stopPrice=stop_price,
            )
            if position_side:
                params["positionSide"] = position_side
            if close_pos:
                params["closePosition"] = "true"
            else:
                if qty is None or qty <= 0:
                    raise ValueError("quantity required for non-closePosition stop")
                params["quantity"] = qty
                if not position_side:  # in hedge mode, reduceOnly is implicit
                    params["reduceOnly"] = "true"
            return self.client.futures_create_order(**params)

        try:
            return _place(close_position)
        except BinanceAPIException as e:
            # -4120: "use Algo Order API" — retry with explicit qty instead
            # -1106: "parameter ... sent when not required" — same fix
            if getattr(e, "code", None) in (-4120, -1106) and close_position and qty and qty > 0:
                log.warning(
                    "stop_market closePosition rejected (%s); retrying with quantity-based reduce-only",
                    e,
                )
                return _place(close_pos=False)
            raise

    def cancel_all(self, symbol: str) -> None:
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except BinanceAPIException as e:
            log.warning("cancel_all(%s) failed: %s", symbol, e)

    def open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        return self.client.futures_get_open_orders(symbol=symbol)
