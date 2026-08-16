"""
SPCX Trend-Reversal Analysis
============================

Detects historical trend-reversal days for a ticker (default: SPCX) using
FOUR independent, price/volume-only methods, then combines them into a
per-day confidence score (how many methods agree). Also runs a simple
backtest of "enter on the flagged day, hold N days" to sanity-check whether
the signal has historically meant anything.

This is NOT a trading system and NOT a prediction of future performance.
It is a descriptive/statistical tool for exploring historical price action.

--------------------------------------------------------------------------
A NOTE ON SPCX SPECIFICALLY (read this before trusting the numbers)
--------------------------------------------------------------------------
As of the date this script was written, SPCX ("Space Exploration
Technologies", NMS) had only ~44 trading days of history (listed
2026-06-12). That is a VERY small sample:
  - A classic 10/30-day MA crossover barely gets 10-15 usable bars after
    the 30-day EMA warms up, so the defaults below are tuned DOWN
    (5/15-day) to leave enough data to actually see crossovers.
  - Expect single-digit numbers of ZigZag/extrema reversal events. Win
    rates, average returns, and "method accuracy" percentages computed
    from a handful of trades are NOT statistically reliable -- treat them
    as descriptive, not predictive, until more history accumulates.
  - Re-run this script periodically (e.g. weekly/monthly) as more bars
    arrive; the confidence-score and backtest numbers will firm up.

Every threshold below is a CLI flag -- see `python reversal_analysis.py -h`.
Defaults are set for SPCX's current short history; if you point this at a
ticker with years of history, bump MA windows back up toward 10/30,
extrema `order` up toward 5-10, and ZigZag threshold as you see fit.

--------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------
    python reversal_analysis.py
    python reversal_analysis.py --ticker SPCX --zigzag-pct 8 --hold-days 5
    python reversal_analysis.py --ticker AAPL --ma-fast 10 --ma-slow 30 \
        --extrema-order 5

Outputs (written to ./output/<TICKER>/):
    reversals.csv        one row per detected reversal, with confidence score
    backtest_trades.csv  one row per simulated trade
    chart.png            price chart with reversal points marked
    summary.txt          human-readable summary stats
"""

from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency: pip install yfinance pandas numpy scipy matplotlib")

try:
    from scipy.signal import argrelextrema
except ImportError:
    sys.exit("Missing dependency: pip install scipy")

import matplotlib

matplotlib.use("Agg")  # headless-safe backend; script saves PNGs, doesn't pop windows
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ==========================================================================
# 1. DATA LOADING
# ==========================================================================

def load_ohlcv(ticker: str, period: str = "max", interval: str = "1d") -> pd.DataFrame:
    """
    Pull daily OHLCV history from yfinance and clean it up.

    Gap handling:
      - Non-trading days (weekends/holidays) are NOT gaps to fill -- they
        simply don't exist in the trading calendar, so we leave them out
        rather than interpolating fake prices for them.
      - Rows yfinance returns with NaN OHLC (halts, delistings, data
        outages) are dropped, not interpolated -- fabricating a price for
        a halted day would corrupt reversal detection.
      - Zero-volume rows are flagged but kept unless OHLC is also NaN,
        since some legitimately low-liquidity days do trade a few shares.
      - The index is de-duplicated, sorted ascending, and timezone info is
        stripped (yfinance returns tz-aware indices which are awkward to
        compare against plain dates).
    """
    raw = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if raw.empty:
        sys.exit(f"No data returned for ticker '{ticker}'. Check the symbol.")

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    before = len(df)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    dropped = before - len(df)
    if dropped:
        print(f"[data] dropped {dropped} row(s) with missing OHLC (halts/outages)")

    zero_vol = int((df["Volume"] == 0).sum())
    if zero_vol:
        print(f"[data] note: {zero_vol} row(s) have zero reported volume")

    if len(df) < 20:
        print(
            f"[data] WARNING: only {len(df)} usable trading days for {ticker}. "
            "Reversal/backtest stats below will be extremely low-confidence."
        )

    return df


