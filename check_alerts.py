"""
Standalone alert checker -- run this on a SCHEDULE, not by loading the page.
================================================================================

The dashboard only runs its detection pipeline when someone has the page
open, so it can't be the thing that notifies you while you're away. This
script is the hands-off equivalent: it runs the same detection pipeline
for a watchlist, records results to the same SQLite history
(history_store.py) used by the dashboard, and emails/webhooks you about
any reversal that's newly confirmed since the last time this ran.

Configure via environment variables (a scheduler sets these, not you
typing a password anywhere interactive):

  ALERT_WATCHLIST       comma-separated tickers, default "SPCX"

  ALERT_SMTP_HOST       e.g. smtp.gmail.com  (omit entirely to skip email)
  ALERT_SMTP_PORT       default 587
  ALERT_SMTP_USER       e.g. your Gmail address
  ALERT_SMTP_PASSWORD   an App Password, NOT your real account password
  ALERT_EMAIL_TO        comma-separated recipient(s)

  ALERT_WEBHOOK_URL     any URL that accepts a JSON POST (Slack incoming
                         webhook, Discord webhook, ntfy.sh, etc.) -- omit
                         to skip

At least one of ALERT_SMTP_HOST / ALERT_WEBHOOK_URL must be set, or this
exits immediately rather than silently doing nothing.

--------------------------------------------------------------------------
How to actually run this on a schedule
--------------------------------------------------------------------------
Locally (Windows Task Scheduler): create a Basic Task that runs
    python C:\\path\\to\\spcx_reversal\\check_alerts.py
on whatever interval you want (e.g. every weekday at market close). Only
fires while your machine is on.

On Render: add a second service of type "Cron Job" (separate from the web
service) pointing at the same repo, with the same Docker image, running
`python check_alerts.py` on a cron schedule -- this is the reliable
option since it doesn't depend on your laptop being on. It's a small
additional cost on top of the web service. See DEPLOY.md.

Detection thresholds here match the dashboard's defaults (see
reversal_analysis.py's own defaults) -- edit the constants below if you
want this to watch with different sensitivity than the dashboard's
default view.
"""

from __future__ import annotations

import os
import sys

from reversal_analysis import (
    build_reversals,
    compute_local_extrema,
    compute_ma_crossover,
    compute_momentum_shift,
    compute_zigzag,
    load_ohlcv,
)
from history_store import record_reversals
from alerts import notify_new_reversals

# Matches reversal_analysis.py / dashboard.py's own defaults.
ZIGZAG_PCT = 5.0
MA_FAST, MA_SLOW, MA_TYPE = 5, 15, "ema"
EXTREMA_ORDER = 3
RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL = 14, 12, 26, 9
MATCH_WINDOW, MIN_CONFIDENCE = 2, 1


def _email_config_from_env() -> dict | None:
    host = os.environ.get("ALERT_SMTP_HOST")
    if not host:
        return None
    return dict(
        host=host,
        port=int(os.environ.get("ALERT_SMTP_PORT", "587")),
        user=os.environ.get("ALERT_SMTP_USER", ""),
        password=os.environ.get("ALERT_SMTP_PASSWORD", ""),
        to_addrs=[a.strip() for a in os.environ.get("ALERT_EMAIL_TO", "").split(",") if a.strip()],
    )


def check_ticker(ticker: str, email_config: dict | None, webhook_url: str | None) -> None:
    try:
        df = load_ohlcv(ticker, period="max")
    except SystemExit as e:
        print(f"[{ticker}] skipped: {e}")
        return

    zz = compute_zigzag(df["Close"], ZIGZAG_PCT)
    ma_events = compute_ma_crossover(df["Close"], MA_FAST, MA_SLOW, MA_TYPE)
    extrema_events = compute_local_extrema(df, EXTREMA_ORDER)
    momentum_events = compute_momentum_shift(df, RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    reversals = build_reversals(
        df, zz, ma_events, extrema_events, momentum_events,
        match_window=MATCH_WINDOW, min_confidence=MIN_CONFIDENCE,
    )

    new_rows = record_reversals(ticker, df, reversals)
    if not new_rows:
        print(f"[{ticker}] no new reversals")
        return

    sent = notify_new_reversals(ticker, new_rows, email_config=email_config, webhook_url=webhook_url)
    print(f"[{ticker}] {len(new_rows)} new reversal(s) -- alerted via: {sent or 'NOTHING (see errors above)'}")


def main():
    email_config = _email_config_from_env()
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL") or None
    if not email_config and not webhook_url:
        sys.exit(
            "No alert channel configured. Set ALERT_SMTP_HOST (+ related) for email, "
            "and/or ALERT_WEBHOOK_URL for a webhook. See this file's docstring."
        )

    watchlist = [t.strip().upper() for t in os.environ.get("ALERT_WATCHLIST", "SPCX").split(",") if t.strip()]
    for ticker in watchlist:
        check_ticker(ticker, email_config, webhook_url)


if __name__ == "__main__":
    main()
