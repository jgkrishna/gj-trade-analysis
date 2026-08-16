"""
Reversal Dashboard (live, auto-refreshing)
===========================================

A Streamlit front-end over the SAME pipeline used by reversal_analysis.py
and predict_next_reversal.py -- it imports their functions directly
(load_ohlcv, the four detection methods, build_reversals, the backtest,
and the gap/percentile projection helpers) rather than reimplementing any
of that logic. This file only adds presentation: layout, plain-English
labels, color coding, and an auto-refresh loop.

--------------------------------------------------------------------------
Run it
--------------------------------------------------------------------------
    python run_dashboard.py        (recommended -- also opens Chrome)
    streamlit run dashboard.py     (from inside this directory)

This dashboard is PUBLIC by default -- no password needed, on purpose,
since it shows only public market data and generic backtest stats, nothing
personal. If you want to lock it back down:
    python hash_password.py
then paste the printed line into a NEW .streamlit/secrets.toml (copy it from
secrets.toml.example first). See DEPLOY.md for the deployed-site equivalent.
The gate reappears automatically as soon as a password hash is configured.

Everything -- ticker, all detection thresholds, hold-days sweep,
auto-refresh interval -- is adjustable from the sidebar; no need to edit
this file. Works for ANY ticker, not just SPCX -- change it in the
sidebar's "Ticker & Data Range" section.

--------------------------------------------------------------------------
Color language used throughout this dashboard
--------------------------------------------------------------------------
  GREEN  = bullish / up (a down->up reversal, a winning backtest number)
  RED    = bearish / down (an up->down reversal, a losing backtest number)
  AMBER  = something to pay closer attention to (overdue window, caution)
  BLUE   = neutral information / magnitude (confidence strength, price line)
Every color-coded item also carries an icon and a text label -- never rely
on color alone (also helps colorblind users and B/W printouts).

This dashboard carries the same caveats as the two scripts it wraps: a
young ticker has a short trading history, reversal counts are single-
digit, and every statistic here is descriptive, not predictive. See the
"How to read this dashboard" panel and the caution banner at the bottom.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from reversal_analysis import (
    Reversal,
    backtest,
    backtest_stats,
    build_reversals,
    compute_local_extrema,
    compute_ma_crossover,
    compute_momentum_shift,
    compute_zigzag,
    load_ohlcv,
)
from predict_next_reversal import (
    confirm_gaps,
    elapsed_percentile_rank,
    gap_percentiles,
    project_trading_dates,
)
from history_store import load_history, record_reversals
from alerts import notify_new_reversals

# This file only works when Streamlit's server is driving it (`streamlit run
# dashboard.py` or `python run_dashboard.py`). Running it as plain
# `python dashboard.py` puts every st.* call below into "bare mode", which
# floods the console with one "missing ScriptRunContext" warning per call.
# Instead of just erroring, self-relaunch through run_dashboard.py (which
# starts the Streamlit server and opens Chrome) so `python dashboard.py`
# just works no matter where it's run from.
from streamlit.runtime.scriptrunner import get_script_run_ctx

if get_script_run_ctx() is None:
    import subprocess
    import sys
    from pathlib import Path

    launcher = Path(__file__).resolve().parent / "run_dashboard.py"
    print(f"[info] dashboard.py was run directly -- relaunching via {launcher.name} ...")
    subprocess.run([sys.executable, str(launcher)])
    raise SystemExit(0)

SITE_NAME = "GJ Trade Analysis"

st.set_page_config(page_title=SITE_NAME, layout="wide", page_icon=":chart_with_upwards_trend:")

# Best-effort hint to search-engine crawlers to skip this page. NOT a real
# guarantee -- Streamlit doesn't expose the actual <head>, so this tag lands
# in <body>, which well-behaved crawlers mostly still honor but isn't the
# standards-guaranteed placement. The real protection is simply that an
# undiscoverable page (no inbound links, never submitted to search consoles)
# won't get crawled in the first place. A true /robots.txt at the domain
# root would need a reverse proxy in front of Streamlit -- possible to add
# later if search-engine exclusion needs to be airtight.
st.markdown('<meta name="robots" content="noindex, nofollow">', unsafe_allow_html=True)


# ==========================================================================
# COLOR SYSTEM -- one place, reused everywhere below. Values match the
# fixed status palette (good/warning/serious/critical) plus the categorical
# slot-1 blue used for neutral/magnitude items. Never hand-picked per use.
# ==========================================================================

COLOR = {
    "good": "#0ca30c",       # bullish / winning / on-target
    "critical": "#d03b3b",   # bearish / losing
    "warning": "#fab219",    # caution / overdue
    "info": "#2a78d6",       # neutral information, magnitude, primary accent
    "neutral_bg": "#e1e0d9", # low-emphasis chip background
    "ink": "#0b0b0b",        # primary text
    "ink_secondary": "#52514e",
    "ink_muted": "#898781",
    "surface": "#fcfcfb",
    "gridline": "#e1e0d9",
}

# Sequential blue ramp for confidence (1-4 confirming methods = magnitude,
# not good/bad, so it gets the sequential treatment, not a status color).
CONFIDENCE_RAMP = {1: "#9ec5f4", 2: "#5598e7", 3: "#2a78d6", 4: "#184f95"}


def badge_html(label: str, kind: str, icon: str = "") -> str:
    """A small colored chip: background carries the status color, text is
    chosen for contrast against it, and label/icon are always present so
    the meaning never depends on color alone."""
    bg = COLOR[kind]
    fg = "#0b0b0b" if kind in ("warning",) else "#ffffff"
    if kind == "neutral_bg":
        bg, fg = COLOR["neutral_bg"], COLOR["ink"]
    text = f"{icon} {label}".strip()
    return (
        f'<span style="background:{bg};color:{fg};padding:5px 14px;'
        f'border-radius:6px;font-weight:600;font-size:1rem;'
        f'display:inline-block;line-height:1.4;">{text}</span>'
    )


def metric_card(label: str, value_html: str, sublabel: str = "") -> str:
    """A metric-style card that (unlike st.metric) can hold colored HTML."""
    sub = f'<div style="font-size:0.78rem;color:{COLOR["ink_muted"]};margin-top:4px;">{sublabel}</div>' if sublabel else ""
    return (
        f'<div style="padding:2px 0;">'
        f'<div style="font-size:0.8rem;font-weight:600;color:{COLOR["ink_secondary"]};'
        f'text-transform:uppercase;letter-spacing:0.03em;margin-bottom:6px;">{label}</div>'
        f'{value_html}{sub}</div>'
    )


def csv_download_button(df: pd.DataFrame, filename: str, key: str) -> None:
    st.download_button(
        "Download CSV", df.to_csv(index=False).encode("utf-8"),
        file_name=filename, mime="text/csv", key=key,
    )


# ==========================================================================
# SECRET RESOLUTION -- shared by the password gate and the alerts config.
# Checks .streamlit/secrets.toml first (local dev), falls back to a plain
# environment variable (Render/Railway). Never hand a real secret to the
# UI itself -- only ever read from these two places.
# ==========================================================================

def _get_secret(key: str) -> str | None:
    try:
        v = st.secrets.get(key)
        if v:
            return v
    except Exception:
        pass  # no secrets.toml at all -- fall through to the env var
    return os.environ.get(key) or None


# ==========================================================================
# ACCESS GATE -- OPT IN, not fail-closed: this dashboard is intended to be
# publicly viewable (no sensitive/personal data, just public market data
# and generic backtest stats), so with NO password configured it renders
# normally, no gate at all. If you ever want to lock it back down, generate
# a hash with `python hash_password.py` and set it as either
# .streamlit/secrets.toml's `password_hash` (gitignored, local dev) or the
# DASHBOARD_PASSWORD_HASH environment variable (Render/Railway, live site)
# -- the gate reappears automatically as soon as one is set. Only a SALTED
# HASH is ever stored/compared -- never the plaintext password. See
# DEPLOY.md for details.
# ==========================================================================

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def _configured_password_hash() -> str | None:
    return _get_secret("password_hash") or _get_secret("DASHBOARD_PASSWORD_HASH")


def resolve_alert_config() -> tuple[dict | None, str | None]:
    """(email_config, webhook_url), each None if not configured. See
    DEPLOY.md for the ALERT_SMTP_* / ALERT_WEBHOOK_URL secrets/env vars."""
    host = _get_secret("ALERT_SMTP_HOST")
    email_config = None
    if host:
        email_config = dict(
            host=host,
            port=int(_get_secret("ALERT_SMTP_PORT") or 587),
            user=_get_secret("ALERT_SMTP_USER") or "",
            password=_get_secret("ALERT_SMTP_PASSWORD") or "",
            to_addrs=[a.strip() for a in (_get_secret("ALERT_EMAIL_TO") or "").split(",") if a.strip()],
        )
    webhook_url = _get_secret("ALERT_WEBHOOK_URL")
    return email_config, webhook_url


def _verify_password(attempt: str, stored_hash: str) -> bool:
    try:
        algo, salt_b64, digest_b64 = stored_hash.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.scrypt(
            attempt.encode("utf-8"), salt=salt,
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(expected),
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False  # malformed stored hash, bad input, etc. -- never crash into "open"


_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60


def require_password() -> None:
    if st.session_state.get("authenticated"):
        return

    stored_hash = _configured_password_hash()
    if not stored_hash:
        return  # no password configured -- intentionally public, render normally

    st.markdown(
        f'<div style="text-align:center;margin-top:12vh;">'
        f'<div style="font-size:0.85rem;font-weight:700;letter-spacing:0.08em;'
        f'color:{COLOR["info"]};text-transform:uppercase;">{SITE_NAME}</div>'
        f'<h2 style="margin-top:4px;">Sign in</h2></div>',
        unsafe_allow_html=True,
    )

    now = time.time()
    lockout_until = st.session_state.get("lockout_until", 0.0)
    locked_out = now < lockout_until

    _, center, _ = st.columns([1, 1, 1])
    with center:
        if locked_out:
            st.error(f"Too many failed attempts. Try again in {int(lockout_until - now)}s.")
        else:
            with st.form("login_form"):
                pw = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="Password")
                submitted = st.form_submit_button("Enter", width="stretch")
            if submitted:
                if _verify_password(pw, stored_hash):
                    st.session_state["authenticated"] = True
                    st.session_state["login_attempts"] = 0
                    st.rerun()
                else:
                    attempts = st.session_state.get("login_attempts", 0) + 1
                    st.session_state["login_attempts"] = attempts
                    if attempts >= _MAX_LOGIN_ATTEMPTS:
                        st.session_state["lockout_until"] = now + _LOCKOUT_SECONDS
                        st.session_state["login_attempts"] = 0
                        st.error(f"Too many failed attempts. Try again in {_LOCKOUT_SECONDS}s.")
                    else:
                        st.error(f"Incorrect password. ({_MAX_LOGIN_ATTEMPTS - attempts} attempt(s) left before a cooldown.)")
    st.stop()


require_password()


# ==========================================================================
# SIDEBAR -- grouped, plain-English labels, help tooltips on every control.
# Mirrors the CLI flags of reversal_analysis.py / predict_next_reversal.py
# so results stay consistent with those scripts.
# ==========================================================================

with st.sidebar:
    st.markdown("## Dashboard Settings")

    st.markdown("### Watchlist & Data Range")
    watchlist_raw = st.text_input(
        "Tickers to track (comma-separated)", value="SPCX",
        help="Any symbols yfinance recognizes. Add more than one to see a summary row for each; "
             "pick which one gets the full detailed view below with 'Focus ticker.'",
    )
    watchlist = [t.strip().upper() for t in watchlist_raw.split(",") if t.strip()] or ["SPCX"]
    ticker = st.selectbox(
        "Focus ticker (detailed view)", watchlist, index=0,
        help="The chart, projection, and tables below all describe this one ticker.",
    )
    period = st.selectbox(
        "History to load", ["max", "5y", "2y", "1y", "6mo"], index=0,
        help="How far back to pull daily price history.",
    )

    st.markdown("### Reversal Detection Sensitivity")
    st.caption(
        "Four independent methods each vote on whether a trend reversal "
        "happened. A signal's confidence score = how many agree."
    )
    zigzag_pct = st.number_input(
        "ZigZag: minimum swing size (%)", value=5.0, step=0.5, min_value=0.5,
        help="Price must retrace this % from the last swing high/low before a ZigZag pivot is confirmed. Lower = more, noisier pivots.",
    )
    ma_fast = st.number_input(
        "Moving average crossover: fast window (days)", value=5, step=1, min_value=1,
        help="Short-term moving-average window used for the fast/slow crossover signal.",
    )
    ma_slow = st.number_input(
        "Moving average crossover: slow window (days)", value=15, step=1, min_value=2,
        help="Long-term moving-average window. A reversal fires when the fast MA crosses this one.",
    )
    ma_type = st.selectbox(
        "Moving average type", ["ema", "sma"], index=0,
        help="EMA weights recent bars more; SMA weights all bars in the window equally.",
    )
    extrema_order = st.number_input(
        "Swing-point sensitivity (bars each side)", value=3, step=1, min_value=1,
        help="A bar must be the highest/lowest among this many bars on EACH side to count as a swing high/low. Higher = fewer, more significant swings.",
    )
    rsi_period = st.number_input(
        "Momentum: RSI period (days)", value=14, step=1, min_value=2,
        help="Lookback window for the Relative Strength Index momentum check.",
    )
    macd_fast = st.number_input("Momentum: MACD fast EMA", value=12, step=1, min_value=1)
    macd_slow = st.number_input("Momentum: MACD slow EMA", value=26, step=1, min_value=2)
    macd_signal = st.number_input("Momentum: MACD signal EMA", value=9, step=1, min_value=1)

    st.markdown("### Signal Combination Rules")
    match_window = st.number_input(
        "Group signals within (days)", value=2, step=1, min_value=0,
        help="Two methods' signals within this many trading days of each other count as agreeing on the SAME reversal.",
    )
    min_confidence = st.number_input(
        "Minimum confirming methods to show", value=1, step=1, min_value=1, max_value=4,
        help="Hide reversals that fewer than this many methods agree on. 1 = show everything.",
    )

    st.markdown("### Backtest Hold Periods")
    hold_days_sweep = st.multiselect(
        "Trading days to hold after each confirmed reversal", [1, 2, 3, 4, 5, 7, 10], default=[3, 4, 5],
        help="For each value, simulates entering right after a reversal is confirmed and exiting N trading days later.",
    )

    st.markdown("### Live Refresh")
    auto_refresh = st.checkbox("Auto-refresh this page", value=False)
    refresh_interval = st.number_input(
        "Refresh interval (seconds)", min_value=60, max_value=3600, value=300, step=30,
        disabled=not auto_refresh,
        help="Daily-bar data changes at most once per session -- 5-15 minutes is plenty.",
    )
    if st.button("Refresh data now", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    if auto_refresh:
        st.markdown(f'<meta http-equiv="refresh" content="{int(refresh_interval)}">', unsafe_allow_html=True)
        st.caption(f"Auto-refreshing every {int(refresh_interval)}s")

    st.markdown("### Alerts")
    alerts_enabled = st.checkbox(
        "Email/webhook me when a NEW reversal is confirmed", value=False,
        help="Checks only run while this page is open/reloading -- for hands-off alerting even "
             "when nobody's looking, run check_alerts.py on a schedule instead. See DEPLOY.md.",
    )
    st.caption(
        "Channels are configured via secrets/env vars, never typed in here -- "
        "see DEPLOY.md for ALERT_SMTP_* / ALERT_WEBHOOK_URL."
    )


# ==========================================================================
# CACHED PIPELINE -- same functions reversal_analysis.py / predict_next_
# reversal.py use, just wrapped so Streamlit doesn't re-fetch on every
# widget interaction.
# ==========================================================================

@st.cache_data(ttl=int(refresh_interval) if auto_refresh else 300, show_spinner="Fetching data & detecting reversals...")
def run_pipeline(ticker, period, zigzag_pct, ma_fast, ma_slow, ma_type, extrema_order,
                  rsi_period, macd_fast, macd_slow, macd_signal, match_window, min_confidence):
    df = load_ohlcv(ticker, period=period)

    zz = compute_zigzag(df["Close"], zigzag_pct)
    ma_events = compute_ma_crossover(df["Close"], ma_fast, ma_slow, ma_type)
    extrema_events = compute_local_extrema(df, extrema_order)
    momentum_events = compute_momentum_shift(df, rsi_period, macd_fast, macd_slow, macd_signal)

    reversals = build_reversals(
        df, zz, ma_events, extrema_events, momentum_events,
        match_window=match_window, min_confidence=min_confidence,
    )
    return df, reversals


try:
    df, reversals = run_pipeline(
        ticker, period, zigzag_pct, ma_fast, ma_slow, ma_type, extrema_order,
        rsi_period, macd_fast, macd_slow, macd_signal, match_window, min_confidence,
    )
except SystemExit as e:
    st.error(str(e))
    st.stop()

st.markdown(
    f'<div style="font-size:0.8rem;font-weight:700;letter-spacing:0.08em;'
    f'color:{COLOR["info"]};text-transform:uppercase;margin-bottom:2px;">{SITE_NAME}</div>',
    unsafe_allow_html=True,
)
st.title(f"{ticker} — Trend-Reversal Dashboard")
st.caption(
    f"Data through {df.index[-1].date()}  ·  {len(df)} trading days of history  ·  "
    f"{len(reversals)} reversal(s) detected  ·  last updated {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}"
)

# ==========================================================================
# WATCHLIST SUMMARY -- only shown once you've actually added a second
# ticker, so the common single-ticker case stays uncluttered.
# ==========================================================================

if len(watchlist) > 1:
    st.subheader("Watchlist Summary")
    summary_rows = []
    for wt in watchlist:
        try:
            wdf, wrevs = run_pipeline(
                wt, period, zigzag_pct, ma_fast, ma_slow, ma_type, extrema_order,
                rsi_period, macd_fast, macd_slow, macd_signal, match_window, min_confidence,
            )
        except SystemExit:
            summary_rows.append({"Ticker": wt, "Status": "No data for this symbol"})
            continue
        if not wrevs:
            summary_rows.append({"Ticker": wt, "Status": "No reversals detected"})
            continue
        wlast = wrevs[-1]
        welapsed = (len(wdf) - 1) - wlast.confirm_idx
        summary_rows.append({
            "Ticker": wt,
            "Last Close": f"${wdf['Close'].iloc[-1]:,.2f}",
            "Current Leg": wlast.new_direction.upper(),
            "Days Into Leg": welapsed,
            "Reversals Detected": len(wrevs),
        })
    summary_df = pd.DataFrame(summary_rows)

    def _leg_bg(val: str) -> str:
        if val == "UP":
            return f"background-color:{COLOR['good']}33;color:{COLOR['good']};font-weight:600;"
        if val == "DOWN":
            return f"background-color:{COLOR['critical']}33;color:{COLOR['critical']};font-weight:600;"
        return ""

    styled_summary = (
        summary_df.style.map(_leg_bg, subset=["Current Leg"])
        if "Current Leg" in summary_df.columns else summary_df.style
    )
    st.dataframe(styled_summary, hide_index=True, width="stretch")
    st.caption(f"Detailed view below covers **{ticker}** -- change 'Focus ticker' in the sidebar for another one.")

with st.expander("How to read this dashboard (glossary)"):
    st.markdown(
        """