# ==========================================================================
# 2. METHOD 1 -- ZIGZAG INDICATOR
# ==========================================================================
# Flags a pivot (swing high or swing low) once price has retraced X% from
# the running extreme since the last confirmed pivot. This is the classic
# ZigZag construction: track a running high/low; once price reverses by
# `pct` percent from that running extreme, the extreme is confirmed as a
# pivot and the running extreme resets in the opposite direction.

@dataclass
class ZigZagPivot:
    idx: int           # positional index of the pivot itself (the actual high/low bar)
    kind: str          # 'high' or 'low'
    price: float
    confirm_idx: int = -1      # bar index where the `pct` retrace was DETECTED --
                                # i.e. the earliest a real-time observer could have
                                # known this pivot happened. Always >= idx, often
                                # several bars later. Use this, not idx, for backtesting.
    provisional: bool = False  # True for the trailing, not-yet-confirmed pivot


def compute_zigzag(close: pd.Series, pct: float) -> list[ZigZagPivot]:
    prices = close.to_numpy()
    n = len(prices)
    if n < 2:
        return []

    # --- Phase 1: establish the initial trend direction from bar 0 -----
    anchor_p = prices[0]
    run_max_i, run_max_p = 0, prices[0]
    run_min_i, run_min_p = 0, prices[0]
    trend = 0
    start_i = n  # if we never find a trend, loop below just won't run

    for i in range(1, n):
        if prices[i] > run_max_p:
            run_max_p, run_max_i = prices[i], i
        if prices[i] < run_min_p:
            run_min_p, run_min_i = prices[i], i

        up_move = (run_max_p - anchor_p) / anchor_p * 100
        down_move = (anchor_p - run_min_p) / anchor_p * 100

        if up_move >= pct and up_move >= down_move:
            trend = 1
            extreme_i, extreme_p = run_max_i, run_max_p
            start_i = i + 1
            break
        if down_move >= pct and down_move >= up_move:
            trend = -1
            extreme_i, extreme_p = run_min_i, run_min_p
            start_i = i + 1
            break

    if trend == 0:
        return []  # price never moved `pct`% in either direction -- no pivots

    # the initial pivot (bar 0) is itself only "known" once the first `pct`
    # move away from it has been detected, at start_i - 1
    pivots: list[ZigZagPivot] = [ZigZagPivot(idx=0, kind=("low" if trend == 1 else "high"),
                                              price=prices[0], confirm_idx=start_i - 1)]

    # --- Phase 2: walk forward, confirming pivots on each `pct` retrace -
    for i in range(start_i, n):
        p = prices[i]
        if trend == 1:  # currently in an up-leg, tracking a running high
            if p > extreme_p:
                extreme_p, extreme_i = p, i
            else:
                retrace = (extreme_p - p) / extreme_p * 100
                if retrace >= pct:
                    pivots.append(ZigZagPivot(idx=extreme_i, kind="high", price=extreme_p, confirm_idx=i))
                    trend = -1
                    extreme_p, extreme_i = p, i
        else:  # currently in a down-leg, tracking a running low
            if p < extreme_p:
                extreme_p, extreme_i = p, i
            else:
                retrace = (p - extreme_p) / extreme_p * 100
                if retrace >= pct:
                    pivots.append(ZigZagPivot(idx=extreme_i, kind="low", price=extreme_p, confirm_idx=i))
                    trend = 1
                    extreme_p, extreme_i = p, i

    # trailing running extreme, not yet confirmed by a `pct` retrace --
    # useful to see on the chart but excluded from confidence scoring
    pivots.append(ZigZagPivot(idx=extreme_i, kind=("high" if trend == 1 else "low"),
                               price=extreme_p, confirm_idx=n - 1, provisional=True))
    return pivots


# ==========================================================================
# 3. METHOD 2 -- MOVING AVERAGE CROSSOVER
# ==========================================================================

