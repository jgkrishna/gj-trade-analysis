"""
SPCX Next-Reversal Window Projection
=====================================

Builds directly on top of reversal_analysis.py -- it imports that module's
detection functions (ZigZag, MA crossover, local extrema, momentum shift,
confidence scoring) rather than reimplementing any of them, so the two
scripts always agree on what counts as a "reversal."

reversal_analysis.py answers "what happened." This script answers a
narrower question: "given how far apart past reversals have been, what
trading-day window should I watch for the NEXT one?"

--------------------------------------------------------------------------
Method (read this before trusting the output)
--------------------------------------------------------------------------
1. Re-runs the same 4-method detection pipeline to get the current,
   up-to-date reversal list (same CLI flags as reversal_analysis.py --
   use the SAME flag values on both scripts, or the two will be looking
   at different reversal sets).
2. Measures the trading-day GAP between each pair of consecutive
   confirmed reversals (using confirm_idx -- the bar where a reversal
   was actually knowable in real time -- not the pivot bar itself,
   which is only identifiable in hindsight).
3. Reports how many trading days have elapsed since the last confirmed
   reversal, and what percentile that is within the historical gap
   distribution (a purely descriptive "is this a long wait by past
   standards" check -- NOT a hazard-rate or survival model).
4. Projects calendar dates for the 10th/25th/50th/75th/90th percentile
   gap lengths, walking forward on the real US market trading calendar
   (weekends + NYSE-observed federal holidays via pandas), not just
   Mon-Fri.

This is NOT a price or direction forecast. It says nothing about whether
the next move will actually be up or down beyond "the opposite of the
current leg" (by construction, reversals alternate direction). With a
young ticker like SPCX, n is small (single digits) -- every number here
is descriptive extrapolation of past spacing, not a prediction.

--------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------
    python predict_next_reversal.py
    python predict_next_reversal.py --ticker SPCX --zigzag-pct 8

Pass the SAME detection flags you used for reversal_analysis.py (see
`python reversal_analysis.py -h` for what each one means). Using
different flags between the two scripts will silently produce a
different combined reversal list and an inconsistent picture.

Output: ./output/<TICKER>/next_reversal_prediction.txt
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from reversal_analysis import (
    Reversal,
    build_reversals,
    compute_local_extrema,
    compute_ma_crossover,
    compute_momentum_shift,
    compute_zigzag,
    load_ohlcv,
)


# ==========================================================================
# 1. RE-RUN THE SHARED DETECTION PIPELINE (no logic duplicated here)
# ==========================================================================

def detect_reversals(args: argparse.Namespace) -> tuple[pd.DataFrame, list[Reversal]]:
    """Runs the exact same 4-method pipeline reversal_analysis.py uses,
    via its own functions, so results stay consistent between scripts."""
    df = load_ohlcv(args.ticker, period=args.period)

    zz = compute_zigzag(df["Close"], args.zigzag_pct)
    ma_events = compute_ma_crossover(df["Close"], args.ma_fast, args.ma_slow, args.ma_type)
    extrema_events = compute_local_extrema(df, args.extrema_order)
    momentum_events = compute_momentum_shift(
        df, args.rsi_period, args.macd_fast, args.macd_slow, args.macd_signal
    )

    reversals = build_reversals(
        df, zz, ma_events, extrema_events, momentum_events,
        match_window=args.match_window, min_confidence=args.min_confidence,
    )
    return df, reversals


# ==========================================================================
# 2. GAP STATISTICS
# ==========================================================================

def confirm_gaps(reversals: list[Reversal]) -> np.ndarray:
    """Trading-day gaps between consecutive CONFIRMED reversals, measured
    on confirm_idx (real-time-knowable bar), not the pivot bar."""
    confirm_idxs = sorted(r.confirm_idx for r in reversals)
    return np.diff(confirm_idxs)


def gap_percentiles(gaps: np.ndarray, pcts=(10, 25, 50, 75, 90)) -> dict[int, int]:
    return {p: int(round(np.percentile(gaps, p))) for p in pcts}


def elapsed_percentile_rank(gaps: np.ndarray, elapsed: int) -> float:
    """% of historical gaps that were <= `elapsed` -- i.e. how far into
    the historical distribution the current wait already sits."""
    if elapsed <= 0:
        return 0.0
    return float((gaps <= elapsed).mean() * 100)


# ==========================================================================
# 3. CALENDAR PROJECTION (real market trading days, not just weekdays)
# ==========================================================================

def project_trading_dates(last_date: pd.Timestamp, n_days_list: list[int]) -> dict[int, pd.Timestamp]:
    from pandas.tseries.holiday import USFederalHolidayCalendar
    from pandas.tseries.offsets import CustomBusinessDay

    bday = CustomBusinessDay(calendar=USFederalHolidayCalendar())
    return {n: (last_date + n * bday) for n in n_days_list}


# ==========================================================================
# 4. MAIN
# ==========================================================================

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ticker", default="SPCX")
    p.add_argument("--period", default="max")

    # Keep these identical in name/default to reversal_analysis.py so the
    # same command line works for both scripts.
    p.add_argument("--zigzag-pct", type=float, default=5.0)
    p.add_argument("--ma-fast", type=int, default=5)
    p.add_argument("--ma-slow", type=int, default=15)
    p.add_argument("--ma-type", choices=["sma", "ema"], default="ema")
    p.add_argument("--extrema-order", type=int, default=3)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--macd-fast", type=int, default=12)
    p.add_argument("--macd-slow", type=int, default=26)
    p.add_argument("--macd-signal", type=int, default=9)
    p.add_argument("--match-window", type=int, default=2)
    p.add_argument("--min-confidence", type=int, default=1)

    p.add_argument("--outdir", default=None, help="Output directory (default ./output/<TICKER>)")
    args = p.parse_args()

    outdir = Path(args.outdir) if args.outdir else Path(__file__).parent / "output" / args.ticker
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[data] fetching {args.ticker} ({args.period}) via reversal_analysis pipeline...")
    df, reversals = detect_reversals(args)
    print(f"[combined] {len(reversals)} reversal event(s) with confidence >= {args.min_confidence}")

    if len(reversals) < 2:
        sys.exit(
            "[error] need at least 2 detected reversals to estimate a gap distribution. "
            "Try loosening --min-confidence or widening --period."
        )

    last = reversals[-1]
    last_confirm_date = df.index[last.confirm_idx]
    elapsed = (len(df) - 1) - last.confirm_idx
    next_direction = "up" if last.new_direction == "down" else "down"

    gaps = confirm_gaps(reversals)
    pcts = gap_percentiles(gaps)
    pct_rank = elapsed_percentile_rank(gaps, elapsed)
    proj_dates = project_trading_dates(df.index[-1], sorted(set(pcts.values())))

    if pct_rank >= 60:
        wait_note = "a long wait relative to history -- overdue by past spacing, though small-n means this is weak evidence"
    elif pct_rank >= 25:
        wait_note = "within the normal historical range"
    else:
        wait_note = "still early relative to history"

    lines = []
    lines.append(f"Next-reversal window projection for {args.ticker}")
    lines.append(f"Data through: {df.index[-1].date()}")
    lines.append(
        f"Last confirmed reversal: {last_confirm_date.date()} "
        f"({last.prior_direction} -> {last.new_direction}, confidence={last.confidence}, "
        f"methods={'+'.join(last.sources) if last.sources else 'n/a'})"
    )
    lines.append(
        f"Currently {elapsed} trading day(s) into the '{last.new_direction}' leg -- "
        f"watching for the flip back to '{next_direction}'."
    )
    lines.append("")
    lines.append(f"Historical gaps between confirmed reversals (n={len(gaps)}): {list(int(g) for g in gaps)} trading days")
    lines.append(
        f"  mean={gaps.mean():.1f}d  median={np.median(gaps):.1f}d  min={int(gaps.min())}d  max={int(gaps.max())}d"
    )
    lines.append(f"  Current elapsed ({elapsed}d) is beyond {pct_rank:.0f}% of historical gaps -- {wait_note}.")
    lines.append("")
    lines.append("Projected trading-day window for the next reversal:")
    lines.append("  percentile | trading days from last confirmed reversal | calendar date")
    for p_ in sorted(pcts):
        n_days = pcts[p_]
        date = proj_dates[n_days]
        lines.append(f"  p{p_:<3d}      | {n_days:<3d}                                          | {date.date()}")

    lines.append("")
    lines.append(f"CAUTION: n={len(gaps)} historical gaps is a very small sample. This is a")
    lines.append("descriptive extrapolation of past reversal spacing, NOT a price/direction")
    lines.append("forecast. Re-run this script (and reversal_analysis.py) as new bars and new")
    lines.append("confirmed reversals arrive -- the window will narrow as the sample grows.")

    text = "\n".join(lines)
    out_path = outdir / "next_reversal_prediction.txt"
    out_path.write_text(text, encoding="utf-8")

    print()
    print(text)
    print()
    print(f"[out] {out_path}")


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        main()
