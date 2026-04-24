"""Render annotated candlestick chart PNGs for Telegram alerts."""
from __future__ import annotations

import io
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.dates as mdates
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from strategy import Signal, Trendline


_BULL = "#26a69a"
_BEAR = "#ef5350"
_ACTION = "#2962ff"
_SAFETY = "#ff6d00"
_ENTRY_LINE = "#212121"
_STOP_LINE = "#d50000"


def _draw_candles(ax, sub: pd.DataFrame, xs: np.ndarray) -> None:
    width = 0.6
    for x, (_, row) in zip(xs, sub.iterrows()):
        color = _BULL if row["close"] >= row["open"] else _BEAR
        ax.plot([x, x], [row["low"], row["high"]], color=color, linewidth=0.7, zorder=2)
        body_lo = min(row["open"], row["close"])
        body_hi = max(row["open"], row["close"])
        rect = patches.Rectangle(
            (x - width / 2, body_lo),
            width,
            max(body_hi - body_lo, (row["high"] - row["low"]) * 1e-3 or 1e-9),
            facecolor=color, edgecolor=color, zorder=3,
        )
        ax.add_patch(rect)


def _draw_trendline(ax, line: Trendline, x_end: int, color: str, label: str,
                    marker: str) -> None:
    xs = np.array([line.first_idx, x_end], dtype=float)
    ys = line.slope * xs + line.intercept
    ax.plot(xs, ys, color=color, linewidth=2.0, label=label, zorder=4)
    for tp in line.touches:
        ax.plot(tp.idx, tp.price, marker, color=color,
                markersize=8, markeredgecolor="white", markeredgewidth=1.0,
                zorder=6)


def render_signal_chart(df: pd.DataFrame, sig: Signal, symbol: str,
                        timeframe: str, recent_n: int = 120) -> bytes:
    """Return PNG bytes for the signal: action+safety lines, touchpoints,
    entry & stop markers."""
    n = len(df)
    start = max(0, n - recent_n)
    sub = df.iloc[start:].copy()
    xs = np.arange(start, n)
    last_x = n - 1

    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=120)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#fafafa")

    _draw_candles(ax, sub, xs)

    # Action line (entry trigger).
    _draw_trendline(ax, sig.action_line, last_x, _ACTION,
                    f"Action line ({len(sig.action_line.touches)} touches)", "o")

    # Safety line — only draw separately if it isn't literally the action line.
    if sig.safety_line is not sig.action_line:
        _draw_trendline(ax, sig.safety_line, last_x, _SAFETY,
                        f"Safety line ({len(sig.safety_line.touches)} touches)", "s")

    # Entry & stop horizontal markers.
    ax.axhline(sig.entry_price, color=_ENTRY_LINE, linestyle="--", linewidth=1, alpha=0.55)
    ax.axhline(sig.stop_price, color=_STOP_LINE, linestyle="--", linewidth=1, alpha=0.7)

    # Right-edge labels.
    ax.text(last_x + 0.4, sig.entry_price, f" Entry {sig.entry_price:.6g}",
            va="center", fontsize=9, color=_ENTRY_LINE)
    ax.text(last_x + 0.4, sig.stop_price, f" SL {sig.stop_price:.6g}",
            va="center", fontsize=9, color=_STOP_LINE)

    # Entry marker arrow.
    arrow_color = "#1b5e20" if sig.side == "BUY" else "#b71c1c"
    span = sub["high"].max() - sub["low"].min() or sig.entry_price * 0.01
    dy = span * 0.06 * (1 if sig.side == "BUY" else -1)
    ax.annotate(
        sig.side,
        xy=(last_x, sig.entry_price),
        xytext=(last_x, sig.entry_price - dy * 2.0),
        ha="center", color=arrow_color, fontsize=11, fontweight="bold",
        arrowprops=dict(arrowstyle="->", color=arrow_color, lw=1.8),
        zorder=7,
    )

    title = f"{symbol}  ·  {timeframe}  ·  {sig.side}  ·  {sig.setup}"
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel("Bar index")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.85)
    ax.grid(True, alpha=0.18, linewidth=0.6)
    ax.set_xlim(start - 1, last_x + 6)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def render_exit_chart(df: pd.DataFrame, *,
                       symbol: str, timeframe: str, side: str, setup: str,
                       entry_price: float, exit_price: float, stop_price: float,
                       line_kind: str, line_slope: float, line_intercept: float,
                       opened_bar_idx: int,
                       recent_n: int = 120) -> bytes:
    """Render the exit chart: entry → exit, with the safety trendline drawn."""
    n = len(df)
    start = max(0, n - recent_n)
    sub = df.iloc[start:].copy()
    xs = np.arange(start, n)
    last_x = n - 1

    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=120)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#fafafa")

    _draw_candles(ax, sub, xs)

    # Safety / action trendline used while the trade was live.
    line_color = _ACTION if line_kind == "up" else _SAFETY
    line_xs = np.array([max(start, opened_bar_idx - 5), last_x], dtype=float)
    line_ys = line_slope * line_xs + line_intercept
    ax.plot(line_xs, line_ys, color=line_color, linewidth=2.0, zorder=4,
            label="Safety line (followed)")

    # Entry / exit / stop levels.
    ax.axhline(entry_price, color=_ENTRY_LINE, linestyle="--", linewidth=1, alpha=0.55)
    ax.axhline(stop_price, color=_STOP_LINE, linestyle="--", linewidth=1, alpha=0.7)

    if start <= opened_bar_idx <= last_x:
        ax.scatter([opened_bar_idx], [entry_price], color="#1b5e20" if side == "LONG" else "#b71c1c",
                   marker="^" if side == "LONG" else "v", s=140, zorder=7,
                   edgecolor="white", linewidth=1.4, label=f"Entry ({side})")
    ax.scatter([last_x], [exit_price], color="black", marker="X", s=140,
               zorder=7, edgecolor="white", linewidth=1.4, label="Exit")

    pnl = (exit_price - entry_price) if side == "LONG" else (entry_price - exit_price)
    pnl_pct = pnl / entry_price * 100
    title = (f"{symbol}  ·  {timeframe}  ·  {side} {setup}  ·  "
             f"PnL {pnl:+.6g} ({pnl_pct:+.2f}%)")
    ax.set_title(title, fontsize=13, fontweight="bold",
                 color=("#1b5e20" if pnl > 0 else "#b71c1c"))
    ax.set_xlabel("Bar index")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.85)
    ax.grid(True, alpha=0.18, linewidth=0.6)
    ax.set_xlim(start - 1, last_x + 6)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