def compute_ma_crossover(close: pd.Series, fast: int, slow: int, ma_type: str = "ema") -> pd.DataFrame:
    """
    Returns a DataFrame of crossover events: index=date, columns=
    ['direction'] where 'up' = fast crossed above slow (bullish flip),
    'down' = fast crossed below slow (bearish flip).

    Crossovers occurring before the slow window has fully warmed up
    (idx < slow) are dropped -- the slow average isn't a reliable trend
    proxy until it has `slow` bars behind it.
    """
    if ma_type == "sma":
        fast_ma = close.rolling(fast).mean()
        slow_ma = close.rolling(slow).mean()
    else:
        fast_ma = close.ewm(span=fast, adjust=False).mean()
        slow_ma = close.ewm(span=slow, adjust=False).mean()

    diff = fast_ma - slow_ma
    sign = np.sign(diff)
    flips = sign.diff().fillna(0)

    events = []
    for i, (date, flip) in enumerate(flips.items()):
        if i < slow:  # warm-up guard
            continue
        if flip > 0:
            events.append((date, "up"))
        elif flip < 0:
            events.append((date, "down"))

    return pd.DataFrame(events, columns=["date", "direction"]).set_index("date")


# ==========================================================================
# 4. METHOD 3 -- LOCAL EXTREMA (SWING HIGH/LOW)
# ==========================================================================

def compute_local_extrema(df: pd.DataFrame, order: int) -> pd.DataFrame:
    """
    Uses scipy.signal.argrelextrema on High (for swing highs) and Low
    (for swing lows) -- using the actual traded extremes rather than
    Close is more faithful to "swing high/low" as chartists use the term.

    `order` = number of bars on each side that must be lower/higher for a
    point to count as a local extreme. Smaller order -> more, noisier
    signals; larger order -> fewer, more significant ones.

    IMPORTANT: like ZigZag, this is retrospective. A bar at index i can
    only be confirmed as a local extremum once `order` bars AFTER it have
    also been observed -- so the earliest real-time confirmation is
    idx + order, not idx. That lag is recorded in the 'confirm_idx' column
    and used by the backtest instead of the raw event date.
    """
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    n = len(df)

    high_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    low_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    # de-duplicate plateaus (argrelextrema with *_equal can return runs of
    # equal neighboring values) by collapsing consecutive indices
    def _collapse(idxs):
        out = []
        for i in idxs:
            if out and i - out[-1] <= order:
                continue
            out.append(int(i))
        return out

    high_idx = _collapse(list(high_idx))
    low_idx = _collapse(list(low_idx))

    events = [(df.index[i], "down", df["High"].iloc[i], min(i + order, n - 1)) for i in high_idx]
    events += [(df.index[i], "up", df["Low"].iloc[i], min(i + order, n - 1)) for i in low_idx]
    out = pd.DataFrame(events, columns=["date", "direction", "price", "confirm_idx"]).set_index("date")
    return out.sort_index()


# ==========================================================================
# 5. METHOD 4 -- MOMENTUM SHIFT (RSI zone-turn + MACD histogram cross)
# ==========================================================================

def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)  # neutral where undefined (e.g. avg_loss == 0)


