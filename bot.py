"""Entry point. Runs the scan loop across all configured symbols."""
from __future__ import annotations

import logging
import signal
import sys
import time
import traceback

from config import Config
from exchange import FuturesExchange
from state import Store
from telegram_notifier import TelegramNotifier
from trader import SymbolTrader


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    _setup_logging()
    log = logging.getLogger("bot")
    cfg = Config()
    try:
        cfg.validate()
    except Exception as e:  # noqa: BLE001
        log.error("Config error: %s", e)
        return 2

    tg = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
    ex = FuturesExchange(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)
    store = Store()

    try:
        bal = ex.wallet_balance_usdt()
    except Exception as e:  # noqa: BLE001
        tg.event("Startup error", f"Could not read wallet balance: {e}", "❌")
        log.error("Startup failed: %s", e)
        return 3

    tg.event(
        "Tori Trendline bot online",
        f"Mode: {'TESTNET' if cfg.testnet else 'LIVE'}  DryRun: {cfg.dry_run}\n"
        f"Symbols: {', '.join(cfg.symbols)}\n"
        f"TF: {cfg.timeframe}   Risk: {cfg.risk_per_trade:.2%}\n"
        f"Leverage cap: x{cfg.max_leverage}\n"
        f"Balance: {bal:.2f} USDT",
        "🚀",
    )

    traders = {s: SymbolTrader(s, cfg, ex, tg, store) for s in cfg.symbols}

    stop = {"flag": False}

    def _graceful(_sig, _frm):
        stop["flag"] = True
        tg.event("Shutdown signal received", "Stopping scan loop…", "🛑")

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    while not stop["flag"]:
        for sym, t in traders.items():
            try:
                t.on_tick()
            except Exception as e:  # noqa: BLE001
                log.exception("tick failed for %s", sym)
                tg.event(
                    "Error during scan",
                    f"{sym}: {e}\n<pre>{traceback.format_exc()[-1500:]}</pre>",
                    "🐛",
                )
        for _ in range(cfg.poll_interval):
            if stop["flag"]:
                break
            time.sleep(1)

    tg.event("Bot stopped cleanly", "", "👋")
    return 0


if __name__ == "__main__":
    sys.exit(main())