- **Reversal** -- a point where price flips from an up-trend to a down-trend, or vice versa.
- **Pivot date** vs. **Confirmed-on date** -- the pivot is the actual high/low bar, but it's only
  identifiable in *hindsight*. "Confirmed on" is the earliest date a real-time observer could
  actually have detected it. All timing stats on this page use the confirmed date, not the pivot.
- **Confidence (1-4)** -- how many of the four independent detection methods (ZigZag, moving-average
  crossover, swing high/low, momentum shift) agree that a reversal happened at that point. Shown as a
  darker blue for higher confidence.
- **Backtest** -- a simplified, no-fee simulation: buy (or short) right after a reversal is confirmed,
  hold for N trading days, see what happened historically. Not a live trading recommendation.
- **Next-reversal projection** -- purely descriptive: "how far apart have past reversals been?"
  projected forward as a calendar window. It is **not** a price or direction forecast.
        """
    )

# ==========================================================================
# PERSIST + (OPTIONALLY) ALERT ON NEWLY-CONFIRMED REVERSALS -- always
# records to the SQLite history log; only sends an alert if the sidebar
# toggle is on AND a channel is actually configured.
# ==========================================================================

_new_rows = record_reversals(ticker, df, reversals)
if _new_rows and alerts_enabled:
    _email_config, _webhook_url = resolve_alert_config()
    if _email_config or _webhook_url:
        _sent = notify_new_reversals(ticker, _new_rows, email_config=_email_config, webhook_url=_webhook_url)
        if _sent:
            st.toast(f"Alerted via {', '.join(_sent)}: {len(_new_rows)} new reversal(s) for {ticker}")
        else:
            st.warning("Alert toggle is on and a channel is configured, but sending failed -- check server logs.")
    else:
        st.caption(
            "Alert toggle is on, but no ALERT_SMTP_HOST or ALERT_WEBHOOK_URL is configured -- see DEPLOY.md."
        )

if len(reversals) < 2:
    st.warning(
        f"Only {len(reversals)} reversal(s) detected with current thresholds -- need at least 2 "
        "to estimate a gap distribution. Try lowering 'Minimum confirming methods to show' in the sidebar."
    )
    st.stop()


# ==========================================================================
# AT A GLANCE -- color-coded headline cards
# ==========================================================================

last = reversals[-1]
last_confirm_date = df.index[last.confirm_idx]
elapsed = (len(df) - 1) - last.confirm_idx
next_direction = "up" if last.new_direction == "down" else "down"
last_close = df["Close"].iloc[-1]
prev_close = df["Close"].iloc[-2] if len(df) > 1 else last_close
close_chg_pct = (last_close - prev_close) / prev_close * 100

gaps = confirm_gaps(reversals)
pcts = gap_percentiles(gaps)
pct_rank = elapsed_percentile_rank(gaps, elapsed)
proj_dates = project_trading_dates(df.index[-1], sorted(set(pcts.values())))

if pct_rank >= 60:
    wait_kind, wait_label, wait_icon = "warning", "OVERDUE vs. history", "⚠"
elif pct_rank >= 25:
    wait_kind, wait_label, wait_icon = "info", "NORMAL RANGE", "●"
else:
    wait_kind, wait_label, wait_icon = "info", "STILL EARLY", "○"

st.subheader("At a Glance")
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    st.metric("Last Close", f"${last_close:,.2f}", f"{close_chg_pct:+.2f}%")

with c2:
    leg_kind, leg_icon = ("good", "▲") if last.new_direction == "up" else ("critical", "▼")
    st.markdown(
        metric_card("Current Trend Leg", badge_html(last.new_direction.upper(), leg_kind, leg_icon)),
        unsafe_allow_html=True,
    )

with c3:
    st.metric("Days Into This Leg", f"{elapsed}d")

with c4:
    watch_kind, watch_icon = ("good", "▲") if next_direction == "up" else ("critical", "▼")
    st.markdown(
        metric_card("Watching For", badge_html(next_direction.upper(), watch_kind, watch_icon)),
        unsafe_allow_html=True,
    )

with c5:
    st.markdown(
        metric_card("Elapsed vs. History", badge_html(wait_label, wait_kind, wait_icon),
                     sublabel=f"beyond {pct_rank:.0f}% of past gaps"),
        unsafe_allow_html=True,
    )


# ==========================================================================
# PRICE CHART WITH REVERSAL MARKERS
# ==========================================================================

st.subheader("Price Chart")
st.caption(
    "Green ▲ = bullish reversal (down→up). Red ▼ = bearish reversal (up→down). "
    "Marker size = confidence (more confirming methods = larger marker)."
)

fig = make_subplots(
    rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.04,
)
fig.add_trace(
    go.Scatter(x=df.index, y=df["Close"], mode="lines", name="Close price",
               line=dict(color=COLOR["info"], width=1.5)),
    row=1, col=1,
)

downs = [r for r in reversals if r.new_direction == "down"]
ups = [r for r in reversals if r.new_direction == "up"]
if downs:
    fig.add_trace(
        go.Scatter(
            x=[r.date for r in downs], y=[r.price for r in downs], mode="markers",
            name="Bearish reversal (up→down)",
            marker=dict(symbol="triangle-down", color=COLOR["critical"],
                        size=[10 + 4 * r.confidence for r in downs],
                        line=dict(width=1, color=COLOR["ink"])),
            text=[f"confidence {r.confidence}/4 ({'+'.join(r.sources)})" for r in downs],
            hovertemplate="%{x}<br>$%{y:.2f}<br>%{text}<extra></extra>",
        ),
        row=1, col=1,
    )
if ups:
    fig.add_trace(
        go.Scatter(
            x=[r.date for r in ups], y=[r.price for r in ups], mode="markers",
            name="Bullish reversal (down→up)",
            marker=dict(symbol="triangle-up", color=COLOR["good"],
                        size=[10 + 4 * r.confidence for r in ups],
                        line=dict(width=1, color=COLOR["ink"])),
            text=[f"confidence {r.confidence}/4 ({'+'.join(r.sources)})" for r in ups],
            hovertemplate="%{x}<br>$%{y:.2f}<br>%{text}<extra></extra>",
        ),
        row=1, col=1,
    )

fig.add_trace(
    go.Bar(x=df.index, y=df["Volume"], name="Volume", marker_color=COLOR["ink_muted"], showlegend=False),
    row=2, col=1,
)
fig.update_layout(
    height=560, margin=dict(l=40, r=20, t=30, b=20),
    plot_bgcolor=COLOR["surface"], paper_bgcolor=COLOR["surface"],
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    hovermode="x unified",
    font=dict(color=COLOR["ink"]),
)
fig.update_yaxes(title_text="Price ($)", gridcolor=COLOR["gridline"], row=1, col=1)
fig.update_yaxes(title_text="Volume", gridcolor=COLOR["gridline"], row=2, col=1)
fig.update_xaxes(gridcolor=COLOR["gridline"])
st.plotly_chart(fig, width="stretch")


# ==========================================================================
# NEXT-REVERSAL PROJECTION
# ==========================================================================

st.subheader("Next-Reversal Window Projection")
st.caption(
    "Purely descriptive extrapolation of how spaced-out past reversals have been. "
    "NOT a price or direction forecast -- see the caution banner at the bottom of this page."
)

left, right = st.columns([1, 1])

with left:
    st.markdown(
        f"**Last confirmed reversal:** {last_confirm_date.date()} "
        f"({last.prior_direction} → {last.new_direction}, "
        f"confidence {last.confidence}/4, "
        f"methods: {', '.join(last.sources) if last.sources else 'n/a'})"
    )
    st.markdown(
        f"**Historical gaps between reversals (n={len(gaps)}):** {list(int(g) for g in gaps)} trading days  \n"
        f"mean = {gaps.mean():.1f}d &nbsp;·&nbsp; median = {np.median(gaps):.1f}d &nbsp;·&nbsp; "
        f"min = {int(gaps.min())}d &nbsp;·&nbsp; max = {int(gaps.max())}d",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"Current wait ({elapsed}d) is beyond **{pct_rank:.0f}%** of historical gaps: "
        + badge_html(wait_label, wait_kind, wait_icon),
        unsafe_allow_html=True,
    )

    proj_table = pd.DataFrame(
        [{"Confidence Percentile": f"p{p_}", "Trading Days Out": pcts[p_],
          "Projected Calendar Date": proj_dates[pcts[p_]].date()}
         for p_ in sorted(pcts)]
    )
    st.dataframe(
        proj_table, hide_index=True, width="stretch",
        column_config={
            "Confidence Percentile": st.column_config.TextColumn(
                help="p50 = median historical gap; p90 = only 10% of past gaps ran longer than this."),
            "Trading Days Out": st.column_config.NumberColumn(help="Trading days after the last confirmed reversal."),
        },
    )
    csv_download_button(proj_table, f"{ticker}_projection.csv", key="dl_projection")

with right:
    hist_fig = go.Figure()
    hist_fig.add_trace(go.Histogram(
        x=gaps, nbinsx=max(int(gaps.max()), 1), marker_color=COLOR["info"], name="Historical gaps",
    ))
    hist_fig.add_vline(
        x=elapsed, line_dash="dash", line_color=COLOR["ink"],
        annotation_text=f"Today: {elapsed}d elapsed", annotation_position="top",
    )
    hist_fig.update_layout(
        title="Historical Reversal-Gap Distribution vs. Today",
        xaxis_title="Trading days between reversals", yaxis_title="Count",
        height=320, margin=dict(l=40, r=20, t=40, b=20),
        plot_bgcolor=COLOR["surface"], paper_bgcolor=COLOR["surface"],
        font=dict(color=COLOR["ink"]),
    )
    hist_fig.update_xaxes(gridcolor=COLOR["gridline"])
    hist_fig.update_yaxes(gridcolor=COLOR["gridline"])
    st.plotly_chart(hist_fig, width="stretch")


# ==========================================================================
# REVERSAL HISTORY TABLE
# ==========================================================================

st.subheader("Reversal History")
rev_table = pd.DataFrame([
    dict(
        Date=r.date.date(), **{"Confirmed On": df.index[r.confirm_idx].date()},
        **{"Prior Direction": r.prior_direction.upper(), "New Direction": r.new_direction.upper()},
        **{"Move Size (%)": round(r.pct_move, 2) if not np.isnan(r.pct_move) else None},
        **{"Confidence (1-4)": r.confidence, "Confirming Methods": "+".join(r.sources)},
    )
    for r in reversals
])


def _direction_bg(val: str) -> str:
    if val == "UP":
        return f"background-color:{COLOR['good']}33;color:{COLOR['good']};font-weight:600;"
    if val == "DOWN":
        return f"background-color:{COLOR['critical']}33;color:{COLOR['critical']};font-weight:600;"
    return ""


def _confidence_bg(val: int) -> str:
    color = CONFIDENCE_RAMP.get(int(val), COLOR["info"])
    fg = "#ffffff" if int(val) >= 3 else COLOR["ink"]
    return f"background-color:{color};color:{fg};font-weight:600;text-align:center;"


styled_rev = (
    rev_table.style
    .map(_direction_bg, subset=["New Direction"])
    .map(_confidence_bg, subset=["Confidence (1-4)"])
)
st.dataframe(
    styled_rev, hide_index=True, width="stretch",
    column_config={
        "Confidence (1-4)": st.column_config.NumberColumn(
            help="How many of the 4 detection methods agree. Darker blue = stronger agreement."),
        "Move Size (%)": st.column_config.NumberColumn(format="%.2f%%"),
    },
)
csv_download_button(rev_table, f"{ticker}_reversal_history.csv", key="dl_reversals")


# ==========================================================================
# BACKTEST SUMMARY
# ==========================================================================

st.subheader("Backtest Summary")
st.caption("Enter next-bar Open right after each reversal is confirmed, hold N trading days, no fees/slippage.")

if hold_days_sweep:
    bt_rows = []
    for h in sorted(set(hold_days_sweep)):
        trades = backtest(df, reversals, h)
        stats = backtest_stats(trades)
        bt_rows.append(dict(
            **{"Hold Days": h, "Trades": stats["n_trades"]},
            **{"Win Rate (%)": round(stats["win_rate"], 1) if stats["n_trades"] else np.nan},
            **{"Avg Return (%)": round(stats["avg_return"], 2) if stats["n_trades"] else np.nan},
            **{"Max Drawdown (%)": round(stats["max_drawdown"], 2) if stats["n_trades"] else np.nan},
        ))
    bt_df = pd.DataFrame(bt_rows)

    def _perf_bg(val: float) -> str:
        if pd.isna(val):
            return ""
        if val > 0:
            return f"color:{COLOR['good']};font-weight:600;"
        if val < 0:
            return f"color:{COLOR['critical']};font-weight:600;"
        return ""

    styled_bt = bt_df.style.map(_perf_bg, subset=["Win Rate (%)", "Avg Return (%)", "Max Drawdown (%)"])
    st.dataframe(
        styled_bt, hide_index=True, width="stretch",
        column_config={
            "Win Rate (%)": st.column_config.NumberColumn(format="%.1f%%"),
            "Avg Return (%)": st.column_config.NumberColumn(format="%.2f%%"),
            "Max Drawdown (%)": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    csv_download_button(bt_df, f"{ticker}_backtest.csv", key="dl_backtest")
else:
    st.info("Select at least one hold-days value in the sidebar to see backtest stats.")


# ==========================================================================
# PERSISTED HISTORY LOG -- cumulative record across sessions/threshold
# changes, from the same SQLite store check_alerts.py writes to. This is
# what makes "did the projection actually pan out" answerable over time.
# ==========================================================================

st.subheader("Persisted History Log")
st.caption(
    "Every reversal ever recorded for this ticker across all past sessions (not just the "
    "current detection settings above). On most cloud hosting this resets on redeploy/restart "
    "unless a persistent disk is attached -- see DEPLOY.md. Locally, it just accumulates."
)
history_df = load_history(ticker)
if history_df.empty:
    st.info("No persisted history yet for this ticker -- it accumulates as the dashboard runs over time.")
else:
    st.dataframe(history_df, hide_index=True, width="stretch")
    csv_download_button(history_df, f"{ticker}_persisted_history.csv", key="dl_history")


# ==========================================================================
# CAUTION
# ==========================================================================

st.warning(
    f"⚠ Small-sample caution: n={len(reversals)} detected reversals / n={len(gaps)} gaps "
    f"({ticker} has {len(df)} trading days of history). Every statistic on this page -- "
    "confidence scores, backtest win rate, and especially the next-reversal projection -- "
    "is descriptive extrapolation of past behavior, NOT a forecast of future price or "
    "direction. Treat the projected window as something to watch, not to trade on."
)