def compute_macd(close: pd.Series, fast: int, slow: int, signal: int):
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_momentum_shift(df: pd.DataFrame, rsi_period: int, macd_fast: int,
                            macd_slow: int, macd_signal: int,
                            rsi_overbought: float = 70, rsi_oversold: float = 30) -> pd.DataFrame:
    """
    Two independent momentum triggers, unioned into one 'momentum' signal
    set (this is method 4 -- a secondary/confirmation signal, not a
    primary pivot detector):

      (a) RSI zone-turn: RSI rises above `rsi_overbought` then turns down
          -> bearish shift; RSI falls below `rsi_oversold` then turns up
          -> bullish shift.
      (b) MACD histogram zero-cross: signal line crosses the MACD line ->
          momentum flip in that direction.

    NOTE: with SPCX's short history, the 26-bar slow EMA in MACD barely
    warms up -- expect few/noisy MACD signals until more data accumulates.
    """
    rsi = compute_rsi(df["Close"], rsi_period)
    _, _, hist = compute_macd(df["Close"], macd_fast, macd_slow, macd_signal)
    n = len(df)
    rsi_order = 2  # bars each side for RSI turning-point detection

    events = []  # (date, direction, confirm_idx)

    # RSI zone-turns: find local peaks/troughs of RSI, keep only those
    # inside the overbought/oversold zone. Like local extrema, a turning
    # point at i needs `rsi_order` bars AFTER it to be confirmed in real time.
    rsi_vals = rsi.to_numpy()
    peak_idx = argrelextrema(rsi_vals, np.greater, order=rsi_order)[0]
    trough_idx = argrelextrema(rsi_vals, np.less, order=rsi_order)[0]
    for i in peak_idx:
        if rsi_vals[i] >= rsi_overbought:
            events.append((df.index[i], "down", min(i + rsi_order, n - 1)))
    for i in trough_idx:
        if rsi_vals[i] <= rsi_oversold:
            events.append((df.index[i], "up", min(i + rsi_order, n - 1)))

    # MACD histogram sign flips (skip the slow-EMA warm-up window). Unlike
    # RSI turns, a crossover is known the same day it happens -- no lookahead.
    hist_sign = np.sign(hist)
    flips = hist_sign.diff().fillna(0)
    for i, (date, flip) in enumerate(flips.items()):
        if i < macd_slow:
            continue
        if flip > 0:
            events.append((date, "up", i))
        elif flip < 0:
            events.append((date, "down", i))

    out = pd.DataFrame(events, columns=["date", "direction", "confirm_idx"]).set_index("date")
    return out.sort_index()


# ==========================================================================
# 6. COMBINE METHODS INTO CONFIDENCE-SCORED REVERSAL EVENTS
# ==========================================================================

@dataclass
class Reversal:
    date: pd.Timestamp
    idx: int
    price: float
    prior_direction: str   # 'up' or 'down' -- trend that was ending
    new_direction: str     # the opposite
    pct_move: float        # magnitude of the swing leg into this pivot
    confidence: int = 0    # how many of the 4 methods agree (1-4)
    sources: list = field(default_factory=list)
    confirm_idx: int = -1  # bar index where this event was first knowable in real
                            # time (>= idx). The backtest enters AFTER this, not
                            # after `idx`, since `idx` itself is only identifiable
                            # in hindsight.


def _nearest_within(dates_dir: pd.DataFrame, target_idx: int, direction: str,
                     date_to_idx: dict, window: int) -> bool:
    """True if `dates_dir` has an event of matching `direction` within
    `window` trading days (by position, not calendar days) of target_idx."""
    if dates_dir.empty:
        return False
    for d, row in dates_dir.iterrows():
        if row["direction"] != direction:
            continue
        i = date_to_idx.get(d)
        if i is not None and abs(i - target_idx) <= window:
            return True
    return False


