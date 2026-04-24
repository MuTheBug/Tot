# Tori Trade's Trendline Bot — Binance USDT-M Futures

Automated implementation of the **Trendline Strategy** from *Tori Trade's Playbook*
(see `Tori Trades Playbook.pdf` in this repo) for Binance USDT-M perpetual futures.
Sends Telegram alerts for every event and auto-scales leverage so the bot can
still trade on very small accounts.

---

## What the bot does (strategy, verbatim from the playbook)

- Works on the **4-hour** timeframe.
- Draws trendlines from **confirmed swing highs/lows** (fractal pivots).
- Two setups, exactly as in the PDF:
  - **Trendline Bounce** — price touches and respects an established trendline
    (≥ 3 clean touchpoints by default, ≥ 1 week of data between first
    touchpoint and entry = 42 bars on 4h).
    *Action line = Safety line*: stop is the trendline itself; it is trailed
    along the line and below each new valid swing.
  - **Trendline Break (2-TP / 3-TP)** — price **closes through** an established
    trendline. Action line is the broken line; a new **opposing** trendline is
    drawn as the safety line. Invalidated on a close back through the safety line.
- Exit rule for both setups: close the position immediately when price **closes
  through** the safety line.

All signal logic lives in `strategy.py`; the orchestration/trading lives in
`trader.py`.

## Small-capital friendly

Position size is derived from **risk % × balance ÷ stop distance** (default 1%
risk). If the resulting notional would be below the exchange minimum (5 USDT
on Binance Futures for most symbols), the bot:

1. Rounds quantity up so min-notional is cleared.
2. Picks the **smallest leverage** such that the required margin fits the
   wallet balance, up to `MAX_LEVERAGE` (default 50×).

So on a $10 account the bot can still place the trade by increasing leverage —
while actual dollar loss on a stop-out stays close to the risk budget
(because the stop is *price*-defined, not margin-defined).

> Running on very high leverage is risky. Tune `MAX_LEVERAGE` and
> `RISK_PER_TRADE` to what you are comfortable losing. **Start on testnet.**

## Telegram alerts for every event

Alerts are sent on:
- startup & shutdown
- new trendline signal (side, setup, entry, stop, qty, leverage, margin, $-risk)
- entry order filled / failed
- stop-loss placed / failed
- trailing stop moved
- position closed (stop hit, rule exit, or manual close)
- scan errors (with traceback)

## Layout

```
bot.py                  # entry point + scan loop
config.py               # env-driven config
exchange.py             # python-binance futures wrapper
strategy.py             # pivots, trendlines, bounce/break signals
risk.py                 # sizing + dynamic leverage
trader.py               # per-symbol orchestration + trailing stop
telegram_notifier.py    # HTML Telegram sender
state.py                # JSON persistence of open trades
requirements.txt
.env.example
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your keys
python bot.py
```

### Telegram

1. Talk to **@BotFather** on Telegram → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<token>/getUpdates`, copy the `chat.id` into
   `TELEGRAM_CHAT_ID`.

### Binance

- Create an API key at <https://www.binance.com/en/my/settings/api-management>
  with **Futures** enabled (no withdrawals).
- For safety, test on `BINANCE_TESTNET=true` first
  (<https://testnet.binancefuture.com>).

## Configuration knobs (see `.env.example`)

| Variable | Meaning | Default |
|---|---|---|
| `SYMBOLS` | USDT-M perps to scan | `BTCUSDT,ETHUSDT,SOLUSDT` |
| `TIMEFRAME` | Candle size | `4h` |
| `RISK_PER_TRADE` | % of wallet to risk on stop | `0.01` |
| `MAX_LEVERAGE` | Cap used when auto-scaling leverage | `50` |
| `MARGIN_TYPE` | `ISOLATED` or `CROSSED` | `ISOLATED` |
| `PIVOT_LOOKBACK` | Bars each side to confirm a swing | `3` |
| `BOUNCE_MIN_TOUCHES` | Touchpoints required for a bounce | `3` |
| `BREAK_MIN_TOUCHES` | Touchpoints required for a break | `2` |
| `MIN_BARS_BETWEEN_TOUCHES` | First touch → entry min distance | `42` (= 1 week on 4h) |
| `TRENDLINE_TOLERANCE` | Touch distance vs. line (fraction) | `0.0035` |
| `POLL_INTERVAL` | Seconds between scans | `60` |
| `DRY_RUN` | `true` = alerts only, no orders | `false` |

## Disclaimer

This is an educational implementation of a publicly available discretionary
strategy. Trendline drawing by an algorithm is approximate — reasonable people
will disagree on the "right" line. Futures trading with leverage can destroy
your account quickly. **Run on testnet first, keep `RISK_PER_TRADE` small,
and never trade with money you cannot afford to lose.**
