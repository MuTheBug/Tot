"""Orchestrates scanning, entries, trailing stops and exits per symbol."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config import Config
from exchange import FuturesExchange
from risk import calc_sizing
from state import ManagedTrade, Store
from strategy import Signal, Trendline, detect_signal, find_pivots
from telegram_notifier import TelegramNotifier

log = logging.getLogger(__name__)


class SymbolTrader:
    def __init__(self, symbol: str, cfg: Config, ex: FuturesExchange,
                 tg: TelegramNotifier, store: Store):
        self.symbol = symbol
        self.cfg = cfg
        self.ex = ex
        self.tg = tg
        self.store = store
        self._last_bar_open: Optional[pd.Timestamp] = None
        self._bootstrap_symbol()

    def _bootstrap_symbol(self) -> None:
        try:
            self.ex.set_margin_type(self.symbol, self.cfg.margin_type)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: margin type setup failed: %s", self.symbol, e)

    # --------- main tick ---------
    def on_tick(self) -> None:
        df = self.ex.klines(self.symbol, self.cfg.timeframe, self.cfg.candle_limit)
        if df.empty:
            return
        # Work on CLOSED bars only: drop the still-forming last candle if its
        # close_time is in the future.
        now = pd.Timestamp.now(tz="UTC")
        if df.iloc[-1]["close_time"] > now:
            df = df.iloc[:-1]
        if df.empty:
            return

        latest_bar_open = df.iloc[-1]["open_time"]
        new_bar = latest_bar_open != self._last_bar_open
        self._last_bar_open = latest_bar_open

        # Always manage an existing position (trail stop / detect exits) even
        # if no new bar just closed.
        trade = self.store.get(self.symbol)
        if trade is not None:
            self._manage_open_trade(df, trade)

        # Only evaluate new entries on a freshly closed bar and if flat.
        if not new_bar:
            return
        if self.ex.has_open_position(self.symbol):
            return

        sig = detect_signal(
            df,
            pivot_lookback=self.cfg.pivot_lookback,
            bounce_min_touches=self.cfg.bounce_min_touches,
            break_min_touches=self.cfg.break_min_touches,
            min_bars_span=self.cfg.min_bars_between_touches,
            tolerance_frac=self.cfg.trendline_tolerance,
        )
        if sig:
            self._enter(df, sig)

    # --------- entries ---------
    def _enter(self, df: pd.DataFrame, sig: Signal) -> None:
        filt = self.ex.symbol_filters(self.symbol)
        try:
            balance = self.ex.wallet_balance_usdt()
        except Exception as e:  # noqa: BLE001
            self.tg.event("Balance lookup failed", f"{self.symbol}: {e}", "⚠️")
            return

        entry = sig.entry_price
        stop = sig.stop_price
        if sig.side == "BUY" and stop >= entry:
            return  # invalid geometry
        if sig.side == "SELL" and stop <= entry:
            return

        try:
            sizing = calc_sizing(
                balance_usdt=balance,
                entry_price=entry,
                stop_price=stop,
                risk_fraction=self.cfg.risk_per_trade,
                min_notional=float(filt["minNotional"]),
                step_size=float(filt["stepSize"]),
                min_qty=float(filt["minQty"]),
                min_leverage=self.cfg.min_leverage,
                max_leverage=self.cfg.max_leverage,
            )
        except Exception as e:  # noqa: BLE001
            self.tg.event("Sizing failed", f"{self.symbol}: {e}", "⚠️")
            return

        qty = self.ex.round_qty(self.symbol, sizing.quantity)
        if qty <= 0:
            self.tg.event(
                "Trade skipped — qty rounds to 0",
                f"{self.symbol} bal={balance:.2f} risk={self.cfg.risk_per_trade:.2%}",
                "⚠️",
            )
            return

        side_order = "BUY" if sig.side == "BUY" else "SELL"
        opp = "SELL" if side_order == "BUY" else "BUY"
        pos_side = None
        if self.cfg.hedge_mode:
            pos_side = "LONG" if side_order == "BUY" else "SHORT"

        body = (
            f"Symbol: <b>{self.symbol}</b>\n"
            f"Setup: <b>{sig.setup}</b>  ({sig.note})\n"
            f"Side: <b>{sig.side}</b>  TF: {self.cfg.timeframe}\n"
            f"Entry: {entry:.6g}\n"
            f"Stop (safety line): {stop:.6g}\n"
            f"Qty: {qty}  Notional: {sizing.notional:.2f} USDT\n"
            f"Leverage: <b>x{sizing.leverage}</b>  Margin: {sizing.margin:.2f} USDT\n"
            f"$ risk if SL: {sizing.risk_usdt:.2f} USDT ({self.cfg.risk_per_trade:.2%} of balance)"
        )
        self.tg.event("Signal", body, "🎯")

        if self.cfg.dry_run:
            self.tg.event("DRY RUN — not placing order", self.symbol, "🧪")
            return

        # Configure leverage on the symbol before placing the order.
        self.ex.set_leverage(self.symbol, sizing.leverage)

        # Entry MARKET order
        try:
            self.ex.market_order(self.symbol, side_order, qty, position_side=pos_side)
        except Exception as e:  # noqa: BLE001
            self.tg.event("Entry order FAILED", f"{self.symbol}: {e}", "❌")
            return
        self.tg.event("Entry filled", f"{self.symbol} {side_order} {qty} @~{entry:.6g}", "✅")

        # Protective stop at the safety line. Pass qty so the exchange
        # wrapper can fall back to a reduce-only stop if the
        # closePosition variant is rejected with -4120 / -1106. If the
        # exchange refuses STOP_MARKET entirely, stop_market() returns
        # None and we manage the stop in software on subsequent ticks.
        software_stop = False
        try:
            sl_order = self.ex.stop_market(
                self.symbol, opp,
                stop_price=stop, close_position=True,
                qty=qty, position_side=pos_side,
            )
            if sl_order is None:
                software_stop = True
        except Exception as e:  # noqa: BLE001
            self.tg.event("Stop-loss placement FAILED — closing position", f"{self.symbol}: {e}", "❌")
            try:
                self.ex.market_order(
                    self.symbol, opp, qty,
                    reduce_only=not self.cfg.hedge_mode,
                    position_side=pos_side,
                )
            except Exception:
                pass
            return
        if software_stop:
            self.tg.event(
                "Software stop armed",
                f"{self.symbol} SL @ {stop:.6g} — exchange refused STOP_MARKET, "
                "bot will market-close on mark-price breach.",
                "🛡️",
            )
        else:
            self.tg.event("Stop placed", f"{self.symbol} SL @ {stop:.6g}", "🛡️")

        # Persist trade + trendline for trailing
        line = sig.safety_line
        mt = ManagedTrade(
            symbol=self.symbol,
            side="LONG" if side_order == "BUY" else "SHORT",
            setup=sig.setup,
            entry_price=entry,
            stop_price=stop,
            quantity=qty,
            leverage=sizing.leverage,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
            line_kind=line.kind,
            line_slope=line.slope,
            line_intercept=line.intercept,
            line_first_idx=line.first_idx,
            opened_bar_idx=len(df) - 1,
            software_stop=software_stop,
        )
        self.store.put(mt)

    # --------- managing an open trade ---------
    def _manage_open_trade(self, df: pd.DataFrame, trade: ManagedTrade) -> None:
        # If exchange reports no open position, the stop must have fired (or
        # the user intervened).  Clean up local state + alert.
        if not self.ex.has_open_position(self.symbol):
            pnl_hint = self._rough_pnl(trade, float(df.iloc[-1]["close"]))
            self.tg.event(
                "Position closed",
                f"{self.symbol} {trade.side} entry={trade.entry_price:.6g} "
                f"last={df.iloc[-1]['close']:.6g}\nEst. PnL: {pnl_hint:+.2f} USDT",
                "🏁",
            )
            self.ex.cancel_all(self.symbol)
            self.store.drop(self.symbol)
            return

        # Software-managed stop: poll mark price against the stored
        # stop_price and market-close if breached. This is the fallback
        # for accounts that reject STOP_MARKET entirely.
        if trade.software_stop:
            try:
                mark = self.ex.mark_price(self.symbol)
            except Exception:  # noqa: BLE001
                mark = float(df.iloc[-1]["close"])
            breached = (trade.side == "LONG" and mark <= trade.stop_price) or \
                       (trade.side == "SHORT" and mark >= trade.stop_price)
            if breached:
                self.tg.event(
                    "Software stop triggered",
                    f"{self.symbol} {trade.side} mark={mark:.6g} SL={trade.stop_price:.6g}",
                    "🛑",
                )
                self._close_now(trade)
                return

        # Check whether the ACTION LINE has been violated on close (break
        # setup exit rule & bounce exit rule both say: close through the line
        # = exit immediately).
        last = df.iloc[-1]
        last_idx = len(df) - 1
        line_val = trade.line_slope * last_idx + trade.line_intercept

        violated = False
        if trade.side == "LONG" and last["close"] < line_val * (1 - 0.0005):
            violated = True
        if trade.side == "SHORT" and last["close"] > line_val * (1 + 0.0005):
            violated = True
        if violated:
            self.tg.event(
                "Safety line closed through — exiting",
                f"{self.symbol} {trade.side} close={last['close']:.6g} line={line_val:.6g}",
                "🚪",
            )
            self._close_now(trade)
            return

        # Trail stop: move it to the line value at the most-recent bar,
        # but ONLY in the favorable direction (never loosen risk).
        new_stop = line_val
        # Snap to the nearest relevant swing to stay "below the new swing low"
        pivots = find_pivots(df.iloc[:-1], lookback=self.cfg.pivot_lookback)
        if trade.side == "LONG":
            lows = [p.price for p in pivots if p.kind == "low" and p.idx >= trade.opened_bar_idx]
            if lows:
                new_stop = max(new_stop, min(lows) * 0.999)
            new_stop = max(new_stop, trade.stop_price)   # only raise
        else:
            highs = [p.price for p in pivots if p.kind == "high" and p.idx >= trade.opened_bar_idx]
            if highs:
                new_stop = min(new_stop, max(highs) * 1.001)
            new_stop = min(new_stop, trade.stop_price)   # only lower (tighter for short)

        new_stop = self.ex.round_price(self.symbol, new_stop)
        # Only move the SL if it changed meaningfully
        if abs(new_stop - trade.stop_price) / max(trade.stop_price, 1e-9) <= 0.001:
            return

        if trade.software_stop:
            # No exchange order to replace — just update internal state
            old = trade.stop_price
            trade.stop_price = new_stop
            self.store.put(trade)
            self.tg.event(
                "Trail stop updated (software)",
                f"{self.symbol} {trade.side} SL: {old:.6g} → {new_stop:.6g}",
                "🧵",
            )
            return

        opp = "SELL" if trade.side == "LONG" else "BUY"
        pos_side = trade.side if self.cfg.hedge_mode else None
        try:
            self.ex.cancel_all(self.symbol)
            result = self.ex.stop_market(
                self.symbol, opp,
                stop_price=new_stop, close_position=True,
                qty=trade.quantity, position_side=pos_side,
            )
        except Exception as e:  # noqa: BLE001
            self.tg.event("Trail stop update FAILED", f"{self.symbol}: {e}", "⚠️")
            return
        # If the wrapper returned None here, the exchange refused STOP_MARKET
        # entirely — switch this trade to software-managed from now on.
        if result is None:
            trade.software_stop = True
            self.tg.event(
                "Switched to software stop",
                f"{self.symbol} SL will be enforced by the bot in software.",
                "🛡️",
            )
        old = trade.stop_price
        trade.stop_price = new_stop
        self.store.put(trade)
        self.tg.event(
            "Trail stop updated",
            f"{self.symbol} {trade.side} SL: {old:.6g} → {new_stop:.6g}",
            "🧵",
        )

    def _close_now(self, trade: ManagedTrade) -> None:
        opp = "SELL" if trade.side == "LONG" else "BUY"
        pos_side = trade.side if self.cfg.hedge_mode else None
        try:
            self.ex.cancel_all(self.symbol)
            self.ex.market_order(
                self.symbol, opp, trade.quantity,
                reduce_only=not self.cfg.hedge_mode,
                position_side=pos_side,
            )
        except Exception as e:  # noqa: BLE001
            self.tg.event("Manual close FAILED", f"{self.symbol}: {e}", "❌")
            return
        self.tg.event("Position closed by rule", self.symbol, "🏁")
        self.store.drop(self.symbol)

    def _rough_pnl(self, trade: ManagedTrade, last_price: float) -> float:
        if trade.side == "LONG":
            return (last_price - trade.entry_price) * trade.quantity
        return (trade.entry_price - last_price) * trade.quantity