def build_reversals(df: pd.DataFrame, zigzag_pivots: list[ZigZagPivot],
                     ma_events: pd.DataFrame, extrema_events: pd.DataFrame,
                     momentum_events: pd.DataFrame, match_window: int,
                     min_confidence: int) -> list[Reversal]:
    date_to_idx = {d: i for i, d in enumerate(df.index)}

    # --- Build the primary candidate list from ZigZag (confirmed only) --
    confirmed_zz = [p for p in zigzag_pivots if not p.provisional]
    candidates = []  # (idx, date, price, new_direction, source, confirm_idx)
    for i in range(1, len(confirmed_zz)):
        prev_p, p = confirmed_zz[i - 1], confirmed_zz[i]
        new_dir = "down" if p.kind == "high" else "up"
        pct_move = abs(p.price - prev_p.price) / prev_p.price * 100
        candidates.append(dict(idx=p.idx, date=df.index[p.idx], price=p.price,
                                new_direction=new_dir, pct_move=pct_move, source="zigzag",
                                confirm_idx=p.confirm_idx))

    # --- Add local-extrema points not already near a ZigZag candidate ---
    zz_idxs = [c["idx"] for c in candidates]
    for date, row in extrema_events.iterrows():
        i = date_to_idx.get(date)
        if i is None:
            continue
        if any(abs(i - zi) <= match_window for zi in zz_idxs):
            continue  # will be picked up as confirmation, not a new candidate
        # magnitude: % move from the previous extremum of the opposite kind
        prior = extrema_events.loc[:date]
        prior = prior[prior["direction"] != row["direction"]]
        if len(prior):
            prev_price = prior["price"].iloc[-1]
            pct_move = abs(row["price"] - prev_price) / prev_price * 100
        else:
            pct_move = float("nan")
        candidates.append(dict(idx=i, date=date, price=row["price"],
                                new_direction=row["direction"], pct_move=pct_move, source="extrema",
                                confirm_idx=int(row["confirm_idx"])))

    candidates.sort(key=lambda c: c["idx"])

    # --- Score each candidate against all 4 methods -----------------
    reversals = []
    for c in candidates:
        zz_hit = any(
            abs(c["idx"] - p.idx) <= match_window
            and ((p.kind == "high") == (c["new_direction"] == "down"))
            for p in confirmed_zz
        )
        extrema_hit = _nearest_within(extrema_events, c["idx"], c["new_direction"], date_to_idx, match_window)
        ma_hit = _nearest_within(ma_events, c["idx"], c["new_direction"], date_to_idx, match_window)
        momentum_hit = _nearest_within(momentum_events, c["idx"], c["new_direction"], date_to_idx, match_window)

        sources = [n for n, hit in [("zigzag", zz_hit), ("extrema", extrema_hit),
                                     ("ma_crossover", ma_hit), ("momentum", momentum_hit)] if hit]
        confidence = len(sources)

        reversals.append(Reversal(
            date=c["date"], idx=c["idx"], price=c["price"],
            prior_direction=("up" if c["new_direction"] == "down" else "down"),
            new_direction=c["new_direction"], pct_move=c["pct_move"],
            confidence=confidence, sources=sources, confirm_idx=c["confirm_idx"],
        ))

    # de-duplicate reversals that ended up on/near the same bar+direction
    reversals.sort(key=lambda r: r.idx)
    deduped: list[Reversal] = []
    for r in reversals:
        if deduped and r.idx - deduped[-1].idx <= match_window and r.new_direction == deduped[-1].new_direction:
            if r.confidence > deduped[-1].confidence:
                deduped[-1] = r
            continue
        deduped.append(r)

    return [r for r in deduped if r.confidence >= min_confidence]


# ==========================================================================
# 7. BACKTEST -- enter on the flagged day, hold N trading days
# ==========================================================================

def backtest(df: pd.DataFrame, reversals: list[Reversal], hold_days: int) -> pd.DataFrame:
    """
    Simplified, no-cost, non-overlapping-in-spirit backtest for evaluating
    signal reliability -- NOT a realistic trading simulation (no slippage,
    commissions, position sizing, or capacity constraints).

    Entry: next bar's Open AFTER `confirm_idx` -- the bar where the
      reversal was actually detectable in real time, NOT the pivot bar
      itself. ZigZag/extrema pivots are only identifiable in hindsight
      (once price has moved away from them), so entering right after the
      pivot bar would silently use future information. This uses the
      later, real-time-honest confirmation point instead.
    Direction: long if new_direction == 'up' (betting the new uptrend
      continues), short if 'down'.
    Exit: Close `hold_days` trading bars after entry.
    Trades that would run past the end of available data are skipped.
    """
    rows = []
    n = len(df)
    for r in reversals:
        entry_idx = r.confirm_idx + 1
        exit_idx = entry_idx + hold_days
        if exit_idx >= n:
            continue  # not enough future data to complete this trade yet
        entry_price = df["Open"].iloc[entry_idx]
        exit_price = df["Close"].iloc[exit_idx]
        direction = "long" if r.new_direction == "up" else "short"
        ret = ((exit_price - entry_price) / entry_price * 100 if direction == "long"
               else (entry_price - exit_price) / entry_price * 100)
        rows.append(dict(
            reversal_date=r.date, confirm_date=df.index[r.confirm_idx],
            confidence=r.confidence, direction=direction,
            entry_date=df.index[entry_idx], entry_price=entry_price,
            exit_date=df.index[exit_idx], exit_price=exit_price, return_pct=ret,
        ))
    return pd.DataFrame(rows)


def backtest_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return dict(n_trades=0, win_rate=float("nan"), avg_return=float("nan"), max_drawdown=float("nan"))

    n_trades = len(trades)
    win_rate = (trades["return_pct"] > 0).mean() * 100
    avg_return = trades["return_pct"].mean()

    # simple sequential equity curve: apply each trade's return in order,
    # one at a time, to a single running stake (illustrative only)
    equity = (1 + trades.sort_values("entry_date")["return_pct"] / 100).cumprod()
    running_max = equity.cummax()
    drawdown = (equity / running_max - 1) * 100
    max_drawdown = drawdown.min()

    return dict(n_trades=n_trades, win_rate=win_rate, avg_return=avg_return, max_drawdown=max_drawdown)


def method_accuracy(df: pd.DataFrame, events: pd.DataFrame, hold_days: int) -> tuple[int, float]:
    """
    For a raw (pre-clustering) per-method signal set: what fraction of
    signals were 'confirmed' by price actually moving in the predicted
    direction over the following `hold_days` bars? Returns (n_signals, pct_confirmed).
    """
    if events.empty:
        return 0, float("nan")
    date_to_idx = {d: i for i, d in enumerate(df.index)}
    n = len(df)
    hits, total = 0, 0
    for date, row in events.iterrows():
        i = date_to_idx.get(date)
        if i is None or i + hold_days >= n:
            continue
        future_ret = (df["Close"].iloc[i + hold_days] - df["Close"].iloc[i]) / df["Close"].iloc[i]
        confirmed = future_ret > 0 if row["direction"] == "up" else future_ret < 0
        hits += int(confirmed)
        total += 1
    return total, (hits / total * 100 if total else float("nan"))


# ==========================================================================
# 8. CHART
# ==========================================================================

def plot_chart(df: pd.DataFrame, reversals: list[Reversal], ticker: str, out_path: Path):
    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )

    ax_price.plot(df.index, df["Close"], color="#4C72B0", linewidth=1.2, label="Close")

    ups = [r for r in reversals if r.new_direction == "up"]
    downs = [r for r in reversals if r.new_direction == "down"]
    if downs:
        ax_price.scatter([r.date for r in downs], [r.price for r in downs],
                          marker="v", color="#C44E52", s=[40 + 25 * r.confidence for r in downs],
                          zorder=5, label="Up→Down reversal", edgecolors="black", linewidths=0.5)
    if ups:
        ax_price.scatter([r.date for r in ups], [r.price for r in ups],
                          marker="^", color="#55A868", s=[40 + 25 * r.confidence for r in ups],
                          zorder=5, label="Down→Up reversal", edgecolors="black", linewidths=0.5)

    ax_price.set_title(f"{ticker} — Price with Detected Trend Reversals\n"
                        f"(marker size scales with confidence score)")
    ax_price.set_ylabel("Price")
    ax_price.legend(loc="best")
    ax_price.grid(alpha=0.3)

    ax_vol.bar(df.index, df["Volume"], color="#8C8C8C", width=0.8)
    ax_vol.set_ylabel("Volume")
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


# ==========================================================================
# 9. MAIN
# ==========================================================================

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ticker", default="SPCX")
    p.add_argument("--period", default="max", help="yfinance period, e.g. max/5y/1y")

    # ZigZag
    p.add_argument("--zigzag-pct", type=float, default=5.0,
                    help="Min %% move from last pivot to confirm a ZigZag reversal (default 5.0)")

    # MA crossover -- tuned down from the textbook 10/30 to suit SPCX's short history
    p.add_argument("--ma-fast", type=int, default=5, help="Fast MA window (default 5; textbook default 10)")
    p.add_argument("--ma-slow", type=int, default=15, help="Slow MA window (default 15; textbook default 30)")
    p.add_argument("--ma-type", choices=["sma", "ema"], default="ema")

    # Local extrema
    p.add_argument("--extrema-order", type=int, default=3,
                    help="Bars on each side to compare for swing high/low (default 3; use 5-10 for longer history)")

    # Momentum (RSI/MACD)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--macd-fast", type=int, default=12)
    p.add_argument("--macd-slow", type=int, default=26)
    p.add_argument("--macd-signal", type=int, default=9)

    # Combination / scoring
    p.add_argument("--match-window", type=int, default=2,
                    help="Trading-day tolerance for treating two methods' signals as 'the same event' (default 2)")
    p.add_argument("--min-confidence", type=int, default=1,
                    help="Drop reversal events with fewer than this many methods agreeing (default 1 = show all)")

    # Backtest
    p.add_argument("--hold-days", type=int, default=3,
                    help="Primary holding period in trading days for the backtest (default 3)")
    p.add_argument("--hold-days-sweep", default="3,4,5",
                    help="Comma-separated holding periods to compare (default '3,4,5')")

    p.add_argument("--outdir", default=None, help="Output directory (default ./output/<TICKER>)")

    args = p.parse_args()

    outdir = Path(args.outdir) if args.outdir else Path(__file__).parent / "output" / args.ticker
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[data] fetching {args.ticker} ({args.period})...")
    df = load_ohlcv(args.ticker, period=args.period)
    print(f"[data] {len(df)} usable trading days: {df.index[0].date()} .. {df.index[-1].date()}")

    if len(df) < max(args.ma_slow, args.macd_slow) + args.hold_days + 2:
        print("[warn] history is short relative to your window settings -- "
              "many signals may be dropped by warm-up guards. Consider shrinking "
              "--ma-slow / --macd-slow or widening --period.")

    # ---- Method 1: ZigZag ----
    zz = compute_zigzag(df["Close"], args.zigzag_pct)
    print(f"[zigzag] {len([p for p in zz if not p.provisional])} confirmed pivot(s)")

    # ---- Method 2: MA crossover ----
    ma_events = compute_ma_crossover(df["Close"], args.ma_fast, args.ma_slow, args.ma_type)
    print(f"[ma_crossover] {len(ma_events)} crossover(s)")

    # ---- Method 3: Local extrema ----
    extrema_events = compute_local_extrema(df, args.extrema_order)
    print(f"[extrema] {len(extrema_events)} swing point(s)")

    # ---- Method 4: Momentum shift ----
    momentum_events = compute_momentum_shift(
        df, args.rsi_period, args.macd_fast, args.macd_slow, args.macd_signal
    )
    print(f"[momentum] {len(momentum_events)} shift(s)")

    # ---- Combine ----
    reversals = build_reversals(
        df, zz, ma_events, extrema_events, momentum_events,
        match_window=args.match_window, min_confidence=args.min_confidence,
    )
    print(f"[combined] {len(reversals)} reversal event(s) with confidence >= {args.min_confidence}")

    # ---- Table ----
    # `date` is the pivot bar itself (identifiable only in hindsight);
    # `confirmed_on` is the earliest bar a real-time observer could have
    # detected the reversal, and is what the backtest actually trades off.
    table = pd.DataFrame([
        dict(date=r.date.date(), confirmed_on=df.index[r.confirm_idx].date(),
             prior_direction=r.prior_direction, new_direction=r.new_direction,
             pct_move=round(r.pct_move, 2) if not np.isnan(r.pct_move) else None,
             confidence=r.confidence, methods="+".join(r.sources))
        for r in reversals
    ])
    table_path = outdir / "reversals.csv"
    table.to_csv(table_path, index=False)
    print(f"[out] {table_path}")
    if not table.empty:
        print(table.to_string(index=False))

    # ---- Chart ----
    chart_path = outdir / "chart.png"
    plot_chart(df, reversals, args.ticker, chart_path)
    print(f"[out] {chart_path}")

    # ---- Summary stats ----
    lines = [f"SPCX-style reversal analysis for {args.ticker}",
             f"History: {df.index[0].date()} .. {df.index[-1].date()} ({len(df)} trading days)",
             ""]

    if len(reversals) >= 2:
        gaps = np.diff([r.idx for r in reversals])
        lines.append(f"Average time between reversals: {gaps.mean():.1f} trading days "
                      f"(n={len(gaps)} gaps)")
    else:
        lines.append("Average time between reversals: n/a (fewer than 2 reversals detected)")

    hold_sweep = [int(x) for x in args.hold_days_sweep.split(",")]
    if reversals:
        for h in sorted(set(hold_sweep + [args.hold_days])):
            rets = []
            for r in reversals:
                exit_idx = r.idx + h
                if exit_idx >= len(df):
                    continue
                fut = (df["Close"].iloc[exit_idx] - df["Close"].iloc[r.idx]) / df["Close"].iloc[r.idx] * 100
                rets.append(abs(fut))
            if rets:
                lines.append(f"Average |move| in the {h} bars following a reversal: "
                              f"{np.mean(rets):.2f}% (n={len(rets)})")

    lines.append("")
    lines.append("Per-method 'confirmed by subsequent trend' accuracy "
                  f"(direction correct after {args.hold_days} trading days):")
    for name, events in [("zigzag (leg-based)", None), ("ma_crossover", ma_events),
                          ("extrema", extrema_events), ("momentum", momentum_events)]:
        if name.startswith("zigzag"):
            zz_events = pd.DataFrame(
                [(df.index[p.idx], "down" if p.kind == "high" else "up")
                 for p in zz if not p.provisional],
                columns=["date", "direction"]
            ).set_index("date") if zz else pd.DataFrame(columns=["direction"])
            n, acc = method_accuracy(df, zz_events, args.hold_days)
        else:
            n, acc = method_accuracy(df, events, args.hold_days)
        acc_str = f"{acc:.1f}%" if not np.isnan(acc) else "n/a"
        lines.append(f"  {name:<20s} n={n:<4d} accuracy={acc_str}")

    # ---- Backtest ----
    lines.append("")
    lines.append(f"Backtest: enter next-bar Open after each reversal is CONFIRMED (not on the pivot")
    lines.append(f"bar itself -- see 'confirmed_on' in reversals.csv), hold N trading days.")
    lines.append(f"(direction = long on down->up flips, short on up->down flips; no fees/slippage)")
    all_trades = []
    for h in sorted(set(hold_sweep + [args.hold_days])):
        trades = backtest(df, reversals, h)
        stats = backtest_stats(trades)
        trades["hold_days"] = h
        all_trades.append(trades)
        if stats["n_trades"] == 0:
            lines.append(f"  hold={h}d: no completed trades yet (not enough future data)")
        else:
            lines.append(
                f"  hold={h}d: n={stats['n_trades']} win_rate={stats['win_rate']:.1f}% "
                f"avg_return={stats['avg_return']:.2f}% max_drawdown={stats['max_drawdown']:.2f}%"
            )

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_path = outdir / "backtest_trades.csv"
    trades_df.to_csv(trades_path, index=False)
    print(f"[out] {trades_path}")

    if len(reversals) < 5:
        lines.append("")
        lines.append(f"CAUTION: only {len(reversals)} reversal event(s) detected -- all statistics "
                      "above are based on a very small sample and should be treated as descriptive, "
                      "not predictive. Re-run this script as more history accumulates.")

    summary_text = "\n".join(lines)
    summary_path = outdir / "summary.txt"
    summary_path.write_text(summary_text, encoding="utf-8")
    print(f"[out] {summary_path}")
    print()
    print(summary_text)


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        main()
